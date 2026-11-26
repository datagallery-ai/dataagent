"""Serializable task ledger carried by native tool-message artifacts."""

import hashlib
import json
import re
from copy import deepcopy
from typing import Literal

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import Field

from nl2sql_react.models import StrictModel

KEY = "nl2sql_query_control"
PREFIX = "nl2sql-react__"
DATA_TOOLS = {PREFIX + name for name in ("search_objects", "execute_sql", "db_explore")}


def message(current, tool_call_id, name, content, *, status="success", artifact=None):
    return ToolMessage(
        content=json.dumps(content, ensure_ascii=False),
        tool_call_id=tool_call_id,
        name=name,
        status=status,
        artifact={**(artifact or {}), KEY: current},
    )


class MissingEvidenceReference(ValueError):
    """A new observation exists, but the model has not identified its relevance."""


class RepairProbeRequired(MissingEvidenceReference):
    """An answer exposed a repair issue that still needs its own observation."""


class MissingBusinessChoice(ValueError):
    """The assumption and its target do not identify one open business choice."""


class MissingDraftSQL(MissingEvidenceReference):
    """The initial lock needs a draft for the code-enforced review."""


class ControlPolicy(StrictModel):
    ambiguity: Literal["clarify", "best_effort_once"] = "clarify"
    max_probes_per_issue: int = Field(default=4, ge=1, le=4, strict=True)
    max_total_probes: int = Field(default=8, ge=1, le=24, strict=True)
    max_issues: int = Field(default=6, ge=1, le=12, strict=True)
    max_contract_revisions: int = Field(default=1, ge=0, le=2, strict=True)
    max_answer_attempts: int = Field(default=3, ge=1, le=5, strict=True)


class QueryContract(StrictModel):
    metric: str = Field(min_length=1, max_length=1000)
    grain: str = Field(min_length=1, max_length=1000)
    numerator: str = Field(min_length=1, max_length=1000)
    denominator: str | None = Field(default=None, max_length=1000)
    joins: list[str] = Field(max_length=12)
    filters: list[str] = Field(default_factory=list, max_length=16)
    # The fields the request asks to SEE, one short name each, in the order they should
    # appear. This is not the set of values the SQL must compute: a ranking key, a
    # tie-break column or a filter basis is computed without being requested. Declaring
    # them here is what lets the runtime compare the answered projection against the
    # request instead of guessing from SQL structure.
    output_columns: list[str] = Field(default_factory=list, max_length=16)


class Issue(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    kind: Literal["database_fact", "business_choice"]
    question: str = Field(min_length=1, max_length=1000)
    acceptance: str = Field(
        min_length=1,
        max_length=1000,
        description="The specific fact or user choice that closes this issue",
    )
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class Resolution(StrictModel):
    issue_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    finding: str = Field(min_length=1, max_length=1500)


def policy(context):
    return ControlPolicy.model_validate(
        context.get_plugin_config("nl2sql-react").get("control", {})
    )


def task_id(state):
    initial = next(m for m in state["messages"] if isinstance(m, HumanMessage))
    if not initial.id:
        raise ValueError("Initial task message ID is required")
    return hashlib.sha256(initial.id.encode()).hexdigest()[:32]


def ledger(state):
    identifier = task_id(state)
    for message in reversed(state["messages"]):
        if isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
            stored = message.artifact.get(KEY)
            if stored and stored["task_id"] == identifier:
                return deepcopy(stored)
    return {
        "task_id": identifier,
        "contract": None,
        "contract_id": None,
        "version": 0,
        "contract_watermark": 0,
        "revisions": [],
        "issues": {},
        "evidence": {},
        "total_probes": 0,
        "answer_attempts": 0,
        "invalid_calls": 0,
        "fingerprints": [],
        "best_effort_used": False,
        "assumptions": [],
        "terminal": None,
    }


def pending(current):
    return {key: value for key, value in current["issues"].items() if value["status"] == "open"}


def summary(current):
    return {
        key: current[key]
        for key in (
            "contract_id",
            "contract",
            "version",
            "issues",
            "evidence",
            "total_probes",
            "best_effort_used",
            "assumptions",
            "invalid_calls",
        )
    }


def normalized(text):
    return re.sub(r"\W+", "", text.casefold())


def contract_id(current, contract):
    body = json.dumps([current["task_id"], current["version"] + 1, contract], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:24]
