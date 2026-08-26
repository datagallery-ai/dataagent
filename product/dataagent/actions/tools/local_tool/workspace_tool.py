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
"""Workspace catalog query tools for Flex subagent directory discovery."""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.agents.subagent_session import resolve_subagent_workspace_session
from dataagent.core.workspace import catalog as workspace_catalog


def _runtime_workspace_and_config(
    _tool_context: ToolExecutionContext,
) -> tuple[Any, dict[str, Any] | None] | tuple[None, None]:
    """Resolve parent workspace root and agent config from tool context."""
    runtime = _tool_context.runtime
    if runtime is None:
        return None, None
    root = getattr(runtime, "workspace_dir", None)
    if root is None:
        return None, None
    get_all_config = getattr(runtime, "get_all_config", None)
    config = get_all_config() if callable(get_all_config) else None
    if config is not None and not isinstance(config, dict):
        config = None
    return root, config


def search_workspaces(*, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """List all indexed subagent workspaces (no query filter; newest first).

    Call before reusing a workspace. Read ``artifacts`` and ``jobs[].task`` in
    the response, pick a ``workspace_rel_path``, and pass it to
    ``submit_subagent``. Omit ``workspace_rel_path`` when the catalog is empty or
    nothing matches. Optionally call ``inspect_workspace`` for more detail.

    Args:
        _tool_context: Injected runtime context (not visible to the LLM).

    Returns:
        dict[str, Any]: Success: ``original_msg``, ``frontend_msg``, and
        ``data.subagent_workspace``. Failure: ``status`` ``ERROR`` with
        ``message``.
    """
    root, config = _runtime_workspace_and_config(_tool_context)
    if root is None:
        return {"status": "ERROR", "message": "search_workspaces requires a mounted runtime workspace."}
    result = workspace_catalog.list_environments(root, config=config)
    return {
        "original_msg": result["original_msg"],
        "frontend_msg": result["frontend_msg"],
        "data": {
            "total_subagent_workspace": result["total_subagent_workspace"],
            "subagent_workspace": result["subagent_workspace"],
        },
    }


def inspect_workspace(
    workspace_rel_path: str | None = None,
    subagent_id: str | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Inspect one subagent workspace (catalog + on-disk files + job status).

    Use after ``search_workspaces`` to confirm a ``workspace_rel_path``. Pass
    ``workspace_rel_path`` or ``subagent_id`` (one required; path wins if both
    set). Path must come from a prior tool result (e.g. ``subagents/{uuid}``).

    Args:
        workspace_rel_path: Relative path from ``search_workspaces`` or a prior
            submit/collect response.
        subagent_id: Subagent session id when the path is unknown.
        _tool_context: Injected runtime context (not visible to the LLM).

    Returns:
        dict[str, Any]: Success: ``original_msg``, ``frontend_msg``, and
        ``data`` with ``catalog``, ``disk.artifacts``, ``jobs_detail``. Failure:
        ``status`` ``ERROR`` with ``message``.
    """
    root, config = _runtime_workspace_and_config(_tool_context)
    if root is None:
        return {"status": "ERROR", "message": "inspect_workspace requires a mounted runtime workspace."}

    resolved_path = str(workspace_rel_path or "").strip()
    resolved_id = str(subagent_id or "").strip()
    if resolved_path and resolved_id:
        resolved_id = ""
    if not resolved_path and not resolved_id:
        return {"status": "ERROR", "message": "inspect_workspace requires workspace_rel_path or subagent_id."}

    if resolved_path:
        try:
            session = resolve_subagent_workspace_session(
                parent_workspace=root,
                workspace_rel_path=resolved_path,
                config=config,
            )
        except ValueError as exc:
            return {"status": "ERROR", "message": str(exc)}
        resolved_id = session.subagent_session_id
    else:
        from dataagent.utils.runtime_paths import resolve_job_subagents_root

        subagents_root = resolve_job_subagents_root(parent_workspace=root, config=config)
        workspace_dir = (subagents_root / resolved_id).resolve()
        if not workspace_dir.is_dir():
            return {"status": "ERROR", "message": f"subagent workspace not found: {resolved_id}"}

    result = workspace_catalog.inspect_environment(root, resolved_id, config=config)
    return {
        "original_msg": result["original_msg"],
        "frontend_msg": result["frontend_msg"],
        "data": result["data"],
    }
