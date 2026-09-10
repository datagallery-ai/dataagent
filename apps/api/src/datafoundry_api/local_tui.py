"""Private, launcher-owned local API; uses ordinary sessions, never anonymous auth."""
from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import uvicorn

from datafoundry_api.auth import PASSWORD_HASHER, AuthService
from datafoundry_api.settings import Settings
from datafoundry_api.store import SqliteStore


def seed_local_session(settings: Settings, token: str, csrf: str) -> None:
    if settings.host != "127.0.0.1" or settings.auth_disabled or settings.registration_mode != "closed":
        raise ValueError("Local TUI requires loopback, authenticated access and closed registration.")
    if len(token) < 32 or len(csrf) < 32:
        raise ValueError("Local TUI requires launcher-generated credentials.")
    store = SqliteStore(settings.metadata_db_path)
    try:
        now = datetime.now(UTC)
        if not store.fetchone("SELECT id FROM users WHERE id = ?", ("local-tui",)):
            store.execute(
                "INSERT INTO users (id,email,display_name,password_hash,email_verified_at,created_at) VALUES (?,?,?,?,?,?)",
                ("local-tui", "local@localhost", "Local User", PASSWORD_HASHER.hash(secrets.token_urlsafe(32)),
                 now.isoformat(), now.isoformat()),
            )
        store.execute("INSERT OR IGNORE INTO workspaces (id,user_id,name) VALUES (?,?,?)",
                      ("local-tui-workspace", "local-tui", "Local workspace"))
        auth = AuthService(store, settings)
        store.execute(
            "INSERT INTO auth_sessions (id,user_id,token_hash,csrf_token_hash,expires_at) VALUES (?,?,?,?,?)",
            (str(uuid4()), "local-tui", auth._hash_token(token), auth._hash_token(csrf),
             (now + timedelta(days=7)).isoformat()),
        )
    finally:
        store.close()


def main() -> None:
    # Do not expose bootstrap credentials to subsequently spawned tools.
    token = os.environ.pop("TUI_LOCAL_SESSION_TOKEN")
    csrf = os.environ.pop("TUI_LOCAL_CSRF_TOKEN")
    ready_file = Path(os.environ.pop("TUI_LOCAL_READY_FILE"))
    settings = Settings.from_env()
    seed_local_session(settings, token, csrf)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        ready_file.write_text(json.dumps({"port": sock.getsockname()[1]}))
        server = uvicorn.Server(uvicorn.Config(
            "datafoundry_api.app:create_app", factory=True, log_level="warning", access_log=False,
        ))
        server.run(sockets=[sock])


if __name__ == "__main__":
    main()
