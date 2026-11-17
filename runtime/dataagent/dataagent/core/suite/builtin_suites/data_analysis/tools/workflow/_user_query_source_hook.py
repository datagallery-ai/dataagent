from __future__ import annotations

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPreHookOutcome
from loguru import logger


async def user_query_source(inv: ToolHookInvocation) -> ToolPreHookOutcome:
    """Pre-hook for ``start_data_analysis_workflow``: pin ``user_query`` to the raw user message.

    The main agent fills the ``user_query`` tool argument itself and may paraphrase the
    instruction (dropping runtime parameters such as ``output_database``). The flex state
    keeps the current turn's user message verbatim in ``state["raw_user_query"]``, so when
    it is non-empty it replaces the argument: the workflow then persists the pristine
    instruction instead of the paraphrase. Runs as a tool pre-hook; ``tool_args`` mutation
    is the sanctioned hook contract, and the mutated arguments feed the tool call.
    """
    state = inv.state or {}
    raw_query = str(state.get("raw_user_query") or "").strip()
    if not raw_query:
        return ToolPreHookOutcome()
    current = str(inv.tool_args.get("user_query") or "").strip()
    if raw_query == current:
        return ToolPreHookOutcome()
    inv.tool_args["user_query"] = raw_query
    inv.hook_context["user_query_source"] = "raw_user_query"
    logger.info(
        "[user_query_source] start_data_analysis_workflow.user_query replaced with "
        f"state['raw_user_query'] ({len(current)} -> {len(raw_query)} chars)"
    )
    return ToolPreHookOutcome()
