"""Content-addressed files and the session/common workspace boundary."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from dataagent.utils.runtime_paths import resolve_session_root, resolve_user_root, validate_session_id, validate_user_id
from langchain_core.tools import BaseTool, tool

from datafoundry_api.auth import Identity
from datafoundry_api.errors import ResourceError
from datafoundry_api.store import SqliteStore

_SAFE_FILE_CHARACTER = re.compile(r"[^A-Za-z0-9._ -]+")
_ORIGIN_BY_SOURCE = {
    "artifact": "generated",
    "knowledge": "knowledge",
    "run-attachment": "run-attachment",
    "skill-package": "skill-package",
    "upload": "uploaded",
    "workspace": "saved",
}
_TEXT_READ_LIMIT = 2 * 1024 * 1024


class FileAssetService:
    """Persist deduplicated bytes and scoped references under ``~/.dataagent``."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def ensure_workspace(self, user_id: str, session_id: str) -> tuple[Path, Path]:
        """Create and return the session-private and user-common workspace directories."""
        resolved_user_id = validate_user_id(user_id)
        resolved_session_id = _validated_session_id(session_id)
        session_root = resolve_session_root(user_id=resolved_user_id, session_id=resolved_session_id)
        workspace_root = resolve_user_root(user_id=resolved_user_id) / "workspace"
        session_root.mkdir(parents=True, exist_ok=True)
        workspace_root.mkdir(parents=True, exist_ok=True)
        (resolve_user_root(user_id=resolved_user_id) / "assets").mkdir(parents=True, exist_ok=True)
        return session_root.resolve(), workspace_root.resolve()

    def create_ref(
        self,
        identity: Identity,
        *,
        filename: str,
        content: bytes,
        source: str,
        mime_type: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        preserve_relative_path: bool = False,
    ) -> dict[str, Any]:
        """Create a content-addressed asset and one tenant-scoped reference.

        Session uploads reuse their active reference when the same filename is uploaded again.
        """
        resolved_user_id = validate_user_id(identity.user_id)
        resolved_session_id = _validated_session_id(session_id) if session_id else None
        safe_name = _safe_relative_path(filename) if preserve_relative_path else _safe_filename(filename)
        sha256 = hashlib.sha256(content).hexdigest()
        existing = self._store.fetchone(
            "SELECT * FROM file_assets WHERE user_id = ? AND sha256 = ?",
            (resolved_user_id, sha256),
        )
        if existing is None:
            asset_id = str(uuid4())
            storage_path = self._asset_path(resolved_user_id, sha256)
            self._write_asset(storage_path, content)
            self._store.execute(
                """
                INSERT INTO file_assets (id, user_id, sha256, size_bytes, storage_path, detected_mime_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    resolved_user_id,
                    sha256,
                    len(content),
                    str(storage_path),
                    mime_type or _mime_type(safe_name),
                    _now(),
                ),
            )
        else:
            asset = dict(existing)
            asset_id = str(asset.get("id", ""))
            storage_path = self._checked_storage_path(resolved_user_id, str(asset.get("storage_path", "")))
            if not storage_path.is_file():
                self._write_asset(storage_path, content)

        now = _now()
        existing_ref = None
        if source == "upload" and resolved_session_id is not None:
            existing_ref = self._store.fetchone(
                """
                SELECT id FROM file_asset_refs
                WHERE workspace_id = ? AND user_id = ? AND source = 'upload'
                  AND session_id = ? AND filename = ? AND deleted_at IS NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (identity.workspace_id, resolved_user_id, resolved_session_id, safe_name),
            )
        if existing_ref is None:
            ref_id = str(uuid4())
            self._store.execute(
                """
                INSERT INTO file_asset_refs (
                    id, file_asset_id, workspace_id, user_id, filename, declared_mime_type, source,
                    session_id, run_id, metadata_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                """,
                (
                    ref_id,
                    asset_id,
                    identity.workspace_id,
                    resolved_user_id,
                    safe_name,
                    mime_type or _mime_type(safe_name),
                    source,
                    resolved_session_id,
                    run_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        else:
            ref_id = str(dict(existing_ref).get("id", ""))
            self._store.execute(
                """
                UPDATE file_asset_refs
                SET file_asset_id = ?, declared_mime_type = ?, run_id = ?, metadata_json = ?,
                    status = 'ready', deleted_at = NULL, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND user_id = ?
                """,
                (
                    asset_id,
                    mime_type or _mime_type(safe_name),
                    run_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    ref_id,
                    identity.workspace_id,
                    resolved_user_id,
                ),
            )
            self._store.execute(
                """
                UPDATE file_asset_refs
                SET status = 'deleted', deleted_at = ?, updated_at = ?
                WHERE workspace_id = ? AND user_id = ? AND source = 'upload'
                  AND session_id = ? AND filename = ? AND id != ? AND deleted_at IS NULL
                """,
                (
                    now,
                    now,
                    identity.workspace_id,
                    resolved_user_id,
                    resolved_session_id,
                    safe_name,
                    ref_id,
                ),
            )
        return self.get_ref(identity, ref_id)

    def create_ref_from_path(
        self,
        identity: Identity,
        *,
        path: Path,
        filename: str,
        source: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a reference from bytes already present in a managed workspace."""
        return self.create_ref(
            identity,
            filename=filename,
            content=path.read_bytes(),
            source=source,
            mime_type=_mime_type(filename),
            session_id=session_id,
            metadata=metadata,
            preserve_relative_path=True,
        )

    def get_ref(self, identity: Identity, ref_id: str) -> dict[str, Any]:
        """Return one active file reference owned by the authenticated tenant."""
        row = self._store.fetchone(
            """
            SELECT r.*, a.sha256, a.size_bytes, a.storage_path, a.detected_mime_type, a.created_at AS asset_created_at
            FROM file_asset_refs r
            JOIN file_assets a ON a.id = r.file_asset_id
            WHERE r.id = ? AND r.workspace_id = ? AND r.user_id = ? AND r.deleted_at IS NULL
            """,
            (ref_id, identity.workspace_id, identity.user_id),
        )
        if row is None:
            raise ResourceError(404, "RESOURCE_NOT_FOUND", f"FILE_ASSET_REF_NOT_FOUND:{ref_id}")
        record = dict(row)
        self._checked_storage_path(identity.user_id, str(record.get("storage_path", "")))
        return record

    def list_refs(
        self,
        identity: Identity,
        *,
        scope: str | None = None,
        session_id: str | None = None,
        sources: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """List active file references with optional scope, session, and source filters."""
        conditions = ["r.workspace_id = ?", "r.user_id = ?", "r.deleted_at IS NULL"]
        params: list[Any] = [identity.workspace_id, identity.user_id]
        if session_id:
            conditions.append("r.session_id = ?")
            params.append(_validated_session_id(session_id))
        elif scope == "workspace":
            conditions.append("r.session_id IS NULL")
        elif scope == "session":
            conditions.append("r.session_id IS NOT NULL")
        if sources:
            conditions.append(f"r.source IN ({','.join('?' for _ in sources)})")
            params.extend(sources)
        rows = self._store.fetchall(
            f"""
            SELECT r.*, a.sha256, a.size_bytes, a.storage_path, a.detected_mime_type
            FROM file_asset_refs r
            JOIN file_assets a ON a.id = r.file_asset_id
            WHERE {" AND ".join(conditions)}
            ORDER BY r.created_at DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def read_ref(self, identity: Identity, ref_id: str) -> tuple[dict[str, Any], bytes]:
        """Read one active reference and return its metadata and bytes."""
        record = self.get_ref(identity, ref_id)
        path = self._checked_storage_path(identity.user_id, str(record.get("storage_path", "")))
        return record, path.read_bytes()

    def materialize_session_ref(self, identity: Identity, ref_id: str, session_id: str) -> Path:
        """Copy or hardlink a reference into the selected session workspace."""
        record = self.get_ref(identity, ref_id)
        session_root, _ = self.ensure_workspace(identity.user_id, session_id)
        target = _resolve_relative(session_root, str(record.get("filename", "file")))
        self._materialize(record, target)
        return target

    def promote_ref(self, identity: Identity, ref_id: str) -> dict[str, Any]:
        """Promote one session-scoped reference into the common user workspace."""
        source = self.get_ref(identity, ref_id)
        if not source.get("session_id"):
            if source.get("source") == "workspace":
                return source
            raise ResourceError(400, "BAD_REQUEST", "Only session-scoped files can be promoted.")
        filename = _safe_relative_path(str(source.get("filename", "file")))
        existing = self._store.fetchone(
            """
            SELECT id FROM file_asset_refs
            WHERE workspace_id = ? AND user_id = ? AND source = 'workspace'
              AND session_id IS NULL AND filename = ? AND deleted_at IS NULL
            """,
            (identity.workspace_id, identity.user_id, filename),
        )
        now = _now()
        if existing is None:
            promoted_id = str(uuid4())
            self._store.execute(
                """
                INSERT INTO file_asset_refs (
                    id, file_asset_id, workspace_id, user_id, filename, declared_mime_type, source,
                    session_id, run_id, metadata_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'workspace', NULL, NULL, ?, 'ready', ?, ?)
                """,
                (
                    promoted_id,
                    source.get("file_asset_id"),
                    identity.workspace_id,
                    identity.user_id,
                    filename,
                    source.get("declared_mime_type"),
                    json.dumps({"promoted_from": ref_id}),
                    now,
                    now,
                ),
            )
        else:
            promoted_id = str(dict(existing).get("id", ""))
            self._store.execute(
                """
                UPDATE file_asset_refs
                SET file_asset_id = ?, declared_mime_type = ?, metadata_json = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND user_id = ?
                """,
                (
                    source.get("file_asset_id"),
                    source.get("declared_mime_type"),
                    json.dumps({"promoted_from": ref_id}),
                    now,
                    promoted_id,
                    identity.workspace_id,
                    identity.user_id,
                ),
            )
        promoted = self.get_ref(identity, promoted_id)
        _, workspace_root = self.ensure_workspace(identity.user_id, str(source.get("session_id", "default_session")))
        self._materialize(promoted, _resolve_relative(workspace_root, filename))
        return promoted

    def delete_ref(self, identity: Identity, ref_id: str) -> dict[str, Any]:
        """Soft-delete one file reference and remove its managed materialized copy."""
        record = self.get_ref(identity, ref_id)
        session_id = str(record.get("session_id", "") or "")
        if record.get("source") in {"upload", "workspace"}:
            if session_id:
                root, _ = self.ensure_workspace(identity.user_id, session_id)
            else:
                root = resolve_user_root(user_id=identity.user_id) / "workspace"
            target = _resolve_relative(root.resolve(), str(record.get("filename", "file")))
            if (target.is_file() or target.is_symlink()) and not self._has_other_materialized_ref(record, ref_id):
                target.unlink()
        self._store.execute(
            """
            UPDATE file_asset_refs SET status = 'deleted', deleted_at = ?, updated_at = ?
            WHERE id = ? AND workspace_id = ? AND user_id = ?
            """,
            (_now(), _now(), ref_id, identity.workspace_id, identity.user_id),
        )
        return {"deleted": True, "id": ref_id}

    def to_dto(self, record: dict[str, Any]) -> dict[str, Any]:
        """Convert an internal file record to the existing frontend DTO."""
        source = str(record.get("source", ""))
        dto: dict[str, Any] = {
            "id": str(record.get("id", "")),
            "assetId": str(record.get("file_asset_id", "")),
            "filename": str(record.get("filename", "")),
            "mimeType": record.get("declared_mime_type")
            or record.get("detected_mime_type")
            or "application/octet-stream",
            "sizeBytes": int(record.get("size_bytes", 0) or 0),
            "sha256": str(record.get("sha256", "")),
            "source": source,
            "origin": _ORIGIN_BY_SOURCE.get(source, "other"),
            "scope": "session" if record.get("session_id") else "workspace",
            "status": str(record.get("status", "ready")),
            "createdAt": str(record.get("created_at", "")),
        }
        if record.get("session_id"):
            dto["sessionId"] = str(record.get("session_id", ""))
        if record.get("run_id"):
            dto["runId"] = str(record.get("run_id", ""))
        return dto

    def workspace_tools(self, identity: Identity, session_id: str) -> tuple[BaseTool, ...]:
        """Create the three dedicated tools that cross the common-workspace boundary."""
        session_root, workspace_root = self.ensure_workspace(identity.user_id, session_id)
        service = self

        @tool
        async def list_workspace_files(path: str = ".") -> dict[str, Any]:
            """List files in the user's cross-session, read-only common workspace."""
            directory = _resolve_relative(workspace_root, path)
            if not directory.is_dir():
                raise ValueError(f"Workspace directory does not exist: {path}")
            files = []
            for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
                resolved = child.resolve()
                _assert_within(workspace_root, resolved)
                relative = resolved.relative_to(workspace_root).as_posix()
                files.append(
                    {
                        "path": relative,
                        "type": "directory" if resolved.is_dir() else "file",
                        "size_bytes": resolved.stat().st_size if resolved.is_file() else None,
                    }
                )
            return {"path": path, "files": files}

        @tool
        async def read_workspace_file(path: str) -> dict[str, Any]:
            """Read a UTF-8 file from the user's cross-session common workspace."""
            file_path = _resolve_relative(workspace_root, path)
            if not file_path.is_file():
                raise ValueError(f"Workspace file does not exist: {path}")
            content = file_path.read_bytes()
            if len(content) > _TEXT_READ_LIMIT:
                raise ValueError(f"Workspace file exceeds {_TEXT_READ_LIMIT} bytes: {path}")
            return {
                "path": file_path.relative_to(workspace_root).as_posix(),
                "content": content.decode("utf-8", errors="replace"),
                "size_bytes": len(content),
                "mime_type": _mime_type(path),
            }

        @tool
        async def promote_workspace_file(
            path: str,
            filename: str | None = None,
            description: str | None = None,
        ) -> dict[str, Any]:
            """Promote a session file into the user's cross-session common workspace."""
            source_path = _resolve_relative(session_root, path)
            if not source_path.is_file():
                raise ValueError(f"Session file does not exist: {path}")
            target_name = _safe_relative_path(filename or source_path.name)
            source_ref = service.create_ref_from_path(
                identity,
                path=source_path,
                filename=target_name,
                source="workspace",
                session_id=session_id,
                metadata={"description": description} if description else None,
            )
            promoted = service.promote_ref(identity, str(source_ref.get("id", "")))
            dto = service.to_dto(promoted)
            dto["download_url"] = f"/api/v1/files/{dto.get('id', '')}/download"
            return dto

        return list_workspace_files, read_workspace_file, promote_workspace_file

    def _asset_path(self, user_id: str, sha256: str) -> Path:
        root = (resolve_user_root(user_id=user_id) / "assets").resolve()
        path = (root / sha256[:2] / sha256[2:4] / sha256).resolve()
        _assert_within(root, path)
        return path

    def _checked_storage_path(self, user_id: str, value: str) -> Path:
        root = (resolve_user_root(user_id=validate_user_id(user_id)) / "assets").resolve()
        path = Path(value).expanduser().resolve()
        _assert_within(root, path)
        return path

    def _has_other_materialized_ref(self, record: dict[str, Any], ref_id: str) -> bool:
        session_id = str(record.get("session_id", "") or "")
        params: list[Any] = [
            record.get("workspace_id"),
            record.get("user_id"),
            record.get("filename"),
            ref_id,
        ]
        session_clause = "session_id IS NULL"
        if session_id:
            session_clause = "session_id = ?"
            params.append(session_id)
        row = self._store.fetchone(
            f"""
            SELECT id FROM file_asset_refs
            WHERE workspace_id = ? AND user_id = ? AND filename = ? AND id != ?
              AND source IN ('upload', 'workspace') AND deleted_at IS NULL AND {session_clause}
            LIMIT 1
            """,
            tuple(params),
        )
        return row is not None

    def _write_asset(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            return
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)

    def _materialize(self, record: dict[str, Any], target: Path) -> None:
        source = self._checked_storage_path(str(record.get("user_id", "")), str(record.get("storage_path", "")))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".materialize-", dir=target.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
            shutil.copy2(source, temporary_path)
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validated_session_id(value: str) -> str:
    try:
        return validate_session_id(value)
    except ValueError as exc:
        raise ResourceError(400, "BAD_REQUEST", str(exc)) from exc


def _mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    cleaned = _SAFE_FILE_CHARACTER.sub("-", name).strip()
    if not cleaned or cleaned in {".", ".."}:
        return f"file-{uuid4()}"
    return cleaned


def _safe_relative_path(value: str) -> str:
    raw = str(value).replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or "\x00" in raw or any(part in {"", ".", ".."} for part in path.parts):
        raise ResourceError(400, "BAD_REQUEST", "Workspace path must be a safe relative path.")
    segments = [_SAFE_FILE_CHARACTER.sub("-", part).strip() for part in path.parts]
    if any(not part or part in {".", ".."} for part in segments):
        raise ResourceError(400, "BAD_REQUEST", "Workspace path contains an invalid segment.")
    return "/".join(segments)


def _resolve_relative(root: Path, value: str) -> Path:
    relative = _safe_relative_path(value) if value not in {"", "."} else "."
    candidate = (root / relative).resolve()
    _assert_within(root, candidate)
    return candidate


def _assert_within(root: Path, path: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ResourceError(400, "BAD_REQUEST", "Workspace path escapes its managed root.") from exc
