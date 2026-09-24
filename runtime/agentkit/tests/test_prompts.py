"""Host prompt rendering preserves path policy and keeps inserted instructions opaque."""

from importlib.resources import files
from pathlib import Path
from string import Formatter
from unittest.mock import Mock

import pytest
from conftest import ScriptedModel

from dataagent.agent import build_agent
from dataagent.bootstrap.paths import WorkspaceInput
from dataagent.prompts import render_agent_prompt, render_filesystem_prompt, render_subagent_prompt


@pytest.mark.parametrize("workspaces", [(), (
    WorkspaceInput("first", Path("/data/中文 {input}")),
    WorkspaceInput("second", Path("/data/second")),
)])
@pytest.mark.parametrize("instructions", ["", 'External {unresolved} {filesystem_instructions} {"a": 1}\n'])
def test_templates_preserve_path_policy_and_external_instructions(workspaces, instructions):
    outputs = Path("/home/session {output}/outputs")
    filesystem = render_filesystem_prompt(outputs=outputs, workspaces=workspaces)
    assert str(outputs) in filesystem
    expected_inputs = "".join(f"- {item.name}: {item.path}\n" for item in workspaces)
    inputs = filesystem.split("no external inputs are configured):\n", 1)[1].split("\nPath and file handling:", 1)[0]
    assert inputs == expected_inputs
    assert "Use absolute paths in file tools" in filesystem
    assert "verify the target path before retrying" in filesystem
    assert "Each `execute` call starts in the output directory" in filesystem
    root = render_agent_prompt(
        filesystem_instructions=filesystem, extension_instructions=instructions,
    )
    before, section = root.split("## Enabled extension instructions\n\n", 1)
    inserted, after = section.split("\n\n## Verification and error handling", 1)
    assert inserted == instructions  # Empty sections and external trailing newlines stay intact.
    assert filesystem in before
    assert "## Delivery" in after
    assert root.startswith("# DataAgent\n")
    child = render_subagent_prompt(
        filesystem_instructions=filesystem, subagent_instructions=instructions,
    )
    assert filesystem in child
    assert child.split("## Role-specific instructions", 1)[1] == (f"\n\n{instructions}" if instructions else "")
    assert "parent's full conversation" in child
    assert "Parent and sibling agents may share" in child
    assert "## Handoff" in child


def test_data_task_prompt_has_only_explicit_injection_slots():
    template = files("dataagent.prompts").joinpath("agent.md").read_text(encoding="utf-8")
    fields = [field for _, field, _, _ in Formatter().parse(template) if field is not None]
    assert fields == ["filesystem_instructions", "extension_instructions"]
    # Do not inherit QwenPaw's unavailable task-graph or report-builder interfaces.
    for unsupported in ("create_plan", "update_subtask", "spawn_subagent", "bi-report-generation"):
        assert unsupported not in template
    for section in (
        "Available capabilities", "Workflow", "Files and execution environment",
        "Enabled extension instructions", "Verification and error handling", "Delivery",
    ):
        assert f"## {section}" in template


def test_subagent_without_instructions():
    rendered = render_subagent_prompt(filesystem_instructions="Paths")
    assert "## Files and execution\n\nPaths\n\n## Handoff" in rendered
    assert rendered.endswith("## Role-specific instructions")


@pytest.mark.parametrize("name, expected", [
    ("filesystem.md", ["output_directory", "input_workspaces"]),
    ("subagent.md", ["filesystem_instructions", "subagent_instructions"]),
])
def test_filesystem_and_subagent_keep_existing_injection_slots(name, expected):
    template = files("dataagent.prompts").joinpath(name).read_text(encoding="utf-8")
    fields = [field for _, field, _, _ in Formatter().parse(template) if field is not None]
    assert fields == expected


def test_agent_assembly_keeps_root_and_child_prompt_scopes(runtime, monkeypatch):
    factory = Mock()
    monkeypatch.setattr("dataagent.agent.create_deep_agent", factory)
    build_agent(runtime, model=ScriptedModel(responses=[]))
    kwargs = factory.call_args.kwargs
    filesystem = render_filesystem_prompt(
        outputs=runtime.paths.for_session("sdk").outputs, workspaces=runtime.paths.workspaces,
    )
    plugin = runtime.paths.home / "plugins/common"
    root_prompt = (plugin / "prompts/agent.md").read_text()
    child_prompt = (plugin / "prompts/general-purpose.md").read_text()
    assert kwargs["system_prompt"] == render_agent_prompt(
        filesystem_instructions=filesystem, extension_instructions=root_prompt,
    )
    child = next(item for item in kwargs["subagents"] if item["name"] == "general-purpose")
    assert child["system_prompt"] == render_subagent_prompt(
        filesystem_instructions=filesystem, subagent_instructions=child_prompt,
    )
    assert root_prompt not in child["system_prompt"]


def test_templates_are_package_resources():
    for name in ("agent.md", "subagent.md", "filesystem.md"):
        assert files("dataagent.prompts").joinpath(name).read_text(encoding="utf-8")
