from __future__ import annotations

from conftest import register_and_login


def test_capabilities_disable_deferred_features(client) -> None:
    register_and_login(client)
    response = client.get("/api/v1/capabilities")
    assert response.status_code == 200
    caps = response.json()["data"]
    assert caps["conversation.memory"] is False
    assert caps["interaction.resume"] is False
    assert caps["runtime.dataTools"] is False
    assert caps["runtime.traceDag"] is False
    assert caps["knowledge"] is False
    assert caps["mcp"] is False
    assert caps["skills"] is False
    assert caps["files"] is False
    assert caps["artifact.list"] is False
    assert caps["llm.advancedSampling"] is False
    assert caps["llm.samplingParams"] is True


def test_workspace_and_run_defaults_include_server_model(client) -> None:
    register_and_login(client)
    workspace = client.get("/api/v1/workspace-config")
    assert workspace.status_code == 200
    workspace_data = workspace.json().get("data", {})
    assert workspace_data.get("datasources") == []
    assert workspace_data.get("knowledgeBases") == []
    assert workspace_data.get("mcpServers") == []
    assert workspace_data.get("skills") == []
    profiles = workspace_data.get("modelProfiles", [])
    assert len(profiles) == 1
    server_default = profiles[0]
    assert server_default.get("id") == "server-default"
    assert server_default.get("modelName") == "test-model"
    assert server_default.get("hasSecret") is True
    assert server_default.get("connectionStatus") == "untested"
    assert "llmEnvFingerprint" not in server_default

    defaults = client.get("/api/v1/run-defaults")
    assert defaults.status_code == 200
    assert defaults.json()["data"] == {
        "enabledDatasourceIds": [],
        "enabledKnowledgeIds": [],
        "enabledMcpServerIds": [],
        "enabledSkillIds": [],
        "activeLlmProfileId": "server-default",
        "activeSkillId": "",
    }


def test_sessions_skills_and_datasource_types_are_empty(client) -> None:
    register_and_login(client)
    sessions = client.get("/api/v1/sessions?limit=50")
    assert sessions.status_code == 200
    assert sessions.json()["data"] == {"sessions": []}

    skills = client.get("/api/v1/skills")
    assert skills.status_code == 200
    assert skills.json()["data"] == []

    types = client.get("/api/v1/datasource-types")
    assert types.status_code == 200
    assert types.json()["data"] == []
