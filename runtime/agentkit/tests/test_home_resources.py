"""Real native graph checks for automatic resources and native file access."""


from unittest.mock import patch

import pytest
from conftest import ScriptedModel, call
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_launch import MODEL, config, options

from dataagent import safe_error
from dataagent.agent import build_agent
from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.declarations import PluginSpec
from dataagent.extensions import select_plugins


def skill(root, name, content="Read this independent Skill."):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Independent test skill\n---\n{content}\n")
    return path


def compile_runtime(runtime):
    with patch("dataagent.agent.create_deep_agent", wraps=create_deep_agent) as create:
        build_agent(runtime, model=ScriptedModel(responses=[AIMessage(content="ok")]))
    return create.call_args.kwargs


def test_fresh_home_has_no_default_test_plugin(tmp_path):
    runtime = prepare_runtime(options(tmp_path, init_home=True))
    assert runtime.settings.plugins.enabled == ()
    assert not (runtime.paths.home / "plugins/common").exists()
    kwargs = compile_runtime(runtime)
    assert kwargs["tools"] == kwargs["subagents"] == kwargs["skills"] == []


def test_installed_common_requires_explicit_enablement(tmp_path, common_plugin):
    runtime = prepare_runtime(options(tmp_path))
    assert common_plugin in runtime.extensions.plugin_roots
    kwargs = compile_runtime(runtime)
    assert kwargs["tools"] == kwargs["subagents"] == kwargs["skills"] == []


@pytest.mark.parametrize("delegate", [False, True])
async def test_loose_skills_have_distinct_labels_and_are_actually_read(tmp_path, delegate, common_plugin):
    user = skill(tmp_path / "home/skills", "personal", "User skill evidence 42.")
    skill(tmp_path / ".dataagent/skills", "project-guide", "Ignored cwd skill evidence 73.")
    config(tmp_path / "home/config.json", {"plugins": {"enabled": ["common"]}})
    runtime = prepare_runtime(options(tmp_path))
    kwargs = compile_runtime(runtime)
    assert [label for _, label in kwargs["skills"]] == ["common", "User"]
    general, = kwargs["subagents"]
    assert general["skills"] == kwargs["skills"]
    responses = [call("read_file", {"file_path": str(user)}), AIMessage(content="Read the Skill.")]
    if delegate:
        responses = [call("task", {"subagent_type": "general-purpose", "description": "Read personal Skill."}),
                     *responses, AIMessage(content="Delegate finished.")]
    model = ScriptedModel(responses=responses)
    await build_agent(runtime, model=model).ainvoke({"messages": [HumanMessage(content="Read Skill")]})
    prompts = "\n".join(str(request[0].content) for request in model.requests)
    assert "User" in prompts and "project-guide" not in prompts
    assert any(isinstance(message, ToolMessage) and "User skill evidence 42" in str(message.content)
               for request in model.requests for message in request)


def test_skill_symlinks_dedup_and_same_name_conflict(tmp_path):
    shared = tmp_path / "shared"
    skill(shared, "shared-guide")
    (tmp_path / "home/skills").mkdir(parents=True)
    (tmp_path / "home/skills/shared-guide").symlink_to(shared / "shared-guide", target_is_directory=True)
    (tmp_path / "home/skills/alias").symlink_to(shared / "shared-guide", target_is_directory=True)
    runtime = prepare_runtime(options(tmp_path))
    compile_runtime(runtime)
    # Distinct files with the same parsed name must not silently override one another.
    (tmp_path / "home/skills/alias").unlink()
    duplicate = tmp_path / "home/skills/duplicate/SKILL.md"
    duplicate.parent.mkdir()
    duplicate.write_text((shared / "shared-guide/SKILL.md").read_text())
    with pytest.raises(ValueError, match="Duplicate Skill name"):
        compile_runtime(runtime)


def test_disabled_plugins_hidden_dirs_and_unreferenced_hooks_are_not_imported(tmp_path):
    config(tmp_path / "home/config.json", MODEL)
    (tmp_path / "home/hooks").mkdir()
    (tmp_path / "home/hooks/unused.py").write_text("raise RuntimeError('must not import')")
    for name in ("broken", ".hidden"):
        root = tmp_path / "home/plugins" / name
        root.mkdir(parents=True)
        (root / ".plugin.json").write_text("broken: [")
    (tmp_path / "home/plugins/ordinary-file").touch()
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path))
    # Builtin plugin directories come first, then user ones; hidden and non-directory
    # entries are ignored and disabled manifests are never parsed.
    user_roots = [root.name for root in runtime.extensions.plugin_roots
                  if root.is_relative_to(tmp_path)]
    assert user_roots == ["broken"]
    assert runtime.extensions.plugin_roots[0].parent == runtime.paths.builtin_plugins
    compile_runtime(runtime)


def test_plugin_conflicts_and_enabled_order(tmp_path):
    for name in ("first", "second"):
        config(tmp_path / "home/plugins" / name / ".plugin.json", {"id": name})
    cli = config(tmp_path / "config.json", {**MODEL, "plugins": {"enabled": ["second", "first"]}})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    assert [spec.id for _, spec in select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots)] == ["second", "first"]
    config(tmp_path / "external/first/.plugin.json", {"id": "first"})
    config(cli, {**MODEL, "plugins": {"enabled": ["first"], "paths": ["./external/first"]}})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    with pytest.raises(ValueError, match="exactly one directory") as error:
        compile_runtime(runtime)
    assert str(tmp_path / "home/plugins/first") in str(error.value)
    assert str(tmp_path / "external/first") in str(error.value)


def test_plugin_candidate_origins_keep_enabled_order_and_unique_scope_labels(tmp_path):
    for scope in ("home", ".dataagent"):
        (tmp_path / scope / "plugins/common").mkdir(parents=True)
    explicit_common = tmp_path / "external/common"
    explicit_custom = tmp_path / "external/custom"
    explicit_common.mkdir(parents=True)
    explicit_custom.mkdir()
    config(explicit_custom / ".plugin.json", {"id": "custom"})
    cli = config(tmp_path / "config.json", {**MODEL, "plugins": {
        "enabled": ["custom", "common"],
        "paths": [str(explicit_common), str(explicit_common), str(explicit_custom)],
    }})

    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    assert runtime.report.plugin_origins == (
        ("custom", ("explicit",)),
        ("common", ("user", "explicit")),
    )
    # These are candidate categories, not a record of successful plugin compilation.
    with pytest.raises(ValueError, match="exactly one directory"):
        select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots)


@pytest.mark.parametrize("name", ["user", "cli"])
def test_reserved_plugin_names(name):
    with pytest.raises(ValueError, match="Reserved plugin ID"):
        PluginSpec(id=name)


async def test_hook_sources_accumulate_and_repeated_registrations_all_execute(tmp_path):
    output = tmp_path / "observed.txt"
    for scope, root in [("user", tmp_path / "home"), ("cli", tmp_path)]:
        hook = root / "hooks/audit.py"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            f"from pathlib import Path\nasync def handle(state, runtime, *, params):\n"
            f"    with Path({str(output)!r}).open('a') as stream: stream.write({scope!r} + '\\n')\n"
        )
        config(root / "config.json", {**(MODEL if scope == "user" else {}), "dataagent": {
            "hooks": [{"entrypoint": "hooks/audit.py:handle", "event": "before_agent"},
                      {"entrypoint": "hooks/audit.py:handle", "event": "before_agent"}],
        }})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=tmp_path / "config.json"))
    await build_agent(runtime, model=ScriptedModel(responses=[AIMessage(content="ok")])).ainvoke({
        "messages": [HumanMessage(content="hello")],
    })
    assert output.read_text().splitlines() == ["user", "user", "cli", "cli"]


@pytest.mark.parametrize("transport", ["sdk", "http"])
async def test_accumulated_hooks_keep_native_before_and_after_order(tmp_path, transport):
    output = tmp_path / "events.txt"
    events = ("before_agent", "after_agent", "before_model", "after_model", "before_tool", "after_tool")
    for scope, root in [("user", tmp_path / "home"), ("cli", tmp_path)]:
        hook = root / "hooks/audit.py"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            "from pathlib import Path\n"
            "def record(params):\n"
            f"    with Path({str(output)!r}).open('a') as stream:\n"
            f"        stream.write(params['event'] + ':{scope}\\n')\n"
            "def state_hook(state, runtime, *, params): record(params)\n"
            "def before_tool(request, *, params): record(params)\n"
            "def after_tool(request, result, *, params): record(params)\n"
        )
        config(root / "config.json", {**(MODEL if scope == "user" else {}), "dataagent": {
            "hooks": [{"event": event, "params": {"event": event},
                       "entrypoint": "hooks/audit.py:" + (event if event.endswith("_tool") else "state_hook")}
                      for event in events],
        }})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=tmp_path / "config.json"))
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1, 2, 3]}), AIMessage(content="Sum: 6"),
    ])
    if transport == "sdk":
        await build_agent(runtime, model=model).ainvoke({"messages": [HumanMessage(content="Sum numbers")]})
    else:
        from test_api import client_for, query, sse, terminal

        async with client_for(runtime, model) as (client, app):
            assert terminal(sse(await client.post("/dataagent/stream", json=query("Sum numbers")))) == ["RUN_FINISHED"]
    phases = ("before_agent", "before_model", "after_model", "before_tool", "after_tool",
              "before_model", "after_model", "after_agent")
    assert output.read_text().splitlines() == [
        f"{event}:{scope}" for event in phases
        for scope in (("user", "cli") if event.startswith("before_") else ("cli", "user"))
    ]


@pytest.mark.parametrize("upper_hooks", [None, []])
async def test_missing_or_empty_upper_hooks_do_not_disable_user_hook(tmp_path, upper_hooks):
    output = tmp_path / "observed.txt"
    home = tmp_path / "home"
    home.mkdir()
    (home / "audit.py").write_text(
        "from pathlib import Path\ndef handle(state, runtime, *, params):\n"
        f"    Path({str(output)!r}).write_text('user hook executed')\n"
    )
    config(home / "config.json", {**MODEL, "dataagent": {"hooks": [{
        "entrypoint": "audit.py:handle", "event": "before_agent",
    }]}})
    upper = {"dataagent": {"hooks": upper_hooks}} if upper_hooks is not None else {}
    cli = config(tmp_path / "config.json", upper)
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    await build_agent(runtime, model=ScriptedModel(responses=[AIMessage(content="ok")])).ainvoke({
        "messages": [HumanMessage(content="test")],
    })
    assert output.read_text() == "user hook executed"


def test_standalone_skills_accumulate_even_when_plugins_are_disabled(tmp_path):
    user = skill(tmp_path / "home/skills", "user-guide")
    skill(tmp_path / ".dataagent/skills", "project-guide")
    cli = config(tmp_path / "config.json", {**MODEL, "plugins": {"enabled": []},
                                               "dataagent": {"hooks": []}})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    assert compile_runtime(runtime)["skills"] == [
        (str(user.parent.parent), "User"),
    ]


@pytest.mark.parametrize("entry", ["../outside.py:handle", "hooks/link.py:handle"])
def test_subscribed_loose_hook_cannot_escape_source_root(tmp_path, entry):
    outside = tmp_path / "outside.py"
    outside.write_text("raise RuntimeError('must not import')\n")
    home = tmp_path / "home"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks/link.py").symlink_to(outside)
    config(home / "config.json", {**MODEL, "dataagent": {
        "hooks": [{"entrypoint": entry, "event": "before_agent"}],
    }})
    with pytest.raises(ValueError, match="escapes"):
        compile_runtime(prepare_runtime(LaunchOptions(cwd=tmp_path)))


@pytest.mark.parametrize("agent", ["root", "general-purpose"])
async def test_native_file_tools_read_home_files_with_glob_characters(tmp_path, monkeypatch, agent):
    # Synthetic config/env data: native tools do not enforce an access whitelist.
    workspace = tmp_path / "中文 space {project}[1]"
    workspace.mkdir()
    home = tmp_path / "用户 home {keys}[2]"
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    config(home / "config.json", MODEL)
    (home / ".env").write_text("SECRET_MARKER=secret-content\n")
    (home / "public.txt").write_text("public-evidence\n")
    runtime = prepare_runtime(LaunchOptions(cwd=workspace, workspace=home))
    calls = [
        call("read_file", {"file_path": str(home / ".env")}),
        call("read_file", {"file_path": str(home / "config.json")}),
        call("ls", {"path": str(home)}),
        call("glob", {"pattern": "*", "path": str(home)}),
        call("grep", {"pattern": "public-evidence", "path": str(home)}),
        call("read_file", {"file_path": str(home / "public.txt")}),
        AIMessage(content="finished"),
    ]
    if agent != "root":
        calls = [call("task", {"subagent_type": agent, "description": "Check local files"}),
                 *calls, AIMessage(content="finished")]
    model = ScriptedModel(responses=calls)
    await build_agent(runtime, model=model).ainvoke({"messages": [HumanMessage(content="Check files")]})
    messages = [message for request in model.requests for message in request if isinstance(message, ToolMessage)]
    assert messages
    assert any("secret-content" in str(message.content) for message in messages)
    assert all(message.status == "success" for message in messages if message.name == "read_file")
    for name in ("ls", "glob", "grep"):
        results = [str(message.content) for message in messages if message.name == name]
        assert results and any("public.txt" in value for value in results)
        if name != "grep":
            assert any("config.json" in value for value in results)
    assert any("public-evidence" in str(message.content) for message in messages)


def test_settings_can_serialize_without_losing_immutability(runtime):
    data = runtime.settings.model_dump(warnings="error")
    assert data["models"]["primary"]["name"] == "test-model"
    assert "test-secret" not in runtime.settings.model_dump_json(warnings="error")
    with pytest.raises(TypeError):
        runtime.settings.models["bad"] = None


def test_early_error_diagnostics_strip_terminal_controls():
    result = safe_error(ValueError("/中文/\x1b[31mbad\n\x07 secret"), ("secret",))
    assert result["message"] == "/中文/bad [REDACTED]"
