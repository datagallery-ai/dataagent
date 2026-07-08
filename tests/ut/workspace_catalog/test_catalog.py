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
"""Unit tests for workspace_catalog write APIs."""

from __future__ import annotations

from pathlib import Path

from helpers import make_subagent_dir, seed_catalog

from dataagent.agents.galatea.utils.json_store import read_json_object
from dataagent.core.workspace import catalog as workspace_catalog
from dataagent.core.workspace.frontmatter import WorkspaceCatalogDoc


def test_load_catalog_missing_file_returns_empty_doc(workspace_root: Path) -> None:
    """load_catalog yields an empty doc when the file is absent."""
    doc = workspace_catalog.load_catalog(workspace_root)
    assert doc.version == 1
    assert doc.subagent_workspace == {}


def test_save_catalog_round_trip(workspace_root: Path) -> None:
    """save_catalog persists and reloads session metadata."""
    doc = WorkspaceCatalogDoc(session_id="sess_a", updated_at="2026-01-01T00:00:00Z")
    workspace_catalog.save_catalog(workspace_root, doc)
    loaded = workspace_catalog.load_catalog(workspace_root)
    assert loaded.session_id == "sess_a"
    assert loaded.updated_at == "2026-01-01T00:00:00Z"


def test_touch_catalog_updates_top_level_only(workspace_root: Path) -> None:
    """touch_catalog updates session_id without altering subagent entries."""
    seed_catalog(
        workspace_root,
        {
            "version": 1,
            "session_id": "old",
            "updated_at": "old",
            "subagent_workspace": {
                "abc": {"updated_at": "t1", "artifacts": ["a.csv"], "jobs": []},
            },
        },
    )
    workspace_catalog.touch_catalog(workspace_root, "new_sess")
    payload = read_json_object(workspace_catalog.catalog_path(workspace_root), {})
    assert payload["session_id"] == "new_sess"
    assert payload["updated_at"] != "old"
    assert payload["subagent_workspace"]["abc"]["artifacts"] == ["a.csv"]


def test_register_environment_creates_entry_and_is_idempotent(workspace_root: Path) -> None:
    """register_environment creates one entry and ignores duplicates."""
    workspace_catalog.register_environment(workspace_root, "id1")
    doc = workspace_catalog.load_catalog(workspace_root)
    assert "id1" in doc.subagent_workspace
    assert doc.subagent_workspace["id1"].jobs == []
    workspace_catalog.register_environment(workspace_root, "id1")
    doc_again = workspace_catalog.load_catalog(workspace_root)
    assert len(doc_again.subagent_workspace) == 1


def test_append_job_appends_and_deduplicates(workspace_root: Path) -> None:
    """append_job adds a job once even when called twice."""
    workspace_catalog.register_environment(workspace_root, "id1")
    workspace_catalog.append_job(
        workspace_root,
        "id1",
        job_id="job1",
        agent_id="arith",
        task="one",
    )
    workspace_catalog.append_job(
        workspace_root,
        "id1",
        job_id="job1",
        agent_id="arith",
        task="one",
    )
    doc = workspace_catalog.load_catalog(workspace_root)
    assert len(doc.subagent_workspace["id1"].jobs) == 1
    assert doc.subagent_workspace["id1"].jobs[0].job_id == "job1"


def test_multi_environment_merge_preserves_existing_keys(workspace_root: Path) -> None:
    """register_environment merges new ids without dropping existing ones."""
    seed_catalog(
        workspace_root,
        {
            "version": 1,
            "subagent_workspace": {
                "a": {"updated_at": "t1", "artifacts": [], "jobs": []},
                "b": {"updated_at": "t2", "artifacts": [], "jobs": []},
            },
        },
    )
    workspace_catalog.register_environment(workspace_root, "c")
    doc = workspace_catalog.load_catalog(workspace_root)
    assert set(doc.subagent_workspace) == {"a", "b", "c"}


def test_reuse_append_job_keeps_single_entry(workspace_root: Path) -> None:
    """Multiple jobs on one workspace stay under a single catalog entry."""
    workspace_catalog.register_environment(workspace_root, "reuse")
    workspace_catalog.append_job(workspace_root, "reuse", job_id="j1", agent_id="a", task="t1")
    workspace_catalog.append_job(workspace_root, "reuse", job_id="j2", agent_id="b", task="t2")
    doc = workspace_catalog.load_catalog(workspace_root)
    assert len(doc.subagent_workspace) == 1
    assert len(doc.subagent_workspace["reuse"].jobs) == 2


def test_refresh_artifacts_skips_framework_dirs(workspace_root: Path) -> None:
    """refresh_artifacts ignores hidden framework directories."""
    target = make_subagent_dir(workspace_root, "art", files=["sales.csv"])
    (target / ".memory").mkdir()
    (target / ".context").mkdir()
    (target / ".runtime").mkdir()
    (target / ".dataagent").mkdir()
    (target / "output").mkdir()
    workspace_catalog.register_environment(workspace_root, "art")
    workspace_catalog.refresh_artifacts(workspace_root, "art")
    doc = workspace_catalog.load_catalog(workspace_root)
    assert doc.subagent_workspace["art"].artifacts == ["output/", "sales.csv"]


def test_refresh_artifacts_picks_up_new_files(workspace_root: Path) -> None:
    """refresh_artifacts updates the list when new files appear."""
    target = make_subagent_dir(workspace_root, "art", files=["old.csv"])
    workspace_catalog.register_environment(workspace_root, "art")
    workspace_catalog.refresh_artifacts(workspace_root, "art")
    (target / "new.md").write_text("x", encoding="utf-8")
    workspace_catalog.refresh_artifacts(workspace_root, "art")
    doc = workspace_catalog.load_catalog(workspace_root)
    assert set(doc.subagent_workspace["art"].artifacts) == {"old.csv", "new.md"}
