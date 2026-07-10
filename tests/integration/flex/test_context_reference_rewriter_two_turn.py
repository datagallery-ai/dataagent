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
"""Integration tests: context_reference_rewriter via FlexAgent.chat() two-turn flow."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import ContextFactory
from dataagent.core.context.context_ir import QueryNode
from dataagent.core.flex.agent import FlexAgent
from dataagent.core.flex.hooks.context_reference_rewriter import context_reference_rewriter
from dataagent.core.flex.utils.planner_prompt_builder import _build_planner_system_and_user_messages
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.interface.sdk.agent import DataAgent

USER_ID = "st_ctx_ref_user"
SESSION_ID = "st_ctx_ref_session"
TURN1_QUERY = "分析销售数据"
TURN2_RAW = "用刚才那个表继续分析"


class _ToolManagerStub:
    """占位 tool_manager：满足 FlexAgent._refresh_workspace_runtime_context。"""

    @staticmethod
    def refresh_user_skills(*, user_id: str | None = None) -> None:
        return None

    @staticmethod
    def list_skills() -> list[dict[str, str]]:
        return []


class _ControlledRewriterLLM:
    """可控 LLM：仅用于 context_reference_rewriter hook。"""

    def __init__(self, table_id: str) -> None:
        self._table_id = table_id
        self.invoke_count = 0

    def invoke(self, _chat_input: Any) -> MagicMock:
        self.invoke_count += 1
        if self.invoke_count == 1:
            analyze_payload = {
                "needs_rewrite": True,
                "mentions": [
                    {"text": "刚才那个表", "target_types": ["Table"], "temporal_hint": "recent"},
                ],
                "skip_reason": "",
            }
            return MagicMock(content=json.dumps(analyze_payload, ensure_ascii=False))

        rewrite = f"用表 {self._table_id}，路径 /workspace/result.csv继续分析"
        rewrite_payload = {
            "decision": "rewrite",
            "rewrite_query": rewrite,
            "resolved_refs": [
                {
                    "mention": "刚才那个表",
                    "target_node": self._table_id,
                    "reason": "唯一 Table",
                }
            ],
            "skip_reason": "",
        }
        return MagicMock(content=json.dumps(rewrite_payload, ensure_ascii=False))


@contextmanager
def _capture_loguru_in_caplog(caplog: pytest.LogCaptureFixture, level: int = logging.DEBUG):
    """将 loguru 日志桥接到 pytest caplog。"""
    bridge_logger = logging.getLogger("context_reference_rewriter_st")

    def _emit(message: Any) -> None:
        record = message.record
        bridge_logger.log(record["level"].no, record["message"])

    handler_id = logger.add(_emit, format="{message}")
    with caplog.at_level(level, logger="context_reference_rewriter_st"):
        try:
            yield
        finally:
            logger.remove(handler_id)


def _build_flex_agent_stub(
    *,
    workspace: Path,
    table_id_holder: dict[str, str | None],
    workflow_capture: dict[str, Any],
) -> FlexAgent:
    """构造 stub FlexAgent，走真实 chat() pre-hook 与 Context 恢复链路。"""
    agent = object.__new__(FlexAgent)
    agent.config = {
        "USER_ID": USER_ID,
        "SESSION_ID": SESSION_ID,
        "WORKSPACE": {"allow_path": [str(workspace)]},
        "AGENT_CONFIG": {},
    }
    agent.config_manager = None
    agent.env_config = SimpleNamespace(tool_manager=_ToolManagerStub())
    agent._builtin_agent_pre_hooks = []
    agent._pre_hooks = [context_reference_rewriter]
    agent._post_hooks = []
    agent.debug = False
    agent.mode = "chat"

    runtime_holder: dict[str, Any] = {}
    llm_holder: dict[str, Any] = {}

    def _create_call_runtime() -> Any:
        runtime = MagicMock()
        runtime.workspace_dir = workspace
        runtime.hierarchy = None
        runtime.reset_flex_planner_user_sync = lambda: None
        runtime.on_subagent_progress = None
        runtime.update_from_state = lambda state: setattr(
            runtime,
            "workspace_dir",
            Path(str(state.get("workspace") or workspace)).expanduser().resolve(),
        )

        def _runtime_llm(name: str) -> _ControlledRewriterLLM:
            table_id = table_id_holder.get("id")
            if name != "planner":
                raise AssertionError(f"unexpected runtime.llm name: {name}")
            if not table_id:
                raise AssertionError("table_id not set before rewriter LLM call")
            if "rewriter" not in llm_holder:
                llm_holder["rewriter"] = _ControlledRewriterLLM(table_id)
            return llm_holder["rewriter"]

        runtime.llm = _runtime_llm
        runtime_holder["runtime"] = runtime
        return runtime

    agent._create_call_runtime = _create_call_runtime

    class _BackendStub:
        def set_runtime(self, _runtime_obj: Any) -> None:
            return None

        async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
            runtime = runtime_holder.get("runtime")
            ctx = agent._get_or_init_context(state, runtime)
            run_id = int(state.get("run_id", 0))
            if run_id == 0 and ctx is not None and table_id_holder.get("id") is None:
                predecessor = ctx.initial_pt or "Query(query00000)"
                ctx.register_node(
                    node_type="Action",
                    description="聚合销售表",
                    action="Tool(python_repl)",
                    params={"code": "df.groupby"},
                    output="ok",
                    success=True,
                    predecessor_node=[predecessor],
                )
                table_id_holder["id"] = ctx.register_node(
                    node_type="Table",
                    label="sales_agg",
                    description="用户分组聚合后的销售数据",
                    path="/workspace/result.csv",
                    predecessor_node=["Action(action00000)"],
                    edge_type="produces",
                )
            workflow_capture["state"] = dict(state)
            return {"messages": [], "complete": True}

        async def astream(self, _initial_state: dict[str, Any], **kwargs: Any):
            state = kwargs.get("input")
            if not isinstance(state, dict):
                state = dict(_initial_state)
            workflow_capture["state"] = dict(state)
            yield ("values", dict(state))

    agent.workflow_backend = _BackendStub()
    return agent


@pytest.mark.asyncio
async def test_two_turn_table_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """两轮 FlexAgent.chat：第一轮产出 Table，第二轮指代消解并同步 Planner 输入。"""
    ContextFactory.clear_context()

    table_id_holder: dict[str, str | None] = {"id": None}
    workflow_capture: dict[str, Any] = {}
    agent = _build_flex_agent_stub(
        workspace=tmp_path,
        table_id_holder=table_id_holder,
        workflow_capture=workflow_capture,
    )

    turn1_state = {
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "run_id": 0,
        "sub_id": 0,
        "workspace": str(tmp_path),
    }
    turn1_out = await agent.chat(TURN1_QUERY, initial_state=turn1_state)
    assert turn1_out.get("complete") is True
    assert table_id_holder["id"] is not None
    table_id = table_id_holder["id"]

    run0_ctx = ContextFactory.get_context(
        user_id=USER_ID,
        session_id=SESSION_ID,
        run_id=0,
        sub_id=0,
    )
    run0_ctx.persist_to_json()
    ContextFactory.clear_context()

    turn2_state = {
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "run_id": 1,
        "sub_id": 0,
        "workspace": str(tmp_path),
    }
    expected_rewrite = "用表（路径 /workspace/result.csv）继续分析"

    with _capture_loguru_in_caplog(caplog, level=logging.DEBUG):
        turn2_out = await agent.chat(TURN2_RAW, initial_state=turn2_state)

    assert turn2_out.get("complete") is True
    workflow_state = workflow_capture["state"]
    assert workflow_state["raw_user_query"] == TURN2_RAW
    assert workflow_state["user_query"] == expected_rewrite
    assert "Table(" not in workflow_state["user_query"]
    assert "/workspace/result.csv" in workflow_state["user_query"]

    run1_ctx = ContextFactory.get_context(
        user_id=USER_ID,
        session_id=SESSION_ID,
        run_id=1,
        sub_id=0,
    )
    assert run1_ctx.initial_pt is not None
    initial_pt = run1_ctx.initial_pt
    traj_node = run1_ctx.state.trajectory.nodes[initial_pt]
    assert traj_node["query"] == expected_rewrite
    assert traj_node["raw_user_query"] == TURN2_RAW

    label = initial_pt.split("(", 1)[1].rstrip(")")
    ir = run1_ctx.state.ir.get_IR(label=label, node_type="Query")
    assert isinstance(ir, QueryNode)
    assert ir.query == expected_rewrite
    assert ir.raw_user_query == TURN2_RAW

    merged = run1_ctx.get_trajectory(trimmed=False)
    assert any(str(node).startswith("Table(") for node in merged.nodes)

    system_prompt = PromptTemplate.from_string("system")
    user_prompt = PromptTemplate.from_string("<user_query>{{ user_query }}</user_query>")
    _, user_message = _build_planner_system_and_user_messages(
        run1_ctx,
        workflow_state,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        instruction="",
        agent_config={},
    )
    user_content = str(user_message.content or "")
    assert expected_rewrite in user_content
    assert TURN2_RAW not in user_content

    log_text = "\n".join(record.message for record in caplog.records)
    assert "decision=rewrite" in log_text or "rewrite" in log_text.lower()
    assert table_id in log_text or "sales_agg" in log_text


@pytest.mark.asyncio
async def test_reused_chat_state_resets_raw_user_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复用 initial_state 时，chat() 应把 raw_user_query 重置为本轮输入。"""
    ContextFactory.clear_context()

    table_id_holder: dict[str, str | None] = {"id": None}
    workflow_capture: dict[str, Any] = {}
    agent = _build_flex_agent_stub(
        workspace=tmp_path,
        table_id_holder=table_id_holder,
        workflow_capture=workflow_capture,
    )
    reused_state: dict[str, Any] = {
        "user_id": USER_ID,
        "session_id": "reused_state_session",
        "run_id": 0,
        "sub_id": 0,
        "workspace": str(tmp_path),
    }

    await agent.chat(TURN1_QUERY, initial_state=reused_state)
    assert reused_state["raw_user_query"] == TURN1_QUERY
    run0_ctx = ContextFactory.get_context(
        user_id=USER_ID,
        session_id="reused_state_session",
        run_id=0,
        sub_id=0,
    )
    run0_ctx.persist_to_json()
    ContextFactory.clear_context()

    reused_state["run_id"] = 1
    await agent.chat(TURN2_RAW, initial_state=reused_state)
    assert workflow_capture["state"]["raw_user_query"] == TURN2_RAW


@pytest.mark.asyncio
async def test_astream_rewrites_query_before_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """astream 路径应注册 QueryNode、执行 hook，并把改写后的 state 传给 backend。"""
    ContextFactory.clear_context()

    table_id_holder: dict[str, str | None] = {"id": "Table(sales_agg)"}
    workflow_capture: dict[str, Any] = {}
    agent = _build_flex_agent_stub(
        workspace=tmp_path,
        table_id_holder=table_id_holder,
        workflow_capture=workflow_capture,
    )

    context = ContextFactory.get_context(user_id=USER_ID, session_id="astream_session", run_id=0, sub_id=0)
    context.register_query(query=TURN2_RAW, additional_files=[])
    context.register_node(
        node_type="Action",
        description="聚合销售表",
        action="Tool(python_repl)",
        params={},
        output="ok",
        success=True,
        predecessor_node=["Query(query00000)"],
    )
    context.register_node(
        node_type="Table",
        label="sales_agg",
        description="用户分组聚合后的销售数据",
        path="/workspace/result.csv",
        predecessor_node=["Action(action00000)"],
        edge_type="produces",
    )

    state = {
        "user_id": USER_ID,
        "session_id": "astream_session",
        "run_id": 0,
        "sub_id": 0,
        "workspace": str(tmp_path),
        "user_query": TURN2_RAW,
    }
    chunks = [item async for item in agent.astream(input=state)]
    assert chunks

    expected_rewrite = "用表（路径 /workspace/result.csv）继续分析"
    assert workflow_capture["state"]["user_query"] == expected_rewrite
    assert workflow_capture["state"]["raw_user_query"] == TURN2_RAW
    ir = context.state.ir.get_IR(label="query00000", node_type="Query")
    assert isinstance(ir, QueryNode)
    assert ir.query == expected_rewrite
    assert ir.raw_user_query == TURN2_RAW


@pytest.mark.asyncio
async def test_dataagent_from_config_registers_default_rewriter_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DataAgent.from_config 应通过 default YAML 挂载 context_reference_rewriter。"""
    monkeypatch.setenv("BAILIAN_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("BAILIAN_API_KEY", "test-key")
    ContextFactory.clear_context()

    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text(
        """
AGENT_CONFIG:
  name: "Context Reference ST"
  type: "react"
  backend: "langgraph"

MODEL:
  chat_model:
    provider: "bailian"
    model_type: "chat"
    params:
      model: "deepseek-v4-flash"
      base_url: "http://127.0.0.1:9999"
      api_key: "test-key"
""",
        encoding="utf-8",
    )

    agent = DataAgent.from_config(cfg_path)
    flex_agent = agent.build_agent_graph()
    assert isinstance(flex_agent, FlexAgent)
    assert any(
        getattr(hook, "__name__", "") == "context_reference_rewriter" for hook in getattr(flex_agent, "_pre_hooks", [])
    )

    table_id = "Table(sales_agg)"
    fake_llm = _ControlledRewriterLLM(table_id)

    def _fake_runtime_llm(_self: Runtime, name: str) -> Any:
        if name != "planner":
            raise AssertionError(f"unexpected runtime.llm name: {name}")
        return fake_llm

    monkeypatch.setattr(Runtime, "llm", _fake_runtime_llm, raising=True)

    workflow_capture: dict[str, Any] = {}

    class _BackendStub:
        def set_runtime(self, _runtime_obj: Any) -> None:
            return None

        async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
            workflow_capture["state"] = dict(state)
            return {"messages": [], "complete": True}

    flex_agent.workflow_backend = _BackendStub()

    context = ContextFactory.get_context(user_id="anonymous", session_id="from_config_session", run_id=0, sub_id=0)
    context.register_query(query=TURN2_RAW, additional_files=[])
    context.register_node(
        node_type="Action",
        description="聚合销售表",
        action="Tool(python_repl)",
        params={},
        output="ok",
        success=True,
        predecessor_node=["Query(query00000)"],
    )
    context.register_node(
        node_type="Table",
        label="sales_agg",
        description="用户分组聚合后的销售数据",
        path="/workspace/result.csv",
        predecessor_node=["Action(action00000)"],
        edge_type="produces",
    )

    out = await agent.chat(
        TURN2_RAW,
        workspace=tmp_path,
        initial_state={
            "session_id": "from_config_session",
            "run_id": 0,
            "sub_id": 0,
        },
    )
    assert out.get("complete") is True
    assert fake_llm.invoke_count == 2
    assert workflow_capture["state"]["user_query"] == "用表（路径 /workspace/result.csv）继续分析"
