from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from conftest import register_and_login
from mcp.server.fastmcp import FastMCP

from datafoundry_api.agent import RunResourceConfig


@pytest.fixture
def live_mcp_url():
    """Run a real local Streamable HTTP MCP server for adapter verification."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    mcp = FastMCP("datafoundry-test", stateless_http=True, json_response=True)

    @mcp.tool()
    def echo(text: str) -> str:
        """Return text with a stable prefix."""
        return f"echo:{text}"

    @mcp.tool()
    def read_file(path: str) -> str:
        """Expose a name collision without reading the host filesystem."""
        return f"not-a-file:{path}"

    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Local MCP verification server did not start.")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_mcp_crud_encrypts_secrets_and_rejects_frontend_stdio(client) -> None:
    register_and_login(client)
    rejected = client.post(
        "/api/v1/mcp-servers",
        json={"id": "local", "name": "Local", "transport": "stdio", "serverUrl": "npx server"},
    )
    assert rejected.status_code == 400
    assert rejected.json().get("error", {}).get("code") == "MCP_TRANSPORT_UNSUPPORTED"

    created = client.post(
        "/api/v1/mcp-servers",
        json={
            "id": "remote-mcp",
            "name": "Remote MCP",
            "transport": "streamable-http",
            "serverUrl": "http://127.0.0.1:9/mcp",
            "authType": "bearer",
            "toolAllowlist": ["echo"],
            "timeoutMs": 1_000,
            "credentials": {"token": "mcp-test-secret"},
            "defaultEnabled": True,
        },
    )
    assert created.status_code == 201, created.text
    server = created.json().get("data", {})
    assert server.get("hasSecret") is True
    assert server.get("healthStatus") == "untested"
    assert "mcp-test-secret" not in created.text

    secret = client.app.state.store.fetchone(
        "SELECT ciphertext FROM encrypted_secrets WHERE ref = ?",
        (server.get("secretRef"),),
    )
    assert secret is not None
    assert "mcp-test-secret" not in str(dict(secret).get("ciphertext", ""))

    workspace = client.get("/api/v1/workspace-config").json().get("data", {})
    assert [item.get("id") for item in workspace.get("mcpServers", [])] == ["remote-mcp"]
    defaults = client.get("/api/v1/run-defaults").json().get("data", {})
    assert defaults.get("enabledMcpServerIds") == ["remote-mcp"]


@pytest.mark.asyncio
async def test_enabled_unreachable_mcp_is_isolated_for_agent_assembly(client) -> None:
    register_and_login(client)
    created = client.post(
        "/api/v1/mcp-servers",
        json={
            "id": "unreachable",
            "name": "Unreachable MCP",
            "transport": "sse",
            "serverUrl": "http://127.0.0.1:9/sse",
            "timeoutMs": 1_000,
            "defaultEnabled": True,
        },
    )
    assert created.status_code == 201
    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    selection = await client.app.state.mcp_servers.resolve_tools(identity, ("unreachable",))
    assert selection.tools == ()
    assert selection.unavailable[0].get("id") == "unreachable"

    tested = client.post("/api/v1/mcp-servers/unreachable/test")
    assert tested.status_code == 502
    assert tested.json().get("error", {}).get("code") == "MCP_TEST_FAILED"
    refreshed = client.get("/api/v1/mcp-servers/unreachable").json().get("data", {})
    assert refreshed.get("healthStatus") == "failed"


@pytest.mark.asyncio
async def test_live_mcp_discovery_call_and_collision_prefix(client, live_mcp_url: str) -> None:
    register_and_login(client)
    created = client.post(
        "/api/v1/mcp-servers",
        json={
            "id": "live",
            "name": "Live MCP",
            "transport": "streamable-http",
            "serverUrl": live_mcp_url,
            "timeoutMs": 5_000,
            "defaultEnabled": True,
        },
    )
    assert created.status_code == 201, created.text

    tested = client.post("/api/v1/mcp-servers/live/test")
    assert tested.status_code == 200, tested.text
    assert tested.json().get("data", {}).get("toolCount") == 2
    manifest = client.get("/api/v1/mcp-servers/live/tools")
    assert {tool.get("name") for tool in manifest.json().get("data", [])} == {"echo", "read_file"}

    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    selection = await client.app.state.mcp_servers.resolve_tools(
        identity,
        ("live",),
        reserved_names={"read_file"},
    )
    tools = {tool.name: tool for tool in selection.tools}
    assert set(tools) == {"echo", "mcp__live__read_file"}
    result = await tools.get("echo").ainvoke({"text": "verified"})
    assert "echo:verified" in str(result)

    skill = client.post(
        "/api/v1/skills",
        data={"id": "mcp-decoupled", "defaultMcpIds": "live"},
        files={
            "file": (
                "SKILL.md",
                b"---\nname: mcp-decoupled\ndescription: Verify native Skill loading.\n---\n\n# Skill\n",
                "text/markdown",
            )
        },
    )
    assert skill.status_code == 201, skill.text
    resources_without_mcp = RunResourceConfig(
        enabled_skill_ids=("mcp-decoupled",),
        active_skill_id="mcp-decoupled",
        enabled_mcp_server_ids=(),
    )
    skill_only_agent = await client.app.state.agent_runtime.agent_for(
        identity.user_id,
        "skill-only-session",
        identity=identity,
        resources=resources_without_mcp,
    )
    skill_only_tools = skill_only_agent.graph.nodes.get("tools").bound.tools_by_name
    assert "echo" not in skill_only_tools
    assert any("SkillsMiddleware" in name for name in skill_only_agent.graph.nodes)

    resources_with_mcp = RunResourceConfig(
        enabled_skill_ids=("mcp-decoupled",),
        active_skill_id="mcp-decoupled",
        enabled_mcp_server_ids=("live",),
    )
    compiled_agent = await client.app.state.agent_runtime.agent_for(
        identity.user_id,
        "compiled-session",
        identity=identity,
        resources=resources_with_mcp,
    )
    cached_agent = await client.app.state.agent_runtime.agent_for(
        identity.user_id,
        "compiled-session",
        identity=identity,
        resources=resources_with_mcp,
    )
    compiled_tools = compiled_agent.graph.nodes.get("tools").bound.tools_by_name
    assert {"list_workspace_files", "read_workspace_file", "promote_workspace_file"} <= set(compiled_tools)
    assert {"echo", "mcp__live__read_file"} <= set(compiled_tools)
    assert compiled_agent.graph is cached_agent.graph

    live = client.get("/api/v1/mcp-servers/live").json().get("data", {})
    assert len(live.get("toolManifest", [])) == 2
    patched = client.patch(
        "/api/v1/mcp-servers/live",
        json={"toolAllowlist": ["echo"], "revision": live.get("revision")},
    )
    assert patched.status_code == 200, patched.text
    patched_server = patched.json().get("data", {})
    assert patched_server.get("healthStatus") == "untested"
    assert "toolManifest" not in patched_server


@pytest.mark.asyncio
async def test_mcp_secret_failure_is_isolated_before_connection(client, live_mcp_url: str) -> None:
    register_and_login(client)
    for server_id, credentials in (("healthy", None), ("broken-secret", {"token": "secret"})):
        body = {
            "id": server_id,
            "name": server_id,
            "transport": "streamable-http",
            "serverUrl": live_mcp_url,
            "timeoutMs": 5_000,
            "defaultEnabled": True,
        }
        if credentials is not None:
            body["authType"] = "bearer"
            body["credentials"] = credentials
        response = client.post("/api/v1/mcp-servers", json=body)
        assert response.status_code == 201, response.text

    client.app.state.store.execute(
        "DELETE FROM encrypted_secrets WHERE owner_kind = ? AND owner_id = ?",
        ("mcp-server", "broken-secret"),
    )
    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    selection = await client.app.state.mcp_servers.resolve_tools(identity, ("healthy", "broken-secret"))
    assert {tool.name for tool in selection.tools} == {"echo", "read_file"}
    assert [item.get("id") for item in selection.unavailable] == ["broken-secret"]
