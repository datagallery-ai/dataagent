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
"""Collect the final result of an asynchronous subagent job."""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext


def collect_subagent(job_id: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Collect the final result of an asynchronous subagent job.

    Args:
        job_id: The job id returned by ``submit_subagent``.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "collect_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "collect_subagent requires a resolved parent workspace."}
    return agent_service.collect(job_id=normalized_job_id)
