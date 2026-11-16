"""Resolve and validate runtime paths; explicitly initialize the user Home when requested.

Importing this module and resolving paths never create files or directories; only
initialize_home writes the default Home layout, and session helpers write a session tree
when a host opens one. Resource discovery belongs to discovery; workspace selection
belongs to startup, and model file permissions belong to extensions.filesystem_backend.
"""

import json
import os
import re
import sqlite3
import sys
from contextlib import closing, nullcontext
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path

from dataagent.declarations import OPAQUE_ID_PATTERN

TEMPLATES = (("config.json", "config.json"), (".env", "env.example"))
_OPAQUE_ID = re.compile(OPAQUE_ID_PATTERN)


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
    """One session's Home state directories and its independently located outputs."""

    root: Path
    outputs: Path
    traces: Path

    @property
    def artifacts(self) -> Path:
        return self.outputs / "artifacts"


@dataclass(frozen=True)
class SessionBinding:
    user_id: str
    thread_id: str
    workspaces: tuple[WorkspaceInput, ...]
    outputs: Path


@dataclass(frozen=True)
class RuntimePaths:
    """Runtime filesystem addresses for configuration, inputs and session resources."""

    home: Path
    workspaces: tuple[WorkspaceInput, ...]
    state_dir: Path
    builtin_plugins: Path
    output_root: Path
    user_id: str = "default"

    @property
    def sessions_db(self) -> Path:
        """Shared file; bootstrap owns bindings, serve owns its session index."""
        return self.home / "runtime" / "sessions.sqlite"

    def for_session(self, thread_id: str) -> SessionPaths:
        """Resolve saved outputs without creating directories or database records."""
        root = session_root(self.home, self.user_id, thread_id)
        binding = find_session_binding(root, check_inputs=False)
        outputs = binding.outputs if binding else self.output_root / self.user_id / thread_id
        return SessionPaths(root, outputs, root / "traces")

    def open_session(
        self, thread_id: str, *, create: bool, output_dir: str | Path | None = None,
        input_workspaces: tuple[WorkspaceInput, ...] | None = None,
    ) -> tuple[SessionPaths, tuple[WorkspaceInput, ...]] | None:
        """Open a saved binding read-only, or register a missing one when create=True."""
        root = session_root(self.home, self.user_id, thread_id)
        binding = find_session_binding(root)
        if binding is not None:
            validate_workspace_binding(binding, input_workspaces)
        outputs = binding.outputs if binding else self.output_root / self.user_id / thread_id
        session = SessionPaths(root, outputs, root / "traces")
        if output_dir is not None:
            outputs = output_directory(output_dir)
            if binding and path_key(outputs) != path_key(binding.outputs):
                raise ValueError("Session is already bound to a different output directory")
            if not binding:
                session = replace(session, outputs=outputs)
        if not binding and not create:
            return None
        workspaces = (binding.workspaces if binding else
                      self.workspaces if input_workspaces is None else input_workspaces)
        if not binding:
            _write_session_binding(
                session, self.user_id, thread_id, workspaces,
                adopt=output_dir is not None,
            )
            # A concurrent opener may have established the binding first.
            binding = read_session_binding(session)
            validate_workspace_binding(binding, input_workspaces)
            session = replace(session, outputs=binding.outputs)
            workspaces = binding.workspaces
        else:
            validate_outputs(session, workspaces)
            ensure_session(self.home, session)
        return session, workspaces

    @classmethod
    def resolve(
        cls, home: Path, workspaces: tuple[WorkspaceInput, ...], *,
        user_id: str = "default", output_root: Path | None = None,
    ):
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
            home=home, workspaces=tuple(checked), state_dir=directory(home / "runtime"),
            builtin_plugins=builtin_plugins_path(), user_id=user_id,
            output_root=output_directory(output_root if output_root is not None else home / "outputs"),
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


def session_root(home: Path, user_id: str, thread_id: str) -> Path:
    """Address private session state, independent of the session's output location."""
    user_id = opaque_id(user_id, "user id")
    thread_id = opaque_id(thread_id, "thread id")
    users = (home / "runtime" / "users").resolve()
    parent = (users / user_id / "sessions").resolve()
    root = (parent / thread_id).resolve()
    if root == parent or not root.is_relative_to(parent):
        raise ValueError("Session path escapes its parent directory")
    return root


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if not path.is_dir():
        raise ValueError(f"Expected a directory: {path}")


def ensure_session(home: Path, session: SessionPaths) -> None:
    """Keep state and Home output parents private; leave business directory modes alone."""
    user_root = session.root.parent.parent
    for path in (
        home / "runtime", home / "runtime" / "users", user_root, session.root.parent, session.root,
        session.traces,
    ):
        ensure_private_dir(path)
    default_outputs = (home / "outputs").resolve()
    if session.outputs.is_relative_to(default_outputs):
        ensure_private_dir(default_outputs)
        relative = session.outputs.relative_to(default_outputs)
        if relative.parts:
            ensure_private_dir(default_outputs / relative.parts[0])
    for path in (session.outputs, session.artifacts):
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError(f"Expected a directory: {path}")


def path_key(path: Path) -> str:
    """Resolve aliases; conservatively reject case-only overlaps on macOS and Windows."""
    value = os.path.normcase(str(path.resolve()))
    return value.casefold() if sys.platform == "darwin" else value


def paths_overlap(left: Path, right: Path) -> bool:
    a, b = Path(path_key(left)), Path(path_key(right))
    return a.is_relative_to(b) or b.is_relative_to(a)


def validate_outputs(session: SessionPaths, workspaces: tuple[WorkspaceInput, ...]) -> None:
    home = session.root.parents[4]
    output = Path(path_key(session.outputs))
    home = Path(path_key(home))
    # Compare against the literal outputs area under the canonical Home. Resolving
    # that exception too would let an outputs -> plugins symlink bypass the rule.
    if paths_overlap(output, home) and not output.is_relative_to(home / "outputs"):
        raise ValueError("Output directory overlaps Home outside its outputs area")
    if any(paths_overlap(session.outputs, item.path) for item in workspaces):
        raise ValueError("Output directory overlaps an input workspace")


def validate_workspace_binding(
    binding: SessionBinding, requested: tuple[WorkspaceInput, ...] | None,
) -> None:
    """Omitted inputs resume the saved list; explicit paths must match it in order."""
    if requested is not None and tuple(directory(item.path, required=True) for item in requested) != (
        tuple(directory(item.path, required=True) for item in binding.workspaces)
    ):
        raise ValueError(
            f"Session {binding.thread_id} is bound to a different workspace list. "
            "Start a new session to use different inputs."
        )


def _binding_db(root: Path) -> Path:
    return root.parents[3] / "sessions.sqlite"


def _database_payload(
    root: Path, connection: sqlite3.Connection | None = None,
) -> dict | None:
    database = _binding_db(root)
    if connection is None and not database.is_file():
        return None
    manager = (nullcontext(connection) if connection is not None else
               closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)))
    with manager as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_bindings'"
        ).fetchone():
            return None
        row = connection.execute(
            "SELECT user_id, thread_id, workspaces, output_dir FROM session_bindings "
            "WHERE user_id=? AND thread_id=?", (root.parent.parent.name, root.name),
        ).fetchone()
    if row is None:
        return None
    return dict(user_id=row[0], thread_id=row[1], workspaces=json.loads(row[2]), outputs=row[3])


def find_session_binding(
    root: Path, *, check_inputs: bool = True,
    _connection: sqlite3.Connection | None = None,
) -> SessionBinding | None:
    payload = _database_payload(root, _connection)
    if payload is None:
        return None
    binding = _parse_session_binding(root, payload, check_inputs=check_inputs)
    if (binding.user_id, binding.thread_id) != (root.parent.parent.name, root.name):
        raise ValueError("Session binding does not match this user")
    return binding


def _write_session_binding(
    session: SessionPaths, user_id: str, thread_id: str, workspaces: tuple[WorkspaceInput, ...],
    *, adopt: bool = False,
) -> None:
    """Atomically check and register ownership within one Home's binding table."""
    if (user_id, thread_id) != (session.root.parent.parent.name, session.root.name):
        raise ValueError("Session binding does not match this user")
    database = _binding_db(session.root)
    ensure_private_dir(database.parent)
    with closing(sqlite3.connect(database, timeout=5)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS session_bindings (
                user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                output_dir TEXT NOT NULL UNIQUE, workspaces TEXT NOT NULL,
                PRIMARY KEY (user_id, thread_id)
            )
        """)
        # Resolve again under the write lock, including aliases in stored paths.
        session = replace(session, outputs=output_directory(session.outputs))
        existing = find_session_binding(session.root, _connection=connection)
        if existing:
            if path_key(existing.outputs) != path_key(session.outputs):
                raise ValueError("Session is already bound to a different output directory")
            workspaces = existing.workspaces
        validate_outputs(session, workspaces)
        for owner_user, owner_thread, output in connection.execute(
            "SELECT user_id, thread_id, output_dir FROM session_bindings"
        ):
            if (owner_user, owner_thread) != (user_id, thread_id) and paths_overlap(
                session.outputs, Path(output)
            ):
                raise ValueError(f"Output directory is used by session {owner_user}/{owner_thread}")
        if not existing and not adopt and session.outputs.exists() and any(
            item.name != "artifacts" or not item.is_dir() or any(item.iterdir())
            for item in session.outputs.iterdir()
        ):
            raise ValueError(
                f"Output directory {session.outputs} already contains files that no session owns. "
                "Pass it explicitly as the SDK output_dir to adopt it, or empty it."
            )
        ensure_session(database.parent.parent, session)
        connection.execute(
            "INSERT INTO session_bindings (user_id, thread_id, output_dir, workspaces) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, thread_id) DO NOTHING",
            (user_id, thread_id, str(session.outputs), json.dumps([
                {"name": item.name, "path": str(item.path)} for item in workspaces
            ])),
        )


def read_session_binding(session: SessionPaths, *, check_inputs: bool = True) -> SessionBinding:
    """Read the session's authoritative database binding."""
    binding = find_session_binding(session.root, check_inputs=check_inputs)
    if binding is None:
        raise ValueError(f"Session binding is unreadable: {session.root}")
    return binding


def _parse_session_binding(
    root: Path, payload: dict, *, check_inputs: bool,
) -> SessionBinding:
    target = f"{_binding_db(root)} ({root.parent.parent.name}/{root.name})"
    try:
        user_id = opaque_id(payload["user_id"], "user id")
        thread_id = opaque_id(payload["thread_id"], "thread id")
        entries = payload["workspaces"]
        outputs = Path(payload["outputs"])
        if not outputs.is_absolute():
            raise ValueError("Session output directory must be absolute")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Session binding is unreadable: {target}") from error
    if not isinstance(entries, list):
        raise ValueError(f"Session binding is unreadable: {target}")
    workspaces = []
    for item in entries:
        try:
            name = opaque_id(item["name"], "workspace name")
            path = Path(item["path"])
            if check_inputs:
                path = directory(path, required=True)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Session binding is unreadable: {target}") from error
        if check_inputs and not os.access(path, os.R_OK | os.X_OK):
            raise ValueError(f"Workspace is not readable: {path}")
        workspaces.append(WorkspaceInput(name, path))
    return SessionBinding(user_id, thread_id, tuple(workspaces), outputs)


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


def output_directory(value: str | Path) -> Path:
    """Accept an explicit output location, independent of cwd or config location."""
    text = str(value)
    if not (text.startswith("~/") or Path(text).is_absolute()):
        raise ValueError("Output directory must be an absolute path or start with ~/")
    return directory(absolute(text, Path("/"), expand_home=True))
