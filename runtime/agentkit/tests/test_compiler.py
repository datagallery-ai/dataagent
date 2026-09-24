import inspect

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from pydantic import ValidationError

from dataagent.declarations import PluginSpec, SubAgentSpec
from dataagent.extensions import compile_extensions, contained_path, select_plugins


def compile_test(runtime, plugins=None):
    return compile_extensions(
        select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots)
        if plugins is None else plugins,
    )


def test_output_is_native_kwargs_without_host_policy(runtime):
    kwargs = compile_test(runtime)
    assert set(kwargs) <= set(inspect.signature(create_deep_agent).parameters)
    assert "callbacks" not in kwargs
    assert {child["name"] for child in kwargs["subagents"]} == {"general-purpose"}
    assert not {"model", "backend", "permissions", "checkpointer"} & set(kwargs)
    assert len(kwargs["middleware"]) == 6
    assert all(isinstance(item, AgentMiddleware) for item in kwargs["middleware"])
    assert kwargs["tools"][0].name == "common__summarize_numbers"


def test_unreferenced_hook_file_is_not_imported(runtime, tmp_path):
    (tmp_path / "unused.py").write_text("raise RuntimeError('must not import')")
    spec = PluginSpec.model_validate({"id": "unused"})
    compile_test(runtime, [(tmp_path, spec)])


def test_invalid_hook_reference_fails(runtime, tmp_path):
    spec = PluginSpec.model_validate({
        "id": "bad", "hooks": [{"event": "after_agent", "entrypoint": "missing.py:handle"}],
    })
    with pytest.raises(FileNotFoundError):
        compile_test(runtime, [(tmp_path, spec)])


def test_escape_and_symlink_rejected(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x=1")
    (root / "link.py").symlink_to(outside)
    for relative in ["../outside.py", "link.py"]:
        with pytest.raises(ValueError, match="escapes"):
            contained_path(root, relative)


def test_invalid_or_duplicate_skills_fail_instead_of_native_silent_skip(runtime, tmp_path):
    skill = tmp_path / "skills/summary/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Instructions without frontmatter")
    spec = PluginSpec.model_validate({"id": "example", "skills": ["skills"]})
    with pytest.raises(ValueError, match="Invalid Skill metadata"):
        compile_test(runtime, [(tmp_path, spec)])
    content = "---\nname: summary\ndescription: Summarize numbers\n---\nUse a tool.\n"
    skill.write_text(content)
    other = tmp_path / "other/summary/SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text(content)
    spec = spec.model_copy(update={"skills": ["skills", "other"]})
    with pytest.raises(ValueError, match="Duplicate Skill name"):
        compile_test(runtime, [(tmp_path, spec)])


@pytest.mark.parametrize("declaration", [
    {"schema_version": 1},
    {"hook_handlers": {}},
    {"hooks": [{"handler": "audit", "events": ["agent.after"]}]},
])
def test_old_plugin_configuration_requires_explicit_migration(declaration):
    with pytest.raises(ValidationError, match="Migrate plugin to schema_version: 2"):
        PluginSpec.model_validate({"id": "old", **declaration})


@pytest.mark.parametrize("schema,base", [
    (PluginSpec, {"id": "example"}),
    (SubAgentSpec, {"name": "example", "description": "Example", "system_prompt": "prompt.md"}),
])
@pytest.mark.parametrize("field", ["middleware", "callbacks"])
@pytest.mark.parametrize("value", [[], [{"entrypoint": "factory.py:create"}]])
def test_removed_factories_rejected_before_import(tmp_path, schema, base, field, value):
    (tmp_path / "factory.py").write_text("raise AssertionError('must not import')")
    with pytest.raises(ValidationError) as caught:
        schema.model_validate({**base, field: value})
    assert any(item["loc"] == (field,) and item["type"] == "extra_forbidden"
               for item in caught.value.errors())


@pytest.mark.parametrize("declaration", [
    {"event": "before_tool", "command": ["echo", "must not run"]},
    {"event": "model_error", "entrypoint": "hook.py:handle"},
    {"event": "tool_error", "entrypoint": "hook.py:handle"},
    {"event": "agent_error", "entrypoint": "hook.py:handle"},
])
def test_removed_command_and_error_hooks_fail_schema_validation(declaration):
    with pytest.raises(ValidationError):
        PluginSpec.model_validate({"id": "unsupported", "hooks": [declaration]})


def test_plain_python_hook_needs_no_langchain_and_duplicate_declarations_remain(tmp_path):
    (tmp_path / "plain.py").write_text(
        "def handle(state, runtime, *, params):\n"
        "    raise RuntimeError('Hook must not execute while compiling')\n"
    )
    entry = {"event": "before_agent", "entrypoint": "plain.py:handle"}
    spec = PluginSpec.model_validate({"id": "plain", "hooks": [entry, entry]})
    kwargs = compile_extensions([(tmp_path, spec)])
    hooks = kwargs["middleware"]
    assert len(hooks) == 2
    assert all(isinstance(item, AgentMiddleware) for item in hooks)
    assert [item.name for item in hooks] == [
        "ConfiguredHook__dataagent-v2__before_agent__0",
        "ConfiguredHook__dataagent-v2__before_agent__1",
    ]
    assert "callbacks" not in kwargs


def test_noncallable_python_hook_is_rejected(tmp_path):
    (tmp_path / "plain.py").write_text("handle = 42")
    spec = PluginSpec.model_validate({"id": "bad", "hooks": [{
        "event": "before_agent", "entrypoint": "plain.py:handle",
    }]})
    with pytest.raises(TypeError, match="plain.py:handle") as caught:
        compile_extensions([(tmp_path, spec)])
    assert str(tmp_path) in str(caught.value)


def test_invalid_plain_hook_signature_identifies_source(tmp_path):
    (tmp_path / "legacy.py").write_text("def handle(event, config): return None")
    spec = PluginSpec.model_validate({"id": "legacy", "hooks": [{
        "event": "before_model", "entrypoint": "legacy.py:handle",
    }]})
    with pytest.raises(TypeError, match="legacy.py:handle") as caught:
        compile_extensions([(tmp_path, spec)])
    assert str(tmp_path) in str(caught.value)
    assert "state, runtime, *, params" in str(caught.value)


def test_duplicate_plugin_ids_rejected_even_without_tools(tmp_path):
    spec = PluginSpec(id="duplicate")
    with pytest.raises(ValueError, match="Duplicate plugin ID"):
        compile_extensions([(tmp_path, spec), (tmp_path, spec)])


def test_python_hooks_are_path_confined(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (tmp_path / "outside.py").write_text("raise RuntimeError('must never import')")
    entry = {"entrypoint": "../outside.py:create", "event": "before_agent"}
    spec = PluginSpec.model_validate({"id": "bad", "hooks": [entry]})
    with pytest.raises(ValueError, match="escapes"):
        compile_extensions([(root, spec)])
