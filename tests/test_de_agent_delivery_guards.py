from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from conftest import ScriptedModel
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.config.declarations import HookSpec
from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import HITLContext
from dataagent.extensions.hooks import compile_hook
from dataagent.extensions.loading import PythonLoader

ROOT = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def host(tmp_path, config):
    cache = {}
    return SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path),
        get_plugin_config=lambda _: config,
        get_plugin_state=lambda _: cache,
    )


async def test_delivery_pair_replans_once_on_native_graph(tmp_path):
    fn = PythonLoader().load(ROOT, "hooks/delivery.py:require_pair")
    mw = compile_hook(
        HookSpec(event="after_model", entrypoint="hooks/delivery.py:require_pair"),
        fn,
        name="delivery",
    )
    model = ScriptedModel(
        responses=[AIMessage(content="done"), AIMessage(content="cannot deliver")]
    )
    graph = create_deep_agent(model=model, middleware=[mw])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="开发特征")]},
        context=host(tmp_path, {"delivery": {"require_sql_pair": True}}),
    )
    assert model.cursor == 2
    assert result["messages"][-1].content == "cannot deliver"


def test_deadline_injects_once_per_task(tmp_path):
    fn = PythonLoader().load(ROOT, "hooks/delivery.py:deadline")
    ctx = host(tmp_path, {"delivery": {"max_model_calls": 2}})
    state = {"messages": [HumanMessage(content="开发特征")]}
    result = fn(state, SimpleNamespace(context=ctx), params={})
    assert "create_*.sql" in result["messages"][0].content
    assert fn(state, SimpleNamespace(context=ctx), params={}) is None


def test_rejected_sources_are_session_scoped(tmp_path):
    loader = PythonLoader()
    record = loader.load(ROOT, "utils/feedback.py:record_feedback")
    filter_ = record.__globals__["filter_candidates"]
    ctx = host(tmp_path, {})
    record(ctx, "不要使用 db.old，它不是累计次数口径")
    assert filter_(ctx, [{"table": "db.old"}, {"table": "db.new"}]) == [{"table": "db.new"}]
    other = tmp_path / "other"
    other.mkdir()
    assert filter_(host(other, {}), [{"table": "db.old"}]) == [{"table": "db.old"}]


async def test_caliber_review_is_once_per_ir_task(tmp_path, monkeypatch):
    cls = PythonLoader().load(ROOT, "hitl/caliber_review.py:CaliberReview")
    module = cls.trigger_policy.__globals__
    fields = [
        {
            "field_id": "identity",
            "value": {"components": [{"field_ref": "user_id"}]},
            "unresolved": [],
        }
    ]
    cache = {"session": {"snapshot": {"task_id": "task1"}}, "ir_field_values": fields}
    ctx = host(tmp_path, {"data_task_ir": {"enabled": True, "enable_ir_caliber_review": True}})
    ctx.get_plugin_state = lambda _: {"data_task_ir": cache}

    async def ready(*args):
        pass

    monkeypatch.setattr(module["core"], "ensure_ir_ready", ready)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "task", "args": {"subagent_type": "nl2sql"}, "id": "sql"}],
            )
        ]
    }
    context = HITLContext(
        state, SimpleNamespace(context=ctx), None, "de-agent/review", event="after_model"
    )
    guard = cls()
    decision = await guard.trigger_policy(context, params={})
    assert decision.triggered
    context = replace(context, decision=decision, answer="按 user_id")
    resume = guard.resume_handler(context, params={})
    state.update(resume.state_update)
    assert not (await guard.trigger_policy(context, params={})).triggered


async def test_semantic_enforcement_overrides_warn_consistency(tmp_path, monkeypatch):
    core = PythonLoader().load(ROOT, "hooks/ir.py:core")
    options = {
        "data_task_ir": {
            "enabled": True,
            "consistency_policy": "warn",
            "semantic_check_policy": "enforce",
        }
    }
    context = host(tmp_path, options)
    cache = {}
    context.get_plugin_state = lambda _: {"data_task_ir": cache}
    context.get_model = lambda _: None

    async def consistent(*args):
        return {"consistent": True, "warnings": []}, "{}"

    monkeypatch.setattr(core, "ainvoke_json_object", consistent)
    monkeypatch.setattr(
        core, "collect_ir_semantic_warnings", lambda *a, **k: ["关联键不能替代统计主体"]
    )
    warnings, completed = await core._ir_consistency_gate(
        context, user_query="每个应用", tool_evidence="", field_results=[]
    )
    assert completed and "关联键不能替代统计主体" in warnings
    assert core.ir_consistency_policy(context) == "enforce"
    options["data_task_ir"]["semantic_check_policy"] = "warn"
    assert core.ir_consistency_policy(context) == "warn"
