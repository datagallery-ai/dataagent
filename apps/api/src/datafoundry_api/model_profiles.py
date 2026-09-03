"""Workspace-scoped model profiles compatible with the existing frontend API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dataagent.core.deepagents.config.models import ModelConfigCompiler
from langchain_core.messages import HumanMessage

from datafoundry_api.auth import Identity
from datafoundry_api.settings import Settings
from datafoundry_api.store import SqliteStore

_MODEL_KIND = "model-profile"
_SERVER_DEFAULT_ID = "server-default"
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PAYLOAD_KEYS = (
    "provider",
    "modelName",
    "baseUrl",
    "fallbackProfileId",
    "frequencyPenalty",
    "maxTokens",
    "presencePenalty",
    "contextLength",
    "reasoningModel",
    "temperature",
    "topP",
    "timeoutMs",
)
_CONNECTIVITY_KEYS = {"provider", "modelName", "baseUrl", "fallbackProfileId"}


class ModelProfileError(Exception):
    """Stable HTTP-facing error raised by model-profile operations."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RuntimeModelSelection:
    """Resolved MODEL slots and a cache identity for one frontend selection."""

    cache_key: str
    model_slots: Mapping[str, Mapping[str, Any]] | None
    primary_model_name: str = "chat_model"
    run_timeout_ms: int | None = None


class ModelProfileService:
    """Persist, test, and compile frontend model profiles for DataAgent."""

    def __init__(self, store: SqliteStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        master_key = settings.secret_master_key
        self._secret_key = hashlib.sha256(master_key.encode("utf-8")).digest() if master_key else None

    def list_profiles(self, identity: Identity) -> list[dict[str, Any]]:
        """Return public model profiles for one user and workspace."""
        self._ensure_server_default(identity)
        rows = self._store.fetchall(
            """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ?
            ORDER BY builtin DESC, updated_at DESC
            """,
            (identity.workspace_id, identity.user_id, _MODEL_KIND),
        )
        return [self._to_dto(self._record_from_row(row)) for row in rows]

    def get_profile(self, identity: Identity, profile_id: str) -> dict[str, Any]:
        """Return one public model profile."""
        self._ensure_server_default(identity)
        return self._to_dto(self._require_record(identity, profile_id))

    def create_profile(self, identity: Identity, body: Mapping[str, Any]) -> dict[str, Any]:
        """Create a model profile using the existing frontend request shape."""
        profile_id = self._validate_profile_id(body.get("id"))
        if profile_id == _SERVER_DEFAULT_ID:
            raise ModelProfileError(409, "CONFLICT", "server-default is reserved for server environment variables.")
        if self._find_record(identity, profile_id) is not None:
            raise ModelProfileError(409, "CONFLICT", f'Model profile "{profile_id}" already exists.')
        return self._to_dto(self._save_profile(identity, profile_id, body, current=None))

    def patch_profile(self, identity: Identity, profile_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Patch a model profile with optimistic revision checking."""
        current = self._require_record(identity, profile_id)
        if bool(current.get("builtin", False)):
            raise ModelProfileError(409, "BUILTIN_RESOURCE_READONLY", "The server-default profile is read-only.")
        return self._to_dto(self._save_profile(identity, profile_id, body, current=current))

    def delete_profile(self, identity: Identity, profile_id: str) -> dict[str, Any]:
        """Delete a model profile and its encrypted credential."""
        current = self._require_record(identity, profile_id)
        if bool(current.get("builtin", False)):
            raise ModelProfileError(409, "BUILTIN_RESOURCE_READONLY", "The server-default profile is read-only.")
        secret_ref = self._optional_string(current.get("secret_ref"))
        if secret_ref:
            self._delete_secret(identity, secret_ref)
        self._store.execute(
            "DELETE FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?",
            (identity.workspace_id, identity.user_id, _MODEL_KIND, profile_id),
        )
        return {"deleted": True, "id": profile_id}

    async def test_profile(self, identity: Identity, profile_id: str) -> dict[str, Any]:
        """Call the configured provider and persist its connection status."""
        self._ensure_server_default(identity)
        record = self._require_record(identity, profile_id)
        model_slot = self._model_slot(identity, record)
        timeout_ms = self._timeout_ms(self._payload(record).get("timeoutMs")) or 30_000
        started_at = asyncio.get_running_loop().time()
        try:
            models = ModelConfigCompiler({"MODEL": {"chat_model": model_slot}}).compile()
            model = models.get("chat_model")
            if model is None:
                raise ValueError("The model profile did not compile a chat model.")
            async with asyncio.timeout(max(0.001, timeout_ms / 1000)):
                response = await model.ainvoke([HumanMessage(content="Reply with exactly OK.")])
        except Exception as exc:
            self._set_status(identity, record, "failed")
            raise ModelProfileError(502, "PROVIDER_TEST_FAILED", f"Model provider probe failed: {exc}") from exc

        updated = self._set_status(identity, record, "connected")
        content = getattr(response, "content", "")
        response_text = content if isinstance(content, str) else str(content)
        elapsed_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
        payload = self._payload(updated)
        return {
            "id": profile_id,
            "latencyMs": elapsed_ms,
            "model": str(payload.get("modelName", "")),
            "response": response_text[:500],
            "reason": f'Model "{payload.get("modelName", "")}" responded successfully.',
            "status": "connected",
            "revision": updated.get("revision", 1),
        }

    def default_profile_id(self, identity: Identity) -> str | None:
        """Choose the first connected default profile, then the first enabled profile."""
        profiles = [profile for profile in self.list_profiles(identity) if profile.get("defaultEnabled") is True]
        connected = next(
            (profile for profile in profiles if profile.get("connectionStatus") == "connected"),
            None,
        )
        selected = connected or (profiles[0] if profiles else None)
        return self._optional_string(selected.get("id")) if selected else None

    def resolve_model_selection(
        self,
        identity: Identity,
        profile_id: str | None,
    ) -> RuntimeModelSelection:
        """Resolve a selected profile and fallback chain into DataAgent MODEL slots."""
        resolved_id = profile_id or self.default_profile_id(identity)
        if not resolved_id:
            raise ModelProfileError(503, "PROVIDER_CONFIG_MISSING", "No model profile is configured.")
        if resolved_id == _SERVER_DEFAULT_ID:
            self._ensure_server_default(identity)
            self._require_record(identity, resolved_id)
            return RuntimeModelSelection(cache_key=f"server-default:{self._server_env_fingerprint()}", model_slots=None)

        slots: dict[str, Mapping[str, Any]] = {}
        cache_parts: list[str] = []
        visited: set[str] = set()
        current_id: str | None = resolved_id
        run_timeout_ms: int | None = None
        while current_id:
            if current_id in visited:
                raise ModelProfileError(400, "MODEL_FALLBACK_CYCLE", f"Model fallback cycle at {current_id}.")
            visited.add(current_id)
            slot_name = "chat_model" if not slots else f"fallback_{len(slots)}"
            if current_id == _SERVER_DEFAULT_ID:
                self._ensure_server_default(identity)
                self._require_record(identity, current_id)
                slots[slot_name] = self._server_model_slot()
                cache_parts.append(f"server-default:{self._server_env_fingerprint()}")
                current_id = None
                continue
            record = self._require_record(identity, current_id)
            if not slots:
                run_timeout_ms = self._timeout_ms(self._payload(record).get("timeoutMs"))
            slots[slot_name] = self._model_slot(identity, record)
            cache_parts.append(f"{current_id}:{record.get('revision', 1)}")
            current_id = self._optional_string(self._payload(record).get("fallbackProfileId"))
        return RuntimeModelSelection(
            cache_key="|".join(cache_parts),
            model_slots=slots,
            run_timeout_ms=run_timeout_ms,
        )

    def _save_profile(
        self,
        identity: Identity,
        profile_id: str,
        body: Mapping[str, Any],
        *,
        current: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if current is not None:
            expected_revision = self._integer(body.get("revision"))
            current_revision = self._integer(current.get("revision")) or 1
            if expected_revision is not None and expected_revision != current_revision:
                raise ModelProfileError(409, "REVISION_CONFLICT", f"REVISION_CONFLICT:{profile_id}")

        previous_payload = self._payload(current) if current is not None else {}
        payload = dict(previous_payload)
        for key in _PAYLOAD_KEYS:
            if key not in body:
                continue
            value = body.get(key)
            if value is None or value == "":
                payload.pop(key, None)
            else:
                payload[key] = value
        self._validate_fallback(identity, profile_id, payload)

        credentials = self._credentials(body)
        secret_ref = self._optional_string(current.get("secret_ref")) if current is not None else None
        if credentials:
            secret_ref = self._put_secret(identity, profile_id, credentials, secret_ref)
        elif body.get("clearCredentials") is True and secret_ref:
            self._delete_secret(identity, secret_ref)
            secret_ref = None

        connectivity_changed = any(previous_payload.get(key) != payload.get(key) for key in _CONNECTIVITY_KEYS)
        credentials_changed = bool(credentials) or body.get("clearCredentials") is True
        previous_status = str(current.get("status", "untested")) if current is not None else "untested"
        status = "untested" if current is None or connectivity_changed or credentials_changed else previous_status
        now = datetime.now(UTC).isoformat()
        created_at = str(current.get("created_at", now)) if current is not None else now
        revision = (self._integer(current.get("revision")) or 0) + 1 if current is not None else 1
        name = self._optional_string(body.get("name"))
        if name is None and current is not None:
            name = self._optional_string(current.get("name"))
        description = body.get("description", current.get("description") if current is not None else "")
        default_enabled = self._boolean(
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
                profile_id,
                identity.workspace_id,
                identity.user_id,
                _MODEL_KIND,
                name or profile_id,
                str(description or ""),
                json.dumps(payload, ensure_ascii=False),
                secret_ref,
                int(default_enabled),
                status,
                revision,
                created_at,
                now,
            ),
        )
        return self._require_record(identity, profile_id)

    def _validate_fallback(self, identity: Identity, profile_id: str, payload: Mapping[str, Any]) -> None:
        visited = {profile_id}
        fallback_id = self._optional_string(payload.get("fallbackProfileId"))
        while fallback_id:
            if fallback_id in visited:
                raise ModelProfileError(400, "MODEL_FALLBACK_CYCLE", f"Model fallback cycle at {fallback_id}.")
            visited.add(fallback_id)
            if fallback_id == _SERVER_DEFAULT_ID:
                self._ensure_server_default(identity)
            fallback = self._require_record(identity, fallback_id)
            fallback_id = self._optional_string(self._payload(fallback).get("fallbackProfileId"))

    def _ensure_server_default(self, identity: Identity) -> None:
        current = self._find_record(identity, _SERVER_DEFAULT_ID)
        if not self._server_env_configured():
            if current is not None and bool(current.get("builtin", False)):
                self._store.execute(
                    "DELETE FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?",
                    (identity.workspace_id, identity.user_id, _MODEL_KIND, _SERVER_DEFAULT_ID),
                )
            return

        slot = self._server_model_slot()
        params = slot.get("params", {})
        expected_payload = {
            "provider": slot.get("provider", "openai-compatible"),
            "modelName": params.get("model", "") if isinstance(params, Mapping) else "",
            "baseUrl": params.get("base_url", "") if isinstance(params, Mapping) else "",
        }
        now = datetime.now(UTC).isoformat()
        if current is None:
            self._store.execute(
                """
                INSERT INTO config_resources (
                    id, workspace_id, user_id, kind, name, description, payload_json, secret_ref,
                    default_enabled, builtin, status, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, 1, 'untested', 1, ?, ?)
                """,
                (
                    _SERVER_DEFAULT_ID,
                    identity.workspace_id,
                    identity.user_id,
                    _MODEL_KIND,
                    "default",
                    "Uses the server LLM environment variables.",
                    json.dumps(expected_payload),
                    now,
                    now,
                ),
            )
            return

        payload = self._payload(current)
        changed = any(payload.get(key) != expected_payload.get(key) for key in ("provider", "modelName", "baseUrl"))
        fingerprint_changed = payload.get("llmEnvFingerprint") != self._server_env_fingerprint()
        if not changed and not (current.get("status") == "connected" and fingerprint_changed):
            return
        if not fingerprint_changed and payload.get("llmEnvFingerprint"):
            expected_payload["llmEnvFingerprint"] = payload.get("llmEnvFingerprint")
        revision = (self._integer(current.get("revision")) or 1) + 1
        self._store.execute(
            """
            UPDATE config_resources
            SET payload_json = ?, status = 'untested', revision = ?, updated_at = ?
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (
                json.dumps(expected_payload),
                revision,
                now,
                identity.workspace_id,
                identity.user_id,
                _MODEL_KIND,
                _SERVER_DEFAULT_ID,
            ),
        )

    def _model_slot(self, identity: Identity, record: Mapping[str, Any]) -> Mapping[str, Any]:
        if record.get("id") == _SERVER_DEFAULT_ID:
            return self._server_model_slot()
        payload = self._payload(record)
        model_name = self._optional_string(payload.get("modelName"))
        if not model_name:
            raise ModelProfileError(503, "PROVIDER_CONFIG_MISSING", f"Model name is missing for {record.get('id')}.")
        secret_ref = self._optional_string(record.get("secret_ref"))
        credentials = self._get_secret(identity, secret_ref) if secret_ref else {}
        api_key = self._optional_string(credentials.get("apiKey")) or self._optional_string(credentials.get("api_key"))
        if not api_key:
            raise ModelProfileError(503, "PROVIDER_CONFIG_MISSING", f"API key is missing for {record.get('id')}.")
        params: dict[str, Any] = {"model": model_name, "api_key": api_key}
        base_url = self._optional_string(payload.get("baseUrl"))
        if base_url:
            params["base_url"] = base_url
        self._copy_model_parameters(payload, params)
        return {
            "provider": self._optional_string(payload.get("provider")) or "openai-compatible",
            "model_type": "chat",
            "params": params,
        }

    def _server_model_slot(self) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "model": os.getenv("LLM_MODEL", "").strip(),
            "api_key": os.getenv("LLM_API_KEY", "").strip(),
        }
        base_url = os.getenv("LLM_BASE_URL", "").strip()
        if base_url:
            params["base_url"] = base_url
        return {
            "provider": os.getenv("LLM_PROVIDER", "openai-compatible").strip() or "openai-compatible",
            "model_type": "chat",
            "params": params,
        }

    def _copy_model_parameters(self, payload: Mapping[str, Any], params: dict[str, Any]) -> None:
        aliases = {
            "temperature": "temperature",
            "topP": "top_p",
            "frequencyPenalty": "frequency_penalty",
            "presencePenalty": "presence_penalty",
            "maxTokens": "max_tokens",
        }
        for source, target in aliases.items():
            value = self._number(payload.get(source))
            if value is not None:
                params[target] = int(value) if source == "maxTokens" else value

    def _set_status(
        self,
        identity: Identity,
        record: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any]:
        payload = self._payload(record)
        if record.get("id") == _SERVER_DEFAULT_ID and status == "connected":
            payload["llmEnvFingerprint"] = self._server_env_fingerprint()
        revision = (self._integer(record.get("revision")) or 1) + 1
        self._store.execute(
            """
            UPDATE config_resources SET payload_json = ?, status = ?, revision = ?, updated_at = ?
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (
                json.dumps(payload),
                status,
                revision,
                datetime.now(UTC).isoformat(),
                identity.workspace_id,
                identity.user_id,
                _MODEL_KIND,
                str(record.get("id", "")),
            ),
        )
        return self._require_record(identity, str(record.get("id", "")))

    def _find_record(self, identity: Identity, profile_id: str) -> dict[str, Any] | None:
        row = self._store.fetchone(
            """
            SELECT * FROM config_resources
            WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
            """,
            (identity.workspace_id, identity.user_id, _MODEL_KIND, profile_id),
        )
        return self._record_from_row(row) if row is not None else None

    def _require_record(self, identity: Identity, profile_id: str) -> dict[str, Any]:
        record = self._find_record(identity, profile_id)
        if record is None:
            raise ModelProfileError(404, "RESOURCE_NOT_FOUND", f"CONFIG_RESOURCE_NOT_FOUND:{profile_id}")
        return record

    def _record_from_row(self, row: Any) -> dict[str, Any]:
        record = dict(row)
        raw_payload = record.get("payload_json", "{}")
        try:
            payload = json.loads(str(raw_payload))
        except json.JSONDecodeError:
            payload = {}
        record["payload"] = payload if isinstance(payload, dict) else {}
        return record

    def _to_dto(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._payload(record)
        payload.pop("llmEnvFingerprint", None)
        dto: dict[str, Any] = {
            "id": str(record.get("id", "")),
            "name": str(record.get("name", "")),
            "description": str(record.get("description", "") or ""),
            "secretRef": record.get("secret_ref"),
            "hasSecret": bool(record.get("secret_ref")),
            "defaultEnabled": bool(record.get("default_enabled", True)),
            "builtin": bool(record.get("builtin", False)),
            "connectionStatus": str(record.get("status", "untested")),
            "revision": self._integer(record.get("revision")) or 1,
            "createdAt": str(record.get("created_at", "")),
            "updatedAt": str(record.get("updated_at", "")),
        }
        dto.update(payload)
        if record.get("id") == _SERVER_DEFAULT_ID and os.getenv("LLM_API_KEY", "").strip():
            dto.update({"secretRef": "env://LLM_API_KEY", "hasSecret": True})
        return dto

    def _payload(self, record: Mapping[str, Any] | None) -> dict[str, Any]:
        if record is None:
            return {}
        payload = record.get("payload", {})
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _put_secret(
        self,
        identity: Identity,
        profile_id: str,
        value: Mapping[str, Any],
        secret_ref: str | None,
    ) -> str:
        if self._secret_key is None:
            raise ModelProfileError(
                503,
                "SECRET_MASTER_KEY_REQUIRED",
                "SECRET_MASTER_KEY is required to save model API keys.",
            )
        ref = secret_ref or f"secret://model-profile/{profile_id}/{uuid4()}"
        if secret_ref:
            owner_row = self._store.fetchone(
                "SELECT workspace_id, user_id, owner_kind, owner_id FROM encrypted_secrets WHERE ref = ?",
                (ref,),
            )
            owner = dict(owner_row) if owner_row is not None else {}
            expected = (identity.workspace_id, identity.user_id, _MODEL_KIND, profile_id)
            actual = (
                owner.get("workspace_id"),
                owner.get("user_id"),
                owner.get("owner_kind"),
                owner.get("owner_id"),
            )
            if actual != expected:
                raise ModelProfileError(409, "SECRET_OWNER_MISMATCH", "Stored model credential owner mismatch.")
        nonce = os.urandom(12)
        encrypted = AESGCM(self._secret_key).encrypt(
            nonce,
            json.dumps(dict(value), ensure_ascii=False).encode("utf-8"),
            None,
        )
        ciphertext, auth_tag = encrypted[:-16], encrypted[-16:]
        now = datetime.now(UTC).isoformat()
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
                _MODEL_KIND,
                profile_id,
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
            raise ModelProfileError(503, "SECRET_MASTER_KEY_REQUIRED", "SECRET_MASTER_KEY is not configured.")
        row = self._store.fetchone(
            """
            SELECT * FROM encrypted_secrets
            WHERE ref = ? AND workspace_id = ? AND user_id = ?
            """,
            (secret_ref, identity.workspace_id, identity.user_id),
        )
        if row is None:
            raise ModelProfileError(404, "SECRET_NOT_FOUND", f"Stored credential not found for {secret_ref}.")
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
            raise ModelProfileError(500, "SECRET_PAYLOAD_INVALID", "Stored model credential is invalid.")
        return value

    def _delete_secret(self, identity: Identity, secret_ref: str) -> None:
        self._store.execute(
            "DELETE FROM encrypted_secrets WHERE ref = ? AND workspace_id = ? AND user_id = ?",
            (secret_ref, identity.workspace_id, identity.user_id),
        )

    def _credentials(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        raw = body.get("credentials")
        credentials = dict(raw) if isinstance(raw, Mapping) else {}
        inline_key = body.get("apiKey") or body.get("api_key")
        if inline_key and not credentials.get("apiKey") and not credentials.get("api_key"):
            credentials["apiKey"] = inline_key
        return credentials or None

    def _validate_profile_id(self, value: Any) -> str:
        profile_id = str(value or "").strip()
        if not _PROFILE_ID.fullmatch(profile_id):
            raise ModelProfileError(
                400,
                "BAD_REQUEST",
                "Model profile id must contain only letters, numbers, '.', '_' or '-'.",
            )
        return profile_id

    def _optional_string(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return value.strip() or None

    def _boolean(self, value: Any, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def _number(self, value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _integer(self, value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    def _timeout_ms(self, value: Any) -> int | None:
        timeout_ms = self._integer(value)
        if timeout_ms is None:
            return None
        return max(1_000, min(600_000, timeout_ms))

    def _server_env_configured(self) -> bool:
        return all(os.getenv(key, "").strip() for key in ("LLM_API_KEY", "LLM_MODEL"))

    def _server_env_fingerprint(self) -> str:
        material = "\0".join(
            (
                os.getenv("LLM_PROVIDER", ""),
                os.getenv("LLM_BASE_URL", ""),
                os.getenv("LLM_MODEL", ""),
                "key:set" if os.getenv("LLM_API_KEY", "").strip() else "key:missing",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
