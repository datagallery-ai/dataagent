# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Thin wrapper tests for workspace catalog tools."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.workspace_tool import inspect_workspace, search_workspaces

_HELPERS_PATH = Path(__file__).resolve().parents[1] / "workspace_catalog" / "helpers.py"
_helpers_spec = importlib.util.spec_from_file_location("workspace_catalog_test_helpers", _HELPERS_PATH)
if _helpers_spec is None or _helpers_spec.loader is None:
    raise ImportError(f"cannot load workspace catalog test helpers from {_HELPERS_PATH}")
_helpers = importlib.util.module_from_spec(_helpers_spec)
_helpers_spec.loader.exec_module(_helpers)
make_subagent_dir = _helpers.make_subagent_dir
seed_catalog = _helpers.seed_catalog


def _tool_context(workspace_root: Path) -> ToolExecutionContext:
    """Build a minimal ToolExecutionContext for workspace tool tests."""
    runtime = SimpleNamespace(
        workspace_dir=workspace_root,
        get_all_config=lambda: {},
    )
    return ToolExecutionContext(runtime=runtime)


def test_search_workspaces_empty_catalog(tmp_path: Path) -> None:
    """search_workspaces returns empty list when catalog is missing."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    result = search_workspaces(_tool_context=_tool_context(workspace_root))
    assert "status" not in result
    assert result["data"]["subagent_workspace"] == []
    assert result["data"]["total_subagent_workspace"] == 0
    assert result["original_msg"]
    assert result["frontend_msg"]


def test_search_workspaces_with_data(tmp_path: Path) -> None:
    """search_workspaces lists seeded catalog entries."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    seed_catalog(
        workspace_root,
        {
            "version": 1,
            "subagent_workspace": {
                "abc": {"updated_at": "t1", "artifacts": ["a.csv"], "jobs": []},
            },
        },
    )
    result = search_workspaces(_tool_context=_tool_context(workspace_root))
    assert result["data"]["total_subagent_workspace"] == 1
    assert result["data"]["subagent_workspace"][0]["workspace_rel_path"] == "subagents/abc"


def test_inspect_workspace_by_subagent_id(tmp_path: Path) -> None:
    """inspect_workspace resolves disk artifacts by subagent_id."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    make_subagent_dir(workspace_root, "abc", files=["report.md"])
    result = inspect_workspace(subagent_id="abc", _tool_context=_tool_context(workspace_root))
    assert result["data"]["subagent_id"] == "abc"
    assert result["data"]["disk"]["artifacts"] == ["report.md"]


def test_inspect_workspace_by_rel_path(tmp_path: Path) -> None:
    """inspect_workspace by path matches inspect by subagent_id."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    make_subagent_dir(workspace_root, "abc", files=["report.md"])
    by_id = inspect_workspace(subagent_id="abc", _tool_context=_tool_context(workspace_root))
    by_path = inspect_workspace(
        workspace_rel_path="subagents/abc",
        _tool_context=_tool_context(workspace_root),
    )
    assert by_path["data"]["subagent_id"] == by_id["data"]["subagent_id"]
    assert by_path["data"]["disk"]["artifacts"] == by_id["data"]["disk"]["artifacts"]


def test_inspect_workspace_invalid_params(tmp_path: Path) -> None:
    """inspect_workspace returns ERROR for missing params or workspace."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    missing = inspect_workspace(_tool_context=_tool_context(workspace_root))
    assert missing["status"] == "ERROR"
    missing_dir = inspect_workspace(subagent_id="missing", _tool_context=_tool_context(workspace_root))
    assert missing_dir["status"] == "ERROR"
