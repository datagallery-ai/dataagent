"""User-managed native Deep Agents Skill packages."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any
from uuid import uuid4

import yaml
from dataagent.utils.runtime_paths import resolve_user_root

from datafoundry_api.auth import Identity
from datafoundry_api.errors import ResourceError
from datafoundry_api.file_assets import FileAssetService
from datafoundry_api.store import SqliteStore

_SKILL_KIND = "skill"
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SKILL_DIRECTORY = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_PACKAGE_BYTES = 25 * 1024 * 1024
_MAX_ARCHIVE_FILES = 200
_MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
_FRONTMATTER = re.compile(r"^---\s*\r?\n([\s\S]*?)\r?\n---\s*\r?\n([\s\S]*)$", re.MULTILINE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedSkillPackage:
    """Validated package metadata and normalized files ready for installation."""

    name: str
    description: str
    version: str
    allowed_tools: tuple[str, ...]
    package_format: str
    package_filename: str
    entry: str
    files: Mapping[str, bytes]
    size_bytes: int


@dataclass(frozen=True)
class RuntimeSkillSelection:
    """Resolved user Skill names and revision identity for one Agent graph."""

    names: tuple[str, ...]
    active_name: str | None
    cache_key: str


@dataclass
class _SkillInstallation:
    target: Path
    backup: Path | None

    def _commit(self) -> None:
        if self.backup is not None:
            _remove_path(self.backup)

    def _rollback(self) -> None:
        _remove_path(self.target)
        if self.backup is not None and self.backup.exists():
            os.replace(self.backup, self.target)


class SkillService:
    """Validate, install, persist, and select per-user native Skills."""

    def __init__(self, store: SqliteStore, files: FileAssetService) -> None:
        self._store = store
        self._files = files
        self._mutation_lock = RLock()

    def list_skills(self, identity: Identity) -> list[dict[str, Any]]:
        """Return all user-managed Skills in the current workspace."""
        rows = self._store.fetchall(
            """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ?
            ORDER BY updated_at DESC
            """,
            (identity.workspace_id, identity.user_id, _SKILL_KIND),
        )
        return [self._to_dto(self._record(row)) for row in rows]

    def get_skill(self, identity: Identity, skill_id: str) -> dict[str, Any]:
        """Return one user-managed Skill."""
        return self._to_dto(self._require_record(identity, skill_id))

    def create_skill(
        self,
        identity: Identity,
        *,
        filename: str,
        content: bytes,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate and install a new SKILL.md or ZIP package."""
        with self._mutation_lock:
            return self._create_skill(identity, filename=filename, content=content, fields=fields)

    def patch_skill(self, identity: Identity, skill_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Update Skill control fields without overriding SKILL.md metadata."""
        with self._mutation_lock:
            current = self._require_record(identity, skill_id)
            self._check_revision(current, body)
            payload = self._payload(current)
            for key in ("defaultDbIds", "defaultKbIds", "defaultMcpIds", "modelProfileId"):
                if key not in body:
                    continue
                value = body.get(key)
                if key == "modelProfileId":
                    if value is None or not str(value).strip():
                        payload.pop(key, None)
                    else:
                        payload[key] = str(value).strip()
                else:
                    payload[key] = list(_string_list(value))
            revision = int(current.get("revision", 1) or 1) + 1
            self._store.execute(
                """
                UPDATE config_resources
                SET payload_json = ?, default_enabled = ?, revision = ?, updated_at = ?
                WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    int(_boolean(body.get("defaultEnabled"), bool(current.get("default_enabled", True)))),
                    revision,
                    _now(),
                    identity.workspace_id,
                    identity.user_id,
                    _SKILL_KIND,
                    skill_id,
                ),
            )
            return self.get_skill(identity, skill_id)

    def replace_skill(
        self,
        identity: Identity,
        skill_id: str,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Atomically replace a Skill package and its installed native directory."""
        with self._mutation_lock:
            return self._replace_skill(identity, skill_id, filename=filename, content=content)

    def delete_skill(self, identity: Identity, skill_id: str) -> dict[str, Any]:
        """Delete a Skill record, installed directory, and package reference."""
        with self._mutation_lock:
            current = self._require_record(identity, skill_id)
            payload = self._payload(current)
            self._store.execute(
                "DELETE FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?",
                (identity.workspace_id, identity.user_id, _SKILL_KIND, skill_id),
            )
            self._remove_install_safely(identity, str(payload.get("installDir", "") or ""))
            self._discard_package_ref(identity, str(payload.get("packageFileRefId", "") or ""))
            return {"deleted": True, "id": skill_id}

    def validate_skill(self, identity: Identity, skill_id: str) -> dict[str, Any]:
        """Revalidate the stored package and installed SKILL.md entry."""
        current = self._require_record(identity, skill_id)
        payload = self._payload(current)
        ref_id = str(payload.get("packageFileRefId", "") or "")
        if not ref_id:
            raise ResourceError(400, "SKILL_PACKAGE_MISSING", f"Skill {skill_id} has no package reference.")
        record, content = self._files.read_ref(identity, ref_id)
        package = parse_skill_package(str(record.get("filename", "SKILL.md")), content)
        install_dir = str(payload.get("installDir", "") or "")
        entry = resolve_user_root(user_id=identity.user_id) / "skills" / install_dir / "SKILL.md"
        if not entry.is_file():
            raise ResourceError(409, "SKILL_INSTALL_MISSING", f"Installed SKILL.md is missing for {skill_id}.")
        installed = _parse_skill_md(entry.read_text(encoding="utf-8"), package.package_filename)
        if installed.name != package.name:
            raise ResourceError(409, "SKILL_INSTALL_MISMATCH", f"Installed Skill metadata differs for {skill_id}.")
        revision = int(current.get("revision", 1) or 1) + 1
        self._store.execute(
            """
            UPDATE config_resources SET status = 'valid', revision = ?, updated_at = ?
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (revision, _now(), identity.workspace_id, identity.user_id, _SKILL_KIND, skill_id),
        )
        return {"id": skill_id, "status": "valid", "validationStatus": "valid", "revision": revision}

    def test_skill(self, identity: Identity, skill_id: str) -> dict[str, Any]:
        """Run the available deterministic Skill package validation."""
        result = self.validate_skill(identity, skill_id)
        result.update({"tested": True, "reason": "SKILL.md and installed package are valid."})
        return result

    def package(self, identity: Identity, skill_id: str) -> tuple[str, str, bytes]:
        """Return the original uploaded Skill package for download."""
        current = self._require_record(identity, skill_id)
        payload = self._payload(current)
        ref_id = str(payload.get("packageFileRefId", "") or "")
        if not ref_id:
            raise ResourceError(404, "RESOURCE_NOT_FOUND", f"SKILL_PACKAGE_NOT_FOUND:{skill_id}")
        record, content = self._files.read_ref(identity, ref_id)
        filename = str(record.get("filename", "SKILL.md"))
        mime_type = str(record.get("declared_mime_type", "application/octet-stream"))
        return filename, mime_type, content

    def resolve_selection(
        self,
        identity: Identity,
        enabled_ids: tuple[str, ...] | None,
        active_id: str | None,
    ) -> RuntimeSkillSelection:
        """Resolve enabled IDs into native Skill names and a graph cache identity."""
        records = [
            self._record(row)
            for row in self._store.fetchall(
                """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ?
            ORDER BY updated_at DESC
            """,
                (identity.workspace_id, identity.user_id, _SKILL_KIND),
            )
        ]
        by_id = {str(record.get("id", "")): record for record in records}
        selected_ids = enabled_ids
        if selected_ids is None:
            selected_ids = tuple(
                str(record.get("id", "")) for record in records if bool(record.get("default_enabled", True))
            )
        missing = [skill_id for skill_id in selected_ids if skill_id not in by_id]
        if missing:
            raise ResourceError(400, "RESOURCE_NOT_FOUND", f"Unknown enabled Skill IDs: {', '.join(missing)}")
        selected = [by_id.get(skill_id, {}) for skill_id in selected_ids]
        names = tuple(str(record.get("name", "")) for record in selected if record)
        active_name = None
        if active_id and active_id in selected_ids:
            active_name = str(by_id.get(active_id, {}).get("name", "") or "") or None
        cache_parts = [f"{record.get('id', '')}:{record.get('revision', 1)}" for record in selected if record]
        return RuntimeSkillSelection(names=names, active_name=active_name, cache_key="|".join(cache_parts) or "none")

    def default_ids(self, identity: Identity) -> tuple[str, ...]:
        """Return default-enabled Skill IDs for the workspace bootstrap payload."""
        return tuple(
            str(item.get("id", "")) for item in self.list_skills(identity) if item.get("defaultEnabled") is True
        )

    def _create_skill(
        self,
        identity: Identity,
        *,
        filename: str,
        content: bytes,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        package = parse_skill_package(filename, content)
        requested_id = str(fields.get("id", "") or "").strip()
        skill_id = self._validate_id(requested_id or _slug(package.name) or f"skill-{uuid4()}")
        if self._find_record(identity, skill_id) is not None:
            raise ResourceError(409, "CONFLICT", f'Skill "{skill_id}" already exists.')
        install_dir = self._install_directory(package.name)
        self._assert_install_name_available(identity, install_dir, exclude_id=None)
        package_ref = self._files.create_ref(
            identity,
            filename=package.package_filename,
            content=content,
            source="skill-package",
            mime_type="application/zip" if package.package_format == "zip" else "text/markdown",
        )
        installation: _SkillInstallation | None = None
        try:
            installation = self._install(identity, package, install_dir)
            now = _now()
            payload = self._package_payload(package, package_ref, install_dir, fields)
            self._store.execute(
                """
                INSERT INTO config_resources (
                    id, workspace_id, user_id, kind, name, description, payload_json, secret_ref,
                    default_enabled, builtin, status, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, 'valid', 1, ?, ?)
                """,
                (
                    skill_id,
                    identity.workspace_id,
                    identity.user_id,
                    _SKILL_KIND,
                    package.name,
                    package.description,
                    json.dumps(payload, ensure_ascii=False),
                    int(_boolean(fields.get("defaultEnabled"), True)),
                    now,
                    now,
                ),
            )
        except Exception:
            if installation is not None:
                self._rollback_install(installation)
            self._discard_package_ref(identity, str(package_ref.get("id", "")))
            raise
        self._commit_install(installation)
        return self.get_skill(identity, skill_id)

    def _replace_skill(
        self,
        identity: Identity,
        skill_id: str,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        current = self._require_record(identity, skill_id)
        package = parse_skill_package(filename, content)
        install_dir = self._install_directory(package.name)
        self._assert_install_name_available(identity, install_dir, exclude_id=skill_id)
        previous_payload = self._payload(current)
        previous_dir = str(previous_payload.get("installDir", "") or "")
        package_ref = self._files.create_ref(
            identity,
            filename=package.package_filename,
            content=content,
            source="skill-package",
            mime_type="application/zip" if package.package_format == "zip" else "text/markdown",
        )
        installation: _SkillInstallation | None = None
        try:
            installation = self._install(identity, package, install_dir)
            payload = self._package_payload(package, package_ref, install_dir, previous_payload)
            revision = int(current.get("revision", 1) or 1) + 1
            self._store.execute(
                """
                UPDATE config_resources
                SET name = ?, description = ?, payload_json = ?, status = 'valid', revision = ?, updated_at = ?
                WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
                """,
                (
                    package.name,
                    package.description,
                    json.dumps(payload, ensure_ascii=False),
                    revision,
                    _now(),
                    identity.workspace_id,
                    identity.user_id,
                    _SKILL_KIND,
                    skill_id,
                ),
            )
        except Exception:
            if installation is not None:
                self._rollback_install(installation)
            self._discard_package_ref(identity, str(package_ref.get("id", "")))
            raise
        self._commit_install(installation)
        if previous_dir and previous_dir != install_dir:
            self._remove_install_safely(identity, previous_dir)
        previous_ref = str(previous_payload.get("packageFileRefId", "") or "")
        if previous_ref and previous_ref != package_ref.get("id"):
            self._discard_package_ref(identity, previous_ref)
        return self.get_skill(identity, skill_id)

    def _find_record(self, identity: Identity, skill_id: str) -> dict[str, Any] | None:
        row = self._store.fetchone(
            """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (identity.workspace_id, identity.user_id, _SKILL_KIND, skill_id),
        )
        return self._record(row) if row is not None else None

    def _require_record(self, identity: Identity, skill_id: str) -> dict[str, Any]:
        record = self._find_record(identity, skill_id)
        if record is None:
            raise ResourceError(404, "RESOURCE_NOT_FOUND", f"CONFIG_RESOURCE_NOT_FOUND:{skill_id}")
        return record

    def _record(self, row: Any) -> dict[str, Any]:
        record = dict(row)
        try:
            payload = json.loads(str(record.get("payload_json", "{}")))
        except json.JSONDecodeError:
            payload = {}
        record["payload"] = payload if isinstance(payload, dict) else {}
        return record

    def _payload(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = record.get("payload", {})
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _to_dto(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._payload(record)
        dto: dict[str, Any] = {
            "id": str(record.get("id", "")),
            "name": str(record.get("name", "")),
            "description": str(record.get("description", "") or ""),
            "secretRef": None,
            "hasSecret": False,
            "defaultEnabled": bool(record.get("default_enabled", True)),
            "builtin": bool(record.get("builtin", False)),
            "validationStatus": str(record.get("status", "untested")),
            "revision": int(record.get("revision", 1) or 1),
            "createdAt": str(record.get("created_at", "")),
            "updatedAt": str(record.get("updated_at", "")),
        }
        dto.update({key: value for key, value in payload.items() if key != "installDir"})
        return dto

    def _install(
        self,
        identity: Identity,
        package: ParsedSkillPackage,
        install_dir: str,
    ) -> _SkillInstallation:
        user_root = resolve_user_root(user_id=identity.user_id)
        skills_root = (user_root / "skills").resolve()
        skills_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".skill-staging-", dir=user_root)).resolve()
        target = (skills_root / install_dir).resolve()
        backup: Path | None = None
        _assert_within(skills_root, target)
        try:
            for relative, content in package.files.items():
                output = (staging / relative).resolve()
                _assert_within(staging, output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
            if target.exists():
                backup = user_root / f".skill-backup-{uuid4()}"
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except OSError:
                if backup is not None and backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return _SkillInstallation(target=target, backup=backup)

    def _commit_install(self, installation: _SkillInstallation | None) -> None:
        if installation is None:
            return
        try:
            installation._commit()
        except Exception:  # noqa: BLE001 - committed state is valid; only obsolete backup cleanup failed
            logger.exception("Failed to remove obsolete Skill installation backup: %s", installation.backup)

    def _rollback_install(self, installation: _SkillInstallation) -> None:
        try:
            installation._rollback()
        except Exception:  # noqa: BLE001 - preserve the original persistence failure
            logger.exception("Failed to roll back Skill installation: %s", installation.target)

    def _discard_package_ref(self, identity: Identity, ref_id: str) -> None:
        if not ref_id:
            return
        try:
            self._files.delete_ref(identity, ref_id)
        except Exception:  # noqa: BLE001 - package refs are garbage-collectable cleanup artifacts
            logger.exception("Failed to discard Skill package reference: %s", ref_id)

    def _remove_install_safely(self, identity: Identity, install_dir: str) -> None:
        try:
            self._remove_install(identity, install_dir)
        except Exception:  # noqa: BLE001 - database state is already authoritative
            logger.exception("Failed to remove obsolete Skill installation: %s", install_dir)

    def _remove_install(self, identity: Identity, install_dir: str) -> None:
        if not install_dir:
            return
        skills_root = (resolve_user_root(user_id=identity.user_id) / "skills").resolve()
        target = (skills_root / install_dir).resolve()
        _assert_within(skills_root, target)
        if target.is_dir():
            shutil.rmtree(target)

    def _assert_install_name_available(
        self,
        identity: Identity,
        install_dir: str,
        *,
        exclude_id: str | None,
    ) -> None:
        for dto in self.list_skills(identity):
            if dto.get("id") == exclude_id:
                continue
            record = self._require_record(identity, str(dto.get("id", "")))
            if self._payload(record).get("installDir") == install_dir:
                raise ResourceError(409, "CONFLICT", f'Skill directory "{install_dir}" is already installed.')

    def _install_directory(self, name: str) -> str:
        directory = _SKILL_DIRECTORY.sub("-", name).strip(".-_").lower()
        if not directory:
            raise ResourceError(400, "BAD_REQUEST", "Skill name cannot be converted to a directory name.")
        return directory[:128]

    def _validate_id(self, value: str) -> str:
        if not _RESOURCE_ID.fullmatch(value):
            raise ResourceError(400, "BAD_REQUEST", "Skill id may contain only letters, numbers, '.', '_' or '-'.")
        return value

    def _check_revision(self, current: Mapping[str, Any], body: Mapping[str, Any]) -> None:
        requested = body.get("revision")
        if requested is None:
            return
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise ResourceError(400, "BAD_REQUEST", "Skill revision must be an integer.")
        if int(requested) != int(current.get("revision", 1) or 1):
            raise ResourceError(409, "REVISION_CONFLICT", f"REVISION_CONFLICT:{current.get('id', '')}")

    def _package_payload(
        self,
        package: ParsedSkillPackage,
        package_ref: Mapping[str, Any],
        install_dir: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "allowedTools": list(package.allowed_tools),
            "version": package.version,
            "packageFileRefId": str(package_ref.get("id", "")),
            "packageFileName": package.package_filename,
            "packageFormat": package.package_format,
            "packageSource": "server",
            "manifest": {
                "entry": package.entry,
                "files": list(package.files),
                "sizeBytes": package.size_bytes,
            },
            "installDir": install_dir,
        }
        for key in ("defaultDbIds", "defaultKbIds", "defaultMcpIds"):
            values = _string_list(fields.get(key))
            if values:
                payload[key] = list(values)
        model_profile_id = str(fields.get("modelProfileId", "") or "").strip()
        if model_profile_id:
            payload["modelProfileId"] = model_profile_id
        return payload


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def parse_skill_package(filename: str, content: bytes) -> ParsedSkillPackage:
    """Parse and validate one uploaded SKILL.md or ZIP package."""
    if not content:
        raise ResourceError(400, "SKILL_PACKAGE_EMPTY", "Skill package is empty.")
    if len(content) > _MAX_PACKAGE_BYTES:
        raise ResourceError(413, "SKILL_PACKAGE_TOO_LARGE", "Skill package exceeds 25 MiB.")
    lower_name = filename.lower()
    if lower_name.endswith(".md"):
        parsed = _parse_skill_md(_decode_utf8(content, filename), filename)
        return ParsedSkillPackage(
            name=parsed.name,
            description=parsed.description,
            version=parsed.version,
            allowed_tools=parsed.allowed_tools,
            package_format="skill-md",
            package_filename=Path(filename).name or "SKILL.md",
            entry="SKILL.md",
            files={"SKILL.md": content},
            size_bytes=len(content),
        )
    if not lower_name.endswith(".zip"):
        raise ResourceError(400, "SKILL_PACKAGE_FORMAT", "Only SKILL.md and ZIP packages are supported.")
    return _parse_skill_zip(filename, content)


def _parse_skill_zip(filename: str, content: bytes) -> ParsedSkillPackage:
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ResourceError(400, "SKILL_ZIP_INVALID", "Skill ZIP package is invalid.") from exc
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > _MAX_ARCHIVE_FILES:
            raise ResourceError(400, "SKILL_ZIP_TOO_MANY_FILES", "Skill ZIP contains too many files.")
        if sum(info.file_size for info in members) > _MAX_EXTRACTED_BYTES:
            raise ResourceError(400, "SKILL_ZIP_TOO_LARGE", "Extracted Skill ZIP exceeds 50 MiB.")
        for info in members:
            _validate_zip_member(info)
        entries = [info for info in members if PurePosixPath(info.filename).name == "SKILL.md"]
        if len(entries) != 1:
            raise ResourceError(400, "SKILL_ENTRY_COUNT", "Skill ZIP must contain exactly one SKILL.md.")
        entry = entries[0]
        prefix = PurePosixPath(entry.filename).parent
        files: dict[str, bytes] = {}
        for info in members:
            path = PurePosixPath(info.filename)
            try:
                relative = path.relative_to(prefix)
            except ValueError as exc:
                raise ResourceError(400, "SKILL_ZIP_LAYOUT", "All Skill files must share the SKILL.md root.") from exc
            relative_name = relative.as_posix()
            if relative_name in {"", "."}:
                continue
            files[relative_name] = archive.read(info)
        skill_md = files.get("SKILL.md")
        if skill_md is None:
            raise ResourceError(400, "SKILL_ENTRY_MISSING", "Skill ZIP entry must be named SKILL.md.")
        parsed = _parse_skill_md(_decode_utf8(skill_md, filename), filename)
        return ParsedSkillPackage(
            name=parsed.name,
            description=parsed.description,
            version=parsed.version,
            allowed_tools=parsed.allowed_tools,
            package_format="zip",
            package_filename=Path(filename).name or "skill.zip",
            entry="SKILL.md",
            files=files,
            size_bytes=sum(len(value) for value in files.values()),
        )


def _parse_skill_md(content: str, filename: str) -> ParsedSkillPackage:
    match = _FRONTMATTER.match(content.strip())
    if match is None:
        raise ResourceError(400, "SKILL_FRONTMATTER_REQUIRED", f"YAML frontmatter is required in {filename}.")
    try:
        raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ResourceError(400, "SKILL_FRONTMATTER_INVALID", f"Invalid YAML frontmatter in {filename}.") from exc
    frontmatter = raw if isinstance(raw, Mapping) else {}
    name = str(frontmatter.get("name", "") or "").strip()
    description = str(frontmatter.get("description", "") or "").strip()
    if not name or not description:
        raise ResourceError(400, "SKILL_NAME_DESCRIPTION_REQUIRED", "Skill name and description are required.")
    return ParsedSkillPackage(
        name=name,
        description=description,
        version=str(frontmatter.get("version", "1.0.0") or "1.0.0").strip(),
        allowed_tools=_string_list(frontmatter.get("allowed-tools", frontmatter.get("allowedTools"))),
        package_format="skill-md",
        package_filename=Path(filename).name,
        entry="SKILL.md",
        files={"SKILL.md": content.encode("utf-8")},
        size_bytes=len(content.encode("utf-8")),
    )


def _decode_utf8(content: bytes, filename: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResourceError(400, "SKILL_ENCODING_INVALID", f"SKILL.md in {filename} must be UTF-8.") from exc


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    mode = info.external_attr >> 16
    if path.is_absolute() or "\x00" in info.filename or any(part in {"", ".", ".."} for part in path.parts):
        raise ResourceError(400, "SKILL_ZIP_PATH", f"Unsafe path in Skill ZIP: {info.filename}")
    if stat.S_ISLNK(mode):
        raise ResourceError(400, "SKILL_ZIP_SYMLINK", f"Symbolic links are not allowed: {info.filename}")


def _assert_within(root: Path, path: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ResourceError(400, "SKILL_PATH_ESCAPE", "Skill package escapes its managed directory.") from exc


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ResourceError(400, "BAD_REQUEST", "Expected a string list.")


def _boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _slug(value: str) -> str:
    return _SKILL_DIRECTORY.sub("-", value).strip(".-_").lower()[:128]


def _now() -> str:
    return datetime.now(UTC).isoformat()
