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
"""Planner 的 error 需保留在 LangGraph 最终状态中，且不能遗留到之后的正常结果。"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from dataagent.core.context.context import ContextFactory
from dataagent.core.flex.nodes.planner import Planner
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.managers.llm_manager.adapters import LLMResponse, LLMStreamChunk

_FIRST_TURN = {"messages": [HumanMessage(content="统计表 t 的行数")], "user_id": "u", "session_id": "s"}
_PLANNER_ERROR = "推理执行错误: network failed"


class _Model:
    """Stream ``reply`` as one chunk, or fail before the first chunk when ``reply`` is None."""

    def __init__(self) -> None:
        self.reply: str | None = None

    def bind_tools(self, _tools: list[Any]) -> _Model:
        return self

    async def astream(self, _messages: Any):
        if self.reply is None:
            raise RuntimeError("network failed")
        yield LLMStreamChunk(content=self.reply)
        yield LLMStreamChunk(final_response=LLMResponse(content=self.reply, usage_metadata={}), done=True)


class _Runtime:
    config_manager = None

    def __init__(self) -> None:
        self.model = _Model()

    def llm(self, _name: str) -> _Model:
        return self.model

    def get_tools_for_llm(self) -> list[Any]:
        return []

    def get_config(self, _key: str, default: Any = None) -> Any:
        return default


@pytest.fixture
def run_planner(monkeypatch):
    """Run a real Planner in a FlexState graph; turns on one thread share a checkpoint."""
    ContextFactory.clear_context()
    ContextFactory.get_context(user_id="u", session_id="s", run_id=0, sub_id=0).register_query(
        query="统计表 t 的行数", additional_files=[]
    )
    planner = Planner(name="planner", chat_model="chat_model")
    monkeypatch.setattr(planner, "_prepare_messages_to_process", lambda *_args, **_kwargs: [])
    runtime = _Runtime()

    async def planner_node(state: FlexState) -> dict[str, Any]:
        return await planner._aprocess(state, runtime)

    graph = StateGraph(FlexState)
    graph.add_node("planner", planner_node)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", END)
    app = graph.compile(checkpointer=InMemorySaver())

    async def run(update: dict[str, Any], *, reply: str | None = None) -> dict[str, Any]:
        runtime.model.reply = reply
        return await app.ainvoke({"complete": False, **update}, config={"configurable": {"thread_id": "t"}})

    yield run
    ContextFactory.clear_context()


@pytest.mark.asyncio
async def test_planner_failure_is_kept_in_final_state(run_planner) -> None:
    result = await run_planner(_FIRST_TURN)

    assert result["complete"] is True
    assert result["messages"][-1].additional_kwargs.get("error") is True
    assert result["error"] == _PLANNER_ERROR


@pytest.mark.asyncio
async def test_model_reply_clears_error_left_by_earlier_failure(run_planner) -> None:
    await run_planner(_FIRST_TURN)

    result = await run_planner({"messages": [HumanMessage(content="再试一次")]}, reply="表 t 共 3 行。")

    assert result["messages"][-1].content == "表 t 共 3 行。"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_intent_gap_notice_clears_error_left_by_earlier_failure(run_planner) -> None:
    await run_planner(_FIRST_TURN)

    result = await run_planner(
        {
            "messages": [HumanMessage(content="再试一次")],
            "intent_complete": False,
            "missing_slots": ["时间范围"],
            "intent_missing_message": "请补充时间范围。",
        }
    )

    assert result["messages"][-1].content == "请补充时间范围。"
    assert result["error"] is None
