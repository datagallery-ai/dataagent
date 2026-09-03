from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class CsrfTestClient(TestClient):
    def request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        if str(method).upper() in UNSAFE_METHODS:
            headers = dict(kwargs.get("headers") or {})
            has_csrf = any(key.lower() == "x-csrf-token" for key in headers)
            token = self.cookies.get("df_csrf")
            if token and not has_csrf:
                headers["X-CSRF-Token"] = token
                kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def auth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "API_HOST": "127.0.0.1",
        "API_PORT": "8787",
        "AUTH_SESSION_SECRET": "pytest-session-secret-value-32b!!",
        "AUTH_PUBLIC_BASE_URL": "http://127.0.0.1:3000",
        "AUTH_REGISTRATION_MODE": "open",
        "AUTH_EMAIL_DELIVERY": "test",
        "SECRET_MASTER_KEY": "pytest-model-secret-master-key",
        "LLM_PROVIDER": "openai-compatible",
        "LLM_MODEL": "test-model",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "test-api-key",
        "METADATA_DB_PATH": str(tmp_path / "workbench.sqlite"),
        "STORAGE_ROOT_DIR": str(tmp_path / "storage"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("RUNTIME_SERVICE_URL", raising=False)
    monkeypatch.delenv("RUNTIME_SERVICE_TOKEN", raising=False)
    return env


@pytest.fixture
def client(auth_env: dict[str, str]) -> Iterator[TestClient]:
    from datafoundry_api.app import create_app
    from datafoundry_api.settings import Settings

    app = create_app(Settings.from_env(os.environ))
    with CsrfTestClient(app) as test_client:
        yield test_client


def register_and_login(
    client: TestClient,
    *,
    email: str = "user@example.test",
    password: str = "secret-password",
    display_name: str = "Test User",
    login_client: str | None = None,
) -> dict[str, str]:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "displayName": display_name},
    )
    assert registered.status_code == 201, registered.text
    token = registered.json()["data"]["verificationToken"]
    verified = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    body: dict[str, object] = {"email": email, "password": password}
    if login_client:
        body["client"] = login_client
    logged_in = client.post("/api/v1/auth/login", json=body)
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()["data"]
