"""Bound root delegations and preserve Local's executed SQL."""

import json
from dataclasses import replace

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from dataagent import AgentContext

KEY = "nl2sql_parent_stop"
ATTEMPT = "nl2sql_parent_bypass"
SELECTION = "nl2sql_local_selection"
SELECT_TOOL = "nl2sql-react__select_result"
MAX_DELEGATIONS = 3
TERMINAL_STATUSES = {"success", "partial", "error", "needs_clarification"}


def last_user(state):
    return next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)


def user_turn(state):
    # Direct SDK calls can supply HumanMessage without an ID.
    return sum(isinstance(m, HumanMessage) for m in state["messages"])


def same_user_turn(marker, state, user):
    return (
        user is not None
        and marker["user_id"] == user.id
        and marker.get("user_turn", user_turn(state)) == user_turn(state)
    )


def is_local_task(call):
    return call["name"] == "task" and call["args"].get("subagent_type") == "nl2sql-react"


def scoped_marker(state, user, **fields):
    return {"user_id": user.id, "user_turn": user_turn(state), **fields}


async def after_tool(request, result, *, params):
    if not is_local_task(request.tool_call):
        return None
    messages = (
        result.update.get("messages", [])
        if isinstance(result, Command) and isinstance(result.update, dict)
        else [result]
    )
    state = request.runtime.state
    user = last_user(state)
    if user is None:
        return None
    changed = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.tool_call_id != request.tool_call["id"]:
            changed.append(message)
            continue
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        if ATTEMPT in artifact:
            changed.append(message)
            continue
        try:
            payload = json.loads(message.text)
        except ValueError:
            payload = None
        status = payload.get("status") if isinstance(payload, dict) else None
        if not isinstance(status, str) or status not in TERMINAL_STATUSES:
            # A real dispatch consumes a slot even when the child errors before producing an envelope.
            payload = {"status": "error", "error": {"code": "local_task_failed"}}
        marker = scoped_marker(
            state, user, description=request.tool_call["args"].get("description"), result=payload
        )
        changed.append(message.model_copy(update={"artifact": {**artifact, KEY: marker}}))
    if isinstance(result, Command):
        return replace(result, update={**result.update, "messages": changed})
    return changed[0]


async def before_tool(request, *, params):
    state, call = request.runtime.state, request.tool_call
    local = is_local_task(call)
    legacy_sql_tool = call["name"].startswith("nl2sql-react__") and call["name"] != SELECT_TOOL
    if not (local or legacy_sql_tool):
        return None
    attempts = turn_markers(state, last_user(state), KEY)
    for marker in reversed(attempts.values()):
        old_description, description = marker["description"], call["args"].get("description")
        same_task = (
            local
            and isinstance(description, str)
            and isinstance(old_description, str)
            and description == old_description
        )
        if same_task or legacy_sql_tool:
            return ToolMessage(
                content=json.dumps(marker["result"], ensure_ascii=False),
                name=call["name"],
                tool_call_id=call["id"],
                artifact={ATTEMPT: marker},
            )

    if local and len(attempts) >= MAX_DELEGATIONS:
        return rejected(
            call,
            "local_delegation_budget_exhausted",
            "This turn already started three Local tasks. Use a successful result or explain what is unresolved.",
        )
    if local:
        delegation = next(
            (m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None
        )
        first_local = (
            next((item for item in delegation.tool_calls if is_local_task(item)), None)
            if delegation
            else None
        )
        if first_local is not None and call["id"] != first_local["id"]:
            # Each request in a parallel tool batch sees the same state. Dispatch only the first
            # Local tool so a second send cannot consume the same available slot.
            return rejected(
                call,
                "local_delegation_must_be_sequential",
                "Only one Local task can start per model turn. Inspect its result before delegating again.",
            )
    return None


def rejected(call, code, message):
    return ToolMessage(
        content=json.dumps({"status": "error", "error": {"code": code, "message": message}}),
        name=call["name"],
        tool_call_id=call["id"],
        status="error",
        artifact={ATTEMPT: {"code": code}},
    )


def turn_markers(state, user, key):
    return {
        item.tool_call_id: marker
        for item in state["messages"]
        if isinstance(item, ToolMessage) and isinstance(item.artifact, dict)
        if (marker := item.artifact.get(key)) and same_user_turn(marker, state, user)
    }


def successful_results(attempts):
    results = {}
    for marker in attempts.values():
        payload = marker["result"]
        sql, query_id = payload.get("sql"), payload.get("query_id")
        if (
            payload.get("status") == "success"
            and isinstance(sql, str)
            and sql.strip()
            and isinstance(query_id, str)
            and query_id
        ):
            results[query_id] = {
                "query_id": query_id,
                "sql": sql,
                "artifacts": payload.get("artifacts", []),
            }
    return results


@tool(response_format="content_and_artifact")
def select_result(query_id: str, runtime: ToolRuntime[AgentContext]) -> tuple[dict, dict]:
    """Select a successful Local query from this user turn as the final SQL.

    Copy its query_id from a successful task reply. Use this when multiple Local
    queries succeeded and one should be the primary answer. Does not run or edit SQL.
    """
    state = runtime.state
    user = last_user(state)
    if query_id not in successful_results(turn_markers(state, user, KEY)):
        return {"status": "error", "error": {"code": "invalid_local_query_selection"}}, {}
    return {"status": "selected", "query_id": query_id}, {
        SELECTION: scoped_marker(state, user, query_id=query_id)
    }


async def after_agent(state, runtime, *, params):
    """Archive this turn's Local results and selected query on the final message."""
    messages = state["messages"]
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or last.tool_calls or last.invalid_tool_calls:
        return None
    user = last_user(state)
    attempts = turn_markers(state, user, KEY)
    results = successful_results(attempts)
    attributes = dict(last.additional_kwargs)
    for key in ("nl2sql_local_results", "nl2sql_local_selected_query_id", "nl2sql_local_failure"):
        attributes.pop(key, None)
    if attempts:
        attributes["nl2sql_local_results"] = list(results.values())
        if results:
            selected = next(
                (
                    m["query_id"]
                    for m in reversed(turn_markers(state, user, SELECTION).values())
                    if m["query_id"] in results
                ),
                None,
            )
            if selected or len(results) == 1:
                attributes["nl2sql_local_selected_query_id"] = selected or next(iter(results))
        else:
            payload = next(reversed(attempts.values()))["result"]
            attributes["nl2sql_local_failure"] = {
                "status": payload.get("status"),
                "error": payload.get("error"),
            }
    if attributes == last.additional_kwargs:
        return None
    return {"messages": [last.model_copy(update={"additional_kwargs": attributes})]}
