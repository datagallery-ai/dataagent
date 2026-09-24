"""Shared plugin/tool/subagent identifier boundaries survive the Hook protocol change."""

import re

import pytest
from pydantic import ValidationError

from dataagent.declarations import LOCAL_ID_PATTERN, RESOURCE_ID_PATTERN, PluginSpec, SubAgentSpec
from dataagent.extensions import compile_extensions


@pytest.mark.parametrize("name,valid", [
    ("a", True), ("snake_case", True), ("a0", True), ("hyphen-name", False),
    ("has.dot", False), ("Upper", False), ("0name", False), ("", False),
])
def test_tool_ids_match_local_pattern(tmp_path, name, valid):
    assert bool(re.fullmatch(LOCAL_ID_PATTERN, name)) is valid
    (tmp_path / "extension.py").write_text('def handle(value: int):\n    """Echo an integer."""\n    return value\n')
    spec = PluginSpec.model_validate({"id": "ids", "tools": {
        name: {"entrypoint": "extension.py:handle"},
    }})
    if valid:
        kwargs = compile_extensions([(tmp_path, spec)])
        assert kwargs["tools"][0].name == f"ids__{name}"
    else:
        with pytest.raises(ValueError) as caught:
            compile_extensions([(tmp_path, spec)])
        assert str(caught.value) == f"Invalid local tool ID: {name}"


@pytest.mark.parametrize("name,valid", [
    ("a", True), ("snake_case", True), ("a0", True), ("hyphen-name", True),
    ("has.dot", False), ("Upper", False), ("0name", False), ("", False),
])
def test_plugin_and_subagent_ids_share_resource_pattern(name, valid):
    assert bool(re.fullmatch(RESOURCE_ID_PATTERN, name)) is valid
    for model, data in [(PluginSpec, {"id": name}), (SubAgentSpec, {
        "name": name, "description": "Example", "system_prompt": "prompt.md",
    })]:
        if valid:
            model.model_validate(data)
        else:
            with pytest.raises(ValidationError) as caught:
                model.model_validate(data)
            assert caught.value.errors()[0]["type"] == "string_pattern_mismatch"
