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
"""

import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

from dataagent.utils.constants import _TZ_CN
from dataagent.utils.runtime_paths import dataagent_home, resolve_user_root


def _cn_format(record: dict) -> str:
    record["time"] = record["time"].astimezone(_TZ_CN)
    return record["format"]


def _make_format(fmt_string: str) -> Any:
    """Create a format callable that converts loguru time to UTC+8 before formatting."""

    def formatter(record: dict) -> str:
        record["time"] = record["time"].astimezone(_TZ_CN)
        return fmt_string

    return formatter


@dataclass(slots=True)
class LoggerConfig:
    """Structured logger configuration."""

    console_level: str = "INFO"
    file_level: str = "TRACE"
    file_path: str | None = None
    console: bool = True
    format_string: str | None = None
    rotation: str = "100 MB"
    retention: str = "7 days"
    compression: str = "zip"
    json_logs: bool = False
    process_name: str = "main"
    redirect_stdout_stderr: bool = False
    file_path_explicit: bool = False


class DataAgentLogger:
    """DataAgent 统一日志管理器"""

    _initialized = False
    _logger_instances: dict[str, Any] = {}
    _config: LoggerConfig | None = None
    _logger = None

    @classmethod
    def init_logger(
        cls,
        config: LoggerConfig | None = None,
    ) -> None:
        """
        初始化日志器

        Args:
            config: 日志配置对象
        """
        effective_config = config or LoggerConfig()
        process_name = effective_config.process_name or "main"

        if cls._initialized and process_name in cls._logger_instances:
            return

        if not effective_config.file_path_explicit and effective_config.file_path is None:
            file_path = None
        else:
            file_path = effective_config.file_path or cls._build_default_log_file_path()
        effective_config = replace(
            effective_config,
            process_name=process_name,
            file_path=file_path,
            file_path_explicit=effective_config.file_path_explicit or effective_config.file_path is not None,
        )
        cls._config = effective_config

        _loguru_logger.remove()

        format_string = effective_config.format_string
        if format_string is None:
            if effective_config.json_logs:
                format_string = "{message}"
            else:
                if process_name and process_name != "main":
                    format_string = (
                        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                        "<level>{level: <8}</level> | "
                        f"<magenta>{process_name}</magenta> | "
                        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                        "<level>{message}</level>\n"
                    )
                else:
                    format_string = (
                        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                        "<level>{level: <8}</level> | "
                        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                        "<level>{message}</level>\n"
                    )

        format_callable = format_string if effective_config.json_logs else _make_format(format_string)

        if effective_config.console:
            _loguru_logger.add(
                sys.stderr,
                level=effective_config.console_level,
                format=format_callable,
                colorize=True,
                backtrace=True,
                # diagnose can dump local variables, including credentials.
                diagnose=False,
            )

        if file_path:
            try:
                log_dir = Path(file_path).parent
                log_dir.mkdir(parents=True, exist_ok=True)

                _loguru_logger.add(
                    file_path,
                    level=effective_config.file_level,
                    format=format_callable,
                    mode="a",
                    rotation=effective_config.rotation,
                    retention=effective_config.retention,
                    compression=effective_config.compression,
                    encoding="utf-8",
                    enqueue=True,
                    backtrace=True,
                    # diagnose can dump local variables, including credentials.
                    diagnose=False,
                    serialize=effective_config.json_logs,
                )
            except OSError as e:
                if effective_config.console:
                    _loguru_logger.warning(f"无法写入日志文件 {file_path}: {e}，已回退到控制台输出")
                else:
                    _loguru_logger.add(
                        sys.stderr,
                        level=effective_config.console_level,
                        format=format_callable,
                        colorize=True,
                        backtrace=True,
                        # diagnose can dump local variables, including credentials.
                        diagnose=False,
                    )
                    _loguru_logger.warning(f"无法写入日志文件 {file_path}: {e}，已强制启用控制台输出")
                cls._config = replace(effective_config, file_path=None)
                file_path = None

        if effective_config.redirect_stdout_stderr:
            cls._redirect_prints_to_logger(process_name)

        cls._logger = _loguru_logger
        cls._logger_instances[process_name] = True

        if not cls._initialized:
            cls._initialized = True

        _loguru_logger.debug(
            "DataAgent 日志系统已初始化 - "
            f"进程: {process_name}, 控制台级别: {effective_config.console_level}, "
            f"文件级别: {effective_config.file_level}, 文件: {file_path or 'None'}"
        )

    @classmethod
    def setup_from_config(cls, process_name: str | None = None) -> None:
        """Initialize logger with built-in defaults."""
        if process_name is None:
            process_name = "main"

        if cls._initialized and process_name in cls._logger_instances:
            return

        logging_config: dict[str, Any] = {}
        console_level = logging_config.get("console_level", logging_config.get("level", "INFO"))
        file_level = logging_config.get("file_level", "TRACE")
        log_format = logging_config.get("format")
        log_file_path = logging_config.get("file_path")
        rotation = logging_config.get("rotation", "100 MB")
        retention = logging_config.get("retention", "7 days")
        compression = logging_config.get("compression", "zip")
        console_output = logging_config.get("console", True)
        json_logs = logging_config.get("json_logs", False)
        redirect_output = logging_config.get("redirect_stdout_stderr", False)

        if "max_file_size" in logging_config:
            rotation = f"{logging_config['max_file_size']} MB"
        if "retention_days" in logging_config:
            retention = f"{logging_config['retention_days']} days"
        if "console_output" in logging_config:
            console_output = logging_config["console_output"]

        cls.init_logger(
            LoggerConfig(
                console_level=console_level,
                file_level=file_level,
                file_path=log_file_path,
                console=console_output,
                format_string=log_format,
                rotation=rotation,
                retention=retention,
                compression=compression,
                json_logs=json_logs,
                process_name=process_name,
                redirect_stdout_stderr=redirect_output,
                file_path_explicit=log_file_path is not None,
            )
        )

    @classmethod
    def get_logger(cls, process_name: str | None = None):
        """获取日志器"""
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
        """重新配置日志器"""
        cls._initialized = False
        cls._logger_instances.clear()
        cls.init_logger(config)

    @classmethod
    def setup_subprocess_logging(cls, process_name: str) -> None:
        """为子进程设置日志"""
        if cls._initialized and "main" in cls._logger_instances:
            main_config = cls._config or LoggerConfig()
            cls.init_logger(replace(main_config, process_name=process_name))
        else:
            cls.setup_from_config(process_name)

    @classmethod
    def is_initialized(cls) -> bool:
        """检查日志系统是否已初始化"""
        return cls._initialized

    @classmethod
    def _build_default_log_file_path(cls, *, process_name: str = "main") -> str:
        """Build the default log path under ``<dataagent_home>/logs`` using a timestamped file name."""
        stamp = datetime.now(tz=_TZ_CN).strftime("%Y%m%d_%H%M%S_%f")
        prefix = f"{process_name}_" if process_name and process_name != "main" else "main_"
        return str(((dataagent_home() / "logs") / f"{prefix}{stamp}.log").resolve())

    @classmethod
    def build_session_log_path(cls, *, user_id: str, session_id: str, process_name: str = "main") -> str:
        """Build a per-user session-specific log path.

        Returns a path like ``~/.dataagent/{user_id}/logs/main_{session_id}.log``
        or ``~/.dataagent/{user_id}/logs/subagent_{session_id}_{sub_id}.log``.
        """
        prefix = f"{process_name}_" if process_name and process_name != "main" else "main_"
        return str((resolve_user_root(user_id=user_id) / "logs" / f"{prefix}{session_id}.log").resolve())

    @classmethod
    def setup_session_log(cls, *, user_id: str, session_id: str, process_name: str = "main") -> None:
        """Reconfigure the logger to write to a per-user session-specific log file.

        This should be called when a main-agent session starts so that its logs
        are stored alongside the sub-agent logs under the same per-user directory.

        Skips reconfiguration if the logger is already configured for a sub-agent
        process (``process_name != "main"``), to avoid overriding the sub-agent's
        own log setup in ``sub_agent_entry.py``.
        """
        current_config = cls._config or LoggerConfig()
        if current_config.process_name and current_config.process_name != "main":
            return
        session_log_path = cls.build_session_log_path(user_id=user_id, session_id=session_id, process_name=process_name)
        cls.reconfigure(
            replace(
                current_config,
                process_name=process_name,
                file_path=session_log_path,
                file_path_explicit=True,
            )
        )

    @classmethod
    def _redirect_prints_to_logger(cls, process_name: str) -> None:
        """将 print 和其他输出重定向到日志系统"""

        class LoggerWriter:
            """将输出重定向到 logger 的包装器"""

            def __init__(self, level: str, process_name: str):
                self.level = level
                self.process_name = process_name
                self.buffer = StringIO()

            def write(self, message: str) -> None:
                """Write a message to the logger."""
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
    """初始化全局日志器的便捷函数"""
    global logger
    _dataagent_logger.init_logger(config)
    logger = _dataagent_logger.get_logger(config.process_name if config else None)


def get_logger(process_name: str | None = None):
    """获取全局日志器"""
    global logger

    if logger is None or not _dataagent_logger.is_initialized():
        _dataagent_logger.init_logger(LoggerConfig(process_name=process_name or "main"))
        logger = _dataagent_logger.get_logger(process_name)

    return logger


def reconfigure(config: LoggerConfig) -> None:
    """重新配置日志器"""
    global logger
    _dataagent_logger.reconfigure(config)
    logger = _dataagent_logger.get_logger(config.process_name)


def setup_subprocess_logging(process_name: str):
    """设置子进程日志"""
    _dataagent_logger.setup_subprocess_logging(process_name)
    return get_logger(process_name)


def setup_session_log(*, user_id: str, session_id: str, process_name: str = "main") -> None:
    """将主 Agent 日志重定向到 per-user 目录下的会话日志文件。

    若当前进程已配置为 sub-agent（process_name != "main"），则跳过，避免
    覆盖 sub-agent 在 ``sub_agent_entry.py`` 中已完成的日志配置。
    """
    global logger
    current_config = DataAgentLogger._config or LoggerConfig()
    if current_config.process_name and current_config.process_name != "main":
        return
    _dataagent_logger.setup_session_log(user_id=user_id, session_id=session_id, process_name=process_name)
    logger = _dataagent_logger.get_logger(process_name)


def build_session_log_path(*, user_id: str, session_id: str, process_name: str = "main") -> str:
    """构建 per-user 会话日志文件路径。"""
    return _dataagent_logger.build_session_log_path(user_id=user_id, session_id=session_id, process_name=process_name)
