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
"""Unit tests for workspace_catalog query APIs."""

from __future__ import annotations

from pathlib import Path

from helpers import make_subagent_dir, seed_catalog

from dataagent.agents.galatea.utils.json_store import write_json_object
from dataagent.core.workspace import catalog as workspace_catalog


def test_list_environments_empty(workspace_root: Path) -> None:
    """list_environments returns zero items for an empty catalog."""
    result = workspace_catalog.list_environments(workspace_root)
    assert result["total_subagent_workspace"] == 0
    assert result["subagent_workspace"] == []
    assert "original_msg" in result


def test_list_environments_multiple_sorted(workspace_root: Path) -> None:
    """list_environments sorts entries by updated_at descending."""
    seed_catalog(
        workspace_root,
        {
            "version": 1,
            "subagent_workspace": {
                "older": {"updated_at": "2026-01-01T00:00:00Z", "artifacts": [], "jobs": []},
                "newer": {"updated_at": "2026-01-02T00:00:00Z", "artifacts": ["a.csv"], "jobs": []},
            },
        },
    )
    result = workspace_catalog.list_environments(workspace_root)
    assert result["total_subagent_workspace"] == 2
    assert result["subagent_workspace"][0]["subagent_id"] == "newer"
    assert result["subagent_workspace"][0]["workspace_rel_path"] == "subagents/newer"


def test_inspect_environment_full(workspace_root: Path) -> None:
    """inspect_environment merges catalog, disk, and job.json details."""
    make_subagent_dir(workspace_root, "full", files=["sales.csv"])
    seed_catalog(
        workspace_root,
        {
            "version": 1,
            "subagent_workspace": {
                "full": {
                    "updated_at": "t1",
                    "artifacts": ["sales.csv"],
                    "jobs": [{"job_id": "j1", "agent_id": "arith", "task": "do math"}],
                },
            },
        },
    )
    jobs_dir = workspace_root / "jobs" / "j1"
    jobs_dir.mkdir(parents=True)
    write_json_object(
        jobs_dir / "job.json",
        {
            "job_id": "j1",
            "agent_id": "arith",
            "task": "do math",
            "status": "completed",
            "metadata": {"subagent_session_id": "full"},
        },
    )
    result = workspace_catalog.inspect_environment(workspace_root, "full")
    data = result["data"]
    assert data["subagent_id"] == "full"
    assert data["workspace_rel_path"] == "subagents/full"
    assert data["catalog"]["artifacts"] == ["sales.csv"]
    assert data["disk"]["artifacts"] == ["sales.csv"]
    assert len(data["jobs_detail"]) == 1
    assert data["jobs_detail"][0]["job_id"] == "j1"


def test_inspect_environment_without_catalog_entry(workspace_root: Path) -> None:
    """inspect_environment still reports disk artifacts without a catalog row."""
    make_subagent_dir(workspace_root, "orphan", files=["report.md"])
    result = workspace_catalog.inspect_environment(workspace_root, "orphan")
    data = result["data"]
    assert data["catalog"] is None
    assert data["disk"]["artifacts"] == ["report.md"]
