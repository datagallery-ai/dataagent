# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Python-side DataAgent logging implementation.

This module intentionally uses the name ``dataagent_logger`` instead of ``logger``
to avoid import ambiguity with the compiled ``logger`` extension module that
may be generated in the same package directory.

Active file is always ``main.<pid>.log`` under ``DATAAGENT_LOG_PATH``
(default ``${DATAAGENT_HOME}/logs``). Rotation compression and directory-global
retention prune run under one Linux ``fcntl.flock`` on ``.retention.lock``.
The log directory is node-local (not a multi-host NFS share).
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import re
import sys
import zipfile
from collections.abc import AsyncIterable, Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

from dataagent.utils.constants import TZ_CN
from dataagent.utils.env_utils import get_env, get_env_bool
from dataagent.utils.runtime_paths import dataagent_home

DEFAULT_LOG_ROTATION = "100 MB"
DEFAULT_LOG_RETENTION_COUNT = 20
# Bound length of sanitized session/run identifiers embedded into each log line.
# Generous enough for uuid4 + prefixes; long enough to keep logs readable.
_MAX_LOG_ID_LEN = 128
# Strip ASCII controls (incl. \n \r \t and ANSI ESC 0x1b) so untrusted ids cannot
# forge log lines via injected line breaks or escape sequences.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_LOG_DIR_MODE = 0o700
_ACTIVE_LOG_MODE = 0o600
_ARCHIVED_LOG_MODE = 0o400
_RETENTION_LOCK_NAME = ".retention.lock"
_ACTIVE_PREFIX = "main"
_TS = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}(?:\.\d+)?"
# Active: main.<pid>.log
_ACTIVE_RE = re.compile(rf"^{_ACTIVE_PREFIX}\.(?P<pid>\d+)\.log$")
# Rotated raw: main.<pid>.<ts>.log
_ROTATED_RAW_RE = re.compile(rf"^{_ACTIVE_PREFIX}\.(?P<pid>\d+)\.(?P<ts>{_TS})\.log$")
# Current and legacy archives: main.<pid>.<ts>.log.(zip|gz|tar|tar.gz)
_ROTATED_ARCHIVE_RE = re.compile(rf"^{_ACTIVE_PREFIX}\.(?P<pid>\d+)\.(?P<ts>{_TS})\.log\.(?:zip|gz|tar(?:\.gz)?)$")


@dataclass(frozen=True, slots=True)
class SessionLogContext:
    """Current request identifiers rendered into each log line."""

    session_id: str | None = None
    run_id: int = 0


_NO_SESSION = SessionLogContext()
_session_context_var: ContextVar[SessionLogContext] = ContextVar("dataagent_log_session", default=_NO_SESSION)
_active_log_files: set[tuple[int, Path]] = set()


def build_log_filename(*, pid: int | None = None) -> str:
    """Build the fixed active log filename ``main.<pid>.log``."""
    return f"{_ACTIVE_PREFIX}.{pid if pid is not None else os.getpid()}.log"


def is_managed_log_filename(name: str) -> bool:
    """True for active logs, rotated raw logs, and supported archives."""
    return bool(_ACTIVE_RE.fullmatch(name) or _ROTATED_RAW_RE.fullmatch(name) or _ROTATED_ARCHIVE_RE.fullmatch(name))


def is_live_active_log_file(path: Path) -> bool:
    """True when path is ``main.<pid>.log`` for a still-running PID."""
    match = _ACTIVE_RE.fullmatch(path.name)
    if not match:
        return False
    return _pid_is_alive(int(match.group("pid")))


def _parse_retention_count(raw: str | None) -> int:
    """Parse ``DATAAGENT_LOG_RETENTION_COUNT``; empty uses default, invalid fails fast."""
    value = DEFAULT_LOG_RETENTION_COUNT if raw is None or raw == "" else raw
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"DATAAGENT_LOG_RETENTION_COUNT must be a positive integer, got {raw!r}") from exc
    if count < 1:
        raise ValueError(f"DATAAGENT_LOG_RETENTION_COUNT must be >= 1, got {count}")
    return count


def build_config_from_env(process_name: str | None = None) -> LoggerConfig:
    """Build ``LoggerConfig`` from hot-path ``DATAAGENT_LOG_*`` env vars."""
    console_raw = get_env("DATAAGENT_LOG_CONSOLE", default="true")
    process = process_name or get_env("DATAAGENT_LOG_PROCESS_NAME") or "main"
    return LoggerConfig(
        console_level=get_env("DATAAGENT_LOG_LEVEL", default="INFO") or "INFO",
        file_level=get_env("DATAAGENT_LOG_FILE_LEVEL", default="TRACE") or "TRACE",
        log_path=get_env("DATAAGENT_LOG_PATH"),
        console=(console_raw or "true").lower() == "true",
        rotation=get_env("DATAAGENT_LOG_ROTATION", default=DEFAULT_LOG_ROTATION) or DEFAULT_LOG_ROTATION,
        retention_count=_parse_retention_count(get_env("DATAAGENT_LOG_RETENTION_COUNT")),
        process_name=process,
        diagnose=get_env_bool("DATAAGENT_LOG_DIAGNOSE", default=False),
    )


def _pid_is_alive(pid: int) -> bool:
    """Return whether ``pid`` appears alive via ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_unlink(path: Path) -> None:
    """Unlink ``path``, ignoring missing or already-gone files."""
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _open_path_with_mode(path: str, flags: int, mode: int) -> int:
    """Open a log-related path without following symlinks and enforce its mode."""
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.fchmod(descriptor, mode)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _active_log_file_opener(path: str, flags: int) -> int:
    """Open an active log or lock file with owner-only read/write access."""
    return _open_path_with_mode(path, flags, _ACTIVE_LOG_MODE)


def _set_path_mode(path: Path, mode: int, flags: int = os.O_RDONLY) -> None:
    """Set an existing log-related path to an exact mode through its descriptor."""
    descriptor = _open_path_with_mode(str(path), flags, mode)
    os.close(descriptor)


def _ensure_log_directory(path: Path) -> None:
    """Create the log directory if needed and enforce owner-only access."""
    path.mkdir(parents=True, mode=_LOG_DIR_MODE, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    _set_path_mode(path, _LOG_DIR_MODE, directory_flags)


def _is_rotated_raw_name(name: str) -> bool:
    """True for uncompressed Loguru renames: ``main.<pid>.<ts>.log``."""
    return bool(_ROTATED_RAW_RE.fullmatch(name))


def _is_prunable_history(path: Path) -> bool:
    """True if managed history may be deleted (dead PID / zip / leftover raw)."""
    name = path.name
    if is_live_active_log_file(path):
        return False
    if _ACTIVE_RE.fullmatch(name):
        return True  # dead-PID leftover active file
    return bool(_ROTATED_RAW_RE.fullmatch(name) or _ROTATED_ARCHIVE_RE.fullmatch(name))


def _history_files(log_dir: Path) -> list[Path]:
    """List managed history files eligible for directory-global retention prune."""
    if not log_dir.is_dir():
        return []
    history: list[Path] = []
    for path in log_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.name == _RETENTION_LOCK_NAME:
            continue
        if not _is_prunable_history(path):
            continue
        history.append(path)
    return history


def _zip_rotated_raw(raw: Path) -> Path | None:
    """Zip a rotated raw file to ``raw.zip`` and delete the raw.

    Idempotent when ``raw`` is already gone or ``raw.zip`` already exists.
    Raises on compression I/O errors so callers may keep the raw as history.
    """
    if not raw.exists():
        return None
    zip_path = Path(f"{raw}.zip")
    if not zip_path.exists():
        creation_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = _open_path_with_mode(str(zip_path), creation_flags, _ACTIVE_LOG_MODE)
        os.close(descriptor)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(raw, arcname=raw.name)
    _set_path_mode(zip_path, _ARCHIVED_LOG_MODE)
    _safe_unlink(raw)
    return zip_path if zip_path.exists() else None


def _reconcile_rotated_raws(log_dir: Path) -> None:
    """Under the retention lock: zip every managed rotated raw; failures leave raw."""
    if not log_dir.is_dir():
        return
    for path in list(log_dir.iterdir()):
        if not path.is_file() or not _is_rotated_raw_name(path.name):
            continue
        try:
            _zip_rotated_raw(path)
        except (OSError, ValueError):
            # Keep raw; prune will still count it toward global N.
            continue


def _enforce_managed_log_permissions(log_dir: Path) -> None:
    """Set live active logs to 0600 and completed or archived logs to 0400."""
    for path in log_dir.iterdir():
        if path.is_file() and is_managed_log_filename(path.name):
            mode = _ACTIVE_LOG_MODE if is_live_active_log_file(path) else _ARCHIVED_LOG_MODE
            _set_path_mode(path, mode)


def _finalize_active_logs_for_pid(pid: int | None = None) -> None:
    """Mark tracked active logs for one process as completed and read-only."""
    target_pid = os.getpid() if pid is None else pid
    for active_pid, path in tuple(_active_log_files):
        if active_pid != target_pid:
            continue
        with contextlib.suppress(FileNotFoundError, OSError):
            _set_path_mode(path, _ARCHIVED_LOG_MODE)
        _active_log_files.discard((active_pid, path))


atexit.register(_finalize_active_logs_for_pid)


def prune_log_history(log_dir: Path, retention_count: int) -> None:
    """Prune managed history to ``retention_count`` newest files (no locking).

    Live ``main.<alive_pid>.log`` files are never counted or deleted. Missing files
    during concurrent cleanup are skipped. Callers that need cross-process safety
    must hold ``.retention.lock`` and preferably run ``_reconcile_rotated_raws`` first.
    """
    if retention_count < 1:
        raise ValueError(f"retention_count must be >= 1, got {retention_count}")

    def _sort_key(path: Path) -> tuple[float, str]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (mtime, path.name)

    history = sorted(_history_files(log_dir), key=_sort_key, reverse=True)
    for stale in history[retention_count:]:
        _safe_unlink(stale)


@contextlib.contextmanager
def _retention_lock(log_dir: Path):
    """Linux-only exclusive flock on ``{log_dir}/.retention.lock`` (node-local)."""
    with open(
        log_dir / _RETENTION_LOCK_NAME,
        "a+",
        encoding="utf-8",
        opener=_active_log_file_opener,
    ) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def enforce_log_retention(log_dir: str | Path, retention_count: int) -> None:
    """Directory-global retention under the cross-process lock (startup / tests)."""
    directory = Path(log_dir)
    _ensure_log_directory(directory)
    with _retention_lock(directory):
        _reconcile_rotated_raws(directory)
        prune_log_history(directory, retention_count)
        _enforce_managed_log_permissions(directory)


def compress_rotated_and_prune(path_in: str | Path, *, log_dir: Path, retention_count: int) -> None:
    """Loguru compression callback: lock, reconcile all managed raws, prune to N."""
    _ = path_in  # directory reconcile covers this path when still present
    enforce_log_retention(log_dir, retention_count)


def _make_format(fmt_string: str) -> Any:
    """Return a Loguru formatter that renders timestamps in ``TZ_CN``."""

    def formatter(record: dict) -> str:
        record["time"] = record["time"].astimezone(TZ_CN)
        return fmt_string

    return formatter


def _patch_record(record: Any) -> None:
    """Inject ``session_id`` / ``run_id`` from ContextVar into the log record."""
    extra = record.setdefault("extra", {})
    ctx = _session_context_var.get()
    extra["session_id"] = ctx.session_id or "-"
    extra["run_id"] = ctx.run_id


@dataclass(slots=True)
class LoggerConfig:
    """Structured logger configuration for per-process ``main.<pid>.log`` files."""

    console_level: str = "INFO"
    file_level: str = "TRACE"
    log_path: str | None = None
    console: bool = True
    format_string: str | None = None
    rotation: str = DEFAULT_LOG_ROTATION
    retention_count: int = DEFAULT_LOG_RETENTION_COUNT
    process_name: str = "main"
    diagnose: bool = False
    redirect_stdout_stderr: bool = False
    # Legacy construct/attr compatibility (br_release_930); ignored by file routing/sink.
    file_path: str | None = None
    file_path_explicit: bool = False
    retention: str = "7 days"
    compression: str = "zip"
    json_logs: bool = False


class DataAgentLogger:
    """DataAgent 统一日志管理器"""

    _initialized = False
    _logger_instances: dict[str, Any] = {}
    _config: LoggerConfig | None = None
    _logger = None

    @classmethod
    def init_logger(cls, config: LoggerConfig | None = None) -> None:
        """初始化日志器"""
        effective_config = config or build_config_from_env()
        process_name = effective_config.process_name or "main"

        if cls._initialized and process_name in cls._logger_instances:
            return

        log_path = effective_config.log_path or str((dataagent_home() / "logs").resolve())
        effective_config = replace(
            effective_config,
            process_name=process_name,
            log_path=log_path,
        )
        cls._config = effective_config

        _loguru_logger.remove()
        _finalize_active_logs_for_pid()
        _loguru_logger.configure(patcher=_patch_record)

        format_string = effective_config.format_string
        if format_string is None:
            proc_segment = f"<magenta>{process_name}</magenta> | " if process_name and process_name != "main" else ""
            format_string = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                f"{proc_segment}"
                "session={extra[session_id]} | run={extra[run_id]} | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>\n"
            )
        format_callable = _make_format(format_string)

        if effective_config.console:
            _loguru_logger.add(
                sys.stderr,
                level=effective_config.console_level,
                format=format_callable,
                colorize=True,
                backtrace=True,
                diagnose=effective_config.diagnose,
            )

        active_file: Path | None = None
        if log_path:
            try:
                log_dir = Path(log_path)
                # Startup prune only (same-PID leftovers may append; dead PIDs are history).
                enforce_log_retention(log_dir, effective_config.retention_count)
                active_file = log_dir / build_log_filename()

                def _compress(path_in: str) -> None:
                    compress_rotated_and_prune(
                        path_in,
                        log_dir=log_dir,
                        retention_count=effective_config.retention_count,
                    )

                _loguru_logger.add(
                    str(active_file),
                    level=effective_config.file_level,
                    format=format_callable,
                    mode="a",
                    rotation=effective_config.rotation,
                    retention=None,
                    compression=_compress,
                    encoding="utf-8",
                    opener=_active_log_file_opener,
                    enqueue=True,
                    backtrace=True,
                    diagnose=effective_config.diagnose,
                )
                _active_log_files.add((os.getpid(), active_file))
            except OSError as e:
                if effective_config.console:
                    _loguru_logger.warning(f"无法写入日志目录 {log_path}: {e}，已回退到控制台输出")
                else:
                    _loguru_logger.add(
                        sys.stderr,
                        level=effective_config.console_level,
                        format=format_callable,
                        colorize=True,
                        backtrace=True,
                        diagnose=effective_config.diagnose,
                    )
                    _loguru_logger.warning(f"无法写入日志目录 {log_path}: {e}，已强制启用控制台输出")
                cls._config = replace(effective_config, log_path=None)
                log_path = None
                active_file = None

        if effective_config.redirect_stdout_stderr:
            cls._redirect_prints_to_logger(process_name)

        cls._logger = _loguru_logger
        cls._logger_instances[process_name] = True
        if not cls._initialized:
            cls._initialized = True

        _loguru_logger.debug(
            "DataAgent 日志系统已初始化 - "
            f"进程: {process_name}, 控制台级别: {effective_config.console_level}, "
            f"文件级别: {effective_config.file_level}, 目录: {log_path or 'None'}, "
            f"文件: {active_file or 'None'}"
        )

    @classmethod
    def setup_from_config(cls, process_name: str | None = None) -> None:
        """Initialize from env-derived config for ``process_name`` (default ``main``)."""
        if process_name is None:
            process_name = "main"
        if cls._initialized and process_name in cls._logger_instances:
            return
        cls.init_logger(build_config_from_env(process_name=process_name))

    @classmethod
    def get_logger(cls, process_name: str | None = None):
        """Return the shared Loguru logger, initializing on first use."""
        if process_name is None:
            process_name = "main"
        if not cls._initialized or process_name not in cls._logger_instances:
            try:
                cls.setup_from_config(process_name)
            except Exception:
                cls.init_logger(LoggerConfig(process_name=process_name))
        return cls._logger or _loguru_logger

    @classmethod
    def reconfigure(cls, config: LoggerConfig) -> None:
        """Reset handlers and re-initialize with ``config``."""
        cls._initialized = False
        cls._logger_instances.clear()
        cls.init_logger(config)

    @classmethod
    def setup_subprocess_logging(cls, process_name: str) -> None:
        """Initialize logging for a subprocess using the main config's directory."""
        if cls._initialized and "main" in cls._logger_instances:
            main_config = cls._config or LoggerConfig()
            cls.init_logger(replace(main_config, process_name=process_name))
        else:
            cls.setup_from_config(process_name)

    @classmethod
    def is_initialized(cls) -> bool:
        """True after the first successful ``init_logger`` call."""
        return cls._initialized

    @classmethod
    def _redirect_prints_to_logger(cls, process_name: str) -> None:
        """Redirect stdout/stderr writes into Loguru (optional)."""

        class LoggerWriter:
            def __init__(self, level: str, process_name: str):
                """Store target log level and owning process name."""
                self.level = level
                self.process_name = process_name
                self.buffer = StringIO()

            def write(self, message: str) -> None:
                """Forward a non-empty stdout/stderr chunk into Loguru."""
                if message.strip():
                    clean_message = message.rstrip("\n\r")
                    if clean_message:
                        if self.level == "INFO":
                            _loguru_logger.trace(f"[STDOUT] {clean_message}")
                        else:
                            _loguru_logger.error(f"[STDERR] {clean_message}")

        sys.stdout = LoggerWriter("INFO", process_name)
        sys.stderr = LoggerWriter("ERROR", process_name)


logger = None
_dataagent_logger = DataAgentLogger()


def init_logger(config: LoggerConfig | None = None) -> None:
    """Module entry: initialize sinks and refresh the module-level ``logger``."""
    global logger
    _dataagent_logger.init_logger(config)
    logger = _dataagent_logger.get_logger(config.process_name if config else None)


def get_logger(process_name: str | None = None):
    """Module entry: get (and lazily initialize) the shared logger."""
    global logger
    if logger is None or not _dataagent_logger.is_initialized():
        _dataagent_logger.init_logger(LoggerConfig(process_name=process_name or "main"))
        logger = _dataagent_logger.get_logger(process_name)
    return logger


def reconfigure(config: LoggerConfig) -> None:
    """Module entry: rebuild sinks from ``config``."""
    global logger
    _dataagent_logger.reconfigure(config)
    logger = _dataagent_logger.get_logger(config.process_name)


def setup_subprocess_logging(process_name: str):
    """Module entry: set up logging for a named subprocess and return its logger."""
    _dataagent_logger.setup_subprocess_logging(process_name)
    return get_logger(process_name)


def _sanitize_log_id(value: object) -> str | None:
    """Return ``value`` as a single, capped log line for the ``session=`` token.

    Drops ASCII control characters (covers ``\\n``, ``\\r``, ``\\t``, ANSI ESC)
    and truncates to ``_MAX_LOG_ID_LEN``. ``None``/empty passthrough is preserved
    so the patcher can fall back to ``"-"``.
    """
    if value is None:
        return None
    cleaned = _CONTROL_CHARS_RE.sub("", str(value)).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_LOG_ID_LEN]


def set_session_log_context(session_id: str, run_id: int = 0) -> Token[SessionLogContext]:
    """Bind session/run for the current Context; return a reset token.

    ``session_id`` is sanitized (control chars stripped, length-capped) before
    storage so hostile ids cannot forge extra log lines via injected ``\n``/``\r``
    or ANSI escapes. Sanitization happens at the single chokepoint where external
    identifiers enter the log extra, keeping the patcher/formatter pure.
    """
    ctx = SessionLogContext(
        session_id=_sanitize_log_id(session_id),
        run_id=int(run_id or 0),
    )
    return _session_context_var.set(ctx)


def reset_session_log_context(token: Token[SessionLogContext]) -> None:
    """Reset session/run ContextVar using a token from ``set_session_log_context``."""
    _session_context_var.reset(token)


def attach_session_log_context(stream: Any, session_id: str, run_id: int = 0) -> Any:
    """Bind session/run in the consumer Task/Context for the stream lifetime."""
    if isinstance(stream, AsyncIterable):

        async def _async_stream():
            token = set_session_log_context(session_id, run_id)
            try:
                async for item in stream:
                    yield item
            finally:
                reset_session_log_context(token)

        return _async_stream()

    if isinstance(stream, Iterable) and not isinstance(stream, (str, bytes, bytearray)):

        def _sync_stream():
            token = set_session_log_context(session_id, run_id)
            try:
                yield from stream
            finally:
                reset_session_log_context(token)

        return _sync_stream()

    return stream
