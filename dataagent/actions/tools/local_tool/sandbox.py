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
"""Unified security module: path auditing + process-level sandbox.

Combines the path-audit capabilities formerly in ``workspace_guard.py``
with the pluggable process-isolation layer (bubblewrap / noop).

Public surface consumed by the rest of the codebase:

* :class:`Sandbox` – ABC with path auditing **and** ``wrap_command``.
* :class:`BubblewrapSandbox` / :class:`NoopSandbox` – concrete impls.
* :class:`SandboxPolicy` – declarative bwrap mount policy.
* :class:`WorkspaceAccessError` – raised on unauthorized path access.
* :func:`create_sandbox` – factory (enabled flag + optional policy).
* :func:`build_workspace_mount_lists` – DataAgent 默认的 ro/rw 挂载清单生成器。
* :func:`is_bwrap_sandbox_usable` – probe whether bwrap can create namespaces.
* :func:`os_config_bind_paths` – 解析 ``/etc`` 下 symlink 目标（DNS/TLS 等）。
* :func:`runtime_python_bind_paths` – 当前解释器 venv/prefix 所需的额外 ro-bind。
* ``set_current_sandbox`` / ``get_current_sandbox`` / ``reset_current_sandbox``
  – contextvars helpers for per-tool-call binding.
"""

from __future__ import annotations

import contextvars
import functools
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from dataagent.utils.constants import DEFAULT_SANDBOX_RO_BINDS, DEFAULT_SANDBOX_TMPFS_PATHS
from dataagent.utils.env_utils import set_env

# ---------------------------------------------------------------------------
# Sandbox policy (bwrap mount descriptors)
# ---------------------------------------------------------------------------

_SANDBOX_L0_SYSTEM_ROOTS: tuple[str, ...] = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc")

# Host OS 配置：若 symlink 指向 L0 外（如 WSL ``/etc/resolv.conf`` → ``/mnt/wsl/...``），
# 需单独 bind 解析后的目录，而非硬编码平台路径。
_OS_CONFIG_PATHS: tuple[str, ...] = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/ssl/certs",
)

# 存在即挂入的常用工具链/配置；本地开发与用户环境走同一套策略。
_USER_READONLY_CANDIDATES: tuple[str, ...] = (
    ".local/bin",
    ".local/share",
    ".cargo/bin",
    ".rustup",
    ".pyenv",
    ".nvm",
    ".bun",
    ".deno",
    ".poetry",
    ".pipx",
    "go/bin",
    ".gitconfig",
    ".config/git",
)
_USER_WRITABLE_CANDIDATES: tuple[str, ...] = (".cache", ".local/state")


def _is_under_path(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _covered_by_bind_roots(path: Path, roots: Sequence[str]) -> bool:
    """Return True when *path* is already visible via one of *roots*."""
    return any(_is_under_path(path, Path(root)) for root in roots)


def _covered_by_sandbox_l0(path: Path) -> bool:
    """Return True when *path* is already visible via default L0 ro-bind roots."""
    return _covered_by_bind_roots(path, _SANDBOX_L0_SYSTEM_ROOTS)


def _existing_paths(*paths: str | Path) -> list[str]:
    out: list[str] = []
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if p.exists():
            out.append(str(p))
    return out


def _bind_root_for_os_config_target(resolved: Path, covered_roots: Sequence[str]) -> str | None:
    """Return the smallest existing directory bind for a resolved OS config path."""
    mount_at = resolved if resolved.is_dir() else resolved.parent
    while mount_at != mount_at.parent:
        if _covered_by_bind_roots(mount_at, covered_roots):
            return None
        if mount_at.exists():
            return str(mount_at)
        mount_at = mount_at.parent
    return None


def os_config_bind_paths(covered_roots: Sequence[str] | None = None) -> list[str]:
    """Return readonly binds for OS config files whose symlink targets fall outside *covered_roots*."""
    l0 = list(covered_roots or _SANDBOX_L0_SYSTEM_ROOTS)
    out: list[str] = []
    seen: list[str] = list(l0)

    for cfg in _OS_CONFIG_PATHS:
        candidate = Path(cfg)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            logger.debug("os_config_bind_paths: skip unreadable {}", cfg, exc_info=True)
            continue

        bind_root = _bind_root_for_os_config_target(resolved, seen)
        if bind_root is None:
            continue
        _append_unique_bind(out, bind_root)
        seen.append(bind_root)

    return out


def _append_unique_bind(paths: list[str], candidate: str) -> None:
    resolved = Path(candidate).resolve()
    if any(_is_under_path(resolved, Path(existing)) for existing in paths):
        return
    if str(resolved) not in paths:
        paths.append(str(resolved))


def runtime_python_bind_paths() -> list[str]:
    """Return extra readonly binds needed to exec ``sys.executable`` inside bwrap.

    Covers:
    - local venv (``sys.prefix`` → ``.venv``)
    - uv/pyenv 等：``sys.base_prefix`` 与 ``sys.prefix`` 不同时的真实解释器目录
    - custom install prefixes (e.g. Docker ``/opt/python/...``)
    - editable installs where the ``dataagent`` package lives outside ``sys.prefix``

    Skips paths already covered by L0 (e.g. system Python under ``/usr``).
    """
    out: list[str] = []

    prefix = Path(sys.prefix).expanduser()
    if prefix.exists() and not _covered_by_sandbox_l0(prefix):
        _append_unique_bind(out, str(prefix))

    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix)).expanduser()
    if base_prefix.exists() and base_prefix.resolve() != prefix.resolve() and not _covered_by_sandbox_l0(base_prefix):
        _append_unique_bind(out, str(base_prefix))

    try:
        resolved_exe = Path(sys.executable).expanduser().resolve()
        if resolved_exe.exists() and not _covered_by_sandbox_l0(resolved_exe):
            exe_root = resolved_exe.parent if resolved_exe.is_file() else resolved_exe
            if not _covered_by_bind_roots(exe_root, [*out, *_SANDBOX_L0_SYSTEM_ROOTS]):
                _append_unique_bind(out, str(exe_root))
    except OSError:
        logger.debug("runtime_python_bind_paths: skip sys.executable bind", exc_info=True)

    try:
        import dataagent

        pkg_dir = Path(dataagent.__file__).resolve().parent
        if pkg_dir.exists() and not _covered_by_sandbox_l0(pkg_dir):
            prefix_resolved = prefix.resolve() if prefix.exists() else None
            if prefix_resolved is None or not _is_under_path(pkg_dir, prefix_resolved):
                _append_unique_bind(out, str(pkg_dir))
    except Exception:
        logger.debug("runtime_python_bind_paths: skip dataagent package bind", exc_info=True)

    return out


def build_workspace_mount_lists(
    *,
    resolved_workspace: Path,
    allow_read_roots: Sequence[Path] = (),
    skill_aliases: Mapping[str, Path] | None = None,
) -> tuple[list[str], list[str]]:
    """构造 bwrap 沙箱的 readonly / writable 挂载清单（DataAgent 默认策略）。

    分层：
    - L0 系统基础：``/usr /lib /lib64 /bin /sbin /etc``。
    - L1 OS 配置：``os_config_bind_paths()`` 解析 ``/etc`` symlink 目标（DNS/TLS 等）。
    - L1 平台可选：``/usr/lib/wsl``（存在则挂，WSL 互操作）。
    - L1.5 运行时 Python：``runtime_python_bind_paths()``（venv / Docker prefix / editable dataagent）。
    - L2 workspace（可写）+ 存在的用户工具链/cache。
    - L3 业务：``allow_read_roots``、skill 目录。

    Args:
        resolved_workspace: 已解析为绝对路径的 workspace 根目录（writable）。
        allow_read_roots: 业务额外开放的只读路径（白名单）。
        skill_aliases: 技能名 → 路径，挂为只读。
    """
    home = Path.home()
    skill_paths = list(dict.fromkeys((skill_aliases or {}).values()))

    readonly_candidates: list[str | Path] = [
        *os_config_bind_paths(_SANDBOX_L0_SYSTEM_ROOTS),
        *_existing_paths("/usr/lib/wsl"),
        *runtime_python_bind_paths(),
        *allow_read_roots,
        *skill_paths,
        *_existing_paths(
            *(str(home / rel) for rel in _USER_READONLY_CANDIDATES),
            "/opt",
        ),
    ]

    readonly_binds: list[str] = list(dict.fromkeys(_SANDBOX_L0_SYSTEM_ROOTS))
    for item in readonly_candidates:
        _append_unique_bind(readonly_binds, str(item))

    writable_binds: list[str] = [
        str(resolved_workspace),
        *_existing_paths(*(str(home / rel) for rel in _USER_WRITABLE_CANDIDATES)),
    ]

    return readonly_binds, writable_binds


@dataclass(frozen=True)
class SandboxPolicy:
    """Declarative sandbox policy describing resource access boundaries."""

    readonly_binds: list[str] = field(default_factory=lambda: list(DEFAULT_SANDBOX_RO_BINDS))
    writable_binds: list[str] = field(default_factory=list)
    tmpfs_paths: list[str] = field(default_factory=lambda: list(DEFAULT_SANDBOX_TMPFS_PATHS))
    # bwrap 必备命名空间：/proc 与 /dev。设为 None 可关闭（默认开，避免 bash <()、
    # /dev/null、Python ssl 之类对 /proc/self、/dev/urandom 的隐式依赖直接失败）。
    proc_path: str | None = "/proc"
    dev_path: str | None = "/dev"
    unshare_net: bool = False
    die_with_parent: bool = True


# ---------------------------------------------------------------------------
# WorkspaceAccessError
# ---------------------------------------------------------------------------


class WorkspaceAccessError(PermissionError):
    """Permission error with path attribution metadata."""

    def __init__(
        self,
        *,
        raw_path: str,
        resolved_path: str,
        allowed_roots: Sequence[str],
        source_kind: str = "user_input",
        operation: str = "access",
    ) -> None:
        self.raw_path = raw_path
        self.resolved_path = resolved_path
        self.allowed_roots = list(allowed_roots)
        self.source_kind = source_kind
        self.operation = operation
        super().__init__(
            f"[{source_kind}] {operation} denied: "
            f"'{raw_path}' (resolved to '{resolved_path}') "
            f"is outside allowed roots {self.allowed_roots}"
        )


# ---------------------------------------------------------------------------
# Sandbox ABC (path auditing + process isolation)
# ---------------------------------------------------------------------------


class Sandbox(ABC):
    """Abstract base class: path auditing **and** process-level isolation.

    Every concrete sandbox carries the workspace / skill / allow-read
    configuration so that ``authorize_read`` / ``authorize_write`` work
    identically regardless of whether bwrap is active.
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        skill_aliases: Mapping[str, str | Path] | None = None,
        allow_read_roots: Sequence[str | Path] | None = None,
    ) -> None:
        self._workspace_root = self._resolve_roots([workspace_root], "workspace path")[0] if workspace_root else None
        self._skill_aliases = self._resolve_skill_aliases(skill_aliases or {})
        self._skill_roots = list(dict.fromkeys(self._skill_aliases.values()))
        if allow_read_roots:
            merged = [self._resolve_roots([p], "allow_path entry")[0] for p in allow_read_roots]
            self._allow_read_roots = list(dict.fromkeys(merged))
        else:
            self._allow_read_roots: list[Path] = []

    # -- properties ----------------------------------------------------------

    @property
    def skill_aliases(self) -> dict[str, Path]:
        """Skill aliases are a mapping of skill names to their paths."""
        return dict(self._skill_aliases)

    @property
    def allow_read_roots(self) -> list[Path]:
        """Allow read roots are a list of paths that are allowed to be read."""
        return list(self._allow_read_roots)

    @property
    def workspace_root(self) -> Path | None:
        """Workspace root is the root of the workspace."""
        return self._workspace_root

    @staticmethod
    def _is_subpath(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _resolve_roots(raw: Sequence[str | Path | None], label: str) -> list[Path]:
        out: list[Path] = []
        for r in raw:
            if r is None:
                continue
            raw_path = Path(str(r))
            if not raw_path.expanduser().is_absolute():
                raise ValueError(f"{label} must be absolute paths, got relative: {r}")
            p = raw_path.expanduser().resolve()
            out.append(p)
        return out

    @staticmethod
    def _resolve_skill_aliases(raw: Mapping[str, str | Path]) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for name, path in raw.items():
            resolved = Path(str(path)).expanduser().resolve()
            if not resolved.is_absolute():
                raise ValueError(f"Skill path for '{name}' must be absolute, got: {path}")
            result[name] = resolved
        return result

    @abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` when the sandbox runtime is usable."""

    @abstractmethod
    def wrap_command(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Return *cmd* wrapped with sandbox invocation arguments."""

    def resolve_prompt_path_alias(self, raw_path: str | Path) -> Path | None:
        """Resolve ``skill/<name>/...`` aliases to absolute paths."""
        parts = Path(str(raw_path)).parts
        if len(parts) >= 2 and parts[0] == "skill":
            skill_name = parts[1]
            if skill_name in self._skill_aliases:
                return (
                    self._skill_aliases[skill_name] / Path(*parts[2:])
                    if len(parts) > 2
                    else self._skill_aliases[skill_name]
                )
        return None

    def authorize_read(
        self,
        path: str | Path,
        *,
        source_kind: str = "user_input",
        operation: str = "read",
        base_dir: str | Path | None = None,
    ) -> Path:
        """Allow reads from the workspace root, skill roots, or allow-read roots."""
        roots = [self._workspace_root] if self._workspace_root is not None else []
        return self._authorize_in_roots(
            path,
            roots=[*roots, *self._skill_roots, *self._allow_read_roots],
            source_kind=source_kind,
            operation=operation,
            base_dir=base_dir,
        )

    def authorize_write(
        self,
        path: str | Path,
        *,
        source_kind: str = "user_input",
        operation: str = "write",
        base_dir: str | Path | None = None,
    ) -> Path:
        """Allow writes only under the workspace root."""
        roots = [self._workspace_root] if self._workspace_root is not None else []
        return self._authorize_in_roots(
            path,
            roots=roots,
            source_kind=source_kind,
            operation=operation,
            base_dir=base_dir,
        )

    def resolve_requested_path(self, path: str | Path, base_dir: str | Path | None = None) -> Path:
        """Normalize a user-supplied path against *base_dir* or cwd."""
        candidate = Path(str(path)).expanduser()
        if not candidate.is_absolute():
            base_path = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd().resolve()
            candidate = base_path / candidate
        return candidate.resolve()

    def _is_under(self, path: Path, roots: Sequence[Path]) -> bool:
        return any(self._is_subpath(path, root) for root in roots)

    def _authorize_in_roots(
        self,
        path: str | Path,
        *,
        roots: Sequence[Path],
        source_kind: str = "user_input",
        operation: str = "access",
        base_dir: str | Path | None = None,
    ) -> Path:
        resolved = self.resolve_requested_path(path, base_dir)
        if self._workspace_root is None and not self._skill_roots and not self._allow_read_roots:
            return resolved
        if not self._is_under(resolved, roots):
            raise WorkspaceAccessError(
                raw_path=str(path),
                resolved_path=str(resolved),
                allowed_roots=[str(r) for r in roots],
                source_kind=source_kind,
                operation=operation,
            )
        return resolved


# ---------------------------------------------------------------------------
# Concrete implementations
# ---------------------------------------------------------------------------


class BubblewrapSandbox(Sandbox):
    """Sandbox implementation backed by bubblewrap (``bwrap``)."""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        workspace_root: str | Path | None = None,
        skill_aliases: Mapping[str, str | Path] | None = None,
        allow_read_roots: Sequence[str | Path] | None = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root, skill_aliases=skill_aliases, allow_read_roots=allow_read_roots)
        self._policy = policy or SandboxPolicy()

    def is_available(self) -> bool:
        return is_bwrap_sandbox_usable()

    def wrap_command(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        args: list[str] = ["bwrap"]

        # tmpfs first, then all binds – child paths override parent tmpfs mounts
        for path in self._policy.tmpfs_paths:
            args += ["--tmpfs", path]

        # /proc 与 /dev：bwrap 命名空间默认是空的，必须显式挂；
        # 缺它们会让 bash 进程替换、/dev/null、Python ssl 等悄悄出错。
        if self._policy.proc_path:
            args += ["--proc", self._policy.proc_path]
        if self._policy.dev_path:
            args += ["--dev", self._policy.dev_path]

        for path in self._policy.readonly_binds:
            if Path(path).exists():
                args += ["--ro-bind", path, path]

        for path in self._policy.writable_binds:
            if Path(path).exists():
                args += ["--bind", path, path]

        if self._policy.unshare_net:
            args.append("--unshare-net")
        if self._policy.die_with_parent:
            args.append("--die-with-parent")

        if cwd:
            args += ["--chdir", cwd]

        args.append("--")
        args.extend(cmd)
        return args


class NoopSandbox(Sandbox):
    """Transparent pass-through – no process isolation applied."""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        skill_aliases: Mapping[str, str | Path] | None = None,
        allow_read_roots: Sequence[str | Path] | None = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root, skill_aliases=skill_aliases, allow_read_roots=allow_read_roots)

    def is_available(self) -> bool:
        return True

    def wrap_command(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        return cmd


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def is_bwrap_sandbox_usable(timeout: float = 2.0) -> bool:
    """Return True when ``bwrap`` exists and can create the needed namespaces.

    ``shutil.which("bwrap")`` is not enough in Docker: the binary can exist while
    the kernel, seccomp profile, or container capabilities reject namespace
    creation. A tiny real invocation catches that before the first user command.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False

    try:
        result = subprocess.run(
            [
                bwrap,
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--die-with-parent",
                "--",
                "true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("bwrap capability probe failed", exc_info=True)
        return False

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else ""
        logger.warning("bwrap capability probe failed with code {}: {}", result.returncode, stderr)
        return False
    return True


def create_sandbox(
    *,
    enabled: bool = True,
    policy: SandboxPolicy | None = None,
    workspace_root: str | Path | None = None,
    skill_aliases: Mapping[str, str | Path] | None = None,
    allow_read_roots: Sequence[str | Path] | None = None,
) -> Sandbox:
    """Factory: return a concrete :class:`Sandbox` based on *enabled* flag.

    When *enabled* is ``True`` but ``bwrap`` is unavailable or cannot create the
    required namespaces, falls back to :class:`NoopSandbox` with a warning.
    """
    kwargs = {"workspace_root": workspace_root, "skill_aliases": skill_aliases, "allow_read_roots": allow_read_roots}
    if not enabled:
        return NoopSandbox(**kwargs)
    policy = policy or SandboxPolicy()
    sandbox = BubblewrapSandbox(policy, **kwargs)
    if not sandbox.is_available():
        set_env("DATAAGENT_SANDBOX_ENABLED", "false")
        logger.warning(
            "bwrap sandbox is not usable, falling back to NoopSandbox and disabling DATAAGENT_SANDBOX_ENABLED"
        )
        return NoopSandbox(**kwargs)
    return sandbox


# ---------------------------------------------------------------------------
# contextvars helpers (per-tool-call binding)
# ---------------------------------------------------------------------------

_current_sandbox: contextvars.ContextVar[Sandbox | None] = contextvars.ContextVar(
    "current_sandbox",
    default=None,
)


def set_current_sandbox(sandbox: Sandbox) -> contextvars.Token:
    """Bind the current runtime-local sandbox for one tool call."""
    return _current_sandbox.set(sandbox)


def get_current_sandbox() -> Sandbox:
    """Return the sandbox bound to the current tool-call context."""
    sandbox = _current_sandbox.get()
    if sandbox is None:
        raise RuntimeError("No sandbox bound to current tool call context")
    return sandbox


def reset_current_sandbox(token: contextvars.Token) -> None:
    """Reset the tool-call local sandbox context."""
    _current_sandbox.reset(token)
