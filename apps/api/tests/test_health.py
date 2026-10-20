from __future__ import annotations


async def test_healthz_returns_ok_envelope(client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["conversation"]["ready"] is True
    assert body["data"]["conversation"]["protocol"] == "dataagent-v2"


async def test_ready_returns_control_plane_status(client) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert data["control_plane"] == "ready"
    assert data["conversation"]["ready"] is True


async def test_config_and_auth_routes_are_absent(client) -> None:
    for path in (
        "/api/v1/auth/status",
        "/api/v1/capabilities",
        "/api/v1/workspace-config",
        "/api/v1/run-defaults",
        "/api/v1/datasource-types",
    ):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_unknown_route_uses_error_envelope(client) -> None:
    response = await client.get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
