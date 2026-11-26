"""Explicit query-contract management and terminal delivery tools."""

from typing import Literal

from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from dataagent import AgentContext
from nl2sql_react.query_control.flow import blocked, finish, message, update
from nl2sql_react.query_control.state import (
    Issue,
    MissingBusinessChoice,
    MissingDraftSQL,
    MissingEvidenceReference,
    QueryContract,
    RepairProbeRequired,
    Resolution,
    ledger,
    pending,
    policy,
    summary,
)
from nl2sql_react.tools.database import configuration
from nl2sql_react.tools.review import review_draft

_RETRYABLE_UPDATE_CODES = {
    MissingEvidenceReference: "missing_evidence_reference",
    MissingBusinessChoice: "missing_business_choice",
    RepairProbeRequired: "repair_probe_required",
    MissingDraftSQL: "draft_sql_required",
}


@tool
async def update_query_state(
    runtime: ToolRuntime[AgentContext],
    contract: QueryContract | None = None,
    issues: list[Issue] | None = None,
    resolutions: list[Resolution] | None = None,
    revision_evidence: list[str] | None = None,
    revision_reason: str = "",
    best_effort_assumption: str = "",
    draft_sql: str | None = None,
    best_effort_issue_id: str | None = None,
) -> Command:
    """Register concrete gaps, close them using evidence, and lock the query contract.

    If a concrete fact is missing, first register a database issue with id, question,
    and acceptance. If observed schema/evidence suffice, draft and validate the SQL,
    then lock its contract directly without creating an issue or running probes.
    search_objects, probe SQL and db_explore must reference that issue_id. Close an
    issue using successful evidence_ids from that issue plus a concrete finding;
    ready db_explore closes its requested issue automatically. Then lock metric,
    grain, numerator, denominator (null if none), joins and filters in contract.
    Supply draft_sql when first locking a contract. Code reviews it once and may
    apply the reviewer's one repaired SQL and contract before locking. Execute the
    returned approved_sql with the returned contract_id, not the original draft.
    Unresolved or failed reviews cannot authorize execution. review_query is optional;
    its matching result is reused without a second review. Later evidenced database
    repairs continue through the existing repair protocol, not a new review budget.
    Use the returned resolved_findings and contract to check the final SQL.
    Also set contract.output_columns to the fields the request asks to SEE, one short
    name each, in output order. This is not the list of values the SQL computes: a
    ranking key, a tie-break column or a filter basis is computed without being shown,
    so leave it out of output_columns and out of the SELECT list. When the request asks
    for one measure only, output_columns has exactly one entry. The runtime compares
    the answered projection against this list and returns projection_check when they
    differ.
    A locked contract changes only with fresh contradiction evidence and a reason.
    To register a repair issue after locking, put the new observation IDs in that
    issues[] item's evidence_ids. Top-level revision_evidence is only for a contract
    change; it does not supply evidence for registering or resolving an issue.
    A successful answer can register a repair issue. Resolve it using new evidence
    from a probe tied to that issue_id before re-executing the answer.
    Reuse issue IDs; renaming a gap does not reset budgets. Register business_choice
    when meaning is missing: default policy ends the task for clarification.
    Only configured best_effort_once permits one explicit assumption plus contract.
    Use best_effort_issue_id to target the open business_choice addressed by the assumption.
    The ID is required when multiple business choices are open. Remaining business choices
    still require clarification. Use observations to resolve database_fact gaps.
    After locking, execute the answer; do not open speculative issues without evidence.
    Call alone; inspect the returned contract_id, issue states and evidence IDs.
    """
    current = ledger(runtime.state)
    try:
        settings = policy(runtime.context)
        options = dict(
            issues=issues or [],
            resolutions=resolutions or [],
            revision_evidence=revision_evidence or [],
            revision_reason=revision_reason,
            best_effort_assumption=best_effort_assumption,
            best_effort_issue_id=best_effort_issue_id,
        )
        candidate = update(current, settings, contract=contract, **options)
        config, _ = configuration(runtime.context)
        if (
            contract is not None
            and not current["contract"]
            and not candidate["terminal"]
            and config.review_sql
        ):
            if not draft_sql or not draft_sql.strip():
                raise MissingDraftSQL(
                    "No state changed. Supply draft_sql with the initial contract for automatic review."
                )
            if pending(candidate):
                raise ValueError(
                    "Resolve observed database gaps before reviewing and locking the answer"
                )
            review = await review_draft(draft_sql, contract, runtime, candidate)
            # Preserve the spent review budget even when the review cannot authorize a lock.
            current["review_calls"] = candidate.get("review_calls", 0)
            current["review"] = review
            if review["status"] != "reviewed":
                blocked(current, "review_failed", "The review failed; no answer was authorized.")
            elif review["unresolved"]:
                blocked(
                    current,
                    "review_unresolved",
                    "; ".join(review["unresolved"]),
                    status="needs_clarification",
                )
            else:
                # Initial contract only: one repair may change SQL and its definition together.
                candidate = update(
                    current,
                    settings,
                    contract=QueryContract.model_validate(review["contract"]),
                    **options,
                )
                candidate["approved_sql"] = review["sql"]
                candidate["approved_sql_key"] = review["output_key"]
                candidate["reviewed_contract_id"] = candidate["contract_id"]
                current = candidate
        else:
            current = candidate
    except (MissingEvidenceReference, MissingBusinessChoice) as error:
        current["invalid_calls"] += 1
        if current["invalid_calls"] >= 2:
            blocked(current, "invalid_control_update", str(error))
        else:
            return Command(
                update={
                    "messages": [
                        message(
                            current,
                            runtime.tool_call_id,
                            "nl2sql-react__update_query_state",
                            {
                                "error": {
                                    "code": _RETRYABLE_UPDATE_CODES[type(error)],
                                    "message": str(error),
                                },
                                "retry_allowed": True,
                                "control": summary(current),
                            },
                            status="error",
                        )
                    ]
                }
            )
    except ValueError as error:
        blocked(current, "invalid_control_update", str(error))
    content = current["terminal"] or {
        "status": "recorded",
        **summary(current),
        "ambiguity_policy": policy(runtime.context).ambiguity,
    }
    if contract is not None and not current["terminal"]:
        if current.get("reviewed_contract_id") == current["contract_id"]:
            content["approved_sql"] = current["approved_sql"]
        if current.get("review"):
            content["review"] = current["review"]
        content["resolved_findings"] = [
            {"issue_id": key, "evidence_ids": issue["evidence_ids"], "finding": issue["finding"]}
            for key, issue in current["issues"].items()
            if issue["kind"] == "database_fact" and issue["status"] == "resolved"
        ]
        content["answer_check"] = (
            "Check the final SQL's filters, denominator and aggregation scope against "
            "the locked contract and resolved findings before executing it."
        )
    return Command(
        update={
            "messages": [
                message(current, runtime.tool_call_id, "nl2sql-react__update_query_state", content)
            ]
        }
    )


@tool(return_direct=True)
async def finish_query(
    runtime: ToolRuntime[AgentContext],
    execution_id: str | None = None,
    definitions: list[str] | None = None,
    assumptions: list[str] | None = None,
    conclusion: str = "",
    status: Literal["success", "partial", "needs_clarification", "error"] = "success",
    unresolved: list[str] | None = None,
) -> Command:
    """Deliver a checked execution and end this subagent immediately, without another model call.

    Call alone, using the execution_id returned by a successful answer SQL for the
    current contract. Code verifies task/contract, unresolved issues and saved results.
    Give only concise definitions, assumptions and conclusion, never SQL/rows/files.
    For an unsolved choice or failure, set status=needs_clarification or error, supply
    unresolved, and leave execution_id null. A partial result uses status=partial.
    File errors are reported with the verified result; do not retry delivery in a loop.
    """
    current = ledger(runtime.state)
    if status not in {"success", "partial", "needs_clarification", "error"}:
        blocked(current, "invalid_finish", "Unknown finish status")
    else:
        current = await finish(
            current,
            runtime,
            execution_id=execution_id,
            definitions=definitions or [],
            assumptions=assumptions or [],
            conclusion=conclusion,
            status=status,
            unresolved=unresolved or [],
        )
    return Command(
        update={
            "messages": [
                message(
                    current, runtime.tool_call_id, "nl2sql-react__finish_query", current["terminal"]
                )
            ]
        }
    )
