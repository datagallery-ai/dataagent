from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from datafoundry_api.app import create_app
from datafoundry_api.local_tui import seed_local_session
from datafoundry_api.settings import Settings


def test_local_identity_is_persistent_and_requires_session_and_csrf(auth_env):
    settings = replace(Settings.from_env(auth_env), registration_mode="closed")
    token, csrf = "s" * 64, "c" * 64
    seed_local_session(settings, token, csrf)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/me").status_code == 401
        assert client.get("/api/v1/me", headers={"cookie": "df_session=wrong"}).status_code == 401
        headers = {"cookie": f"df_session={token}"}
        me = client.get("/api/v1/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["data"]["user"]["id"] == "local-tui"
        assert client.get("/api/v1/workspace-config", headers=headers).status_code == 200
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 403
        assert client.post("/api/v1/auth/login", json={"email": "local@localhost", "password": "password"}).status_code == 401
        assert client.post("/api/v1/auth/register", json={"email": "x@y.com", "password": "password"}).status_code == 403
        assert client.post("/api/v1/auth/logout", headers={**headers, "x-csrf-token": csrf}).status_code == 200
        assert client.get("/api/v1/me", headers=headers).status_code == 401
    seed_local_session(settings, "n" * 64, "d" * 64)
    with TestClient(create_app(settings)) as client:
        me = client.get("/api/v1/me", headers={"cookie": f'df_session={"n" * 64}'})
        assert me.json()["data"]["workspace"]["id"] == "local-tui-workspace"


def test_local_bootstrap_rejects_unsafe_settings(auth_env):
    settings = replace(Settings.from_env(auth_env), registration_mode="closed")
    for unsafe in [replace(settings, host="0.0.0.0"), replace(settings, auth_disabled=True),
                   replace(settings, registration_mode="open")]:
        with pytest.raises(ValueError):
            seed_local_session(unsafe, "s" * 64, "c" * 64)
    with pytest.raises(ValueError):
        seed_local_session(settings, "short", "short")
