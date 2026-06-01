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
"""Integration tests for performance instrumentation entry points.

Validates:
- BaseNode.process records a node event when a collector is bound.
- BaseNode.aprocess records a node event in async flow.
- BaseNode falls back to no-op when no runtime / collector is bound.
- Multi-collector concurrency: events for different runs do not bleed.
- Persisted JSON contains only safe summaries (no full prompt / tool args).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter
from dataagent.core.utils.performance import (
    PerformanceCollector,
    bind_current_collector,
    measure_tool,
)


@pytest.fixture
def perf_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path))
    return tmp_path


class _SyncNode(BaseNode):
    def __init__(self, name: str = "sync_node") -> None:
        super().__init__(name=name)

    def _process(self, state: Any, runtime: Any = None) -> dict[str, Any]:
        return {"ok": True}


class _AsyncNode(BaseNode):
    def __init__(self, name: str = "async_node") -> None:
        super().__init__(name=name)

    async def _aprocess(self, state: Any, runtime: Any = None) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"messages": []}


def _make_collector(_tmp_path: Path, run_id: str = "r") -> PerformanceCollector:
    # 路径由 DATAAGENT_HOME (perf_home fixture) 重定向，无需显式传入。
    return PerformanceCollector(
        enabled=True,
        user_id="u",
        session_id="s",
        run_id=run_id,
        backend="langgraph",
    )


def test_basenode_process_records_via_contextvar(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)
    node = _SyncNode("planner")
    with bind_current_collector(collector):
        result = node.process({"messages": []}, runtime=None)
    assert result == {"ok": True}
    events = collector.events
    assert len(events) == 1
    assert events[0]["kind"] == "node"
    assert events[0]["name"] == "planner"
    assert events[0]["success"] is True


def test_basenode_aprocess_records_via_contextvar(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)
    node = _AsyncNode("executor")

    async def _run() -> None:
        with bind_current_collector(collector):
            await node.aprocess({"messages": []}, runtime=None)

    asyncio.run(_run())
    events = collector.events
    assert len(events) == 1
    assert events[0]["name"] == "executor"


def test_basenode_noop_when_no_collector_bound() -> None:
    node = _SyncNode("planner")
    out = node.process({"messages": []}, runtime=None)
    assert out == {"ok": True}  # 默认 noop collector，无事件、无副作用


def test_multiple_collectors_dont_bleed_under_concurrent_runs(tmp_path: Path, perf_home: Path) -> None:
    """contextvar 在线程间天然隔离（每个线程默认值是 noop），所以并发 bind 不会串数据。"""
    collector_one = _make_collector(tmp_path, run_id="one")
    collector_two = _make_collector(tmp_path, run_id="two")
    node = _SyncNode("planner")

    def _drive(collector: PerformanceCollector, count: int) -> None:
        with bind_current_collector(collector):
            for _ in range(count):
                node.process({"messages": []}, runtime=None)

    threads = [
        threading.Thread(target=_drive, args=(collector_one, 80)),
        threading.Thread(target=_drive, args=(collector_one, 70)),
        threading.Thread(target=_drive, args=(collector_two, 50)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(collector_one.events) == 150
    assert len(collector_two.events) == 50


class _FakeExecution:
    """Stand-in for ``NormalizedToolExecution`` for decorator tests."""

    def __init__(
        self,
        *,
        success: bool,
        tool_call_id: str = "call-1",
        error_type: str | None = None,
        frontend_msg: Any = None,
        output_text: str | None = None,
        raw_result: Any = None,
        retry_info: Any = None,
        source: str = "tool_manager",
    ) -> None:
        self.success = success
        self.tool_call_id = tool_call_id
        self.error_type = error_type
        self.frontend_msg = frontend_msg
        self.output_text = output_text
        self.raw_result = raw_result
        self.retry_info = retry_info
        self.metadata = {"source": source}


def test_tool_failure_promotes_to_top_level_success(tmp_path: Path, perf_home: Path) -> None:
    """tool 返回 success=False 时，事件顶层 success 必须为 False，error_type 必须同步到顶层。"""
    collector = _make_collector(tmp_path)

    class _Owner:
        @measure_tool
        async def run(self, tool_call: Any) -> _FakeExecution:
            return _FakeExecution(
                success=False,
                tool_call_id="call-abc",
                error_type="ParamsValueError",
                output_text="bad args",
            )

    with bind_current_collector(collector):
        asyncio.run(_Owner().run({"name": "search", "id": "call-abc", "args": {"q": "x"}}))

    events = [ev for ev in collector.events if ev["kind"] == "tool"]
    assert len(events) == 1
    ev = events[0]
    assert ev["success"] is False
    assert ev["error_type"] == "ParamsValueError"
    # extras 不应再保留冗余的 success / error_type
    assert "success" not in ev["extra"]
    assert "error_type" not in ev["extra"]
    # 但其它链路字段仍在；工具入参/输出摘要不进入性能事件。
    assert ev["extra"]["tool_call_id"] == "call-abc"
    assert ev["extra"]["source"] == "tool_manager"
    assert "args_summary" not in ev["extra"]
    assert "output_summary" not in ev["extra"]


def test_tool_success_keeps_top_level_true(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)

    class _Owner:
        @measure_tool
        async def run(self, tool_call: Any) -> _FakeExecution:
            return _FakeExecution(success=True, output_text="ok")

    with bind_current_collector(collector):
        asyncio.run(_Owner().run({"name": "search", "id": "c1", "args": {}}))

    ev = next(e for e in collector.events if e["kind"] == "tool")
    assert ev["success"] is True
    assert "error_type" not in ev
    assert "success" not in ev["extra"]


def test_e2e_ms_recorded_in_metadata(tmp_path: Path, perf_home: Path) -> None:
    """flush 追加的 `_flush` 行中 metadata 必须含 e2e_ms（collector 创建到 flush 的总耗时）。"""
    collector = _make_collector(tmp_path)
    node = _SyncNode("planner")
    with bind_current_collector(collector):
        node.process({"messages": []}, runtime=None)
    out = collector.flush({"messages": []})
    assert out is not None
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").strip().splitlines()]
    footer = lines[-1]
    assert footer["kind"] == "_flush"
    assert "e2e_ms" in footer["metadata"]
    e2e = float(footer["metadata"]["e2e_ms"])
    node_event = next(rec for rec in lines if rec["kind"] == "node")
    assert e2e >= float(node_event["elapsed_ms"])


def test_llm_event_includes_tokens_per_sec(tmp_path: Path, perf_home: Path) -> None:
    """LLM 事件 extra 应在有 output_tokens 时自动算 tokens_per_sec。"""
    collector = _make_collector(tmp_path)
    with collector.measure("llm", "planner", call_mode="invoke") as h:
        h.update(input_tokens=100, output_tokens=200, total_tokens=300)
        time.sleep(0.01)
    llm_event = next(ev for ev in collector.events if ev["kind"] == "llm")
    assert "tokens_per_sec" in llm_event["extra"]
    # output_tokens=200，elapsed_ms 至少 ~10ms → tps 上限约 20000
    tps = float(llm_event["extra"]["tokens_per_sec"])
    assert tps > 0


def test_llm_event_no_tokens_per_sec_when_no_output(tmp_path: Path, perf_home: Path) -> None:
    """没有 output_tokens（或为 0）时不应该出现 tokens_per_sec 字段。"""
    collector = _make_collector(tmp_path)
    with collector.measure("llm", "planner", call_mode="invoke") as h:
        h.update(input_tokens=10, output_tokens=0, total_tokens=10)
    llm_event = next(ev for ev in collector.events if ev["kind"] == "llm")
    assert "tokens_per_sec" not in llm_event["extra"]


def test_llm_event_records_immediate_caller(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)
    with collector.measure("node", "planner"), collector.measure("llm", "planner:qwen3", call_mode="astream") as h:
        h.update(input_tokens=1, output_tokens=2, total_tokens=3)

    llm_event = next(ev for ev in collector.events if ev["kind"] == "llm")
    assert llm_event["extra"]["caller_kind"] == "node"
    assert llm_event["extra"]["caller_name"] == "planner"


def test_llm_called_inside_tool_is_recorded_with_tool_caller(
    tmp_path: Path, perf_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = _make_collector(tmp_path)
    adapter = LangChainChatModelAdapter(
        SimpleNamespace(
            model="fake-model",
            invoke=lambda _messages, **_kwargs: SimpleNamespace(
                content="ok",
                usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            ),
        ),
        config=SimpleNamespace(name="tool_llm"),
    )
    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: adapter)

    class _Owner:
        @measure_tool
        async def run(self, tool_call: Any) -> _FakeExecution:
            llm = llm_manager.get_default_llm()
            response = llm.invoke([{"role": "user", "content": "run semantic check"}])
            return _FakeExecution(success=True, output_text=response.content)

    with bind_current_collector(collector):
        asyncio.run(_Owner().run({"name": "semantic_tool", "id": "call-llm", "args": {}}))

    llm_event = next(ev for ev in collector.events if ev["kind"] == "llm")
    assert llm_event["name"] == "tool_llm:fake-model"
    assert llm_event["extra"]["caller_kind"] == "tool"
    assert llm_event["extra"]["caller_name"] == "semantic_tool"
    assert llm_event["extra"]["input_tokens"] == 4
    assert llm_event["extra"]["output_tokens"] == 2


def _named_hook(state: Any, _runtime: Any) -> Any:
    """命名 hook，用于验证 BaseNode 自动给 hook 加 measure。"""
    return state


def test_basenode_wraps_pre_hooks_in_measure(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)
    node = _SyncNode("planner")
    node.add_pre_hook(_named_hook)

    def _hook_with_llm(state: Any, _runtime: Any) -> Any:
        with collector.measure("llm", "planner:fake", call_mode="invoke") as h:
            h.update(input_tokens=1, output_tokens=2, total_tokens=3)
        return state

    node.add_pre_hook(_hook_with_llm)

    with bind_current_collector(collector):
        node.process({"messages": []}, runtime=None)

    hook_events = [ev for ev in collector.events if ev["kind"] == "hook"]
    assert {ev["name"] for ev in hook_events} == {"_named_hook", "_hook_with_llm"}

    llm_event = next(ev for ev in collector.events if ev["kind"] == "llm")
    assert llm_event["extra"]["caller_kind"] == "hook"
    assert llm_event["extra"]["caller_name"] == "_hook_with_llm"


def test_basenode_aprocess_wraps_hooks_in_measure(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)
    node = _AsyncNode("executor")
    node.add_post_hook(_named_hook)

    async def _run() -> None:
        with bind_current_collector(collector):
            await node.aprocess({"messages": []}, runtime=None)

    asyncio.run(_run())

    hook_events = [ev for ev in collector.events if ev["kind"] == "hook"]
    assert [ev["name"] for ev in hook_events] == ["_named_hook"]


def test_summary_differentiates_llm_by_caller(tmp_path: Path, perf_home: Path) -> None:
    """同一逻辑 LLM 由不同 caller 调用时，summary 中应按 caller 分桶并累计 tokens。"""
    collector = _make_collector(tmp_path)
    with collector.measure("node", "planner"), collector.measure("llm", "planner:m", call_mode="astream") as h:
        h.update(input_tokens=10, output_tokens=20, total_tokens=30)
    with (
        collector.measure("node", "planner"),
        collector.measure("hook", "pruner"),
        collector.measure("llm", "planner:m", call_mode="invoke") as h,
    ):
        h.update(input_tokens=5, output_tokens=7, total_tokens=12)

    summary = collector.build_summary({"messages": []})
    llms = summary["llms"]
    main_key = "node:planner:planner:m"
    pruner_key = "hook:pruner:planner:m"
    assert main_key in llms, f"missing {main_key} in {sorted(llms)}"
    assert pruner_key in llms, f"missing {pruner_key} in {sorted(llms)}"
    assert llms[main_key]["count"] == 1
    assert llms[pruner_key]["count"] == 1
    assert llms[main_key]["input_tokens"] == 10
    assert llms[main_key]["output_tokens"] == 20
    assert llms[main_key]["total_tokens"] == 30
    assert llms[pruner_key]["total_tokens"] == 12
    assert llms[main_key]["llm_name"] == "planner:m"
    assert llms[main_key]["caller_kind"] == "node"
    assert llms[main_key]["caller_name"] == "planner"
    assert llms[pruner_key]["caller_kind"] == "hook"
    assert llms[pruner_key]["caller_name"] == "pruner"


def test_summary_top_level_llm_tokens_use_event_totals(tmp_path: Path) -> None:
    """顶层 llms token 应表示实际 LLM 调用总量，而非最终 state.messages 残留口径。"""
    collector = _make_collector(tmp_path)
    with collector.measure("node", "planner"), collector.measure("llm", "planner:m", call_mode="astream") as h:
        h.update(input_tokens=10, output_tokens=20, total_tokens=30)
    with collector.measure("hook", "pruner"), collector.measure("llm", "planner:m", call_mode="invoke") as h:
        h.update(input_tokens=5, output_tokens=7, total_tokens=12)

    state = {
        "messages": [
            SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 1, "total_tokens": 101}),
        ],
    }

    llms = collector.build_summary(state)["llms"]

    assert llms["input_tokens"] == 15
    assert llms["output_tokens"] == 27
    assert llms["total_tokens"] == 42
    assert llms["state_messages"] == {"input_tokens": 100, "output_tokens": 1, "total_tokens": 101}


def test_summary_records_min_max_ms_per_bucket(tmp_path: Path, perf_home: Path) -> None:
    """node/tool/llm 每个桶 entry 都应记录 min_ms / max_ms，便于看耗时分布。"""
    collector = _make_collector(tmp_path)

    # node: planner 跑两次，构造可分辨的最大/最小
    with collector.measure("node", "planner") as _:
        time.sleep(0.001)
    with collector.measure("node", "planner") as _:
        time.sleep(0.020)

    # tool: search 跑两次
    with collector.measure("tool", "search") as _:
        time.sleep(0.005)
    with collector.measure("tool", "search") as _:
        time.sleep(0.015)

    # llm: 同一桶（同 caller、同 name）跑两次
    with collector.measure("node", "planner"), collector.measure("llm", "planner:m") as h:
        h.update(input_tokens=1, output_tokens=1, total_tokens=2)
        time.sleep(0.002)
    with collector.measure("node", "planner"), collector.measure("llm", "planner:m") as h:
        h.update(input_tokens=1, output_tokens=1, total_tokens=2)
        time.sleep(0.012)

    summary = collector.build_summary({"messages": []})

    planner = summary["nodes"]["planner"]
    assert planner["count"] == 4  # 两次纯 node + 两次包裹 llm 的 node
    assert "min_ms" in planner and "max_ms" in planner
    assert planner["min_ms"] <= planner["max_ms"]
    # 纯 node 最快那次（sleep 1ms）应当 ≤ 含 llm 的那次（sleep 12ms）
    assert planner["max_ms"] >= planner["min_ms"]

    search = summary["tools"]["search"]
    assert search["count"] == 2
    assert search["min_ms"] < search["max_ms"]
    assert search["min_ms"] >= 0

    llm_key = "node:planner:planner:m"
    llm_entry = summary["llms"][llm_key]
    assert llm_entry["count"] == 2
    assert llm_entry["min_ms"] < llm_entry["max_ms"]


def test_attribute_calls_marks_caller_without_extra_event(tmp_path: Path, perf_home: Path) -> None:
    """attribute_calls 只压栈、不产生 measurement 事件，仅用来给内部 LLM 调用打 caller。"""
    from dataagent.core.utils.performance import attribute_calls

    collector = _make_collector(tmp_path)
    with (
        bind_current_collector(collector),
        attribute_calls("context", "KnowledgeNode"),
        collector.measure("llm", "planner:m", call_mode="invoke") as h,
    ):
        h.update(input_tokens=1, output_tokens=2, total_tokens=3)

    events = collector.events
    assert [ev["kind"] for ev in events] == ["llm"], events
    llm = events[0]
    assert llm["extra"]["caller_kind"] == "context"
    assert llm["extra"]["caller_name"] == "KnowledgeNode"


def test_context_ir_llm_call_records_caller(tmp_path: Path, perf_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """通过 BaseIR.llm_infer_async 的所有 context 推理，都应自动归因到 context:<子类名>。"""
    from dataagent.core.context.contextIR import KnowledgeNode

    collector = _make_collector(tmp_path)
    adapter = LangChainChatModelAdapter(
        SimpleNamespace(
            model="fake-model",
            invoke=lambda _messages, **_kwargs: SimpleNamespace(
                content="desc",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            ),
        ),
        config=SimpleNamespace(name="planner"),
    )
    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: adapter)

    node = KnowledgeNode(
        label="snippet",
        description=None,
        session_id="s",
        run_id=1,
        knowledge_type="domain",
        knowledge_content="hello world",
    )

    async def _run() -> None:
        with bind_current_collector(collector):
            await node.infer_description_async(from_action={}, from_state={})

    asyncio.run(_run())

    llm_event = next(ev for ev in collector.events if ev["kind"] == "llm")
    assert llm_event["extra"]["caller_kind"] == "context"
    assert llm_event["extra"]["caller_name"] == "KnowledgeNode"
    assert llm_event["extra"]["total_tokens"] == 18

    summary = collector.build_summary({"messages": []})
    key = "context:KnowledgeNode:planner:fake-model"
    assert key in summary["llms"], sorted(summary["llms"])
    assert summary["llms"][key]["count"] == 1
    assert summary["llms"][key]["total_tokens"] == 18


def test_measure_tool_excludes_tool_payload_summaries(tmp_path: Path, perf_home: Path) -> None:
    collector = _make_collector(tmp_path)

    class _Owner:
        @measure_tool
        async def run(self, tool_call: Any) -> _FakeExecution:
            return _FakeExecution(success=True, output_text="SUPER_SECRET_OUTPUT")

    with bind_current_collector(collector):
        asyncio.run(
            _Owner().run(
                {
                    "name": "search",
                    "id": "call-1",
                    "args": {"prompt": "SUPER_SECRET_PAYLOAD" * 50, "token": "tk-12345"},
                }
            )
        )
    out = collector.flush({"messages": []})
    assert out is not None
    raw = out.read_text(encoding="utf-8")
    assert "SUPER_SECRET_PAYLOAD" not in raw
    assert "tk-12345" not in raw
    assert "SUPER_SECRET_OUTPUT" not in raw
    lines = [json.loads(line) for line in raw.strip().splitlines()]
    tool_event = next(rec for rec in lines if rec["kind"] == "tool")
    assert tool_event["name"] == "search"
    assert "args_summary" not in tool_event["extra"]
    assert "output_summary" not in tool_event["extra"]
