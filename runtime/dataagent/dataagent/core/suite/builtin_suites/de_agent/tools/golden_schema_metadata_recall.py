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
"""Golden-schema constrained metadata recall tool."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.tools import sub_agent_tool
from dataagent.actions.tools.semantic_tool.metadata_recall import (
    _build_metadata_recall_sub_agent_config,
    _extract_recall_result_from_state,
)
from dataagent.utils.constants import DEFAULT_SUBAGENT_TOOL_TIMEOUT
from dataagent.utils.info_utils import get_current_query


async def metadata_recall(
    query: str,
    add_user_query: bool = False,
    timeout: int = DEFAULT_SUBAGENT_TOOL_TIMEOUT,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Recall metadata through a golden-schema constrained metadata subagent.

    This wrapper keeps the public tool name as ``metadata_recall`` while loading
    the de_agent golden-schema subagent config from the same suite directory. It
    injects the outer tool ``path`` config into the subagent's
    ``get_golden_schema_columns_info`` tool before launching the subagent.

    Args:
        query: Natural-language metadata retrieval request for the subagent.
        add_user_query: Whether to append the parent user query for broader
            metadata recall.
        timeout: Maximum subagent execution time in seconds.
        _tool_context: Injected tool context carrying config, runtime, and model
            bindings from the parent agent.

    Returns:
        A metadata recall result extracted from the subagent final state.
    """
    source_config_path = Path(__file__).with_name("golden_schema_metadata_recall_agent.yaml")
    with source_config_path.open(encoding="utf-8") as f:
        source_config = yaml.safe_load(f) or {}
    _inject_golden_schema_tasks_path(source_config, _tool_context.tool_config)

    guard = get_current_sandbox()
    workspace_path = guard.workspace_root or Path.cwd().resolve()
    temp_config = _build_metadata_recall_sub_agent_config(
        source_config,
        config_manager=_tool_context.config_manager,
        tool_config=_tool_context.tool_config,
        workspace_root=workspace_path,
    )

    user_query_str = get_current_query(_tool_context.runtime) if _tool_context.runtime else ""
    enhanced_query = f"用户本轮的检索需求：{query}."
    if add_user_query:
        enhanced_query += (
            f"\n需要同时检索和原始任务相关的元数据、UDF和Join 信息，以下是原始任务的描述：{user_query_str}"
        )

    temp_root = workspace_path
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="golden_schema_metadata_recall_sub_agent_",
        dir=temp_root,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        yaml.safe_dump(temp_config, temp_file, allow_unicode=False, sort_keys=False)
        temp_config_path = temp_file.name

    try:
        res = await sub_agent_tool(query=enhanced_query, config_path=temp_config_path, timeout=timeout)
    finally:
        Path(temp_config_path).unlink(missing_ok=True)

    return _extract_recall_result_from_state(res)


def _inject_golden_schema_tasks_path(source_config: dict[str, Any], tool_config: dict[str, Any] | None) -> None:
    tasks_path = _resolve_golden_schema_tasks_path(tool_config)
    if not tasks_path:
        return

    tools = source_config.get("TOOLS")
    if not isinstance(tools, dict):
        return
    local_functions = tools.get("local_functions")
    if not isinstance(local_functions, list):
        return

    for local_function in local_functions:
        if not isinstance(local_function, dict) or local_function.get("name") != "get_golden_schema_columns_info":
            continue
        config = local_function.get("config")
        if not isinstance(config, dict):
            config = {}
            local_function["config"] = config
        config["path"] = tasks_path
        return


def _resolve_golden_schema_tasks_path(tool_config: dict[str, Any] | None) -> str | None:
    if not isinstance(tool_config, dict):
        return None
    path = tool_config.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    config = tool_config.get("config")
    if isinstance(config, dict):
        config_path = config.get("path")
        if isinstance(config_path, str) and config_path.strip():
            return config_path.strip()
    return None
