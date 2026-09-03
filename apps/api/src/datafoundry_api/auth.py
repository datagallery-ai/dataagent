from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request, Response

from datafoundry_api.errors import AuthError
from datafoundry_api.settings import Settings
from datafoundry_api.store import SqliteStore

SESSION_COOKIE = "df_session"
CSRF_COOKIE = "df_csrf"
WEB_SESSION_TTL = timedelta(days=30)
TUI_SESSION_TTL = timedelta(days=7)
EMAIL_TOKEN_TTL = timedelta(hours=24)
PASSWORD_HASHER = PasswordHasher()


@dataclass
class Identity:
    user_id: str
    email: str
    display_name: str | None
    workspace_id: str
    workspace_name: str
    session_id: str
    csrf_token_hash: str
    expires_at: str


class AuthService:
    def __init__(self, store: SqliteStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def public_status(self) -> dict[str, Any]:
        return {
            "publicBaseUrl": self.settings.public_base_url,
            "registrationEnabled": self.settings.registration_mode == "open",
        }

    def register(self, email: str, password: str, display_name: str | None) -> dict[str, Any]:
        if self.settings.registration_mode != "open":
            raise AuthError(403, "REGISTRATION_CLOSED", "Registration is closed for this deployment.")
        normalized_email = _normalize_email(email)
        _assert_password(password)
        if self.store.fetchone("SELECT id FROM users WHERE email = ?", (normalized_email,)):
            raise AuthError(409, "CONFLICT", "Email is already registered.")
        user_id = str(uuid4())
        workspace_id = f"personal-{user_id}"
        workspace_name = f"{display_name}'s workspace" if display_name else "Personal workspace"
        now = _now().isoformat()
        self.store.execute(
            "INSERT INTO users (id, email, display_name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, normalized_email, display_name, PASSWORD_HASHER.hash(password), now),
        )
        self.store.execute(
            "INSERT INTO workspaces (id, user_id, name) VALUES (?, ?, ?)",
            (workspace_id, user_id, workspace_name),
        )
        verification_token = secrets.token_urlsafe(32)
        self.store.execute(
            "INSERT INTO auth_tokens (id, user_id, purpose, token_hash, expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                user_id,
                "email_verification",
                self._hash_token(verification_token),
                (_now() + EMAIL_TOKEN_TTL).isoformat(),
            ),
        )
        payload = {
            "user": _user_dto(user_id, normalized_email, display_name),
            "workspace": {"id": workspace_id, "name": workspace_name},
        }
        if self.settings.email_delivery == "test":
            payload["verificationToken"] = verification_token
        return payload

    def verify_email(self, token: str) -> dict[str, Any]:
        row = self._require_token("email_verification", token)
        now = _now().isoformat()
        self.store.execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (now, row["user_id"]))
        self.store.execute("UPDATE auth_tokens SET consumed_at = ? WHERE id = ?", (now, row["id"]))
        user = self.store.fetchone("SELECT * FROM users WHERE id = ?", (row["user_id"],))
        if user is None:
            raise AuthError(401, "UNAUTHORIZED", "Authentication required.")
        return {"user": _user_dto(user["id"], user["email"], user["display_name"])}

    def login(self, email: str, password: str, client: str | None) -> tuple[dict[str, Any], str, str, int]:
        normalized_email = _normalize_email(email)
        user = self.store.fetchone("SELECT * FROM users WHERE email = ?", (normalized_email,))
        if user is None or not _verify_password(user["password_hash"], password):
            raise AuthError(401, "UNAUTHORIZED", "Invalid email or password.")
        if not user["email_verified_at"]:
            raise AuthError(403, "EMAIL_NOT_VERIFIED", "Email verification is required before login.")
        ttl = TUI_SESSION_TTL if client == "tui" else WEB_SESSION_TTL
        expires_at = (_now() + ttl).isoformat()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        workspace = self.store.fetchone("SELECT * FROM workspaces WHERE user_id = ?", (user["id"],))
        if workspace is None:
            raise AuthError(500, "INTERNAL_ERROR", "Workspace is missing.")
        self.store.execute(
            """
            INSERT INTO auth_sessions (id, user_id, token_hash, csrf_token_hash, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid4()), user["id"], self._hash_token(session_token), self._hash_token(csrf_token), expires_at),
        )
        return (
            {
                "user": _user_dto(user["id"], user["email"], user["display_name"]),
                "workspace": {"id": workspace["id"], "name": workspace["name"]},
                "session": {"expiresAt": expires_at},
            },
            session_token,
            csrf_token,
            int(ttl.total_seconds()),
        )

    def authenticate(self, session_token: str | None) -> Identity:
        if not session_token:
            raise AuthError(401, "UNAUTHORIZED", "Authentication required.")
        session = self.store.fetchone(
            """
            SELECT s.*, u.email, u.display_name, w.id AS workspace_id, w.name AS workspace_name
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            JOIN workspaces w ON w.user_id = u.id
            WHERE s.token_hash = ? AND s.revoked_at IS NULL
            """,
            (self._hash_token(session_token),),
        )
        if session is None or datetime.fromisoformat(session["expires_at"]) <= _now():
            raise AuthError(401, "UNAUTHORIZED", "Authentication required.")
        return Identity(
            user_id=session["user_id"],
            email=session["email"],
            display_name=session["display_name"],
            workspace_id=session["workspace_id"],
            workspace_name=session["workspace_name"],
            session_id=session["id"],
            csrf_token_hash=session["csrf_token_hash"],
            expires_at=session["expires_at"],
        )

    def validate_csrf(self, identity: Identity, csrf_token: str | None) -> None:
        if not csrf_token:
            raise AuthError(403, "CSRF_INVALID", "CSRF token is required.")
        if self._hash_token(csrf_token) != identity.csrf_token_hash:
            raise AuthError(403, "CSRF_INVALID", "CSRF token is invalid.")

    def rotate_csrf(self, identity: Identity) -> tuple[str, int]:
        csrf_token = secrets.token_urlsafe(32)
        self.store.execute(
            "UPDATE auth_sessions SET csrf_token_hash = ? WHERE id = ?",
            (self._hash_token(csrf_token), identity.session_id),
        )
        remaining = max(0, int((datetime.fromisoformat(identity.expires_at) - _now()).total_seconds()))
        return csrf_token, remaining

    def logout(self, identity: Identity) -> dict[str, bool]:
        self.store.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE id = ?",
            (_now().isoformat(), identity.session_id),
        )
        return {"ok": True}

    def me(self, identity: Identity) -> dict[str, Any]:
        return {
            "user": _user_dto(identity.user_id, identity.email, identity.display_name),
            "workspace": {"id": identity.workspace_id, "name": identity.workspace_name},
        }

    def _hash_token(self, token: str) -> str:
        return hmac.new(self.settings.session_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _require_token(self, purpose: str, token: str) -> Any:
        row = self.store.fetchone(
            """
            SELECT * FROM auth_tokens
            WHERE purpose = ? AND token_hash = ? AND consumed_at IS NULL
            """,
            (purpose, self._hash_token(token)),
        )
        if row is None or datetime.fromisoformat(row["expires_at"]) <= _now():
            raise AuthError(400, "BAD_REQUEST", "Token is invalid or expired.")
        return row


def identity_from_request(request: Request, auth: AuthService) -> Identity:
    return auth.authenticate(request.cookies.get(SESSION_COOKIE))


def attach_auth_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    max_age: int,
    settings: Settings,
) -> None:
    _set_cookie(response, SESSION_COOKIE, session_token, http_only=True, max_age=max_age, settings=settings)
    _set_cookie(response, CSRF_COOKIE, csrf_token, http_only=False, max_age=max_age, settings=settings)


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    _set_cookie(response, SESSION_COOKIE, "", http_only=True, max_age=0, settings=settings)
    _set_cookie(response, CSRF_COOKIE, "", http_only=False, max_age=0, settings=settings)


def _set_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    http_only: bool,
    max_age: int,
    settings: Settings,
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=settings.cookie_path,
        secure=settings.cookie_secure,
        httponly=http_only,
        samesite="lax",
    )


def _user_dto(user_id: str, email: str, display_name: str | None) -> dict[str, str]:
    payload = {"id": user_id, "email": email}
    if display_name:
        payload["displayName"] = display_name
    return payload


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise AuthError(400, "BAD_REQUEST", "A valid email address is required.")
    return normalized


def _assert_password(password: str) -> None:
    if len(password) < 6:
        raise AuthError(400, "BAD_REQUEST", "Password must be at least 6 characters.")


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _now() -> datetime:
    return datetime.now(UTC)
