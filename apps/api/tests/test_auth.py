from __future__ import annotations

from conftest import register_and_login


def test_auth_status_is_public(client) -> None:
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {
            "publicBaseUrl": "http://127.0.0.1:3000",
            "registrationEnabled": True,
        },
    }


def test_register_login_and_me_roundtrip(client) -> None:
    register_and_login(client)
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    data = me.json()["data"]
    assert data["user"]["email"] == "user@example.test"
    assert data["user"]["displayName"] == "Test User"
    assert data["workspace"]["id"]


def test_login_sets_session_and_csrf_cookies(client) -> None:
    register_and_login(client)
    assert client.cookies.get("df_session")
    assert client.cookies.get("df_csrf")


def test_login_requires_email_verification(client) -> None:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": "pending@example.test", "password": "secret-password", "displayName": "Pending"},
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "pending@example.test", "password": "secret-password"},
    )
    assert login.status_code == 403
    assert login.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_me_requires_session(client) -> None:
    response = client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_logout_requires_csrf(client) -> None:
    register_and_login(client)
    response = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_logout_clears_cookies(client) -> None:
    register_and_login(client)
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert response.json()["data"] == {"ok": True}
    me = client.get("/api/v1/me")
    assert me.status_code == 401


def test_csrf_refresh_rotates_token(client) -> None:
    register_and_login(client)
    previous = client.cookies.get("df_csrf")
    response = client.post("/api/v1/auth/csrf/refresh")
    assert response.status_code == 200
    token = response.json()["data"]["csrfToken"]
    assert token
    assert token != previous
    assert client.cookies.get("df_csrf") == token


def test_tui_login_returns_session_expiry(client) -> None:
    data = register_and_login(client, login_client="tui")
    assert data["session"]["expiresAt"]
