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
"""Submit an asynchronous subagent job."""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.jobs.envelope import envelope_from_tool_context
from dataagent.utils.constants import DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC


def submit_subagent(
    agent_id: str,
    task: str,
    timeout_sec: int = DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC,
    workspace_rel_path: str | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Submit an asynchronous subagent job.

    Use this tool to delegate bounded work to a registered specialist subagent.
    Poll with ``poll_subagent`` and collect the final payload with ``collect_subagent``.

    To continue in an existing subagent workspace, pass ``workspace_rel_path`` from a
    prior ``submit_subagent`` / ``collect_subagent`` response (for example
    ``subagents/{id}``). Omit it to allocate a fresh workspace.

    Args:
        agent_id: Registered specialist id from ``SUBAGENT_CONFIGS``.
        task: Task description forwarded to the subagent.
        timeout_sec: Job timeout in seconds.
        workspace_rel_path: Optional relative path under the parent workspace for reuse.
        _tool_context: Injected runtime/config context (not visible to the LLM).
    """
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "submit_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "submit_subagent requires a resolved parent workspace."}
    return agent_service.submit(
        agent_id=str(agent_id or "").strip(),
        task=str(task or ""),
        timeout_sec=int(timeout_sec or DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC),
        job_envelope=envelope_from_tool_context(_tool_context) or None,
    )
