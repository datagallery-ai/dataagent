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

import pytest

from dataagent.actions.tools import a2a
from dataagent.actions.tools.a2a import A2AClientWrapper, AgentConfig


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
