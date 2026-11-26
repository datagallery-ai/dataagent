"""Host integration remains opt-in and keeps its standard routing."""

import json
from types import SimpleNamespace

from conftest import ScriptedModel, call
from langchain.agents.middleware import hook_config
from langchain_core.messages import AIMessage, ToolMessage

from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.config.declarations import HookSpec, SubAgentSpec
from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hooks import compile_hook
from dataagent.extensions.loading import PythonLoader
from dataagent.extensions.subagents import materialize_subagents


async def test_local_is_not_loaded_without_any_plugin_configuration(tmp_path, monkeypatch):
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
                }
            }
        )
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    assert runtime.settings.plugins.enabled == ()
    original = PythonLoader.load

    def load(self, root, entrypoint):
        assert root.name != "nl2sql-react"
        return original(self, root, entrypoint)

    monkeypatch.setattr(PythonLoader, "load", load)
    model = ScriptedModel(responses=[AIMessage(content="Ordinary root reply")])
    async with create_agent(runtime, model=model) as agent:
        result = await agent.ainvoke({"messages": [("user", "Hello")]})
    assert result["messages"][-1].text == "Ordinary root reply"
    assert "local SQL service SQL delegate" not in model.requests[0][0].text
    assert not any(name.startswith("nl2sql-react__") for name in model.tool_schemas[0])


async def test_standard_subagent_hook_keeps_its_original_routing(tmp_path):
    @hook_config(can_jump_to=["end"])
    def stop(state, runtime, *, params):
        return {"messages": [AIMessage(content="Hook reply")], "jump_to": "end"}

    (tmp_path / "agent.md").write_text("Handle the delegated request.")
    spec = SubAgentSpec(
        name="test-child",
        description="Handles the delegated request",
        system_prompt_file="agent.md",
        tools=[],
        hooks=[HookSpec(entrypoint="unused.py:stop", event="before_model")],
    )
    subagents = materialize_subagents(
        [(tmp_path, "test-plugin", spec)],
        tool_registry={},
        loader=SimpleNamespace(load=lambda root, entrypoint: stop),
        skill_sources=[],
    )
    model = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "test-child", "description": "Resolve this"}),
            AIMessage(content="Child model completed"),
            AIMessage(content="Parent resumed"),
        ]
    )
    graph = create_deep_agent(model=model, subagents=subagents)
    result = await graph.ainvoke({"messages": [("user", "Delegate this request")]})
    reply = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert reply.text == "Child model completed"
    assert result["messages"][-1].text == "Parent resumed"
    assert model.cursor == 3


async def test_host_hook_adapter_keeps_its_original_routing():
    @hook_config(can_jump_to=["end"])
    def stop(state, runtime, *, params):
        return {"messages": [AIMessage(content="Hook reply")], "jump_to": "end"}

    model = ScriptedModel(responses=[AIMessage(content="Original model path")])
    middleware = compile_hook(
        HookSpec(entrypoint="unused.py:stop", event="before_model"),
        stop,
        name="stop",
    )
    graph = create_deep_agent(model=model, middleware=[middleware])
    result = await graph.ainvoke({"messages": [("user", "Host routing remains unchanged")]})
    assert result["messages"][-1].text == "Original model path"
    assert model.cursor == 1
