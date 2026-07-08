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
"""Workspace catalog frontmatter types for ``workspace_catalog.json``."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JobSummary:
    """One job summary stored under a subagent workspace catalog entry."""

    job_id: str
    agent_id: str
    task: str


@dataclass
class SubagentWorkspaceEntry:
    """Catalog summary for one subagent workspace directory."""

    updated_at: str = ""
    artifacts: list[str] = field(default_factory=list)
    jobs: list[JobSummary] = field(default_factory=list)


@dataclass
class WorkspaceCatalogDoc:
    """Top-level ``workspace_catalog.json`` document."""

    version: int = 1
    session_id: str = ""
    updated_at: str = ""
    subagent_workspace: dict[str, SubagentWorkspaceEntry] = field(default_factory=dict)
