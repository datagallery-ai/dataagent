# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Preload cached semantic metadata into the planner user query."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from loguru import logger

from dataagent.actions.tools.semantic_tool.search_tables_with_schema import (
    get_semantic_retrieve_context,
    read_semantic_retrieve_context_cache,
)
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.utils.info_utils import get_current_query

_SEMANTIC_RETRIEVE_CONTEXT_OPEN = "<semantic_retrieve_context>"
_SEMANTIC_RETRIEVE_CONTEXT_CLOSE = "</semantic_retrieve_context>"


def semantic_retrieve_context_loader(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """Append preloaded semantic context to ``state["user_query"]`` before planning.

    The injected block is wrapped by ``<semantic_retrieve_context>`` and contains only retrieved business context.
    """
    query = _resolve_original_query(state, runtime)
    if not query:
        logger.debug("[semantic_retrieve_context_loader] skipped: empty original query")
        return state

    workspace_root = _resolve_workspace_root(state, runtime)
    if workspace_root is None:
        logger.debug("[semantic_retrieve_context_loader] skipped: workspace is unavailable")
        return state
    run_id = state.get("run_id", getattr(runtime, "run_id", None))
    sub_id = state.get("sub_id", getattr(runtime, "sub_id", None))
    cached = read_semantic_retrieve_context_cache(
        query,
        workspace_root=workspace_root,
    )
    if cached is not None:
        return _inject_semantic_retrieve_context(state, runtime, cached, query)

    config_manager = getattr(runtime, "config_manager", None)
    if config_manager is None:
        logger.debug("[semantic_retrieve_context_loader] skipped: config_manager is unavailable")
        return state

    try:
        client = SemanticServiceClient.from_config(config_manager)
        result = get_semantic_retrieve_context(
            query,
            client=client,
            workspace_root=workspace_root,
            run_id=run_id,
            sub_id=sub_id,
            source="semantic_retrieve_context_loader",
        )
    except Exception as exc:
        logger.warning(f"[semantic_retrieve_context_loader] semantic retrieve failed: {exc}")
        return state

    return _inject_semantic_retrieve_context(state, runtime, result, query)


def _resolve_original_query(state: dict[str, Any], runtime: Any) -> str:
    try:
        query = get_current_query(runtime)
    except Exception as exc:
        logger.debug(f"[semantic_retrieve_context_loader] get_current_query skipped: {exc}")
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
            logger.debug(f"[semantic_retrieve_context_loader] query node sync failed: {exc}")
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
