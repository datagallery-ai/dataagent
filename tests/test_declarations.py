"""Shared declarations preserve the JSON contract without redefining native tool schemas."""

from typing import get_args

import pytest
from pydantic import ValidationError

from dataagent.config.declarations import HookSpec, PluginSpec, SubAgentSpec, ToolSpec
from dataagent.config.settings import AgentSettings


def test_product_plugin_and_subagent_use_the_same_hook_declaration():
    for model in (AgentSettings, PluginSpec, SubAgentSpec):
        assert get_args(model.model_fields["hooks"].annotation)[0] is HookSpec


def test_tool_declaration_keeps_native_arguments_and_defaults_to_all_agents():
    """Visibility does not introduce a second tool argument schema or change default exposure."""
    assert set(ToolSpec.model_fields) == {"entrypoint", "scope"}
    plugin = PluginSpec.model_validate(
        {
            "id": "example",
            "tools": {"count": {"entrypoint": "tools.py:count"}},
        }
    )
    assert isinstance(plugin.tools.get("count"), ToolSpec)
    assert plugin.model_dump().get("tools") == {
        "count": {"entrypoint": "tools.py:count", "scope": "all"}
    }
    for invalid in (
        {},
        {"entrypoint": ""},
        {"entrypoint": "tools.py:count", "parameters": {}},
        {"entrypoint": "tools.py:count", "scope": "unknown"},
    ):
        with pytest.raises(ValidationError):
            ToolSpec.model_validate(invalid)


@pytest.mark.parametrize(
    "model,base",
    [
        (AgentSettings, {}),
        (PluginSpec, {"id": "example"}),
        (SubAgentSpec, {"name": "example", "description": "Example", "system_prompt_file": "Help"}),
    ],
)
def test_grouped_hooks_share_the_same_internal_declaration(model, base):
    parsed = model.model_validate(
        {
            **base,
            "hooks": {
                "before_agent": [],
                "before_model": [{"entrypoint": "audit.py:handle", "params": {"label": "first"}}],
                "before_tool": [
                    {"entrypoint": "guard.py:handle", "matcher": "common__summarize_numbers"}
                ],
            },
        }
    )
    assert [hook.event for hook in parsed.hooks] == ["before_model", "before_tool"]
    assert parsed.hooks[0].params == {"label": "first"}
    assert parsed.hooks[1].matcher == "common__summarize_numbers"


@pytest.mark.parametrize(
    "hooks",
    [
        {"unknown": []},
        {"before_model": {}},
        {"before_model": ["audit.py:handle"]},
        {"before_model": [{"event": "before_model", "entrypoint": "audit.py:handle"}]},
        {"before_model": [{"entrypoint": "audit.py:handle", "matcher": "tool"}]},
        {"before_tool": [{"entrypoint": "audit.py:handle", "matcher": " "}]},
    ],
)
def test_invalid_grouped_hooks_fail_explicitly(hooks):
    with pytest.raises(ValueError):
        AgentSettings.model_validate({"hooks": hooks})


@pytest.mark.parametrize(
    "model,data",
    [
        (PluginSpec, {"schema_version": 2, "id": "sample"}),
        (
            SubAgentSpec,
            {
                "schema_version": 2,
                "name": "child",
                "description": "x",
                "system_prompt_file": "p.md",
            },
        ),
    ],
)
def test_unsupported_schema_version_is_rejected(model, data):
    with pytest.raises(ValidationError, match="schema_version"):
        model.model_validate(data)


def test_old_field_names_are_rejected():
    for model, data in [
        (PluginSpec, {"id": "old", "system_prompt": "p.md"}),
        (PluginSpec, {"id": "old", "config": "defaults.yaml"}),
        (SubAgentSpec, {"name": "child", "description": "x", "system_prompt": "p.md"}),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate(data)
