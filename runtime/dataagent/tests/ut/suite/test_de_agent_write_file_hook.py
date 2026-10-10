"""Verify de_agent's write_file policy is wired to the shared tool."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolHookRunner
from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config
from dataagent.utils.runtime_paths import dataagent_package_path


def _write_file_entry() -> dict:
    """Load the active write_file entry from the de_agent Suite."""
    path = dataagent_package_path("core", "suite", "builtin_suites", "de_agent", "tools", "tools.yaml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return next(
        item for item in document.get("TOOLS", {}).get("local_functions", []) if item.get("name") == "write_file"
    )


@pytest.mark.asyncio
async def test_write_file_hook_blocks_new_insert_and_allows_other_writes(tmp_path: Path) -> None:
    """The Suite pre-hook protects new INSERT scripts without replacing the shared writer."""
    entry = _write_file_entry()
    assert entry.get("module") == "dataagent.actions.tools.local_tool.tools"
    assert entry.get("function") == "write_file"
    hooks = load_tool_hooks_from_config(entry.get("hooks"))
    assert len(hooks.pre) == 1
    assert len(hooks.post) == 1

    insert_path = tmp_path / "insert_feature.sql"
    invocation = ToolHookInvocation(
        tool_name="write_file",
        tool_call_id="test-call",
        tool_args={"path": str(insert_path), "content": "SELECT 1", "purpose": "update SQL"},
        runtime=SimpleNamespace(),
        metadata={},
    )
    with pytest.raises(ValueError, match="created by the nl2sql subagent first"):
        await ToolHookRunner.run_pre_hooks(hooks.pre, invocation)

    insert_path.write_text("SELECT 1", encoding="utf-8")
    await ToolHookRunner.run_pre_hooks(hooks.pre, invocation)
    invocation.tool_args.update({"path": str(tmp_path / "create_feature.sql")})
    await ToolHookRunner.run_pre_hooks(hooks.pre, invocation)
    invocation.tool_args.pop("purpose", None)
    await ToolHookRunner.run_pre_hooks(hooks.pre, invocation)
