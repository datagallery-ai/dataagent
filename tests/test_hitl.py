"""Handler inheritance plus native before-tool interrupts, subagents and recovery."""

from dataclasses import replace

import pytest
from conftest import ScriptedModel, call
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import (
    HITLConfig,
    HITLContext,
    HITLHandler,
    HITLMiddleware,
    HITLPolicy,
    HITLPolicySpec,
    HITLState,
    InteractionRequest,
    ResumeDecision,
    TriggerDecision,
)


class IRState(HITLState):
    ir: dict


def context():
    return HITLContext({}, None, {"name": "task", "args": {}, "id": "call"}, "main/test")


def middleware(handler=None, **spec):
    return HITLMiddleware(
        [
            HITLPolicy(
                HITLPolicySpec(id="gate", matcher="consume", **spec),
                handler or HITLHandler(),
            )
        ]
    )


def resume(result, answer):
    return Command(resume={result["__interrupt__"][0].id: answer})


def build(mw, saver, executed, responses=None):
    @tool
    def consume(runtime: ToolRuntime) -> str:
        """Consume approved business state."""
        executed.append(runtime.state.get("ir", {}))
        return "consumed"

    return create_deep_agent(
        model=ScriptedModel(
            responses=responses
            if responses is not None
            else [
                call("consume", {}, "original"),
                AIMessage(content="done"),
            ]
        ),
        tools=[consume],
        middleware=[mw],
        state_schema=IRState,
        checkpointer=saver,
    )


def test_handler_defaults_and_single_method_override():
    class MyHandler(HITLHandler):
        def interaction_builder(self, context, *, params):
            return InteractionRequest(question="Which identity?")

    handler = MyHandler()
    assert handler.trigger_policy(context(), params={}).triggered
    assert handler.interaction_builder(context(), params={}).question == "Which identity?"
    decision = handler.resume_handler(replace(context(), answer="user_id"), params={})
    assert decision.action == "replan" and "user_id" in decision.message
    assert decision.state_update == {}
    default = HITLHandler().interaction_builder(context(), params={"question": "Confirm?"})
    assert default.question == "Confirm?"


async def test_defaults_interrupt_then_replan_without_running_original_tool():
    executed = []
    mw = middleware(params={"question": "Which identity?", "options": ["user_id", "device_id"]})
    graph = build(mw, InMemorySaver(), executed)
    config = {"configurable": {"thread_id": "defaults"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    event = result["__interrupt__"][0].value
    assert event["message"] == "Which identity?" and event["policy_id"] == "main/gate"
    assert not executed
    result = await graph.ainvoke(resume(result, "user_id"), config)
    assert not executed
    message = result["messages"][-2]
    assert message.tool_call_id == "original" and message.status == "error"
    assert "user_id" in message.content
    history = (await graph.aget_state(config)).values["hitl_history"]
    assert history["original:main/gate"]["action"] == "replan"
    assert "hitl_history" not in result


@pytest.mark.parametrize("answer", ["", "  ", {"unexpected": "form"}])
async def test_invalid_text_reinterrupts_then_accepts_reply(answer):
    executed = []
    graph = build(middleware(), InMemorySaver(), executed)
    config = {"configurable": {"thread_id": "invalid"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, answer), config)
    assert result["__interrupt__"][0].value["validation_error"]
    assert not executed
    result = await graph.ainvoke(resume(result, "user_id"), config)
    assert not result.get("__interrupt__")


@pytest.mark.parametrize("action", ["continue", "reject", "replan"])
async def test_resume_actions_and_runtime_business_state(action):
    class Custom(HITLHandler):
        async def resume_handler(self, context, *, params):
            assert context.answer == "user_id"
            return ResumeDecision(action=action, state_update={"ir": {"identity": context.answer}})

    executed = []
    graph = build(middleware(Custom()), InMemorySaver(), executed)
    config = {"configurable": {"thread_id": "actions"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, "user_id"), config)
    assert result["ir"] == {"identity": "user_id"}
    assert executed == ([{"identity": "user_id"}] if action == "continue" else [])
    assert result["messages"][-2].tool_call_id == "original"


async def test_trigger_false_passes_without_building_interaction():
    class Custom(HITLHandler):
        async def trigger_policy(self, context, *, params):
            return TriggerDecision(triggered=False)

        def interaction_builder(self, context, *, params):
            raise AssertionError("Untriggered provider called")

    executed = []
    graph = build(middleware(Custom()), InMemorySaver(), executed)
    result = await graph.ainvoke(
        {"messages": [("user", "SQL")], "ir": {}},
        {"configurable": {"thread_id": "pass"}},
    )
    assert not result.get("__interrupt__") and executed == [{}]


async def test_sqlite_rebuild_freezes_question_params_and_keeps_original_call(tmp_path):
    class Original(HITLHandler):
        def resume_handler(self, context, *, params):
            assert params["frozen"] == "original"
            assert context.interaction.question == "Original question"
            return ResumeDecision(action="continue")

    class Rebuilt(Original):
        def trigger_policy(self, context, *, params):
            raise AssertionError("Preparation repeated")

        def interaction_builder(self, context, *, params):
            raise AssertionError("Question regenerated")

    config = {"configurable": {"thread_id": "sqlite"}}
    path = str(tmp_path / "checkpoints.sqlite")
    executed = []
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = build(
            middleware(
                Original(),
                params={
                    "question": "Original question",
                    "frozen": "original",
                },
            ),
            saver,
            executed,
        )
        result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = build(
            middleware(Rebuilt(), params={"frozen": "changed"}),
            saver,
            executed,
            responses=[AIMessage(content="done")],
        )
        result = await graph.ainvoke(resume(result, "yes"), config)
        assert executed == [{}]
        calls = [c for m in result["messages"] if isinstance(m, AIMessage) for c in m.tool_calls]
        assert [c["id"] for c in calls] == ["original"]


@pytest.mark.parametrize("change", ["remove", "version"])
async def test_pending_policy_change_cannot_bypass_gate(change):
    saver, executed = InMemorySaver(), []
    config = {"configurable": {"thread_id": "change"}}
    graph = build(middleware(), saver, executed)
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    mw = HITLMiddleware([]) if change == "remove" else middleware(version="2")
    graph = build(mw, saver, executed, responses=[])
    with pytest.raises(ValueError, match="incompatible version"):
        await graph.ainvoke(resume(result, "yes"), config)
    assert not executed


async def test_multiple_policies_require_all_confirmations():
    class Continue(HITLHandler):
        def resume_handler(self, context, *, params):
            return ResumeDecision(action="continue")

    mw = HITLMiddleware(
        [HITLPolicy(HITLPolicySpec(id=name, matcher="consume"), Continue()) for name in ("a", "b")]
    )
    executed = []
    graph = build(mw, InMemorySaver(), executed)
    config = {"configurable": {"thread_id": "multiple"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert result["__interrupt__"][0].value["policy_id"] == "main/b" and not executed
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert executed == [{}]
    assert len((await graph.aget_state(config)).values["hitl_history"]) == 2


async def test_parallel_resume_preserves_history_and_completed_execution():
    class Continue(HITLHandler):
        def resume_handler(self, context, *, params):
            return ResumeDecision(action="continue")

    calls = AIMessage(
        content="", tool_calls=[call("consume", {}, key).tool_calls[0] for key in ("a", "b")]
    )
    saver, executed = InMemorySaver(), []
    config = {"configurable": {"thread_id": "parallel"}}
    graph = build(middleware(Continue()), saver, executed, responses=[calls])
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    assert len(result["__interrupt__"]) == 2
    graph = build(middleware(Continue()), saver, executed, responses=[])
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert len(executed) == 1 and len(result["__interrupt__"]) == 1
    graph = build(middleware(Continue()), saver, executed, responses=[AIMessage(content="done")])
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert len(executed) == 2
    assert set((await graph.aget_state(config)).values["hitl_history"]) == {
        "a:main/gate",
        "b:main/gate",
    }


async def test_resume_failure_does_not_run_tool_or_commit_mutation():
    class Broken(HITLHandler):
        def resume_handler(self, context, *, params):
            context.state["ir"]["changed"] = True
            raise ValueError("resume failed")

    executed = []
    graph = build(middleware(Broken()), InMemorySaver(), executed)
    config = {"configurable": {"thread_id": "atomic"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    with pytest.raises(ValueError, match="resume failed"):
        await graph.ainvoke(resume(result, "yes"), config)
    assert not executed and (await graph.aget_state(config)).values["ir"] == {}


def test_configuration_inheritance_loading_and_validation(tmp_path):
    (tmp_path / "handler.py").write_text(
        "from dataagent.extensions.hitl import HITLHandler, InteractionRequest\n"
        "class Custom(HITLHandler):\n"
        "    async def interaction_builder(self, context, *, params):\n"
        "        return InteractionRequest(question='Question')\n"
    )
    mw = HITLMiddleware.from_config(
        {
            "policies": [
                {"id": "custom", "handler": "handler.py:Custom"},
                {"id": "default"},
            ]
        },
        root=tmp_path,
    )
    assert isinstance(mw.policies["main/custom"].handler, HITLHandler)
    assert type(mw.policies["main/default"].handler) is HITLHandler
    for policies in (
        [{"id": "a"}, {"id": "a"}],
        [{"id": "a", "at": "unknown"}],
        [{"id": "a", "at": "after_model", "matcher": "task"}],
        [{"id": "a", "matcher": " "}],
    ):
        with pytest.raises(ValueError):
            HITLConfig.model_validate({"policies": policies})
    with pytest.raises(ValueError, match="framework state"):
        ResumeDecision(state_update={"messages": []})


def test_invalid_handler_signatures_fail_before_execution():
    class Broken(HITLHandler):
        def resume_handler(self, context, params):
            pass

    with pytest.raises(TypeError, match="signature"):
        middleware(Broken())
    with pytest.raises(TypeError, match="inherit"):
        middleware(object())
