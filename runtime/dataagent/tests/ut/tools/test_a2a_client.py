# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for the A2A client wrapper."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from dataagent.actions.tools import a2a
from dataagent.actions.tools.a2a import A2AClientWrapper, A2AToolWrapper, AgentConfig
from dataagent.core.errors import DataAgentError


class _FakeA2AClient:
    async def __aenter__(self) -> _FakeA2AClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def send_message(self, _request: Any):
        """Return an empty async response iterator."""

        async def _responses():
            if False:
                yield None

        return _responses()


@pytest.mark.asyncio
async def test_call_tool_uses_non_streaming_client_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2A tools should preserve the legacy non-streaming southbound call path."""
    captured: dict[str, Any] = {}

    async def _fake_create_client(*, agent: str, client_config: Any):
        captured["agent"] = agent
        captured["streaming"] = client_config.streaming
        return _FakeA2AClient()

    monkeypatch.setattr(a2a, "create_client", _fake_create_client)

    wrapper = A2AClientWrapper(AgentConfig(agent_id="local", base_url="http://127.0.0.1:9999"))
    await wrapper.call_tool("chat", {"message": "hello"})

    assert captured == {"agent": "http://127.0.0.1:9999", "streaming": False}


@pytest.mark.asyncio
async def test_call_tool_reports_rejected_bearer_token_as_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 401 from the message endpoint should produce a clear, non-retriable auth error."""

    async def _reject_client_creation(*, agent: str, client_config: Any):
        request = httpx.Request("POST", f"{agent}/a2a/jsonrpc")
        response = httpx.Response(401, request=request)
        response.raise_for_status()

    monkeypatch.setattr(a2a, "create_client", _reject_client_creation)
    wrapper = A2AClientWrapper(
        AgentConfig(
            agent_id="protected_agent",
            base_url="http://127.0.0.1:9999",
            auth_token="wrong-secret-token",
        )
    )

    with pytest.raises(DataAgentError) as exc_info:
        await wrapper.call_tool("chat", {"message": "hello"})

    error = exc_info.value
    assert error.source == "tool"
    assert "A2A authentication failed for agent 'protected_agent'" in error.fact
    assert "HTTP 401 Unauthorized" in error.fact
    assert "Check TOOLS.A2A auth_token" in error.fact
    assert "wrong-secret-token" not in error.fact
    assert "wrong-secret-token" not in str(error.to_dict())


@pytest.mark.asyncio
async def test_tool_wrapper_preserves_authentication_error_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The A2A tool wrapper should preserve auth type and retry policy from the client."""

    async def _reject_call(self: A2AClientWrapper, tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        raise DataAgentError(
            source="config",
            fact=f"A2A authentication failed while calling '{tool_name}'",
            component="tool",
        )

    monkeypatch.setattr(A2AClientWrapper, "call_tool", _reject_call)
    client = A2AClientWrapper(AgentConfig(agent_id="protected_agent", base_url="http://127.0.0.1:9999"))
    tool = A2AToolWrapper(
        client,
        "chat",
        {
            "name": "chat",
            "description": "Remote chat",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    )

    result = await tool.acall(message="hello")

    assert result.success is False
    assert result.error is not None and result.error.source == "config"


class _FailedTaskA2AClient:
    async def __aenter__(self) -> _FailedTaskA2AClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def send_message(self, _request: Any):
        async def _responses():
            from a2a.types.a2a_pb2 import StreamResponse, TaskState

            response = StreamResponse()
            response.task.id = "task-failed"
            response.task.status.state = TaskState.TASK_STATE_FAILED
            if response.task.status.message.parts:
                response.task.status.message.parts[0].text = "remote agent failed"
            else:
                part = response.task.status.message.parts.add()
                part.text = "remote agent failed"
            yield response

        return _responses()


@pytest.mark.asyncio
async def test_call_tool_promotes_remote_failed_task_to_dataagent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_create_client(*, agent: str, client_config: Any):
        return _FailedTaskA2AClient()

    monkeypatch.setattr(a2a, "create_client", _fake_create_client)
    wrapper = A2AClientWrapper(AgentConfig(agent_id="remote", base_url="http://127.0.0.1:9999"))

    with pytest.raises(DataAgentError) as exc_info:
        await wrapper.call_tool("chat", {"message": "hello"})

    error = exc_info.value
    assert error.source == "tool"
    assert error.source == "tool"
    assert "FAILED" in error.fact or "failed" in error.fact.lower()
