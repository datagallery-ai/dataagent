# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Workspace read/write policy and SysOperation split tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.core.deep_agent.builders.access import (
    WorkspaceAccessPolicy,
    build_sys_operations,
)
from dataagent.core.deep_agent.spec import SkillSpec
from dataagent.core.deep_agent.tool_builder import build_harness_tools


def _skill_spec(tmp_path: Path) -> SkillSpec:
    return SkillSpec(
        builtin_root=tmp_path / "builtin-skills",
        builtin_allowlist=frozenset(),
        custom_dirs=(tmp_path / "custom-skills",),
        user_root=tmp_path / "user-skills",
    )


def test_policy_builds_distinct_read_and_write_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    skills = _skill_spec(tmp_path)

    policy = WorkspaceAccessPolicy.from_config(
        {"WORKSPACE": {"allow_path": [str(allowed)]}},
        workspace_root=workspace,
        skills=skills,
    )

    assert policy.write_roots == (workspace,)
    assert policy.read_roots == (
        workspace,
        allowed,
        skills.builtin_root,
        skills.custom_dirs[0],
        skills.user_root,
    )
    assert policy.can_read(allowed / "data.csv")
    assert not policy.can_write(allowed / "data.csv")
    assert policy.can_write(workspace / "report.md")


def test_policy_rejects_relative_allow_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="relative path not allowed"):
        WorkspaceAccessPolicy.from_config(
            {"WORKSPACE": {"allow_path": ["relative"]}},
            workspace_root=tmp_path,
        )


def test_policy_resolves_symlinks_before_authorization(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "escape"
    link.symlink_to(outside, target_is_directory=True)

    policy = WorkspaceAccessPolicy.from_config({}, workspace_root=workspace)

    assert not policy.can_write(link / "file.txt")
    with pytest.raises(PermissionError, match="outside workspace"):
        policy.require_write(link / "file.txt")


def test_sys_operations_enforce_separate_sandbox_roots(tmp_path: Path) -> None:
    from openjiuwen.core.runner.runner import Runner

    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    policy = WorkspaceAccessPolicy.from_config(
        {"WORKSPACE": {"allow_path": [str(allowed)]}},
        workspace_root=workspace,
    )

    binding = build_sys_operations(policy, agent_name=f"access-{tmp_path.name}")

    assert binding.primary._run_config.restrict_to_sandbox is True
    assert binding.primary._run_config.sandbox_root == [str(workspace)]
    assert binding.read_only._run_config.restrict_to_sandbox is True
    assert binding.read_only._run_config.sandbox_root == [
        str(workspace),
        str(allowed),
    ]
    assert Runner.resource_mgr.get_sys_operation(binding.read_only_id) is None


def test_sys_operation_applies_bash_allowlist_and_fingerprints_it(tmp_path: Path) -> None:
    policy = WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path)

    restricted = build_sys_operations(
        policy,
        agent_name=f"bash-restricted-{tmp_path.name}",
        shell_allowlist=("echo", "pwd"),
    )
    other = build_sys_operations(
        policy,
        agent_name=f"bash-restricted-{tmp_path.name}",
        shell_allowlist=("echo",),
    )

    assert restricted.primary._run_config.shell_allowlist == ["echo", "pwd"]
    assert restricted.primary_id != other.primary_id


def test_harness_tools_route_only_read_tools_to_read_operation(tmp_path: Path) -> None:
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path),
        agent_name=f"tool-routing-{tmp_path.name}",
    )
    tools = build_harness_tools(
        binding.primary,
        read_sys_operation=binding.read_only,
    )
    operations = {
        tool.card.name: (
            getattr(tool, "operation", None)
            or getattr(tool, "_operation", None)
        )
        for tool in tools
    }

    for name in ("read_file", "glob", "grep", "list_files"):
        assert operations[name] is binding.read_only
    for name in ("write_file", "edit_file", "bash"):
        assert operations[name] is binding.primary


def test_harness_tools_use_unique_openjiuwen_implementations(tmp_path: Path) -> None:
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path),
        agent_name=f"tool-ownership-{tmp_path.name}",
    )
    tools = build_harness_tools(binding.primary, read_sys_operation=binding.read_only)
    tools_by_name = {tool.card.name: tool for tool in tools}

    expected_openjiuwen_tools = {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "list_files",
        "bash",
    }
    assert expected_openjiuwen_tools <= tools_by_name.keys()
    assert len(tools_by_name) == len(tools)
    for name in expected_openjiuwen_tools - {"bash"}:
        assert type(tools_by_name[name]).__module__.startswith("openjiuwen.harness.tools.")
    from openjiuwen.harness.tools.shell import BashTool

    assert isinstance(tools_by_name["bash"], BashTool)


def test_harness_todo_tools_use_configured_workspace(tmp_path: Path) -> None:
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path),
        agent_name=f"todo-workspace-{tmp_path.name}",
    )
    tools = build_harness_tools(
        binding.primary,
        read_sys_operation=binding.read_only,
        todo_workspace=str(tmp_path),
    )
    tools_by_name = {tool.card.name: tool for tool in tools}

    for name in ("todo_create", "todo_list", "todo_modify", "todo_get"):
        assert tools_by_name[name].workspace == str(tmp_path)


def test_empty_bash_allowlist_disables_bash_tool(tmp_path: Path) -> None:
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path),
        agent_name=f"bash-disabled-{tmp_path.name}",
        shell_allowlist=(),
    )
    tools = build_harness_tools(
        binding.primary,
        read_sys_operation=binding.read_only,
        bash_allowlist=(),
    )

    assert binding.primary._run_config.shell_allowlist == ["__dataagent_bash_disabled__"]
    assert "bash" not in {tool.card.name for tool in tools}


@pytest.mark.asyncio
async def test_bash_allowlist_checks_every_compound_command(tmp_path: Path) -> None:
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path),
        agent_name=f"bash-compound-{tmp_path.name}",
        shell_allowlist=("echo",),
    )
    tools = {
        tool.card.name: tool
        for tool in build_harness_tools(
            binding.primary,
            read_sys_operation=binding.read_only,
            bash_allowlist=("echo",),
        )
    }

    result = await tools["bash"].invoke({"command": "echo ok && whoami"})

    assert result.success is False
    assert "BASH_TOOL_WHITELIST" in result.error
    assert "whoami" in result.error


@pytest.mark.asyncio
async def test_bash_allowlist_checks_stream_and_newlines(tmp_path: Path) -> None:
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config({}, workspace_root=tmp_path),
        agent_name=f"bash-stream-{tmp_path.name}",
        shell_allowlist=("echo",),
    )
    tools = {
        tool.card.name: tool
        for tool in build_harness_tools(
            binding.primary,
            read_sys_operation=binding.read_only,
            bash_allowlist=("echo",),
        )
    }

    outputs = [
        output
        async for output in tools["bash"].stream(
            {"command": "echo ok\nwhoami"}
        )
    ]

    assert len(outputs) == 1
    assert outputs[0].success is False
    assert "whoami" in outputs[0].error


@pytest.mark.asyncio
async def test_controlled_tools_read_allow_path_but_write_tool_cannot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    workspace.mkdir()
    allowed.mkdir()
    shared_file = allowed / "shared.txt"
    shared_file.write_text("shared data", encoding="utf-8")
    binding = build_sys_operations(
        WorkspaceAccessPolicy.from_config(
            {"WORKSPACE": {"allow_path": [str(allowed)]}},
            workspace_root=workspace,
        ),
        agent_name=f"tool-access-{tmp_path.name}",
    )
    tools = {
        tool.card.name: tool
        for tool in build_harness_tools(
            binding.primary,
            read_sys_operation=binding.read_only,
        )
    }

    read_result = await tools["read_file"].invoke({"file_path": str(shared_file)})
    assert read_result.success is True
    assert "shared data" in read_result.data["content"]

    try:
        write_result = await tools["write_file"].invoke(
            {"file_path": str(allowed / "blocked.txt"), "content": "blocked"}
        )
    except Exception as exc:
        assert "outside sandbox" in str(exc)
    else:
        assert write_result.success is False
        assert "outside sandbox" in str(write_result.error)
    assert not (allowed / "blocked.txt").exists()
