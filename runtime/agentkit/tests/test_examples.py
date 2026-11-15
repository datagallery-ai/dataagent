"""The shipped config and native Python hooks, through SDK and HTTP assembly."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, ToolMessage
from test_api import client_for, query, sse, terminal

from dataagent import LaunchOptions, prepare_runtime
from dataagent.declarations import HookSpec, normalize_hooks
from dataagent.lowlevel import build_graph
from dataagent.settings import (
    A2ASettings,
    AgentSettings,
    Limits,
    LoopDetectionSettings,
    ModelSelection,
    ModelSettings,
    PluginSettings,
    Settings,
)
from dataagent.strict_json import read_json

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/config.json"


def test_full_config_example_explicitly_covers_all_settings_fields():
    raw = read_json(EXAMPLE)
    assert "server" not in raw
    sections = (
        (raw, Settings),
        (raw["dataagent"], AgentSettings),
        (raw["dataagent"]["model"], ModelSelection),
        (raw["dataagent"]["limits"], Limits),
        (raw["dataagent"]["loop_detection"], LoopDetectionSettings),
        (raw["plugins"], PluginSettings),
        (raw["a2a"], A2ASettings),
        *((model, ModelSettings) for model in raw["models"].values()),
    )
    for values, schema in sections:
        expected = set(schema.model_fields)
        if schema is Settings:
            expected -= {"server"}
        assert set(values) == expected, schema.__name__
    hooks = normalize_hooks(raw["dataagent"]["hooks"])
    assert set().union(*(set(hook) for hook in hooks)) == set(HookSpec.model_fields)


@pytest.fixture
def example_runtime(tmp_path, monkeypatch, common_plugin):
    monkeypatch.setenv("LLM_MODEL", "example-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "example-test-secret")
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=EXAMPLE))


async def test_shipped_config_runs_python_hooks(example_runtime):
    assert all(binding.base_dir == EXAMPLE.parent for binding in example_runtime.extensions.hooks)
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1, 2, 3]}),
        AIMessage(content="Count 3, sum 6."),
    ])
    graph = build_graph(example_runtime, model=model)
    result = await graph.ainvoke({"messages": [("user", "请统计 1、2、3")]})
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    assert json.loads(message.content)["sum"] == 6
    assert result["messages"][-1].content == "Count 3, sum 6."


async def test_shipped_python_hook_rejects_before_model(example_runtime):
    model = ScriptedModel(responses=[])
    with pytest.raises(ValueError, match="4000"):
        await build_graph(example_runtime, model=model).ainvoke({
            "messages": [("user", "中" * 4001)],
        })
    assert model.requests == []


async def test_tool_hook_denial_reaches_http_as_one_real_error(example_runtime):
    hooks = tuple(
        spec.model_copy(update={
            "params": {"blocked_tools": ["common__summarize_numbers"]},
        }) if spec.event == "before_tool" else spec
        for spec in example_runtime.settings.dataagent.hooks
    )
    settings = example_runtime.settings.model_copy(update={
        "dataagent": example_runtime.settings.dataagent.model_copy(update={"hooks": hooks}),
    })
    bindings = tuple(replace(binding, spec=spec) for binding, spec in
                     zip(example_runtime.extensions.hooks, hooks, strict=True))
    runtime = replace(example_runtime, settings=settings,
                      extensions=replace(example_runtime.extensions, hooks=bindings))
    model = ScriptedModel(responses=[call("common__summarize_numbers", {"numbers": [1, 2]})])
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/dataagent/stream", json=query("统计 1、2")))
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["code"] == "INTERNAL_ERROR"
    assert "blocked by the configured policy" in events[-1]["message"]
    assert len(model.requests) == 1


async def test_sdk_explicitly_clearing_hooks_and_bindings_does_not_keep_stale_declarations(example_runtime):
    settings = example_runtime.settings.model_copy(update={
        "dataagent": example_runtime.settings.dataagent.model_copy(update={"hooks": ()}),
    })
    runtime = replace(example_runtime, settings=settings,
                      extensions=replace(example_runtime.extensions, hooks=()))
    model = ScriptedModel(responses=[AIMessage(content="ok")])
    result = await build_graph(runtime, model=model).ainvoke({"messages": [("user", "中" * 4001)]})
    assert result["messages"][-1].content == "ok"


@pytest.mark.parametrize("change", ["replace", "clear"])
def test_sdk_cannot_silently_use_stale_hook_bindings(example_runtime, change):
    hooks = () if change == "clear" else tuple(
        spec.model_copy(update={"params": {"changed": True}})
        for spec in example_runtime.settings.dataagent.hooks
    )
    settings = example_runtime.settings.model_copy(update={
        "dataagent": example_runtime.settings.dataagent.model_copy(update={"hooks": hooks}),
    })
    with pytest.raises(ValueError, match="Hook bindings do not match"):
        build_graph(replace(example_runtime, settings=settings), model=ScriptedModel(responses=[]))
