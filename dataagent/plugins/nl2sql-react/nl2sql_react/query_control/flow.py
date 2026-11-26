"""Plugin control hooks: bounded issues, stable contracts and terminal delivery."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import sqlglot
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from dataagent import safe_error
from nl2sql_react.hooks.projection import compare, describe
from nl2sql_react.hooks.results import Selection, build_result, capture_result
from nl2sql_react.query_control.state import (
    DATA_TOOLS,
    KEY,
    PREFIX,
    MissingBusinessChoice,
    MissingEvidenceReference,
    RepairProbeRequired,
    contract_id,
    ledger,
    message,
    normalized,
    pending,
    policy,
    summary,
)
from nl2sql_react.tools.database import configuration, redaction_secrets
from nl2sql_react.tools.review import sql_key, stored_sql_key

FINISH_TOOL = PREFIX + "finish_query"


def blocked(current, code, message, *, status="error"):
    current["terminal"] = {
        "status": status,
        "query_id": None,
        "execution_id": None,
        "sql": None,
        "rows": [],
        "artifacts": [],
        "final_manifest": None,
        "error": {"code": code, "message": message},
        "unresolved": [issue["question"] for issue in pending(current).values()] or [message],
        "assumptions": current["assumptions"],
        "control": summary(current),
        "review": current.get("review"),
        "stop_reason": code,
    }


def projection_notice(result, note):
    """Append an advisory projection note to a successful answer result.

    The observation and its artifact stay intact: rows, columns and query_id are what
    the rest of the flow reads. Only the visible text gains one field.
    """
    try:
        visible = json.loads(result.content)
    except (ValueError, TypeError):
        return result
    if not isinstance(visible, dict):
        return result
    return result.model_copy(
        update={"content": json.dumps({**visible, "projection_check": note}, ensure_ascii=False)}
    )


def fail_call(request, current, code, text, *, status="error"):
    blocked(current, code, text, status=status)
    return message(current, request.tool_call["id"], request.tool_call["name"], current["terminal"])


def correct_call(request, current, code, text):
    """Reject without I/O; allow one task-wide correction of invocation mistakes."""
    current["invalid_calls"] += 1
    if current["invalid_calls"] >= 2 or request.tool_call["name"] == FINISH_TOOL:
        return fail_call(request, current, code, text)
    return message(
        current,
        request.tool_call["id"],
        request.tool_call["name"],
        {
            "error": {"code": code, "message": text},
            "retry_allowed": True,
            "control": summary(current),
        },
        status="error",
    )


def check_business(current, settings):
    choices = [i for i in pending(current).values() if i["kind"] == "business_choice"]
    if choices and (settings.ambiguity == "clarify" or current["best_effort_used"]):
        blocked(
            current,
            "business_choice_required",
            "Database probes cannot decide the business definition.",
            status="needs_clarification",
        )


def add_issue(current, item, settings):
    key = item.id
    existing = current["issues"].get(key)
    if existing:
        if existing["kind"] != item.kind:
            raise ValueError("An existing issue cannot change kind")
        # Registration is idempotent, not an edit API. Preserve the original
        # question, acceptance and all spent budgets even if the model restates
        # them while submitting a finding through the separate resolution field.
        return
    if len(current["issues"]) >= settings.max_issues:
        raise ValueError("Task issue budget exhausted; renaming a gap does not reset its budget")
    if any(
        normalized(i["question"]) == normalized(item.question) for i in current["issues"].values()
    ):
        raise ValueError("Reuse the existing issue ID for the same question")
    if current["contract"] and not item.evidence_ids:
        if len(current["evidence"]) > current["contract_watermark"]:
            raise MissingEvidenceReference(
                "Issue not registered. Set evidence_ids inside the specific issues[] item, "
                'for example issues=[{..., "evidence_ids": ["e1"]}], using the actual new observation ID. '
                "Top-level revision_evidence does not register issue evidence; it is only for changing contract. "
                "an empty result alone does not prove an error. If no observation contradicts the "
                "locked definition, finish the existing answer instead of investigating alternatives."
            )
        raise ValueError("A new issue after contract lock must cite a new observation")
    validate_sources(
        current,
        item.evidence_ids,
        after=current["contract_watermark"] if current["contract"] else 0,
        allow_failed=item.kind == "database_fact",
    )
    current["issues"][key] = {
        **item.model_dump(),
        "status": "open",
        "attempts": 0,
        "explore_calls": 0,
        "finding": None,
    }


def validate_sources(current, ids, *, after=0, issue_id=None, allow_failed=False):
    for identifier in ids:
        observation = current["evidence"].get(identifier)
        if (
            not observation
            or (not observation["success"] and not allow_failed)
            or observation["sequence"] <= after
        ):
            raise ValueError(
                "Evidence must be successful, task-local, relevant to the issue and newer than the contract when revising"
            )
    if issue_id is not None and not any(
        current["evidence"][identifier]["issue_id"] == issue_id for identifier in ids
    ):
        issue = current["issues"][issue_id]
        for identifier in ids:
            observation = current["evidence"][identifier]
            if (
                issue["status"] == "open"
                and identifier in issue["evidence_ids"]
                and observation["kind"] == "answer"
                and observation["contract_id"] == current["contract_id"]
                and observation["sequence"] > current["contract_watermark"]
            ):
                raise RepairProbeRequired(
                    f"Update rejected without changing state. Evidence {identifier} can register "
                    f"repair issue {issue_id}. Register it separately if it is not already open. "
                    f"Run execute_sql with purpose='probe' and issue_id='{issue_id}', then resolve "
                    "the issue using the new evidence ID from that probe."
                )
        raise ValueError(
            "At least one observation must come from the issue being resolved; other successful observations may support it"
        )


def update(
    current,
    settings,
    *,
    contract,
    issues,
    resolutions,
    revision_evidence,
    revision_reason,
    best_effort_assumption,
    best_effort_issue_id=None,
):
    candidate = deepcopy(current)  # Reject an invalid edit as a whole.
    for item in issues:
        add_issue(candidate, item, settings)
    check_business(candidate, settings)
    if candidate["terminal"]:
        return candidate
    for resolution in resolutions:
        issue = candidate["issues"].get(resolution.issue_id)
        if (
            not issue
            or issue["status"] not in {"open", "resolved"}
            or issue["kind"] != "database_fact"
        ):
            raise ValueError("Only an open database issue can be resolved using observations")
        validate_sources(candidate, resolution.evidence_ids, issue_id=resolution.issue_id)
        issue.update(
            status="resolved", evidence_ids=resolution.evidence_ids, finding=resolution.finding
        )
    if best_effort_issue_id is not None and not best_effort_assumption:
        raise MissingBusinessChoice("best_effort_issue_id requires best_effort_assumption.")
    if best_effort_assumption:
        choices = {
            key: issue
            for key, issue in pending(candidate).items()
            if issue["kind"] == "business_choice"
        }
        if (
            settings.ambiguity == "best_effort_once"
            and not candidate["best_effort_used"]
            and not choices
        ):
            raise MissingBusinessChoice(
                "Update rejected without changing state. best_effort_assumption requires an open "
                "business_choice; it cannot resolve a database_fact. Resolve observed fact gaps "
                "using their evidence_ids and finding. If a business decision remains, register "
                "that separate choice and supply its assumption and contract; otherwise omit "
                "best_effort_assumption."
            )
        if (
            settings.ambiguity != "best_effort_once"
            or candidate["best_effort_used"]
            or not choices
            or contract is None
        ):
            raise ValueError(
                "Best effort needs the explicit policy, one open business choice, a contract and one unused allowance"
            )
        if best_effort_issue_id is None and len(choices) == 1:
            best_effort_issue_id = next(iter(choices))
        if best_effort_issue_id is None or best_effort_issue_id not in choices:
            raise MissingBusinessChoice(
                "Set best_effort_issue_id to the open business choice addressed by the "
                f"assumption. Available IDs: {', '.join(choices)}."
            )
        candidate["best_effort_used"] = True
        candidate["assumptions"].append(best_effort_assumption)
        choices[best_effort_issue_id].update(status="assumed", finding=best_effort_assumption)
        check_business(candidate, settings)
        if candidate["terminal"]:
            return candidate
    if contract is not None:
        value = contract.model_dump()
        if value != candidate["contract"]:
            if candidate["contract"]:
                if len(candidate["revisions"]) >= settings.max_contract_revisions:
                    raise ValueError("Contract revision budget exhausted")
                if not revision_evidence or not revision_reason.strip():
                    raise ValueError(
                        "Changing a locked contract requires new contradiction evidence and its reason"
                    )
                validate_sources(
                    candidate, revision_evidence, after=candidate["contract_watermark"]
                )
                candidate["revisions"].append(
                    {
                        "previous_contract_id": candidate["contract_id"],
                        "contract": candidate["contract"],
                        "evidence_ids": revision_evidence,
                        "reason": revision_reason,
                    }
                )
            candidate["contract_id"] = contract_id(candidate, value)
            candidate["contract"] = value
            candidate["version"] += 1
            candidate["contract_watermark"] = len(candidate["evidence"])
    return candidate


def fingerprint(name, args):
    args = {
        k: v
        for k, v in args.items()
        if k not in {"issue_id", "contract_id", "repair_issue_id", "repair_reason"}
    }
    if name == PREFIX + "execute_sql":
        try:
            args["sql"] = sqlglot.parse_one(args["sql"], read="sqlite").sql(normalize=True)
        except (sqlglot.errors.SqlglotError, KeyError, TypeError):
            pass
    return hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()


def finish_after_tool(result):
    """Route a terminal tool result through the standard Agent's direct-return path."""
    if isinstance(result, ToolMessage):
        update = {"messages": [result]}
    elif isinstance(result, Command) and isinstance(result.update, dict):
        update = result.update
    else:
        return None
    messages = update.get("messages", [])
    terminal = next(
        (
            response
            for response in reversed(messages)
            if isinstance(response, ToolMessage)
            and isinstance(response.artifact, dict)
            and response.artifact.get(KEY, {}).get("terminal")
        ),
        None,
    )
    if terminal is None or terminal.name == FINISH_TOOL:
        return None
    # The router uses the last AI tool call to pick return_direct. This pair is
    # added after the real tool decision, and the ToolNode never runs it.
    route_id = f"nl2sql-react-route-finish-{terminal.tool_call_id}"
    route_metadata = {"nl2sql_react_route_only": True}
    update = {
        **update,
        "messages": [
            *messages,
            AIMessage(
                id=f"{route_id}-request",
                content="",
                tool_calls=[{"name": FINISH_TOOL, "args": {}, "id": route_id}],
                additional_kwargs=route_metadata,
            ),
            ToolMessage(
                id=f"{route_id}-receipt",
                name=FINISH_TOOL,
                tool_call_id=route_id,
                content="",
                additional_kwargs=route_metadata,
            ),
        ],
    }
    return replace(result, update=update) if isinstance(result, Command) else Command(update=update)


async def before_tool(request, *, params):
    result = _before_tool(request)
    return finish_after_tool(result) or result


def _before_tool(request):
    current = ledger(request.runtime.state)
    settings = policy(request.runtime.context)
    name, args = request.tool_call["name"], request.tool_call["args"]
    ai = next(
        (m for m in reversed(request.runtime.state["messages"]) if isinstance(m, AIMessage)), None
    )
    if ai is not None and len(ai.tool_calls) != 1:
        return fail_call(
            request,
            current,
            "parallel_control_calls",
            "Call one subagent tool at a time so each decision sees committed evidence.",
        )
    if current["terminal"]:
        return message(current, request.tool_call["id"], name, current["terminal"])
    tool = getattr(request, "tool", None)
    schema = tool.tool_call_schema if tool is not None else None
    if hasattr(schema, "model_validate"):
        try:
            schema.model_validate(args)
        except ValueError as error:
            detail = safe_error(error, redaction_secrets(request.runtime.context))
            if isinstance(error, ValidationError) and any(
                item["type"] == "extra_forbidden" for item in error.errors()
            ):
                detail["message"] += (
                    " Remove unexpected fields entirely. Renaming an unknown field or setting it "
                    "to null is still invalid. Use only fields declared in the tool schema. "
                    "No state update or database access occurred."
                )
            return correct_call(request, current, "invalid_tool_input", detail["message"])
    check_business(current, settings)
    if current["terminal"]:
        return message(current, request.tool_call["id"], name, current["terminal"])
    if name not in DATA_TOOLS:
        return None
    if any(i["kind"] == "business_choice" for i in pending(current).values()):
        return fail_call(
            request,
            current,
            "business_choice_required",
            "Record the configured one-time assumption before any more database calls.",
            status="needs_clarification",
        )
    answer = name == PREFIX + "execute_sql" and args.get("purpose") == "answer"
    if answer:
        if not current["contract"]:
            return fail_call(
                request,
                current,
                "contract_required",
                "Lock a query contract and pass its current contract_id before answer SQL.",
            )
        if args.get("contract_id") != current["contract_id"]:
            if any(
                r["previous_contract_id"] == args.get("contract_id") for r in current["revisions"]
            ):
                return fail_call(
                    request,
                    current,
                    "contract_required",
                    "An old contract version cannot authorize this answer.",
                )
            return correct_call(
                request,
                current,
                "invalid_contract_reference",
                "This SQL was not executed. Copy the complete current contract_id from control; "
                "do not shorten or invent it. Keep the locked definition unchanged.",
            )
        if any(i["kind"] != "execution_error" for i in pending(current).values()):
            return fail_call(
                request,
                current,
                "unresolved_database_issue",
                "Resolve the recorded database gaps before executing an answer.",
            )
        if current["answer_attempts"] >= settings.max_answer_attempts:
            return fail_call(
                request,
                current,
                "answer_budget_exhausted",
                "Answer/repair attempt budget exhausted.",
            )
        config, _ = configuration(request.runtime.context)
        if config.review_sql and current["answer_attempts"] == 0:
            if (
                current.get("review", {}).get("status") != "reviewed"
                or current.get("reviewed_contract_id") != current["contract_id"]
            ):
                return fail_call(
                    request,
                    current,
                    "review_required",
                    "No reviewed draft belongs to this contract.",
                )
            try:
                matches = sql_key(args.get("sql", "")) == stored_sql_key(
                    current, "approved_sql", "approved_sql_key"
                )
            except ValueError:
                matches = False
            if not matches:
                return correct_call(
                    request,
                    current,
                    "reviewed_sql_required",
                    "No SQL was executed. Use approved_sql from the contract lock: "
                    + current["approved_sql"],
                )
        previous = [
            e
            for e in current["evidence"].values()
            if e["kind"] == "answer" and e["success"] and e["contract_id"] == current["contract_id"]
        ]
        if previous:
            repair = current["issues"].get(args.get("repair_issue_id"))
            if (
                not repair
                or repair["status"] != "resolved"
                or not args.get("repair_reason", "").strip()
            ):
                return fail_call(
                    request,
                    current,
                    "answer_already_executed",
                    "Finish the checked answer, or cite a newly resolved repair issue and its specific reason.",
                )
            try:
                validate_sources(
                    current,
                    repair["evidence_ids"],
                    after=previous[-1]["sequence"],
                    issue_id=repair["id"],
                )
            except ValueError:
                return fail_call(
                    request,
                    current,
                    "stale_repair_evidence",
                    "Repair evidence must be newer than the previous answer, from the resolved repair issue.",
                )
    else:
        issue = current["issues"].get(args.get("issue_id"))
        if not issue or issue["status"] != "open" or issue["kind"] != "database_fact":
            return correct_call(
                request,
                current,
                "issue_required",
                "This call was not executed. Every probe/search/exploration needs a registered open database issue_id. "
                "Register a concrete missing fact before probing; if this is the checked answer, use purpose='answer' "
                "and the current contract_id. Do not reopen resolved issues just to repeat checks.",
            )
        if (
            issue["attempts"] >= settings.max_probes_per_issue
            or current["total_probes"] >= settings.max_total_probes
        ):
            return fail_call(
                request,
                current,
                "issue_budget_exhausted",
                "Repeated checks did not close the issue; stop or clarify its acceptance condition.",
            )
        if name == PREFIX + "db_explore" and issue["explore_calls"]:
            return fail_call(
                request,
                current,
                "exploration_already_used",
                "Reuse this issue's exploration; only a bounded targeted follow-up is allowed.",
            )
    signature = fingerprint(name, args)
    if signature in current["fingerprints"]:
        return fail_call(
            request,
            current,
            "duplicate_query",
            "An equivalent call was already attempted; reuse its observation.",
        )
    if name == PREFIX + "db_explore":
        focus = (
            args.get("focus", "")
            + "\nRegistered issue: "
            + json.dumps(current["issues"][args["issue_id"]], ensure_ascii=False)
        )
        if current["contract"]:
            focus += (
                "\nLocked query contract (investigate only a concrete contradiction; do not enumerate meanings): "
                + json.dumps(current["contract"], ensure_ascii=False)
            )
        return request.override(tool_call={**request.tool_call, "args": {**args, "focus": focus}})
    return None


def observation(result):
    if isinstance(result.artifact, dict) and "nl2sql_observation" in result.artifact:
        return result.artifact["nl2sql_observation"]
    try:
        value = json.loads(result.text)
        return value if isinstance(value, dict) else {"error": "Invalid observation"}
    except ValueError:
        return {"error": result.text}


def exploration_result(current, settings, issue_id, body, evidence_id):
    report = body.get("report", {})
    unresolved = report.get("unresolved", [])
    typed = report.get("blocking_issues", [])
    classified = {b["question"] for b in typed}
    # Legacy free text cannot prove the gap is database-resolvable. Fail closed.
    typed = [
        *typed,
        *(
            {
                "kind": "business_choice",
                "question": text,
                "acceptance": "Clarify this unclassified blocking question",
            }
            for text in unresolved
            if text not in classified
        ),
    ]
    if body.get("status") == "ready" and not typed and not body.get("error"):
        current["issues"][issue_id].update(
            status="resolved",
            evidence_ids=[evidence_id],
            finding="Exploration submitted observed query context",
        )
        return
    if not typed:
        blocked(
            current,
            "exploration_failed",
            "Database exploration failed or exhausted its budget; no automatic restart.",
        )
        return
    for index, item in enumerate(typed):
        if index == 0 and item["kind"] == "database_fact":
            # Keep the original question, acceptance and spent budget. New wording
            # in a report must not create a fresh allowance for the same investigation.
            current["issues"][issue_id]["remaining_gap"] = item["question"]
            continue
        key = "gap_" + hashlib.sha256(normalized(item["question"]).encode()).hexdigest()[:16]
        if key not in current["issues"]:
            if len(current["issues"]) >= settings.max_issues:
                blocked(
                    current,
                    "issue_budget_exhausted",
                    "Exploration produced more unresolved issues than the task budget.",
                )
                return
            current["issues"][key] = {
                **item,
                "id": key,
                "status": "open",
                "attempts": 1,
                "explore_calls": 1,
                "evidence_ids": [],
                "finding": None,
            }
    # Business uncertainty does not invalidate the actual metadata already found.
    if all(item["kind"] == "business_choice" for item in typed) and body.get("schema"):
        current["issues"][issue_id].update(
            status="resolved",
            evidence_ids=[evidence_id],
            finding="Database context observed; business choice remains separate",
        )
    check_business(current, settings)


async def after_tool(request, result, *, params):
    updated = await _after_tool(request, result)
    return finish_after_tool(result if updated is None else updated) or updated


async def _after_tool(request, result):
    if not isinstance(result, ToolMessage) or (
        isinstance(result.artifact, dict) and KEY in result.artifact
    ):
        return None
    name, args = request.tool_call["name"], request.tool_call["args"]
    if name not in DATA_TOOLS:
        if result.status == "error":
            return fail_call(
                request, ledger(request.runtime.state), "invalid_tool_input", result.text
            )
        return None
    current = ledger(request.runtime.state)
    settings = policy(request.runtime.context)
    body = observation(result)
    answer = name == PREFIX + "execute_sql" and args.get("purpose") == "answer"
    issue_id = args.get("issue_id", "")
    current["fingerprints"].append(fingerprint(name, args))
    if answer:
        current["answer_attempts"] += 1
    else:
        current["total_probes"] += 1
        current["issues"][issue_id]["attempts"] += 1
        if name == PREFIX + "db_explore":
            current["issues"][issue_id]["explore_calls"] += 1
    number = len(current["evidence"]) + 1
    evidence_id = f"e{number}"
    success = result.status != "error" and not body.get("error")
    current["evidence"][evidence_id] = {
        "sequence": number,
        "tool": name,
        "tool_call_id": result.tool_call_id,
        "issue_id": issue_id,
        "kind": "answer" if answer else "probe",
        "success": success,
        "contract_id": current["contract_id"],
    }
    if name == PREFIX + "execute_sql":
        result = (
            await capture_result(
                request,
                result,
                params={
                    "contract_id": current["contract_id"],
                    "purpose": args.get("purpose"),
                },
            )
            or result
        )
        if answer:
            if success:
                if "execution_error" in current["issues"]:
                    current["issues"]["execution_error"].update(
                        status="resolved", finding="Corrected SQL executed"
                    )
                # Compare what this answer shows against the fields the contract declared.
                # Advisory: the row values are still recorded and usable, but the model is
                # told how the projection differs from the contract.
                config, _ = configuration(request.runtime.context)
                declared = (
                    ((current["contract"] or {}).get("output_columns") or [])
                    if config.projection_check
                    else []
                )
                note = describe(*compare(declared, args.get("sql", "")))
                if note:
                    current["evidence"][evidence_id]["projection_note"] = note
                    result = projection_notice(result, note)
            else:
                current["issues"]["execution_error"] = {
                    "id": "execution_error",
                    "kind": "execution_error",
                    "status": "open",
                    "question": str(body.get("error")),
                    "acceptance": "Repair this SQL execution error without changing its meaning",
                    "attempts": current["answer_attempts"],
                    "evidence_ids": [evidence_id],
                }
    if name == PREFIX + "db_explore":
        exploration_result(current, settings, issue_id, body, evidence_id)
    artifact = {**(result.artifact or {}), KEY: current}
    try:
        visible = json.loads(result.text)
        content = json.dumps(
            {"evidence_id": evidence_id, **visible, "control": summary(current)}, ensure_ascii=False
        )
    except ValueError:
        content = (
            json.dumps(
                {"evidence_id": evidence_id, "control": summary(current)}, ensure_ascii=False
            )
            + "\n"
            + result.text
        )
    if current["terminal"]:
        content = json.dumps(current["terminal"], ensure_ascii=False)
    return result.model_copy(update={"content": content, "artifact": artifact})


def terminal_message(current):
    return AIMessage(
        content=json.dumps(current["terminal"], ensure_ascii=False),
        id=current["task_id"] + "-final",
    )


async def after_model(state, runtime, *, params):
    ai = state["messages"][-1]
    invalid = getattr(ai, "invalid_tool_calls", [])
    if not invalid:
        return None
    current = ledger(state)
    current["invalid_calls"] += 1
    # Turn malformed requests into explicit non-execution errors. Native graph
    # routing otherwise sees zero tool calls and silently ends the child.
    calls = [
        *ai.tool_calls,
        *(
            {
                "name": c["name"] or "invalid_tool",
                "args": {},
                "id": c.get("id") or f"invalid-{ai.id}-{i}",
                "type": "tool_call",
            }
            for i, c in enumerate(invalid)
        ),
    ]
    detail = "Malformed tool arguments JSON. No calls from this response were executed. Resubmit one complete valid tool call."
    if current["invalid_calls"] >= 2:
        blocked(current, "invalid_tool_input", detail)
    replacement = ai.model_copy(update={"tool_calls": calls, "invalid_tool_calls": []})
    errors = [
        message(
            current,
            c["id"],
            c["name"],
            current["terminal"]
            or {
                "error": {"code": "invalid_tool_json", "message": detail},
                "control": summary(current),
                "retry_allowed": True,
            },
            status="error",
        )
        for c in calls
    ]
    messages = [replacement, *errors]
    if current["terminal"]:
        messages.append(terminal_message(current))
    # Answered calls let the standard router return to the model. A final
    # AI message with no tool calls takes its exit path instead.
    return {"messages": messages}


def unfinished_answer(state, current):
    """The one answer execution that may be delivered when the child never called finish_query.

    All of these must hold, otherwise the original fail-closed behavior stands:
    a contract is locked and nothing is left unresolved; exactly one successful answer
    execution exists for the current contract; no answer ran after a contract revision;
    the execution record and its saved artifacts were captured by code.
    """
    if not current["contract"] or pending(current) or current["terminal"] or current["revisions"]:
        return None
    answers = [
        e
        for e in current["evidence"].values()
        if e["kind"] == "answer" and e["success"] and e["contract_id"] == current["contract_id"]
    ]
    if len(answers) != 1:
        return None
    for item in state["messages"]:
        if not isinstance(item, ToolMessage) or item.tool_call_id != answers[0]["tool_call_id"]:
            continue
        record = (
            item.artifact.get("nl2sql_local_result") if isinstance(item.artifact, dict) else None
        )
        if (
            record
            and record["task_id"] == current["task_id"]
            and record.get("contract_id") == current["contract_id"]
            and record["result"].get("purpose") == "answer"
            and not record["artifact_error"]
        ):
            return record
    return None


async def after_agent(state, runtime, *, params):
    current = ledger(state)
    if not current["terminal"]:
        try:
            config, _ = configuration(runtime.context)
            record = unfinished_answer(state, current) if config.deliver_unfinished_answer else None
        except (ValueError, AttributeError, KeyError):
            record = None
        if record is not None:
            current = await finish(
                current,
                runtime,
                execution_id=record["query_id"],
                definitions=[],
                assumptions=[],
                conclusion="Delivered automatically: the subagent ended without calling finish_query.",
                status="success",
                unresolved=[],
                state=state,
                auto_delivered=True,
            )
        if not current["terminal"]:
            blocked(
                current,
                "finish_query_required",
                "The subagent ended without a valid finish_query call.",
            )
    return {"messages": [terminal_message(current)]}


async def finish(
    current,
    runtime,
    *,
    execution_id,
    definitions,
    assumptions,
    conclusion,
    status,
    unresolved,
    state=None,
    auto_delivered=False,
):
    # Tool calls read state from their runtime; agent hooks pass the graph state explicitly.
    state = runtime.state if state is None else state
    if current["terminal"]:
        return current
    if status in {"needs_clarification", "error"}:
        blocked(
            current,
            "explicit_stop",
            "; ".join(unresolved) or conclusion or "Task could not be completed",
            status=status,
        )
        return current
    if pending(current) or not current["contract"]:
        blocked(
            current,
            "unresolved_issue",
            "Cannot deliver an answer with unresolved issues or no locked contract.",
            status="needs_clarification"
            if any(i["kind"] == "business_choice" for i in pending(current).values())
            else "error",
        )
        return current
    records = [
        m.artifact.get("nl2sql_local_result")
        for m in state["messages"]
        if isinstance(m, ToolMessage) and isinstance(m.artifact, dict)
    ]
    selected = next(
        (
            r
            for r in records
            if r and r["query_id"] == execution_id and r["task_id"] == current["task_id"]
        ),
        None,
    )
    if not selected or selected.get("contract_id") != current["contract_id"]:
        blocked(
            current,
            "stale_execution",
            "Select a successful execution belonging to the current task and contract version.",
        )
        return current
    unresolved = list(
        dict.fromkeys(
            [
                *unresolved,
                *(i["question"] for i in current["issues"].values() if i["status"] == "assumed"),
            ]
        )
    )
    review_matches = False
    if current.get("reviewed_contract_id") == selected.get("contract_id"):
        try:
            review_matches = sql_key(selected["result"]["sql"]) == stored_sql_key(
                current, "approved_sql", "approved_sql_key"
            )
        except (ValueError, KeyError):
            pass
    payload = await build_result(
        state,
        runtime,
        Selection(
            status="partial" if current["best_effort_used"] else status,
            query_id=execution_id,
            definitions=definitions,
            assumptions=list(dict.fromkeys([*current["assumptions"], *assumptions])),
            conclusion=conclusion,
            unresolved=unresolved,
        ),
        extra={
            "control": {**summary(current), "revisions": current["revisions"]},
            "review": current.get("review"),
            "review_applies_to_selected_sql": review_matches,
            "stop_reason": "auto_delivered"
            if auto_delivered
            else "best_effort"
            if current["best_effort_used"]
            else "completed",
        },
    )
    if payload["status"] == "error":
        payload["stop_reason"] = "invalid_execution"
    current["terminal"] = payload
    return current
