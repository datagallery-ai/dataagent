"""Native !577 lifecycle: durable tasks, turn boundaries and failure policy."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from dataagent.assembly.context import AgentContext
from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"
FIELD = {
    "field_id": "time_context",
    "value": {
        "input_windows": [{"window_id": "w7", "time_field": "pt_d", "range": {"number": 7}}],
    },
    "unresolved": [],
}
TEMPLATE = {"field_id": "time_context", "writable_template": deepcopy(FIELD)}


@pytest.fixture
def h(tmp_path, monkeypatch):
    loader = PythonLoader()
    start = loader.load(PLUGIN, "hooks/ir.py:prepare_turn")
    planner = loader.load(PLUGIN, "hooks/ir.py:prepare_context")
    before = loader.load(PLUGIN, "hooks/ir.py:ensure_ir_ready")
    core = before.__globals__["core"]
    session = core.ir_session
    options = {"data_task_ir": {"enabled": True, "refresh_policy": "eager"}}
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    root = tmp_path / "users/u/sessions/session-a"
    context = SimpleNamespace(
        session=SimpleNamespace(root=root, outputs=outputs),
        get_plugin_config=lambda _: options,
        get_plugin_state=lambda _: states,
        get_model=lambda _: object(),
    )
    states = {}
    fill = AsyncMock(return_value=[deepcopy(FIELD)])
    gate = AsyncMock(return_value=([], True))
    relation = AsyncMock(return_value=({"relation": "continue", "reason": "修改上一任务"}, "{}"))
    monkeypatch.setattr(core, "fill_ir_fields", fill)
    monkeypatch.setattr(core, "_ir_consistency_gate", gate)
    monkeypatch.setattr(core, "list_field_templates", lambda _: [deepcopy(TEMPLATE)])
    monkeypatch.setattr(session, "ainvoke_json_object", relation)

    async def turn(query, *, history=(), plan=True):
        state = {"messages": [*history, HumanMessage(content=query)]}
        runtime = SimpleNamespace(context=context)
        await start(state, runtime, params={})
        update = await planner(state, runtime, params={})
        if update:
            state["messages"].extend(update["messages"])
        return state

    async def consume(state, name="task"):
        request = SimpleNamespace(
            runtime=SimpleNamespace(context=context),
            state=state,
            tool_call={"id": "sql", "name": name, "args": {"subagent_type": "nl2sql"}},
        )
        return await before(request, params={})

    def evidence(query="db.fact", payload=None):
        folder = outputs / "metadata"
        folder.mkdir(exist_ok=True)
        import time

        (folder / f"{time.time_ns()}.json").write_text(
            json.dumps(
                {
                    "kind": "schema",
                    "query": query,
                    "payload": payload or {"id": "string"},
                    "recorded_at_ns": time.time_ns(),
                }
            )
        )

    return SimpleNamespace(
        context=context,
        options=options,
        states=states,
        core=core,
        session=session,
        fill=fill,
        gate=gate,
        relation=relation,
        turn=turn,
        consume=consume,
        evidence=evidence,
        start=start,
        planner=planner,
        loader=loader,
    )


def snapshot(h):
    return h.session.current(h.context)["snapshot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["eager", "on_consume"])
async def test_first_task_without_todos_commits_and_unchanged_consumers_reuse(h, policy):
    h.options["data_task_ir"]["refresh_policy"] = policy
    state = await h.turn("近7天下载次数")
    h.relation.assert_not_called()
    h.evidence()
    assert await h.consume(state) is None
    first = snapshot(h)
    assert first["status"] == "committed"
    assert len(first["evidence"]) == 1
    assert first["queries"] == ["近7天下载次数"]
    assert await h.consume(state, "de-agent__check_and_repair_sql") is None
    assert h.fill.await_count == h.gate.await_count == 1
    assert len(list(h.session.current(h.context)["store"].root.glob("versions/*.json"))) >= 2


@pytest.mark.asyncio
async def test_followup_inherits_task_evidence_and_original_requirements(h):
    state = await h.turn("近7天下载次数，排除刷量")
    h.evidence()
    await h.consume(state)
    task = snapshot(h)["task_id"]
    next_state = await h.turn("改为30天，取消刷量过滤，其他不变", history=state["messages"])
    assert await h.consume(next_state) is None
    assert snapshot(h)["task_id"] == task
    assert len(snapshot(h)["evidence"]) == 1
    assert "近7天下载次数" in h.fill.await_args.kwargs["user_query"]
    assert "取消刷量过滤" in h.fill.await_args.kwargs["user_query"]
    assert h.fill.await_args.kwargs["current_values"] == [FIELD]
    assert h.relation.await_count == 1
    summaries = [m for m in next_state["messages"] if isinstance(m, SystemMessage)]
    assert "后续修改/撤销优先" in summaries[-1].content
    await h.planner(next_state, SimpleNamespace(context=h.context), params={})
    assert h.relation.await_count == 1


@pytest.mark.asyncio
async def test_new_task_excludes_old_metadata_and_old_document_messages(h):
    state = await h.turn("下载统计")
    h.evidence("db.downloads")
    state["messages"].extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "id": "doc",
                        "args": {
                            "file_path": str(h.context.session.outputs.parent / "business.md")
                        },
                    }
                ],
            ),
            ToolMessage(content="OLD DOC", tool_call_id="doc"),
        ]
    )
    await h.consume(state)
    old = snapshot(h)["task_id"]
    h.relation.return_value = ({"relation": "new", "reason": "独立曝光任务"}, "{}")
    next_state = await h.turn("独立任务：曝光次数", history=state["messages"])
    h.evidence("db.exposures")
    await h.consume(next_state)
    assert snapshot(h)["task_id"] != old
    assert snapshot(h)["queries"] == ["独立任务：曝光次数"]
    args = h.fill.await_args.kwargs
    assert "db.downloads" not in args["tool_evidence"]
    assert "OLD DOC" not in args["tool_evidence"]
    assert "db.exposures" in args["tool_evidence"]
    assert args["current_values"] is None


@pytest.mark.asyncio
async def test_fresh_context_restores_snapshot_and_uses_same_task(h):
    state = await h.turn("近7天下载次数")
    h.evidence()
    await h.consume(state)
    task = snapshot(h)["task_id"]
    h.context.get_plugin_state = lambda _: fresh
    fresh = {}
    next_state = await h.turn("改成10天，其他不变")
    await h.consume(next_state)
    assert snapshot(h)["task_id"] == task
    assert len(snapshot(h)["evidence"]) == 1
    assert h.fill.await_args.kwargs["current_values"] == [FIELD]


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["inspect", "uncertain", "unavailable"])
async def test_nonbinding_relation_never_overwrites_or_injects_history(h, relation):
    state = await h.turn("下载次数")
    await h.consume(state)
    store = h.session.current(h.context)["store"]
    original = store.load()
    if relation == "unavailable":
        h.relation.side_effect = TimeoutError("offline")
    else:
        h.relation.return_value = ({"relation": relation, "reason": "不更新"}, "{}")
    next_state = await h.turn("解释一下", history=state["messages"])
    assert await h.consume(next_state) is None
    assert h.core.get_ir_context(h.context) == ""
    assert store.load() == original
    assert h.fill.await_count == 1


@pytest.mark.asyncio
async def test_warn_commits_warnings_without_repair_or_model_visible_diagnostics(h):
    h.gate.return_value = (["time_context：窗口疑似冲突"], True)
    state = await h.turn("下载次数")
    assert await h.consume(state) is None
    assert snapshot(h)["consistency_status"] == "warning"
    assert snapshot(h)["consistency_warnings"] == ["time_context：窗口疑似冲突"]
    assert h.fill.await_count == 1
    assert "窗口疑似冲突" not in h.core.get_ir_context(h.context)


@pytest.mark.asyncio
async def test_enforce_repairs_only_affected_fields_once(h):
    h.options["data_task_ir"]["consistency_policy"] = "enforce"
    updated = deepcopy(FIELD)
    updated["value"]["input_windows"][0]["range"]["number"] = 30
    h.fill.side_effect = [[deepcopy(FIELD)], [updated]]
    h.gate.side_effect = [(["time_context：应为30天"], True), ([], True)]
    state = await h.turn("近30天下载次数")
    assert await h.consume(state) is None
    assert h.fill.await_count == 2
    assert h.fill.await_args.kwargs["field_ids"] == {"time_context"}
    assert snapshot(h)["fields"] == [updated]


@pytest.mark.asyncio
async def test_enforce_unchanged_repair_falls_back_and_does_not_loop(h):
    h.options["data_task_ir"]["consistency_policy"] = "enforce"
    h.gate.return_value = (["time_context：应为30天"], True)
    state = await h.turn("近30天下载次数")
    assert await h.consume(state) is None
    assert snapshot(h)["status"] == "draft"
    assert h.core.get_ir_context(h.context) == ""
    assert await h.consume(state) is None
    assert h.fill.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["strict", "fallback_without_ir"])
async def test_extraction_failure_persists_evidence_and_never_reuses_old_ir(h, policy):
    state = await h.turn("下载次数")
    await h.consume(state)
    h.options["data_task_ir"]["failure_policy"] = policy
    next_state = await h.turn("改成30天")
    h.evidence()
    h.fill.side_effect = TimeoutError("offline")
    result = await h.consume(next_state)
    assert (result.status == "error") if policy == "strict" else result is None
    saved = snapshot(h)
    assert saved["status"] == "draft" and len(saved["evidence"]) == 1
    assert h.states["data_task_ir"]["ir_input_signature"] is None
    if policy != "strict":
        assert h.core.get_ir_context(h.context) == ""
        assert saved["fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_incompatible_or_expired_candidates_are_not_inherited(h, expired):
    state = await h.turn("下载次数")
    h.evidence()
    await h.consume(state)
    old = snapshot(h)
    if expired:
        h.session.save(
            h.context,
            {**old, "evidence_observed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
        )
    else:
        h.options["data_task_ir"]["compact_json"] = True
    next_state = await h.turn("改成30天")
    await h.consume(next_state)
    assert h.fill.await_args.kwargs["current_values"] is None
    assert h.fill.await_args.kwargs["tool_evidence"] == ""
    assert snapshot(h)["task_id"] == old["task_id"]


@pytest.mark.asyncio
async def test_concurrent_consumers_share_binding_and_single_refresh(h):
    state = await h.turn("下载次数")
    assert await asyncio.gather(*(h.consume(state) for _ in range(3))) == [None] * 3
    assert h.fill.await_count == 1


def test_snapshot_conflict_and_corruption_are_not_silently_overwritten(h):
    store_type = h.session.IRSessionStore
    root = h.context.session.root
    left = store_type(root, "u", "session-a")
    right = store_type(root, "u", "session-a")
    first = left.save(
        {"status": "draft", "task_id": "task", "queries": [], "fields": [], "evidence": []},
        expected_revision=None,
    )
    with pytest.raises(RuntimeError, match="另一轮"):
        right.save(first, expected_revision=None)
    (left.root / "current.json").write_text("broken")
    with pytest.raises(ValueError):
        left.load()


def test_session_context_copies_share_clients_but_not_plugin_state(tmp_path):
    runtime = SimpleNamespace(
        settings=SimpleNamespace(plugins=SimpleNamespace(enabled=("de-agent",)))
    )
    first = AgentContext(
        runtime, session=SimpleNamespace(root=tmp_path / "a", outputs=tmp_path / "oa")
    )
    first.get_plugin_state("de-agent")["ir"] = "a"
    second = first.for_session(SimpleNamespace(root=tmp_path / "b", outputs=tmp_path / "ob"))
    assert second.get_plugin_state("de-agent") == {}
    assert second._models is first._models
    same = first.for_session(first.session)
    assert same.get_plugin_state("de-agent")["ir"] == "a"


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_native_graph_runs_hooks_across_two_turns_without_todos(
    tmp_path, monkeypatch, fallback
):
    """Exercise the real middleware/reducer/checkpoint wiring, not just hook functions."""
    import sys

    from conftest import ScriptedModel, call
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from dataagent.assembly.graph import build_graph
    from dataagent.bootstrap import LaunchOptions, prepare_runtime

    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "offline",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "unused",
                    }
                },
                "plugins": {
                    "enabled": ["de-agent"],
                    "config": {
                        "de-agent": {
                            "data_task_ir": {"enabled": True},
                            "dataops": {"enabled": False},
                        }
                    },
                },
            }
        )
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    model = ScriptedModel(
        responses=[
            call("de-agent__read_ir", {}),
            AIMessage(content="完成"),
            AIMessage(content="交付已确认"),
            call("de-agent__read_ir", {}),
            AIMessage(content="已更新"),
            AIMessage(content="修改已确认"),
        ]
    )
    paths, _ = runtime.paths.open_session("ir-graph", create=True)
    context = AgentContext(runtime, model=model, session=paths)
    before_modules = set(sys.modules)
    graph = build_graph(
        runtime, model=model, context=context, session=paths, checkpointer=InMemorySaver()
    )
    loaded = [module for name, module in tuple(sys.modules.items()) if name not in before_modules]
    cores = [
        module
        for module in loaded
        if Path(getattr(module, "__file__", "") or "") == PLUGIN / "utils/data_task_ir/core.py"
    ]
    assert cores
    fill = AsyncMock(
        return_value=[deepcopy(FIELD)], side_effect=TimeoutError("IR offline") if fallback else None
    )
    relation = AsyncMock(return_value=({"relation": "continue", "reason": "修改窗口"}, "{}"))
    for core in cores:
        monkeypatch.setattr(core, "fill_ir_fields", fill)
        monkeypatch.setattr(core, "_ir_consistency_gate", AsyncMock(return_value=([], True)))
        monkeypatch.setattr(core.ir_session, "ainvoke_json_object", relation)
    invocation = {"configurable": {"thread_id": "ir-graph"}}
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="近7天下载次数")]}, invocation, context=context
    )
    assert first["__interrupt__"][0].value["policy_id"] == "de-agent/human_feedback_guard"
    first = await graph.ainvoke(
        Command(resume={first["__interrupt__"][0].id: "符合需求"}), invocation, context=context
    )
    assert not first.get("__interrupt__")
    messages = [
        m for m in first["messages"] if isinstance(m, ToolMessage) and m.name == "de-agent__read_ir"
    ]
    assert len(messages) == 1 and messages[0].status == "success"
    if fallback:
        assert messages[0].content == ""
        assert messages[0].response_metadata["data_task_ir"]["status"] == "fallback_without_ir"
    else:
        assert "DataTaskIR" in messages[0].content
        assert "data_task_ir" not in messages[0].response_metadata
    relation.assert_not_called()
    second = await graph.ainvoke(
        {"messages": [HumanMessage(content="改成30天，其他不变")]}, invocation, context=context
    )
    assert second["__interrupt__"][0].value["policy_id"] == "de-agent/human_feedback_guard"
    second = await graph.ainvoke(
        Command(resume={second["__interrupt__"][0].id: "符合需求"}), invocation, context=context
    )
    assert not second.get("__interrupt__")
    # Two user turns plus two confirmation replies, each adding fresh evidence.
    assert fill.await_count == 4
    assert relation.await_count == 1
    summaries = [m for m in second["messages"] if m.id == "data-task-business-context"]
    assert len(summaries) == 1
    assert "近7天下载次数" in summaries[0].content
    assert "改成30天" in summaries[0].content
    assert any(
        isinstance(message, SystemMessage)
        and message.id == "data-task-business-context"
        and "改成30天" in message.content
        for request in model.requests[2:]
        for message in request
    )
    snapshot = context.get_plugin_state("de-agent")["data_task_ir"]["session"]["snapshot"]
    assert snapshot["queries"] == ["近7天下载次数", "改成30天，其他不变"]


@pytest.mark.asyncio
async def test_multi_turn_disabled_keeps_current_turn_evidence_without_snapshots(h):
    h.options["data_task_ir"]["multi_turn"] = False
    state = await h.turn("下载次数")
    h.evidence("db.downloads")
    await h.consume(state)
    next_state = await h.turn("曝光次数", history=state["messages"])
    h.evidence("db.exposures")
    await h.consume(next_state)
    assert h.fill.await_args.kwargs["user_query"] == "曝光次数"
    assert "db.downloads" not in h.fill.await_args.kwargs["tool_evidence"]
    assert "db.exposures" in h.fill.await_args.kwargs["tool_evidence"]
    assert not h.context.session.root.exists()
    h.relation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option,value",
    [
        ("refresh_policy", "bad"),
        ("consistency_policy", "bad"),
        ("failure_policy", "bad"),
    ],
)
async def test_invalid_config_fails_before_snapshot_mutation(h, option, value):
    h.options["data_task_ir"][option] = value
    with pytest.raises(ValueError):
        await h.turn("下载次数")
    assert not h.context.session.root.exists()
    h.fill.assert_not_called()
    h.relation.assert_not_called()


@pytest.mark.parametrize(
    ("key", "value"), [("multi_turn", "yes"), ("evidence_ttl_seconds", "86400")]
)
def test_session_options_require_yaml_boolean_and_number(h, key, value):
    h.options["data_task_ir"][key] = value
    with pytest.raises(ValueError, match=key):
        h.session.prepare_turn(h.context, {})


@pytest.mark.asyncio
async def test_degraded_field_and_gate_protocol_failure_are_not_committed(h):
    state = await h.turn("下载次数")
    broken = deepcopy(FIELD)
    broken["_extraction_error"] = "TimeoutError"
    h.fill.return_value = [broken]
    await h.consume(state)
    assert snapshot(h)["status"] == "draft"
    assert h.core.get_ir_context(h.context) == ""
    h.gate.assert_not_called()
    next_state = await h.turn("改成30天")
    h.fill.return_value = [deepcopy(FIELD)]
    h.gate.return_value = (["检查协议失败"], False)
    await h.consume(next_state)
    assert snapshot(h)["status"] == "draft"
    assert h.core.get_ir_context(h.context) == ""
    assert await h.consume(next_state) is None
    assert h.gate.await_count == 1  # Same failed inputs are not retried indefinitely.


@pytest.mark.asyncio
async def test_successful_user_feedback_is_durable_but_error_feedback_is_ignored(h):
    state = await h.turn("下载次数")
    await h.consume(state)
    state["messages"].extend(
        [
            ToolMessage(content="排除刷量", name="request_human_feedback", tool_call_id="reply"),
            ToolMessage(
                content="猜测值", name="request_human_feedback", tool_call_id="err", status="error"
            ),
        ]
    )
    await h.consume(state)
    assert [record["content"] for record in snapshot(h)["feedback"]] == ["排除刷量"]
    assert "排除刷量" in h.fill.await_args.kwargs["user_query"]
    assert "猜测值" not in h.fill.await_args.kwargs["user_query"]


@pytest.mark.asyncio
async def test_evidence_replacement_and_identical_reread_use_latest_signature(h):
    state = await h.turn("下载次数")
    h.evidence(payload={"id": "string"})
    await h.consume(state)
    h.evidence(payload={"id": "bigint"})
    await h.consume(state)
    assert h.fill.await_count == 2
    assert "bigint" in h.fill.await_args.kwargs["tool_evidence"]
    assert "string" not in h.fill.await_args.kwargs["tool_evidence"]
    h.evidence(payload={"id": "bigint"})
    await h.consume(state)
    assert h.fill.await_count == 2
    next_state = await h.turn("下载次数")
    await h.consume(next_state)
    assert h.fill.await_count == 2
    assert snapshot(h)["status"] == "committed"


@pytest.mark.asyncio
async def test_snapshot_restores_and_updates_in_a_separate_python_process(h):
    import subprocess
    import sys

    state = await h.turn("近7天下载次数")
    h.evidence()
    await h.consume(state)
    task = snapshot(h)["task_id"]
    script = """
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from langchain_core.messages import HumanMessage
from dataagent.extensions.loading import PythonLoader
root, outputs, plugin = map(Path, sys.argv[1:])
cache = {}
options = {"data_task_ir": {"enabled": True, "refresh_policy": "eager"}}
context = SimpleNamespace(session=SimpleNamespace(root=root, outputs=outputs),
    get_plugin_config=lambda _: options, get_plugin_state=lambda _: cache, get_model=lambda _: object())
loader = PythonLoader()
start = loader.load(plugin, "hooks/ir.py:prepare_turn")
planner = loader.load(plugin, "hooks/ir.py:prepare_context")
core = start.__globals__["core"]
async def relation(*args):
    return {"relation": "continue", "reason": "续接"}, "{}"
async def fill(*args, **kwargs):
    assert kwargs["current_values"]
    return kwargs["current_values"]
async def gate(*args, **kwargs):
    return [], True
core.ir_session.ainvoke_json_object = relation
core.fill_ir_fields = fill
core._ir_consistency_gate = gate
async def main():
    state = {"messages": [HumanMessage(content="改成30天，其他不变")]}
    runtime = SimpleNamespace(context=context)
    await start(state, runtime, params={})
    await planner(state, runtime, params={})
    await core.ensure_ir_ready(context, state)
    saved = core.ir_session.current(context)["snapshot"]
    print(json.dumps({"task_id": saved["task_id"], "queries": saved["queries"], "evidence":len(saved["evidence"])}))
asyncio.run(main())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(h.context.session.root),
            str(h.context.session.outputs),
            str(PLUGIN),
        ],
        cwd=PLUGIN.parents[2],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    restored = json.loads(result.stdout)
    assert restored["task_id"] == task
    assert restored["queries"] == ["近7天下载次数", "改成30天，其他不变"]
    assert restored["evidence"] == 1
