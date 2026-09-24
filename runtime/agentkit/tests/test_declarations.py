"""Shared declarations preserve the JSON contract without redefining native tool schemas."""

from typing import get_args

import pytest
from pydantic import ValidationError

from dataagent.declarations import HookSpec, PluginSpec, SubAgentSpec, ToolSpec
from dataagent.settings import AgentSettings


def test_product_plugin_and_subagent_use_the_same_hook_declaration():
    for model in (AgentSettings, PluginSpec, SubAgentSpec):
        assert get_args(model.model_fields["hooks"].annotation)[0] is HookSpec


def test_tool_declaration_preserves_entrypoint_only_contract():
    assert set(ToolSpec.model_fields) == {"entrypoint"}
    plugin = PluginSpec.model_validate({
        "id": "example", "tools": {"count": {"entrypoint": "tools.py:count"}},
    })
    assert isinstance(plugin.tools["count"], ToolSpec)
    assert plugin.model_dump()["tools"] == {"count": {"entrypoint": "tools.py:count"}}
    for invalid in ({}, {"entrypoint": ""}, {"entrypoint": "tools.py:count", "parameters": {}}):
        with pytest.raises(ValidationError):
            ToolSpec.model_validate(invalid)


@pytest.mark.parametrize("model,base", [
    (AgentSettings, {}), (PluginSpec, {"id": "example"}),
    (SubAgentSpec, {"name": "example", "description": "Example", "system_prompt": "Help"}),
])
def test_grouped_hooks_share_the_same_internal_declaration(model, base):
    parsed = model.model_validate({**base, "hooks": {
        "before_agent": [],
        "before_model": [{"entrypoint": "audit.py:handle", "params": {"label": "first"}}],
        "before_tool": [{"entrypoint": "guard.py:handle", "matcher": "common__summarize_numbers"}],
    }})
    assert [hook.event for hook in parsed.hooks] == ["before_model", "before_tool"]
    assert parsed.hooks[0].params == {"label": "first"}
    assert parsed.hooks[1].matcher == "common__summarize_numbers"


@pytest.mark.parametrize("hooks", [
    {"unknown": []}, {"before_model": {}}, {"before_model": ["audit.py:handle"]},
    {"before_model": [{"event": "before_model", "entrypoint": "audit.py:handle"}]},
    {"before_model": [{"entrypoint": "audit.py:handle", "matcher": "tool"}]},
    {"before_tool": [{"entrypoint": "audit.py:handle", "matcher": " "}]},
])
def test_invalid_grouped_hooks_fail_explicitly(hooks):
    with pytest.raises(ValueError):
        AgentSettings.model_validate({"hooks": hooks})
