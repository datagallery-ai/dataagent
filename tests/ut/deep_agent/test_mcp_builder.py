# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""YAML-to-OpenJiuWen MCP adapter tests."""

from __future__ import annotations

from typing import Any

import pytest

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.tools.mcp import build_mcp_servers
from dataagent.core.deep_agent.spec import DeepAgentBuildSpec, McpServerSpec


def test_normalizes_flat_stdio_mcp_yaml() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "mcp_servers": [
                    {
                        "server_id": "analytics",
                        "transport_type": "stdio",
                        "config": {
                            "command": "python",
                            "args": ["-m", "analytics_mcp"],
                            "env": {"API_KEY": "secret"},
                            "cwd": "/tmp",
                        },
                    }
                ]
            }
        }
    )

    assert spec.mcp_servers == (
        McpServerSpec(
            path="TOOLS.mcp_servers[0]",
            server_id="analytics",
            server_name="analytics",
            client_type="stdio",
            server_path="python",
            params={
                "command": "python",
                "args": ["-m", "analytics_mcp"],
                "env": {"API_KEY": "secret"},
                "cwd": "/tmp",
            },
        ),
    )


def test_normalizes_named_mapping_and_streamable_http_alias() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "mcp_servers": [
                    {
                        "warehouse": {
                            "server_id": "warehouse-id",
                            "transport_type": "streamable_http",
                            "config": {
                                "url": "https://example.test/mcp",
                                "auth_headers": {"Authorization": "Bearer token"},
                                "auth_query_params": {"tenant": 7},
                                "timeout": 30,
                            },
                        }
                    }
                ]
            }
        }
    )

    assert spec.mcp_servers[0] == McpServerSpec(
        path="TOOLS.mcp_servers[0]",
        server_id="warehouse-id",
        server_name="warehouse",
        client_type="streamable-http",
        server_path="https://example.test/mcp",
        params={"timeout": 30},
        auth_headers={"Authorization": "Bearer token"},
        auth_query_params={"tenant": "7"},
    )


def test_name_and_url_shorthand_defaults_to_sse() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "mcp_servers": [
                    {
                        "name": "remote-tools",
                        "url": "http://localhost:8000/sse",
                        "headers": {"X-Token": "token"},
                    }
                ]
            }
        }
    )

    assert spec.mcp_servers[0].client_type == "sse"
    assert spec.mcp_servers[0].server_id == "remote-tools"
    assert spec.mcp_servers[0].server_name == "remote-tools"
    assert spec.mcp_servers[0].server_path == "http://localhost:8000/sse"
    assert spec.mcp_servers[0].auth_headers == {"X-Token": "token"}


def test_builds_openjiuwen_mcp_server_config() -> None:
    built = build_mcp_servers(
        [
            McpServerSpec(
                path="TOOLS.mcp_servers[0]",
                server_id="remote",
                server_name="remote",
                client_type="sse",
                server_path="https://example.test/sse",
                auth_headers={"Authorization": "Bearer token"},
                auth_query_params={"tenant": "demo"},
            )
        ]
    )

    config = built[0]
    assert config.server_id == "remote"
    assert config.server_name == "remote"
    assert config.server_path == "https://example.test/sse"
    assert config.client_type == "sse"
    assert config.auth_headers == {"Authorization": "Bearer token"}
    assert config.auth_query_params == {"tenant": "demo"}


def test_adapter_keeps_mcps_out_of_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    harness_tool = object()
    built_mcp = object()

    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_harness_tools",
        lambda sys_operation, language, **kwargs: [harness_tool],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_local_tools",
        lambda specs: [],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_mcp_servers",
        lambda specs: [built_mcp],
    )

    adapter = DeepAgentAdapter(
        {
            "TOOLS": {
                "mcp_servers": [
                    {
                        "name": "remote",
                        "url": "http://localhost:8000/sse",
                    }
                ]
            }
        }
    )

    assert adapter.build_tools(object()) == [harness_tool]
    assert adapter.build_mcps() == [built_mcp]


def test_dataagent_passes_mcp_configs_to_deep_agent_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import openjiuwen.harness
    from openjiuwen.core.runner.runner import Runner

    from dataagent.interface.sdk import agent as agent_module

    harness_tool = object()
    mcp_config = object()
    sys_operation = object()
    captured: dict[str, Any] = {}

    class AddResult:
        @staticmethod
        def is_err() -> bool:
            return False

    monkeypatch.setattr(agent_module, "build_model_from_config", lambda config: object())
    monkeypatch.setattr(agent_module, "build_system_prompt", lambda config: "prompt")
    monkeypatch.setattr(agent_module.DataAgent, "_resolve_workspace", lambda self: tmp_path)
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.DeepAgentAdapter.build_tools",
        lambda self, operation, **kwargs: [harness_tool],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.DeepAgentAdapter.build_mcps",
        lambda self: [mcp_config],
    )
    monkeypatch.setattr(Runner.resource_mgr, "add_sys_operation", lambda card: AddResult())
    monkeypatch.setattr(Runner.resource_mgr, "get_sys_operation", lambda resource_id: sys_operation)

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openjiuwen.harness, "create_deep_agent", fake_create_deep_agent)

    agent = agent_module.DataAgent(
        {
            "AGENT_CONFIG": {"name": "mcp-test"},
            "TOOLS": {
                "mcp_servers": [
                    {
                        "name": "remote",
                        "url": "http://localhost:8000/sse",
                    }
                ]
            },
        }
    )
    agent._build_deep_agent()

    assert captured["tools"] == [harness_tool]
    assert captured["mcps"] == [mcp_config]
    assert captured["workspace"].root_path == tmp_path


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"TOOLS": {"mcp_servers": "bad"}}, "must be a list"),
        (
            {
                "TOOLS": {
                    "mcp_servers": [
                        {
                            "server_id": "stdio-server",
                            "transport_type": "stdio",
                            "config": {},
                        }
                    ]
                }
            },
            "config.command is required",
        ),
        (
            {
                "TOOLS": {
                    "mcp_servers": [
                        {
                            "server_id": "remote",
                            "transport_type": "sse",
                        }
                    ]
                }
            },
            "requires url or server_path",
        ),
        (
            {
                "TOOLS": {
                    "mcp_servers": [
                        {
                            "server_id": "remote",
                            "transport_type": "websocket",
                            "url": "https://example.test/mcp",
                        }
                    ]
                }
            },
            "is unsupported",
        ),
        (
            {
                "TOOLS": {
                    "mcp_servers": [
                        {"server_id": "duplicate", "url": "https://one.test/sse"},
                        {"server_id": "duplicate", "url": "https://two.test/sse"},
                    ]
                }
            },
            "duplicates MCP server",
        ),
    ],
)
def test_rejects_invalid_mcp_yaml(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message) as exc_info:
        DeepAgentBuildSpec.from_config(config)

    assert "TOOLS.mcp_servers" in str(exc_info.value)
