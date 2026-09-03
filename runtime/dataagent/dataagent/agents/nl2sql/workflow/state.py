"""Native LangGraph state contracts for the NL2SQL subagent."""

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from deepagents.middleware.filesystem import FilesystemState

from dataagent.utils.constants import DEFAULT_NL2SQL_REF_RETRIES, DEFAULT_NL2SQL_SEL_RETRIES


@dataclass
class Result:
    """One mutable SQL candidate as it moves through the deterministic pipeline."""

    id: int
    sql: str
    prompt: str = ""
    strategy: Literal["prompt", "skeleton", "icl", "dc"] = "prompt"
    score: float = 0.0
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    columns: list[str] | None = field(default_factory=list)
    rows: list[tuple[Any, ...]] | None = field(default_factory=list)
    rows_preview: list[tuple[str, ...]] | None = field(default_factory=list)
    error: str | None = None
    need_ref: bool = False
    security_checked: bool = False
    security_violations: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class NL2SQLStructuredResult:
    """Compact NL2SQL result returned to the parent agent."""

    sql: str
    sql_path: str
    csv_path: str
    columns: list[str]
    row_count: int
    rows_preview: list[list[Any]]
    confidence: float
    error: str | None = None


class NL2SQLInputState(FilesystemState, total=False):
    """Public input accepted by the NL2SQL subgraph."""

    question: str


class NL2SQLState(NL2SQLInputState, total=False):
    """Internal state used by the deterministic NL2SQL graph."""

    sql: str
    confidence: float
    columns: list[str] | None
    rows: list[tuple[Any, ...]] | None
    rows_preview: list[tuple[str, ...]] | None
    error: str | None
    keywords: list[str]
    schema: dict[str, Any]
    joins: list[tuple[str, str]]
    schema_str: str
    few_shot_examples: str
    sql_rules: str
    evidence: str
    generation_results: list[Result]
    validation_results: list[Result]
    security_sql_approved: bool
    ref_retries: int
    proceed: bool
    execution_results: list[Result]
    sel_retries: int
    stream_message: str


class NL2SQLOutputState(FilesystemState, total=False):
    """Public output merged by Deep Agents after a compiled-subagent call."""

    pass


def get_default_state(question: str, **override: Any) -> NL2SQLState:
    """Return a fresh native NL2SQL state with default field values."""
    default_state: NL2SQLState = {
        "messages": [],
        "files": {},
        "question": question,
        "sql": "",
        "confidence": 0.0,
        "columns": None,
        "rows": None,
        "rows_preview": None,
        "error": None,
        "keywords": [],
        "schema": {},
        "joins": [],
        "schema_str": "",
        "few_shot_examples": "",
        "evidence": "",
        "sql_rules": "",
        "generation_results": [],
        "validation_results": [],
        "security_sql_approved": False,
        "ref_retries": DEFAULT_NL2SQL_REF_RETRIES,
        "proceed": True,
        "execution_results": [],
        "sel_retries": DEFAULT_NL2SQL_SEL_RETRIES,
        "stream_message": "",
    }
    return cast(NL2SQLState, {**default_state, **override})
