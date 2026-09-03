"""Compile legacy workspace settings into native Deep Agents backends."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission

from dataagent.utils.runtime_paths import resolve_session_root, validate_session_id, validate_user_id

WorkspaceBackendType = Literal["filesystem", "state"]

_BACKEND_TYPE_ALIASES: dict[str, WorkspaceBackendType] = {
    "filesystem": "filesystem",
    "filesystembackend": "filesystem",
    "state": "state",
    "statebackend": "state",
}


@dataclass(frozen=True)
class WorkspaceConfig:
    """Native backend, permissions, and prompt information for a workspace."""

    backend: BackendProtocol
    permissions: tuple[FilesystemPermission, ...]
    system_prompt: str
    workspace_root: Path | None = None
    backend_type: WorkspaceBackendType = "filesystem"
    shell_enabled: bool = True


@dataclass(frozen=True)
class _AllowedPath:
    virtual_root: Path
    host_root: Path


class WorkspaceConfigCompiler:
    """Compile ``WORKSPACE.path`` and ``WORKSPACE.allow_path`` settings."""

    def __init__(
        self,
        config: Mapping[str, Any],
        backend: BackendProtocol | None = None,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._config = config
        self._backend = backend
        self._user_id = validate_user_id(str(user_id or config.get("USER_ID") or "anonymous"))
        self._session_id = validate_session_id(str(session_id or "default_session"))

    def compile(self) -> WorkspaceConfig:
        """Build the primary workspace and additional read-only mounts."""
        workspace_config = self._workspace_config()
        backend_type = self._backend_type(workspace_config)
        workspace_root = self._resolve_workspace_root(workspace_config, backend_type)
        backend = (
            self._backend
            if self._backend is not None
            else self._create_default_backend(
                backend_type,
                workspace_root,
            )
        )
        routes: dict[str, BackendProtocol] = {}
        readonly_patterns: list[str] = []
        readonly_mounts: list[tuple[str, Path]] = []

        for index, allowed_path in enumerate(self._allow_paths(workspace_config)):
            if workspace_root is not None and self._is_within(allowed_path.host_root, workspace_root):
                continue
            virtual_path = self._virtual_mount_path(allowed_path.virtual_root, index)
            if virtual_path in routes:
                continue
            routes[virtual_path] = FilesystemBackend(root_dir=allowed_path.host_root, virtual_mode=True)
            readonly_patterns.extend((virtual_path.rstrip("/"), f"{virtual_path}**"))
            readonly_mounts.append((virtual_path, allowed_path.host_root))

        if routes:
            backend = CompositeBackend(default=backend, routes=routes)

        permissions: tuple[FilesystemPermission, ...] = ()
        if readonly_patterns:
            permissions = (FilesystemPermission(operations=["write"], paths=readonly_patterns, mode="deny"),)
        prompt = self._build_system_prompt(workspace_root, readonly_mounts)
        return WorkspaceConfig(
            backend=backend,
            permissions=permissions,
            system_prompt=prompt,
            workspace_root=workspace_root,
            backend_type=backend_type,
            shell_enabled=backend_type == "filesystem" and workspace_root is not None,
        )

    def _workspace_config(self) -> Mapping[str, Any]:
        raw = self._config.get("WORKSPACE", {})
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError("WORKSPACE must be a mapping.")
        return raw

    def _backend_type(self, workspace_config: Mapping[str, Any]) -> WorkspaceBackendType:
        if self._backend is not None:
            return self._infer_backend_type(self._backend)
        raw_backend_type = str(workspace_config.get("backend", "filesystem") or "filesystem").strip().lower()
        backend_type = _BACKEND_TYPE_ALIASES.get(raw_backend_type)
        if backend_type is None:
            allowed = ", ".join(sorted(_BACKEND_TYPE_ALIASES))
            raise ValueError(f"WORKSPACE.backend must be one of: {allowed}.")
        return backend_type

    def _resolve_workspace_root(
        self,
        workspace_config: Mapping[str, Any],
        backend_type: WorkspaceBackendType,
    ) -> Path | None:
        raw = str(workspace_config.get("path", "") or "").strip()
        if backend_type == "state":
            if raw:
                raise ValueError("WORKSPACE.path cannot be used with WORKSPACE.backend: state.")
            return None
        if self._backend is not None:
            default_backend = self._default_backend(self._backend)
            backend_root = getattr(default_backend, "cwd", None)
            resolved_backend_root = Path(backend_root).resolve() if backend_root is not None else None
            if raw and resolved_backend_root is not None and Path(raw).expanduser().resolve() != resolved_backend_root:
                raise ValueError("WORKSPACE.path does not match the externally supplied filesystem backend root.")
            return resolved_backend_root
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError("WORKSPACE.path must be an absolute path or null.")
            root = path.resolve()
        else:
            root = resolve_session_root(
                user_id=self._user_id,
                session_id=self._session_id,
                config=self._config,
            )
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("WORKSPACE.path must point to a directory.")
        return root

    @staticmethod
    def _create_default_backend(
        backend_type: WorkspaceBackendType,
        workspace_root: Path | None,
    ) -> BackendProtocol:
        if backend_type == "state":
            return StateBackend()
        if workspace_root is None:
            raise ValueError("Filesystem workspace requires a resolved workspace root.")
        return FilesystemBackend(root_dir=workspace_root, virtual_mode=True)

    @staticmethod
    def _infer_backend_type(backend: BackendProtocol) -> WorkspaceBackendType:
        default_backend = WorkspaceConfigCompiler._default_backend(backend)
        return "state" if isinstance(default_backend, StateBackend) else "filesystem"

    @staticmethod
    def _default_backend(backend: BackendProtocol) -> BackendProtocol:
        current = backend
        while isinstance(current, CompositeBackend):
            current = current.default
        return current

    @staticmethod
    def _allow_paths(workspace_config: Mapping[str, Any]) -> tuple[_AllowedPath, ...]:
        raw = workspace_config.get("allow_path", ())
        if raw is None:
            return ()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings.")

        paths: list[_AllowedPath] = []
        seen: set[tuple[Path, Path]] = set()
        for item in raw:
            configured_path = str(item).strip()
            if not configured_path:
                continue
            path = Path(configured_path).expanduser()
            if not path.is_absolute():
                raise ValueError(f"WORKSPACE.allow_path entries must be absolute paths: {configured_path!r}")
            virtual_root = Path(os.path.abspath(path))
            host_root = path.resolve()
            if not host_root.exists():
                continue
            if not host_root.is_dir():
                raise ValueError(f"WORKSPACE.allow_path entries must point to directories: {configured_path!r}")
            identity = (virtual_root, host_root)
            if identity not in seen:
                paths.append(_AllowedPath(virtual_root=virtual_root, host_root=host_root))
                seen.add(identity)
        return tuple(paths)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _virtual_mount_path(root: Path, index: int) -> str:
        if root == Path("/"):
            return f"/allowed-root-{index}/"
        return f"{root.as_posix().rstrip('/')}/"

    @staticmethod
    def _build_system_prompt(workspace_root: Path | None, readonly_mounts: list[tuple[str, Path]]) -> str:
        if workspace_root is None and not readonly_mounts:
            return ""

        lines = ["# Workspace"]
        if workspace_root is not None:
            lines.extend(
                (
                    "The writable workspace is mounted at `/` in the filesystem tools.",
                    f"Its host directory is `{workspace_root}`.",
                )
            )
        else:
            lines.append("The writable workspace at `/` is stored in the agent state.")

        if readonly_mounts:
            lines.append("Additional read-only directories:")
            for virtual_path, host_path in readonly_mounts:
                if virtual_path.rstrip("/") == host_path.as_posix().rstrip("/"):
                    lines.append(f"- `{virtual_path.rstrip('/')}`")
                else:
                    lines.append(f"- `{virtual_path.rstrip('/')}` -> `{host_path}`")
        return "\n".join(lines)
