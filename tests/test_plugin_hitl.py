"""Plugin HITL declarations compile into one root middleware with contributed state."""

import pytest
from conftest import ScriptedModel, call
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dataagent.config.declarations import PluginSpec
from dataagent.core.graph import create_deep_agent
from dataagent.extensions.compiler import compile_extensions
from dataagent.extensions.hitl import HITLMiddleware


async def test_plugin_handler_state_and_namespaces_are_automatically_assembled(tmp_path):
    (tmp_path / "handler.py").write_text(
        "from dataagent import HITLHandler, HITLState, ResumeDecision\n"
        "class State(HITLState):\n"
        "    choice: str\n"
        "class Handler(HITLHandler):\n"
        "    state_schema = State\n"
        "    def resume_handler(self, context, *, params):\n"
        "        return ResumeDecision(action='continue', state_update={'choice': context.answer})\n"
    )
    spec = PluginSpec.model_validate(
        {
            "id": "sample",
            "hitl": {
                "policies": [
                    {
                        "id": "approval",
                        "matcher": "consume",
                        "handler": "handler.py:Handler",
                    }
                ]
            },
        }
    )

    @tool
    def consume(runtime: ToolRuntime) -> str:
        """Read the state contributed by the handler."""
        return runtime.state["choice"]

    kwargs = compile_extensions([(tmp_path, spec)], mcp_tools=[consume])
    assert sum(isinstance(m, HITLMiddleware) for m in kwargs["middleware"]) == 1
    graph = create_deep_agent(
        model=ScriptedModel(responses=[call("consume", {}), AIMessage(content="done")]),
        checkpointer=InMemorySaver(),
        **kwargs,
    )
    config = {"configurable": {"thread_id": "plugin"}}
    result = await graph.ainvoke({"messages": [("user", "execute")]}, config)
    item = result["__interrupt__"][0]
    assert item.value["policy_id"] == "sample/approval"
    result = await graph.ainvoke(Command(resume={item.id: "confirmed"}), config)
    assert result["choice"] == "confirmed"
    assert result["messages"][-2].content == "confirmed"


def test_plugin_schema_rejects_invalid_hitl():
    with pytest.raises(ValueError):
        PluginSpec.model_validate(
            {
                "id": "sample",
                "hitl": {
                    "policies": [
                        {
                            "id": "bad",
                            "at": "after_model",
                            "matcher": "task",
                        }
                    ]
                },
            }
        )


def test_disabled_hitl_skips_files_and_python_imports(tmp_path):
    spec = PluginSpec.model_validate(
        {
            "id": "off",
            "hitl": {
                "enabled": False,
                "instructions": "missing.md",
                "policies": [
                    {
                        "id": "unused",
                        "handler": "missing.py:Missing",
                    }
                ],
            },
        }
    )
    result = compile_extensions([(tmp_path, spec)])
    assert not any(isinstance(m, HITLMiddleware) for m in result["middleware"])
    assert not result["system_prompt"]


@pytest.mark.parametrize("path", ["missing.md", "../outside.md", "empty.md"])
def test_enabled_instructions_reject_missing_escaping_or_empty_files(tmp_path, path):
    (tmp_path / "empty.md").write_text("  ")
    spec = PluginSpec.model_validate({"id": "sample", "hitl": {"instructions": path}})
    with pytest.raises((ValueError, FileNotFoundError)):
        compile_extensions([(tmp_path, spec)])


async def test_model_feedback_waits_before_parallel_work_and_returns_raw_reply(tmp_path):
    (tmp_path / "hitl.md").write_text("Ask the user when identity is ambiguous.")
    specs = [
        PluginSpec.model_validate(
            {
                "id": name,
                "hitl": {
                    "enabled": True,
                    "instructions": "hitl.md",
                },
            }
        )
        for name in ("first", "second")
    ]
    executed = []

    @tool
    def consume() -> str:
        """Do work after clarification."""
        executed.append(True)
        return "SQL"

    kwargs = compile_extensions([(tmp_path, s) for s in specs], mcp_tools=[consume])
    mw = next(m for m in kwargs["middleware"] if isinstance(m, HITLMiddleware))
    assert [t.name for t in mw.tools] == ["request_human_feedback"]
    assert "Ask the user" in kwargs["system_prompt"]
    message = AIMessage(
        content="",
        tool_calls=[
            call("consume", {}, "premature").tool_calls[0],
            call(
                "request_human_feedback",
                {"question": "Which identity?", "options": ["user_id"]},
                "ask",
            ).tool_calls[0],
        ],
    )
    model = ScriptedModel(
        responses=[message, call("consume", {}, "approved"), AIMessage(content="done")]
    )
    saver = InMemorySaver()
    graph = create_deep_agent(model=model, checkpointer=saver, **kwargs)
    config = {"configurable": {"thread_id": "ask"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")]}, config)
    assert not executed and result["__interrupt__"][0].value["message"] == "Which identity?"
    result = await graph.ainvoke(Command(resume={result["__interrupt__"][0].id: " "}), config)
    assert result["__interrupt__"][0].value["validation_error"] and not executed
    # Rebuild the graph: question/tool arguments survive in the native checkpoint.
    graph = create_deep_agent(model=model, checkpointer=saver, **kwargs)
    result = await graph.ainvoke(Command(resume={result["__interrupt__"][0].id: "user_id"}), config)
    assert executed == [True]
    feedback = next(
        m for m in result["messages"] if getattr(m, "name", None) == "request_human_feedback"
    )
    assert feedback.content == "user_id" and feedback.tool_call_id == "ask"
    assert not any(
        c["id"] == "premature"
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for c in m.tool_calls
    )
