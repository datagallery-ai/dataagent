"""Resolve and validate runtime paths; explicitly initialize the user Home when requested.

Importing this module and resolving paths never create files or directories; only
initialize_home writes the default Home layout, and session helpers write a session tree
when a host opens one. Resource discovery belongs to discovery; workspace selection
belongs to startup, and model file permissions belong to extensions.filesystem_backend.
"""

import json
import os
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

TEMPLATES = (("config.json", "config.json"), (".env", "env.example"))
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def opaque_id(value: str, label: str) -> str:
    """Accept a path-safe id: letters, digits, underscore and hyphen, at most 128 characters."""
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe opaque id of at most 128 characters")
    return value


@dataclass(frozen=True)
class WorkspaceInput:
    """One read-only input. `name` is a logical label and is never a path segment."""

    name: str
    path: Path


@dataclass(frozen=True)
class SessionPaths:
    """Private directories for one `(user_id, thread_id)` under Home runtime."""

    root: Path
    outputs: Path
    logs: Path
    traces: Path

    @property
    def session_file(self) -> Path:
        return self.root / "session.json"

    @property
    def artifacts(self) -> Path:
        return self.outputs / "artifacts"


@dataclass(frozen=True)
class SessionBinding:
    user_id: str
    thread_id: str
    workspaces: tuple[WorkspaceInput, ...]


@dataclass(frozen=True)
class RuntimePaths:
    """Runtime filesystem addresses for configuration, inputs and session resources."""

    home: Path
    workspaces: tuple[WorkspaceInput, ...]
    state_dir: Path
    builtin_plugins: Path
    user_id: str = "default"

    def for_session(self, thread_id: str) -> SessionPaths:
        return session_paths(self.home, self.user_id, thread_id)

    def open_session(self, thread_id: str, *, create: bool) -> tuple[SessionPaths, tuple[WorkspaceInput, ...]] | None:
        """Load a session binding, or create one from this process's workspace list."""
        session = self.for_session(thread_id)
        if session.session_file.is_file():
            binding = read_session_binding(session)
            if binding.user_id != self.user_id or binding.thread_id != thread_id:
                raise ValueError("Session binding does not match this profile")
            ensure_session(self.home, session)
            return session, binding.workspaces
        if not create:
            return None
        ensure_session(self.home, session)
        write_session_binding(session, self.user_id, thread_id, self.workspaces)
        return session, self.workspaces

    @classmethod
    def resolve(cls, home: Path, workspaces: tuple[WorkspaceInput, ...], *, user_id: str = "default"):
        """Validate read-only inputs and derive addresses; never create directories."""
        user_id = opaque_id(user_id, "user id")
        names, seen = set(), set()
        checked = []
        for item in workspaces:
            name = opaque_id(item.name, "workspace name")
            path = directory(item.path, required=True)
            if not os.access(path, os.R_OK | os.X_OK):
                raise ValueError(f"Workspace is not readable: {path}")
            if name in names:
                raise ValueError(f"Duplicate workspace name: {name}")
            if path in seen:
                raise ValueError(f"Duplicate workspace path: {path}")
            names.add(name)
            seen.add(path)
            checked.append(WorkspaceInput(name, path))
        return cls(
            home, tuple(checked), directory(home / "runtime"), builtin_plugins_path(), user_id,
        )


def home_path(cwd: Path) -> Path:
    """Resolve DATAAGENT_HOME or ~/.dataagent; relative values are anchored to cwd."""
    value = os.environ.get("DATAAGENT_HOME", "")
    if not value.strip():
        try:
            value = str(Path.home() / ".dataagent")
        except RuntimeError:
            raise ValueError("Cannot determine user home; set DATAAGENT_HOME") from None
    return directory(absolute(value, cwd, expand_home=True))


def session_paths(home: Path, user_id: str, thread_id: str) -> SessionPaths:
    """Address a session directory and reject ids that would escape `runtime/users`."""
    user_id = opaque_id(user_id, "user id")
    thread_id = opaque_id(thread_id, "thread id")
    users = (home / "runtime" / "users").resolve()
    parent = (users / user_id / "sessions").resolve()
    root = (parent / thread_id).resolve()
    if root == parent or not root.is_relative_to(parent):
        raise ValueError("Session path escapes its parent directory")
    return SessionPaths(root, root / "outputs", root / "logs", root / "traces")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if not path.is_dir():
        raise ValueError(f"Expected a directory: {path}")


def ensure_session(home: Path, session: SessionPaths) -> None:
    """Create the session tree. Directory mode is 0700 even when umask is wider."""
    user_root = session.root.parent.parent
    for path in (
        home / "runtime", home / "runtime" / "users", user_root, session.root.parent, session.root,
        session.outputs, session.logs, session.traces, session.artifacts,
    ):
        ensure_private_dir(path)


def write_session_binding(
    session: SessionPaths, user_id: str, thread_id: str, workspaces: tuple[WorkspaceInput, ...],
) -> None:
    """Create `session.json` once. An existing binding is kept and cannot be replaced."""
    payload = {
        "user_id": user_id, "thread_id": thread_id,
        "workspaces": [{"name": item.name, "path": str(item.path)} for item in workspaces],
    }
    target = session.session_file
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.write("\n")
    os.chmod(target, 0o600)


def read_session_binding(session: SessionPaths) -> SessionBinding:
    """Load the workspace list saved for this session and require each directory to exist."""
    target = session.session_file
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        user_id = opaque_id(payload["user_id"], "user id")
        thread_id = opaque_id(payload["thread_id"], "thread id")
        entries = payload["workspaces"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Session binding is unreadable: {target}") from error
    if not isinstance(entries, list):
        raise ValueError(f"Session binding is unreadable: {target}")
    workspaces = []
    for item in entries:
        try:
            name = opaque_id(item["name"], "workspace name")
            path = directory(Path(item["path"]), required=True)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Session binding is unreadable: {target}") from error
        if not os.access(path, os.R_OK | os.X_OK):
            raise ValueError(f"Workspace is not readable: {path}")
        workspaces.append(WorkspaceInput(name, path))
    return SessionBinding(user_id, thread_id, tuple(workspaces))


def initialize_home(home: Path) -> None:
    """Explicitly create Home directories and templates, without overwriting existing files."""
    for path in (home, *(home / name for name in ("skills", "hooks", "plugins", "runtime"))):
        missing = []
        candidate = path
        while not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        for candidate in reversed(missing):
            candidate.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir():
            raise ValueError(f"Expected a directory: {path}")
    for name, template in TEMPLATES:
        destination = home / name
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not destination.is_file():
                raise ValueError(f"Expected a file: {destination}") from None
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(files("dataagent.bootstrap").joinpath("templates", template).read_text("utf-8"))


def builtin_plugins_path() -> Path:
    """Plugins shipped inside the package: `dataagent/builtin-plugins` once installed as a wheel,
    the repository's `builtin-plugins` directory when running from source."""
    package = Path(__file__).resolve().parents[1]
    installed = package / "builtin-plugins"
    return installed if installed.is_dir() else package.parent / "builtin-plugins"


def absolute(value: str | Path, cwd: Path, *, expand_home: bool = False) -> Path:
    """Make a lexical absolute path against cwd, without resolving symlinks or checking existence.

    Expand a leading ~ only when requested; directory() performs real-path validation.
    """
    text = str(value)
    if expand_home and (text == "~" or text.startswith("~/")):
        text = str(Path.home()) + text[1:]
    path = Path(text)
    path = Path(os.path.abspath(path if path.is_absolute() else cwd / path))
    if "~" in path.parts:
        raise ValueError(f"Literal '~' path segments are unsupported: {path}")
    return path


def directory(path: Path, *, required: bool = False) -> Path:
    """Validate and resolve a directory path without creating it.

    Missing paths are allowed unless required=True; files and dangling symlinks are rejected.
    """
    if path.exists() or path.is_symlink():
        if not path.is_dir():
            raise ValueError(f"Expected a directory: {path}")
    elif required:
        raise ValueError(f"Directory does not exist: {path}")
    resolved = path.resolve()
    if "~" in resolved.parts:
        raise ValueError(f"Literal '~' path segments are unsupported: {resolved}")
    return resolved
