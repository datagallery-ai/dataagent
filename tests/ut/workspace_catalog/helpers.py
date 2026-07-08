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
"""Shared helpers for workspace_catalog frontmatter tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dataagent.agents.galatea.utils.json_store import write_json_object
from dataagent.core.workspace.catalog import catalog_path


def seed_catalog(workspace_root: Path, doc: dict[str, Any]) -> Path:
    """Write a workspace_catalog.json fixture under workspace_root."""
    path = catalog_path(workspace_root)
    write_json_object(path, doc)
    return path


def make_subagent_dir(
    workspace_root: Path,
    subagent_id: str,
    *,
    files: list[str] | None = None,
) -> Path:
    """Create subagents/{id} with optional demo files."""
    target = workspace_root / "subagents" / subagent_id
    target.mkdir(parents=True, exist_ok=True)
    for name in files or []:
        (target / name).write_text("demo", encoding="utf-8")
    return target
