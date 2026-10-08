"""Real DeepSeek requests verify A2A success and invalid-model terminal states."""

import json
import os
import uuid

import httpx
import pytest
import yaml

from dataagent.a2a_server import DataAgentExecutor, build_agent_card
from dataagent.a2a_server.server import create_a2a_server
from dataagent.interface.sdk.agent import DataAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send", "stream"])
@pytest.mark.parametrize("invalid_model", [True, False])
async def test_a2a_terminal_state_with_real_model(tmp_path, monkeypatch, mode, invalid_model):
    """Expose a real agent through A2A and verify task status and persisted artifacts."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is required for real LLM validation")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    model = "invalid-model-a2a-terminal-state" if invalid_model else os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    config = {
        "AGENT_CONFIG": {
            "name": "A2A terminal state validation",
            "version": "1.0",
            "description": "Real LLM validation",
            "backend": "langgraph",
            "type": "react",
        },
        "MODEL": {
            "chat_model": {
                "provider": "deepseek",
                "model_type": "chat",
                "params": {
                    "model": model,
                    "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                    "api_key": api_key,
                    "max_retries": 0,
                    "timeout": 30,
                },
            },
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    config_path.chmod(0o600)
    agent = DataAgent.from_config(config_path)
    app = create_a2a_server(build_agent_card(agent), DataAgentExecutor(agent))
    payload = {
        "message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": "Reply with OK."}]},
        "configuration": {"returnImmediately": False},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://a2a-test",
        headers={"A2A-Version": "1.0"},
        timeout=90,
    ) as client:
        response = await client.post(f"/a2a/rest/message:{mode}", json=payload)
        assert response.status_code == 200, response.text
        expected = "TASK_STATE_FAILED" if invalid_model else "TASK_STATE_COMPLETED"
        if mode == "send":
            task = response.json().get("task", {})
            assert task.get("status", {}).get("state") == expected, response.text
            task_id = task.get("id")
        else:
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            statuses = [event.get("statusUpdate", {}) for event in events if "statusUpdate" in event]
            assert statuses[-1].get("status", {}).get("state") == expected, response.text
            task_id = statuses[-1].get("taskId")
            if invalid_model:
                assert not any("artifactUpdate" in event for event in events), response.text
        stored_response = await client.get(f"/a2a/rest/tasks/{task_id}")
        assert stored_response.status_code == 200, stored_response.text
        stored_task = stored_response.json()
        assert stored_task.get("status", {}).get("state") == expected, stored_task
        artifacts = stored_task.get("artifacts", [])
        if invalid_model:
            assert not artifacts, stored_task
            parts = stored_task.get("status", {}).get("message", {}).get("parts", [])
            assert any("invalid-model-a2a-terminal-state" in part.get("text", "") for part in parts), stored_task
        else:
            assert artifacts, stored_task
            assert any("OK" in part.get("text", "") for artifact in artifacts for part in artifact.get("parts", []))
