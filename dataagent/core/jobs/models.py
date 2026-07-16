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
"""Job domain models for the Ferry Job control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
ACTIVE_STATUSES = frozenset({"queued", "running"})


def agent_binding(*, pool: str = "local") -> dict[str, Any]:
    """Build allocation metadata for a subagent job."""
    return {"agent": {"pool": str(pool or "").strip() or "local"}}


def resource_binding(resource_id: str, *, task_type: str, amount: int, unit: str) -> dict[str, Any]:
    """Build allocation metadata for a resource job."""
    return {
        "resource": {
            "id": str(resource_id or "").strip(),
            "task_type": str(task_type or "").strip(),
            "amount": int(amount),
            "unit": str(unit or "").strip(),
        }
    }


@dataclass
class JobSnapshot:
    """Point-in-time job status plus incremental events."""

    job_id: str
    agent_id: str
    status: str
    cursor: str = "0"
    events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    allocation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the snapshot for poll tool responses."""
        return {
            "job_id": self.job_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "cursor": self.cursor,
            "events": self.events,
            "metadata": self.metadata,
            "request": self.request,
            "allocation": self.allocation,
        }


@dataclass
class JobResult:
    """Persisted terminal job result written to ``result.json``."""

    job_id: str
    agent_id: str
    status: str
    summary: str = ""
    error: str = ""
    original_msg: Any = None
    frontend_msg: str = ""
    state: dict[str, Any] | None = None
    subagent_session_id: str = ""
    workspace_rel_path: str = ""
    published_path: str = ""
    published_artifacts: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result for ``collect_subagent`` and storage."""
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "outputs": self.outputs,
            "metrics": self.metrics,
            "published_path": self.published_path,
            "published_artifacts": self.published_artifacts,
        }
        if str(self.status or "").strip().lower() == "completed":
            payload["original_msg"] = self.original_msg
            payload["frontend_msg"] = self.frontend_msg
            payload["state"] = self.state if isinstance(self.state, dict) else {}
            payload["subagent_session_id"] = self.subagent_session_id
            payload["workspace_rel_path"] = self.workspace_rel_path
            return payload
        if self.original_msg is not None:
            payload["original_msg"] = self.original_msg
        if self.frontend_msg:
            payload["frontend_msg"] = self.frontend_msg
        if self.state is not None:
            payload["state"] = self.state
        if self.subagent_session_id:
            payload["subagent_session_id"] = self.subagent_session_id
        if self.workspace_rel_path:
            payload["workspace_rel_path"] = self.workspace_rel_path
        return payload
