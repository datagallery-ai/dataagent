from __future__ import annotations


def test_healthz_returns_ok_envelope(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"status": "ok"}}


def test_ready_returns_control_plane_status(client) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ready"
    assert body["data"]["control_plane"] == "ready"
    assert body["data"]["runtime"]["provider"] == "dataagent"


def test_unknown_route_uses_error_envelope(client) -> None:
    response = client.get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_v1_options_returns_cors_preflight(client) -> None:
    response = client.options(
        "/api/v1/me",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-CSRF-Token",
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "X-CSRF-Token" in response.headers["access-control-allow-headers"]
