"""Declared names share NAME_PATTERN; caller-chosen IDs share OPAQUE_ID_PATTERN."""

import re

import pytest
from pydantic import ValidationError

from dataagent.config.declarations import NAME_PATTERN, OPAQUE_ID_PATTERN, PluginSpec, SubAgentSpec
from dataagent.extensions import compile_extensions


@pytest.mark.parametrize(
    "name,valid",
    [
        ("a", True),
        ("snake_case", True),
        ("a0", True),
        ("hyphen-name", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("has.dot", False),
        ("Upper", False),
        ("0name", False),
        ("", False),
    ],
)
def test_tool_ids_match_the_name_pattern(tmp_path, name, valid):
    assert bool(re.fullmatch(NAME_PATTERN, name)) is valid
    (tmp_path / "extension.py").write_text(
        'def handle(value: int):\n    """Echo an integer."""\n    return value\n'
    )
    spec = PluginSpec.model_validate(
        {
            "id": "ids",
            "tools": {
                name: {"entrypoint": "extension.py:handle"},
            },
        }
    )
    if valid:
        kwargs = compile_extensions([(tmp_path, spec)])
        assert kwargs["tools"][0].name == f"ids__{name}"
    else:
        with pytest.raises(ValueError) as caught:
            compile_extensions([(tmp_path, spec)])
        assert str(caught.value) == f"Invalid local tool ID: {name}"


@pytest.mark.parametrize(
    "name,valid",
    [
        ("a", True),
        ("snake_case", True),
        ("a0", True),
        ("hyphen-name", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("has.dot", False),
        ("Upper", False),
        ("0name", False),
        ("", False),
    ],
)
def test_plugin_and_subagent_ids_share_the_name_pattern(name, valid):
    assert bool(re.fullmatch(NAME_PATTERN, name)) is valid
    for model, data in [
        (PluginSpec, {"id": name}),
        (
            SubAgentSpec,
            {
                "name": name,
                "description": "Example",
                "system_prompt_file": "prompt.md",
            },
        ),
    ]:
        if valid:
            model.model_validate(data)
        else:
            with pytest.raises(ValidationError) as caught:
                model.model_validate(data)
            assert caught.value.errors()[0]["type"] == "string_pattern_mismatch"


def test_settings_reuse_the_two_patterns():
    from dataagent.bootstrap.paths import opaque_id
    from dataagent.config.settings import A2AAgentSettings, WorkspaceSettings

    A2AAgentSettings(id="remote-agent", url="http://x")
    with pytest.raises(ValidationError):
        A2AAgentSettings(id="Remote", url="http://x")
    WorkspaceSettings(name="Sales-2026", path="/tmp")
    with pytest.raises(ValidationError):
        WorkspaceSettings(name="../../evil name!", path="/tmp")
    assert opaque_id("3f2c9b1e-uuid", "thread id") == "3f2c9b1e-uuid"
    assert re.fullmatch(OPAQUE_ID_PATTERN, "A" * 129) is None
