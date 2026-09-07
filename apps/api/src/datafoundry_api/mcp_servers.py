"""Workspace-scoped MCP configuration backed by the official LangChain adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dataagent.core.deepagents.config.tool_hooks import tag_tool
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection

from datafoundry_api.auth import Identity
from datafoundry_api.errors import ResourceError
from datafoundry_api.settings import Settings
from datafoundry_api.store import SqliteStore

_MCP_KIND = "mcp-server"
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_TIMEOUT_MS = 30_000
_PAYLOAD_KEYS = ("transport", "serverUrl", "apiUrl", "authType", "toolAllowlist", "timeoutMs")
_DISCOVERY_KEYS = {"transport", "serverUrl", "authType", "toolAllowlist", "timeoutMs"}


@dataclass(frozen=True)
class McpToolSelection:
    """Live MCP tools, unavailable servers, and cache identity for one run."""

    tools: tuple[BaseTool, ...]
    unavailable: tuple[dict[str, str], ...]
    cache_key: str


class McpServerService:
    """Persist remote MCP servers and discover LangChain tools on demand."""

    def __init__(self, store: SqliteStore, settings: Settings) -> None:
        self._store = store
        master_key = settings.secret_master_key
        self._secret_key = hashlib.sha256(master_key.encode("utf-8")).digest() if master_key else None

    def list_servers(self, identity: Identity) -> list[dict[str, Any]]:
        """Return public MCP server configurations for one workspace."""
        rows = self._store.fetchall(
            """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ?
            ORDER BY updated_at DESC
            """,
            (identity.workspace_id, identity.user_id, _MCP_KIND),
        )
        return [self._to_dto(self._record(row)) for row in rows]

    def get_server(self, identity: Identity, server_id: str) -> dict[str, Any]:
        """Return one public MCP server configuration."""
        return self._to_dto(self._require_record(identity, server_id))

    def create_server(self, identity: Identity, body: Mapping[str, Any]) -> dict[str, Any]:
        """Create one frontend-managed remote MCP server."""
        server_id = _validate_id(body.get("id"), "MCP server")
        if self._find_record(identity, server_id) is not None:
            raise ResourceError(409, "CONFLICT", f'MCP server "{server_id}" already exists.')
        record = self._save(identity, server_id, body, current=None)
        return self._to_dto(record)

    def patch_server(self, identity: Identity, server_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Patch one MCP server with optimistic revision checking."""
        current = self._require_record(identity, server_id)
        return self._to_dto(self._save(identity, server_id, body, current=current))

    def delete_server(self, identity: Identity, server_id: str) -> dict[str, Any]:
        """Delete an MCP server and its encrypted credentials."""
        current = self._require_record(identity, server_id)
        secret_ref = str(current.get("secret_ref", "") or "")
        if secret_ref:
            self._delete_secret(identity, secret_ref)
        self._store.execute(
            "DELETE FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?",
            (identity.workspace_id, identity.user_id, _MCP_KIND, server_id),
        )
        return {"deleted": True, "id": server_id}

    async def test_server(self, identity: Identity, server_id: str) -> dict[str, Any]:
        """Initialize the MCP server, list tools, and persist health plus manifest."""
        record = self._require_record(identity, server_id)
        started = time.perf_counter()
        try:
            tools = await self._discover_record(identity, record)
        except ResourceError:
            self._set_status(identity, record, "failed")
            raise
        except Exception as exc:
            self._set_status(identity, record, "failed")
            raise ResourceError(502, "MCP_TEST_FAILED", f"MCP server probe failed: {exc}") from exc
        manifest = [_tool_manifest(tool) for tool in tools]
        updated = self._set_status(identity, record, "connected", manifest=manifest)
        return {
            "id": server_id,
            "status": "connected",
            "latencyMs": round((time.perf_counter() - started) * 1000),
            "toolCount": len(manifest),
            "revision": int(updated.get("revision", 1) or 1),
        }

    async def list_tools(self, identity: Identity, server_id: str) -> list[dict[str, Any]]:
        """Discover and return the current allowlisted tool manifest."""
        record = self._require_record(identity, server_id)
        try:
            return [_tool_manifest(tool) for tool in await self._discover_record(identity, record)]
        except ResourceError:
            raise
        except Exception as exc:
            raise ResourceError(502, "MCP_DISCOVERY_FAILED", f"MCP tool discovery failed: {exc}") from exc

    async def resolve_tools(
        self,
        identity: Identity,
        enabled_ids: tuple[str, ...] | None,
        *,
        reserved_names: set[str] | None = None,
    ) -> McpToolSelection:
        """Resolve enabled servers and isolate per-server connection failures."""
        records = [
            self._record(row)
            for row in self._store.fetchall(
                """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ?
            ORDER BY updated_at DESC
            """,
                (identity.workspace_id, identity.user_id, _MCP_KIND),
            )
        ]
        by_id = {str(record.get("id", "")): record for record in records}
        selected_ids = enabled_ids
        if selected_ids is None:
            selected_ids = tuple(
                str(record.get("id", "")) for record in records if bool(record.get("default_enabled", True))
            )
        missing = [server_id for server_id in selected_ids if server_id not in by_id]
        if missing:
            raise ResourceError(400, "RESOURCE_NOT_FOUND", f"Unknown enabled MCP server IDs: {', '.join(missing)}")
        selected = [by_id.get(server_id, {}) for server_id in selected_ids]

        async def discover(record: dict[str, Any]) -> tuple[dict[str, Any], list[BaseTool] | Exception]:
            try:
                tools = await self._discover_record(identity, record)
                return record, tools
            except Exception as exc:  # noqa: BLE001 - each remote MCP must fail independently
                return record, exc

        results = await asyncio.gather(*(discover(record) for record in selected))
        available: list[tuple[str, BaseTool]] = []
        unavailable: list[dict[str, str]] = []
        for record, result in results:
            server_id = str(record.get("id", ""))
            if isinstance(result, Exception):
                unavailable.append({"id": server_id, "name": str(record.get("name", server_id)), "reason": str(result)})
                continue
            available.extend((server_id, tool) for tool in result)

        name_counts = Counter(tool.name for _, tool in available)
        reserved = reserved_names or set()
        resolved_tools: list[BaseTool] = []
        signatures: list[str] = []
        for server_id, discovered_tool in available:
            tool_name = discovered_tool.name
            if name_counts.get(tool_name, 0) > 1 or tool_name in reserved:
                tool_name = f"mcp__{_safe_tool_segment(server_id)}__{_safe_tool_segment(tool_name)}"
            tool_copy = discovered_tool.model_copy(update={"name": tool_name})
            resolved_tools.append(tag_tool(tool_copy, "mcp", server_id))
            signatures.append(json.dumps(_tool_manifest(tool_copy), ensure_ascii=False, sort_keys=True, default=str))
        revision_parts = [f"{record.get('id', '')}:{record.get('revision', 1)}" for record in selected]
        unavailable_parts = [f"{item.get('id', '')}:unavailable" for item in unavailable]
        fingerprint = hashlib.sha256("\0".join(sorted(signatures)).encode("utf-8")).hexdigest()[:16]
        cache_key = "|".join((*revision_parts, *unavailable_parts, fingerprint)) or "none"
        return McpToolSelection(tools=tuple(resolved_tools), unavailable=tuple(unavailable), cache_key=cache_key)

    def default_ids(self, identity: Identity) -> tuple[str, ...]:
        """Return default-enabled MCP IDs for the workspace bootstrap payload."""
        return tuple(
            str(item.get("id", "")) for item in self.list_servers(identity) if item.get("defaultEnabled") is True
        )

    def _save(
        self,
        identity: Identity,
        server_id: str,
        body: Mapping[str, Any],
        *,
        current: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if current is not None:
            _check_revision(current, body, server_id)
        previous_payload = self._payload(current)
        payload = dict(previous_payload)
        for key in _PAYLOAD_KEYS:
            if key not in body:
                continue
            value = body.get(key)
            if value is None or value == "":
                payload.pop(key, None)
            else:
                payload[key] = value
        payload["transport"] = _transport(payload.get("transport"))
        payload["serverUrl"] = _server_url(payload.get("serverUrl"))
        payload["authType"] = _auth_type(payload.get("authType"))
        allowlist = _string_list(payload.get("toolAllowlist"))
        if allowlist:
            payload["toolAllowlist"] = list(allowlist)
        else:
            payload.pop("toolAllowlist", None)
        payload["timeoutMs"] = _timeout_ms(payload.get("timeoutMs"))

        credentials = _credentials(body)
        secret_ref = str(current.get("secret_ref", "") or "") if current is not None else ""
        if credentials:
            secret_ref = self._put_secret(identity, server_id, credentials, secret_ref or None)
        elif body.get("clearCredentials") is True and secret_ref:
            self._delete_secret(identity, secret_ref)
            secret_ref = ""

        discovery_changed = any(previous_payload.get(key) != payload.get(key) for key in _DISCOVERY_KEYS)
        credentials_changed = credentials is not None or body.get("clearCredentials") is True
        if discovery_changed or credentials_changed:
            payload.pop("toolManifest", None)
        previous_status = str(current.get("status", "untested")) if current is not None else "untested"
        status = "untested" if current is None or discovery_changed or credentials_changed else previous_status
        now = _now()
        revision = int(current.get("revision", 1) or 1) + 1 if current is not None else 1
        created_at = str(current.get("created_at", now)) if current is not None else now
        name = str(body.get("name", current.get("name", server_id) if current else server_id) or server_id).strip()
        description = str(body.get("description", current.get("description", "") if current else "") or "")
        default_enabled = _boolean(
            body.get("defaultEnabled"),
            bool(current.get("default_enabled", True)) if current is not None else True,
        )
        self._store.execute(
            """
            INSERT INTO config_resources (
                id, workspace_id, user_id, kind, name, description, payload_json, secret_ref,
                default_enabled, builtin, status, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, user_id, kind, id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                payload_json = excluded.payload_json,
                secret_ref = excluded.secret_ref,
                default_enabled = excluded.default_enabled,
                status = excluded.status,
                revision = excluded.revision,
                updated_at = excluded.updated_at
            """,
            (
                server_id,
                identity.workspace_id,
                identity.user_id,
                _MCP_KIND,
                name,
                description,
                json.dumps(payload, ensure_ascii=False),
                secret_ref or None,
                int(default_enabled),
                status,
                revision,
                created_at,
                now,
            ),
        )
        return self._require_record(identity, server_id)

    async def _discover_record(self, identity: Identity, record: Mapping[str, Any]) -> list[BaseTool]:
        return await self._discover_connection(record, self._connection(identity, record))

    async def _discover_connection(self, record: Mapping[str, Any], connection: Connection) -> list[BaseTool]:
        server_id = str(record.get("id", ""))
        client = MultiServerMCPClient({server_id: connection}, handle_tool_errors=False)
        timeout_seconds = _timeout_ms(self._payload(record).get("timeoutMs")) / 1000
        async with asyncio.timeout(timeout_seconds):
            tools = await client.get_tools(server_name=server_id)
        allowlist = set(_string_list(self._payload(record).get("toolAllowlist")))
        return [tool for tool in tools if not allowlist or tool.name in allowlist]

    def _connection(self, identity: Identity, record: Mapping[str, Any]) -> Connection:
        payload = self._payload(record)
        transport = _transport(payload.get("transport"))
        connection: dict[str, Any] = {
            "transport": "streamable_http" if transport == "streamable-http" else "sse",
            "url": _server_url(payload.get("serverUrl")),
            "timeout": _timeout_ms(payload.get("timeoutMs")) / 1000,
        }
        headers = self._headers(identity, record, payload)
        if headers:
            connection["headers"] = headers
        return cast("Connection", connection)

    def _headers(
        self,
        identity: Identity,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, str]:
        secret_ref = str(record.get("secret_ref", "") or "")
        secret = self._get_secret(identity, secret_ref) if secret_ref else {}
        headers = _string_mapping(secret.get("headers"))
        auth_type = _auth_type(payload.get("authType"))
        token = str(secret.get("token") or secret.get("bearerToken") or secret.get("apiKey") or "").strip()
        if auth_type == "bearer" and token:
            headers["Authorization"] = f"Bearer {token}"
        custom_header = secret.get("customHeader")
        if auth_type == "custom-header" and isinstance(custom_header, Mapping):
            name = str(custom_header.get("name", "") or "").strip()
            value = str(custom_header.get("value", "") or "").strip()
            if name and value:
                headers[name] = value
        return headers

    def _set_status(
        self,
        identity: Identity,
        record: Mapping[str, Any],
        status: str,
        *,
        manifest: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(record)
        if manifest is not None:
            payload["toolManifest"] = manifest
        revision = int(record.get("revision", 1) or 1) + 1
        self._store.execute(
            """
            UPDATE config_resources SET payload_json = ?, status = ?, revision = ?, updated_at = ?
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False),
                status,
                revision,
                _now(),
                identity.workspace_id,
                identity.user_id,
                _MCP_KIND,
                str(record.get("id", "")),
            ),
        )
        return self._require_record(identity, str(record.get("id", "")))

    def _find_record(self, identity: Identity, server_id: str) -> dict[str, Any] | None:
        row = self._store.fetchone(
            """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (identity.workspace_id, identity.user_id, _MCP_KIND, server_id),
        )
        return self._record(row) if row is not None else None

    def _require_record(self, identity: Identity, server_id: str) -> dict[str, Any]:
        record = self._find_record(identity, server_id)
        if record is None:
            raise ResourceError(404, "RESOURCE_NOT_FOUND", f"CONFIG_RESOURCE_NOT_FOUND:{server_id}")
        return record

    def _record(self, row: Any) -> dict[str, Any]:
        record = dict(row)
        try:
            payload = json.loads(str(record.get("payload_json", "{}")))
        except json.JSONDecodeError:
            payload = {}
        record["payload"] = payload if isinstance(payload, dict) else {}
        return record

    def _payload(self, record: Mapping[str, Any] | None) -> dict[str, Any]:
        if record is None:
            return {}
        payload = record.get("payload", {})
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _to_dto(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._payload(record)
        dto: dict[str, Any] = {
            "id": str(record.get("id", "")),
            "name": str(record.get("name", "")),
            "description": str(record.get("description", "") or ""),
            "secretRef": record.get("secret_ref"),
            "hasSecret": bool(record.get("secret_ref")),
            "defaultEnabled": bool(record.get("default_enabled", True)),
            "builtin": bool(record.get("builtin", False)),
            "healthStatus": str(record.get("status", "untested")),
            "revision": int(record.get("revision", 1) or 1),
            "createdAt": str(record.get("created_at", "")),
            "updatedAt": str(record.get("updated_at", "")),
        }
        dto.update(payload)
        return dto

    def _put_secret(
        self,
        identity: Identity,
        server_id: str,
        value: Mapping[str, Any],
        secret_ref: str | None,
    ) -> str:
        if self._secret_key is None:
            raise ResourceError(503, "SECRET_MASTER_KEY_REQUIRED", "SECRET_MASTER_KEY is required for MCP secrets.")
        ref = secret_ref or f"secret://mcp-server/{server_id}/{uuid4()}"
        if secret_ref:
            owner_row = self._store.fetchone(
                "SELECT workspace_id, user_id, owner_kind, owner_id FROM encrypted_secrets WHERE ref = ?",
                (ref,),
            )
            owner = dict(owner_row) if owner_row is not None else {}
            actual = (
                owner.get("workspace_id"),
                owner.get("user_id"),
                owner.get("owner_kind"),
                owner.get("owner_id"),
            )
            expected = (identity.workspace_id, identity.user_id, _MCP_KIND, server_id)
            if actual != expected:
                raise ResourceError(409, "SECRET_OWNER_MISMATCH", "Stored MCP credential owner mismatch.")
        nonce = os.urandom(12)
        encrypted = AESGCM(self._secret_key).encrypt(
            nonce,
            json.dumps(dict(value), ensure_ascii=False).encode("utf-8"),
            None,
        )
        ciphertext, auth_tag = encrypted[:-16], encrypted[-16:]
        now = _now()
        self._store.execute(
            """
            INSERT INTO encrypted_secrets (
                ref, workspace_id, user_id, owner_kind, owner_id, iv, auth_tag, ciphertext, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET
                iv = excluded.iv,
                auth_tag = excluded.auth_tag,
                ciphertext = excluded.ciphertext,
                updated_at = excluded.updated_at
            """,
            (
                ref,
                identity.workspace_id,
                identity.user_id,
                _MCP_KIND,
                server_id,
                base64.b64encode(nonce).decode("ascii"),
                base64.b64encode(auth_tag).decode("ascii"),
                base64.b64encode(ciphertext).decode("ascii"),
                now,
                now,
            ),
        )
        return ref

    def _get_secret(self, identity: Identity, secret_ref: str) -> dict[str, Any]:
        if self._secret_key is None:
            raise ResourceError(503, "SECRET_MASTER_KEY_REQUIRED", "SECRET_MASTER_KEY is not configured.")
        row = self._store.fetchone(
            """
            SELECT * FROM encrypted_secrets WHERE ref = ? AND workspace_id = ? AND user_id = ?
            """,
            (secret_ref, identity.workspace_id, identity.user_id),
        )
        if row is None:
            raise ResourceError(404, "SECRET_NOT_FOUND", f"Stored MCP credential not found for {secret_ref}.")
        record = dict(row)
        encrypted = base64.b64decode(str(record.get("ciphertext", ""))) + base64.b64decode(
            str(record.get("auth_tag", ""))
        )
        plaintext = AESGCM(self._secret_key).decrypt(
            base64.b64decode(str(record.get("iv", ""))),
            encrypted,
            None,
        )
        value = json.loads(plaintext.decode("utf-8"))
        if not isinstance(value, dict):
            raise ResourceError(500, "SECRET_PAYLOAD_INVALID", "Stored MCP credential is invalid.")
        return value

    def _delete_secret(self, identity: Identity, secret_ref: str) -> None:
        self._store.execute(
            "DELETE FROM encrypted_secrets WHERE ref = ? AND workspace_id = ? AND user_id = ?",
            (secret_ref, identity.workspace_id, identity.user_id),
        )


def _validate_id(value: Any, label: str) -> str:
    resource_id = str(value or "").strip()
    if not _RESOURCE_ID.fullmatch(resource_id):
        raise ResourceError(400, "BAD_REQUEST", f"{label} id may contain only letters, numbers, '.', '_' or '-'.")
    return resource_id


def _transport(value: Any) -> str:
    normalized = str(value or "streamable-http").strip().lower().replace("_", "-")
    if normalized in {"http", "streamablehttp"}:
        normalized = "streamable-http"
    if normalized not in {"sse", "streamable-http"}:
        raise ResourceError(400, "MCP_TRANSPORT_UNSUPPORTED", "Frontend MCP supports only SSE or Streamable HTTP.")
    return normalized


def _server_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResourceError(400, "MCP_SERVER_URL_INVALID", "MCP serverUrl must be an absolute HTTP(S) URL.")
    return url


def _auth_type(value: Any) -> str:
    normalized = str(value or "none").strip().lower()
    if normalized not in {"none", "bearer", "custom-header"}:
        raise ResourceError(400, "MCP_AUTH_TYPE_INVALID", "MCP authType is invalid.")
    return normalized


def _timeout_ms(value: Any) -> int:
    if value is None or value == "":
        return _DEFAULT_TIMEOUT_MS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResourceError(400, "BAD_REQUEST", "MCP timeoutMs must be a number.")
    return max(1_000, min(600_000, int(value)))


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ResourceError(400, "BAD_REQUEST", "MCP toolAllowlist must be a string list.")


def _credentials(body: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = body.get("credentials")
    credentials = dict(raw) if isinstance(raw, Mapping) else {}
    for key in ("token", "bearerToken", "apiKey", "headers", "customHeader"):
        if body.get(key) is not None and credentials.get(key) is None:
            credentials[key] = body.get(key)
    return credentials or None


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(entry) for key, entry in value.items() if str(key).strip() and entry is not None}


def _boolean(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _check_revision(current: Mapping[str, Any], body: Mapping[str, Any], resource_id: str) -> None:
    requested = body.get("revision")
    if requested is None:
        return
    if isinstance(requested, bool) or not isinstance(requested, (int, float)):
        raise ResourceError(400, "BAD_REQUEST", "MCP revision must be an integer.")
    if int(requested) != int(current.get("revision", 1) or 1):
        raise ResourceError(409, "REVISION_CONFLICT", f"REVISION_CONFLICT:{resource_id}")


def _tool_manifest(tool: BaseTool) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    try:
        schema = tool.get_input_schema().model_json_schema()
    except (AttributeError, TypeError, ValueError):
        schema = {}
    return {"name": tool.name, "description": tool.description or "", "inputSchema": schema}


def _safe_tool_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def _now() -> str:
    return datetime.now(UTC).isoformat()
