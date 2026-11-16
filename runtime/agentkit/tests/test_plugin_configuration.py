"""Configuration and Hook contracts without invoking an Agent or substituting a model."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from dataagent import LaunchOptions, prepare_runtime
from dataagent.bootstrap.discovery import extension_revision
from dataagent.bootstrap.startup import refresh_extensions
from dataagent.declarations import HookSpec
from dataagent.extensions.hooks import compile_hook
from dataagent.extensions.run_hooks import RunHook
from dataagent.models import AgentContext


def _plugin(home, name, *, requires=(), config=None):
    path = home / "plugins" / name
    path.mkdir(parents=True, exist_ok=True)
    manifest = {"id": name, "requires": list(requires)}
    if config is not None:
        manifest["defaults_file"] = "defaults.json"
        (path / "defaults.json").write_text(json.dumps({"plugins": {"config": config}}))
    (path / ".plugin.json").write_text(json.dumps(manifest))
    return path


def _runtime(tmp_path, enabled, config=None):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "unused",
                        "base_url": "http://localhost/v1",
                        "api_key": "unused",
                    }
                },
                "plugins": {"enabled": enabled, "config": config or {}},
            }
        )
    )
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))


def test_dependencies_merge_defaults_and_keep_hook_sources(tmp_path):
    """A diamond loads once, dependencies precede dependents, and user values win."""
    home = tmp_path / "home"
    a = _plugin(home, "a", config={"a": {"x": 1, "nested": {"left": 1}, "nodes": [1, 2]}})
    _plugin(home, "b", requires=["a"], config={"a": {"x": 2, "nested": {"right": 2}}})
    _plugin(home, "c", requires=["a"])
    d = _plugin(
        home,
        "d",
        requires=["b", "c"],
        config={
            "a": {
                "run_hooks": {
                    "before_run": [{"entrypoint": "hooks.py:apply"}],
                }
            }
        },
    )
    runtime = _runtime(tmp_path, ["d"], {"a": {"x": 3, "nodes": [4]}})
    assert runtime.settings.plugins.enabled == ("a", "b", "c", "d")
    value = runtime.settings.plugins.config.get("a")
    assert value.get("x") == 3
    assert value.get("nested") == {"left": 1, "right": 2}
    assert value.get("nodes") == [4]
    assert runtime.extensions.run_hooks.get("a")[0].base_dir == d.resolve()
    assert any(item.path == a / "defaults.json" for item in runtime.report.configs)


def test_dependency_errors_and_paths_are_checked(tmp_path):
    """Missing dependencies, cycles and escaping config paths fail before Python imports."""
    home = tmp_path / "home"
    a = _plugin(home, "a", requires=["b"])
    with pytest.raises(ValueError, match="not found"):
        _runtime(tmp_path, ["a"])
    _plugin(home, "b", requires=["a"])
    with pytest.raises(ValueError, match="a -> b -> a"):
        _runtime(tmp_path, ["a"])
    (a.parent / "outside.json").write_text("{}")
    (a / ".plugin.json").write_text(json.dumps({"id": "a", "defaults_file": "../outside.json"}))
    with pytest.raises(ValueError, match="escapes"):
        _runtime(tmp_path, ["a"])


def test_refresh_rebuilds_defaults_and_tracks_external_hook_source(tmp_path):
    """Refresh does not accumulate Hooks twice and watches user-declared handler files."""
    p = _plugin(tmp_path / "home", "a", config={"a": {"x": 1}})
    hook = tmp_path / "hook.py"
    hook.write_text("def handle(state, runtime, *, params):\n    return None\n")
    runtime = _runtime(
        tmp_path, ["a"], {"a": {"run_hooks": {"before_run": [{"entrypoint": "hook.py:handle"}]}}}
    )
    old = extension_revision(runtime)
    hook.write_text(hook.read_text() + "# changed\n")
    assert extension_revision(runtime) != old
    (p / "defaults.json").write_text(json.dumps({"plugins": {"config": {"a": {"x": 2}}}}))
    updated = refresh_extensions(runtime)
    assert updated.settings.plugins.config.get("a").get("x") == 2
    assert len(updated.extensions.run_hooks.get("a")) == 1
    assert runtime.settings.plugins.config.get("a").get("x") == 1


def test_context_exposes_copied_config_and_bound_session(tmp_path):
    """Plugin writes to returned config cannot affect another invocation or session."""
    _plugin(tmp_path / "home", "a", config={"a": {"nested": {"value": 1}}})
    runtime = _runtime(tmp_path, ["a"])
    a = AgentContext(runtime, session=runtime.paths.for_session("a"))
    b = AgentContext(runtime, session=runtime.paths.for_session("b"))
    a.get_plugin_config("a").get("nested").update(value=9)
    assert b.get_plugin_config("a").get("nested").get("value") == 1
    assert a.session.outputs != b.session.outputs
    assert a.model_dump() == {}


async def test_run_hook_updates_replace_results_and_cannot_erase_failure():
    """Updates preserve unrelated input; after_run replaces success but cannot hide errors."""

    def before(state, runtime, *, params):
        state.get("nested").append(2)
        return {"schema_str": ""}

    def after(state, runtime, *, params):
        return {"artifact": "result.sql"}

    value = {"question": "q", "nested": [1]}
    updated = await RunHook("before_run", before, {}, None).ainvoke(value)
    assert value.get("nested") == updated.get("nested") == [1]
    assert updated.get("schema_str") == ""
    hook = RunHook("after_run", after, {}, None)
    assert await hook.ainvoke({"sql": "select 1"}) == {"artifact": "result.sql"}
    with pytest.raises(ValueError, match="failed"):
        await hook.ainvoke({"error": "backend failed"})


async def test_native_tool_hook_replaces_request_and_preserves_command_state():
    """Native replacement reaches execution while correlated state updates survive."""
    request = ToolCallRequest(
        tool_call={"name": "write_file", "id": "one", "args": {}},
        tool=None,
        state={},
        runtime=SimpleNamespace(),
    )

    def before(request, *, params):
        return request.override(tool_call={**request.tool_call, "args": {"content": "changed"}})

    def after(request, result, *, params):
        return Command(
            update={
                **result.update,
                "messages": [ToolMessage(content="checked", tool_call_id="one")],
            }
        )

    async def execute(request):
        assert request.tool_call.get("args").get("content") == "changed"
        return Command(
            update={"extra": 7, "messages": [ToolMessage(content="written", tool_call_id="one")]}
        )

    pre = compile_hook(
        HookSpec(event="before_tool", entrypoint="test.py:before"), before, name="pre"
    )
    post = compile_hook(
        HookSpec(event="after_tool", entrypoint="test.py:after"), after, name="post"
    )

    async def invoke(request):
        return await pre.awrap_tool_call(request, execute)

    result = await post.awrap_tool_call(request, invoke)
    assert result.update.get("extra") == 7
    assert result.update.get("messages")[0].content == "checked"


async def test_before_tool_can_return_a_correctable_error_without_executing():
    """A guard can stop a file write without aborting the Agent or fabricating success."""

    def reject(request, *, params):
        return ToolMessage(content="Use the NL2SQL task", tool_call_id="one", status="error")

    async def unexpected(request):
        raise AssertionError("Blocked tool must not execute")

    request = ToolCallRequest(
        tool_call={"name": "write_file", "id": "one", "args": {}},
        tool=None,
        state={},
        runtime=SimpleNamespace(),
    )
    hook = compile_hook(
        HookSpec(event="before_tool", entrypoint="test.py:reject"), reject, name="guard"
    )
    result = await hook.awrap_tool_call(request, unexpected)
    assert result.status == "error" and result.tool_call_id == "one"


async def test_nl2sql_perceptor_preserves_explicit_context_including_empty_values():
    """Task fields take precedence without semantic calls or configured-file fallback."""
    from dataagent.extensions.loading import PythonLoader

    root = Path(__file__).resolve().parents[1] / "builtin-plugins/nl2sql"
    loader = PythonLoader()
    config_type = loader.load(root, "nl2sql/config_manager.py:NL2SQLConfig")
    perceptor = loader.load(root, "nl2sql/nodes/perceptor.py:PerceptorNode")
    default_state = loader.load(root, "nl2sql/state.py:get_default_state")
    config = config_type(
        {
            "model": "primary",
            "database": {"dialect": "hive"},
            "nodes": ["perceptor", "generator", "validator", "reflector"],
            "executor": {},
        }
    )
    assert "executor" not in config.enabled_nodes
    node = perceptor(config=config, user_schema="missing.md", user_evidence="missing.md")
    state = default_state(
        question="q",
        schema_str="",
        evidence="",
        sql_rules="from hook",
        context_overrides=["schema_str", "evidence", "sql_rules"],
    )
    result = await node.run(state)
    assert result.get("schema_str") == result.get("evidence") == ""
    assert result.get("sql_rules") == "from hook"


def test_plugin_defaults_cannot_change_host_settings(tmp_path):
    """Config fragments cannot recursively enable plugins or override model credentials."""
    root = _plugin(tmp_path / "home", "a", config={})
    (root / "defaults.json").write_text(json.dumps({"plugins": {"enabled": ["b"]}}))
    with pytest.raises(ValueError, match="only contribute plugins.config"):
        _runtime(tmp_path, ["a"])


def test_run_hooks_without_a_compiled_subagent_fail_instead_of_being_ignored(tmp_path):
    from conftest import ScriptedModel

    from dataagent.lowlevel import build_graph

    _plugin(tmp_path / "home", "a")
    hook = tmp_path / "hook.py"
    hook.write_text("def handle(value, runtime, *, params):\n    return None\n")
    runtime = _runtime(
        tmp_path, ["a"], {"a": {"run_hooks": {"before_run": [{"entrypoint": "hook.py:handle"}]}}}
    )
    with pytest.raises(ValueError, match=r"plugins\.config\.a\.run_hooks needs a compiled SubAgent"):
        build_graph(runtime, model=ScriptedModel(responses=[]))


def test_subagent_services_expose_only_factory_inputs():
    from dataclasses import fields

    from dataagent import SubagentServices

    assert [field.name for field in fields(SubagentServices)] == [
        "get_model", "semantic_client", "outputs_dir", "run_hooks",
    ]
