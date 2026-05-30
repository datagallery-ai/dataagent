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
"""NL2SQLAgent.astream 性能采集 session/state 绑定测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from dataagent.agents.nl2sql.agent import NL2SQLAgent


def _make_agent() -> NL2SQLAgent:
    agent = object.__new__(NL2SQLAgent)
    agent.backend = "langgraph"
    agent.workflow_backend = MagicMock()
    return agent


@pytest.fixture
def capture_perf_run(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def _factory(agent: NL2SQLAgent) -> dict[str, Any]:
        def _fake_perf_run(*, state: Any, backend: Any, flush_state_provider: Any):
            captured["bind_state"] = dict(state) if isinstance(state, dict) else state
            captured["backend"] = backend

            class _Ctx:
                def __enter__(self):
                    return MagicMock()

                def __exit__(self, *_exc: Any) -> None:
                    captured["flush_state"] = flush_state_provider()

            return _Ctx()

        monkeypatch.setattr(agent, "_performance_run", _fake_perf_run)
        return captured

    return _factory


@pytest.mark.asyncio
async def test_astream_binds_performance_before_stream(capture_perf_run) -> None:
    """astream 应在构造 state 后再进入 _performance_run，并使用最终 state flush。"""
    agent = _make_agent()
    captured = capture_perf_run(agent)

    async def _fake_stream(_state: Any, **kwargs: Any):
        yield ("values", {"session_id": "sess-42", "num_turns": 2, "question": "q1"})

    agent.workflow_backend.astream = _fake_stream

    chunks = []
    async for item in agent.astream("hello", session_id="sess-42"):
        chunks.append(item)

    assert captured["bind_state"]["session_id"] == "sess-42"
    assert captured["bind_state"]["question"] == "hello"
    assert captured["backend"] == "langgraph"
    assert captured["flush_state"]["num_turns"] == 2
    assert len(chunks) == 1


@pytest.mark.asyncio
async def test_astream_input_path_uses_input_state_for_perf(capture_perf_run) -> None:
    agent = _make_agent()
    captured = capture_perf_run(agent)
    input_state = {"user_id": "u1", "session_id": "s1", "question": "q"}

    async def _fake_stream(_state: Any, **kwargs: Any):
        assert kwargs["input"] is input_state
        yield ("values", {**input_state, "num_turns": 1})

    agent.workflow_backend.astream = _fake_stream

    async for _ in agent.astream(input=input_state):
        pass

    assert captured["bind_state"]["session_id"] == "s1"
    assert captured["flush_state"]["num_turns"] == 1
