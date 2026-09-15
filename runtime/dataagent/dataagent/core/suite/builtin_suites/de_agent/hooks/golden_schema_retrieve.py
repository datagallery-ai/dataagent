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
"""Preload golden-schema metadata into the planner user query."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.suite.builtin_suites.de_agent.tools.get_golden_schema import (
    get_golden_schema_retrieve_context,
    read_golden_schema_retrieve_context_cache,
)
from dataagent.utils.info_utils import get_current_query

_SEMANTIC_RETRIEVE_CONTEXT_OPEN = "<semantic_retrieve_context>"
_SEMANTIC_RETRIEVE_CONTEXT_CLOSE = "</semantic_retrieve_context>"


class _GoldenSchemaRetrieveNoResultError(RuntimeError):
    """Raised when golden-schema retrieval returns no usable table candidates."""


def golden_schema_retrieve_context_loader(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """Inject golden-schema table context into the planner query before execution.

    The hook resolves the original parent query, loads the matching golden-schema
    table allowlist through the de_agent tools config, and appends a
    ``<semantic_retrieve_context>`` block to ``state["user_query"]``. If lookup
    fails, the metadata recall agent is stopped with an explanatory AI message.

    Args:
        state: Current Flex state passed to the agent pre-hook.
        runtime: Runtime object used to resolve config, workspace, and original
            query metadata.

    Returns:
        The updated state, either with injected context or with a terminal error
        message when golden-schema retrieval fails.
    """
    query = _resolve_original_query(state, runtime)
    if not query:
        logger.debug("[golden_schema_retrieve_context_loader] skipped: empty original query")
        return state

    workspace_root = _resolve_workspace_root(state, runtime)
    if workspace_root is None:
        logger.debug("[golden_schema_retrieve_context_loader] skipped: workspace is unavailable")
        return state
    run_id = state.get("run_id", getattr(runtime, "run_id", None))
    sub_id = state.get("sub_id", getattr(runtime, "sub_id", None))
    config_manager = getattr(runtime, "config_manager", None)
    configured_tasks_path = _resolve_golden_schema_tasks_path(config_manager) if config_manager is not None else None

    try:
        cached = read_golden_schema_retrieve_context_cache(
            query,
            workspace_root=workspace_root,
            tasks_path=configured_tasks_path,
        )
        if cached is None and config_manager is None:
            logger.debug("[golden_schema_retrieve_context_loader] skipped: config_manager is unavailable")
            return state

        if cached is None:
            client = SemanticServiceClient.from_config(config_manager)
            result = get_golden_schema_retrieve_context(
                query,
                client=client,
                workspace_root=workspace_root,
                run_id=run_id,
                sub_id=sub_id,
                source="golden_schema_retrieve_context_loader",
                tasks_path=configured_tasks_path,
            )
        else:
            result = cached

        if not _has_recalled_tables(result):
            raise _GoldenSchemaRetrieveNoResultError("golden schema returned no usable table candidates")
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[golden_schema_retrieve_context_loader] ending agent after golden schema failure: {}",
            error_detail,
        )
        return _end_agent_after_golden_schema_failure(state, error_detail)

    return _inject_semantic_retrieve_context(state, runtime, result, query)


def _resolve_original_query(state: dict[str, Any], runtime: Any) -> str:
    try:
        query = get_current_query(runtime)
    except Exception as exc:
        logger.debug(f"[golden_schema_retrieve_context_loader] get_current_query skipped: {exc}")
        query = None
    if isinstance(query, str) and query.strip():
        return query.strip()

    for value in (
        state.get("parent_user_query"),
        state.get("user_query"),
        getattr(runtime, "parent_user_query", None),
        getattr(runtime, "user_query", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve_workspace_root(state: dict[str, Any], runtime: Any) -> Optional[Path]:  # noqa: UP045
    workspace = state.get("workspace") or getattr(runtime, "workspace_dir", None)
    if not workspace:
        return None
    return Path(str(workspace)).expanduser().resolve()


def _resolve_golden_schema_tasks_path(config_manager: Any) -> str | None:
    """Read the golden schema tasks path from the configured local function."""
    try:
        local_functions = config_manager.get("TOOLS.local_functions", [])
    except Exception as exc:
        logger.debug(f"[golden_schema_retrieve_context_loader] tool config lookup failed: {exc}")
        return None

    if not isinstance(local_functions, list):
        return None
    for tool_config in local_functions:
        if not isinstance(tool_config, dict) or tool_config.get("name") != "get_golden_schema_columns_info":
            continue
        direct_path = tool_config.get("path")
        if isinstance(direct_path, str) and direct_path.strip():
            return direct_path.strip()
        config = tool_config.get("config")
        if isinstance(config, dict):
            config_path = config.get("path")
            if isinstance(config_path, str) and config_path.strip():
                return config_path.strip()
    return None


def _has_recalled_tables(payload: dict[str, Any]) -> bool:
    """Return whether golden-schema retrieval produced at least one table candidate."""
    tables = payload.get("tables")
    return isinstance(tables, list) and bool(tables)


def _end_agent_after_golden_schema_failure(
    state: dict[str, Any],
    error_detail: str,
) -> dict[str, Any]:
    """Append a failure notice and final golden-schema error, then stop the owning agent."""
    messages = state.get("messages")
    final_messages = list(messages) if isinstance(messages, list) else []
    final_messages.append(
        AIMessage(
            content=(
                "Golden schema metadata retrieval did not return usable tables; "
                "this metadata recall agent was stopped.\n\n"
                f"{error_detail}"
            )
        )
    )
    state["messages"] = final_messages
    state["complete"] = True
    return state


def _inject_semantic_retrieve_context(
    state: dict[str, Any],
    runtime: Any,
    payload: dict[str, Any],
    original_query: str,
) -> dict[str, Any]:
    context_text = str(payload.get("context_text") or "").strip()
    if not context_text:
        return state

    base_query = str(state.get("user_query") or original_query).strip() or original_query
    if _SEMANTIC_RETRIEVE_CONTEXT_OPEN in base_query:
        return state

    augmented_query = _build_augmented_user_query(base_query, context_text)
    state["user_query"] = augmented_query
    context = get_context_for_flex_state(state, runtime, swallow_errors=True)
    if context is not None and context.initial_pt:
        try:
            context.modify_node(graph_node_label=context.initial_pt, changes={"query": augmented_query})
        except Exception as exc:
            logger.debug(f"[golden_schema_retrieve_context_loader] query node sync failed: {exc}")
    return state


def _build_augmented_user_query(base_query: str, context_text: str) -> str:
    return (
        f"{base_query}\n\n"
        f"{_SEMANTIC_RETRIEVE_CONTEXT_OPEN}\n"
        "The following metadata context was retrieved before planning. "
        "Use it as already retrieved evidence.\n\n"
        f"{context_text}"
        "\n"
        f"{_SEMANTIC_RETRIEVE_CONTEXT_CLOSE}"
    )
