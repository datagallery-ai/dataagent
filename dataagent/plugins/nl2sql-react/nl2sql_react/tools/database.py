"""Prompt-driven SQL agent tools over the shared DataAgent SQL service."""

import asyncio
from pathlib import Path
from threading import Event
from typing import Annotated, Literal

import sqlglot
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage
from pydantic import Field, field_validator
from sqlglot import exp

from dataagent import (
    AgentContext,
    RecallError,
    RecallOptions,
    build_sql_service,
    recall_database,
    safe_error,
)
from nl2sql_react.models import StrictModel
from nl2sql_react.query_control.state import ControlPolicy, ledger


class DBExploreOptions(RecallOptions):
    """The ReAct tool allows more exploration turns than the workflow default."""

    max_steps: int = Field(default=8, ge=2, le=64)
    max_tool_rounds: int | None = Field(default=1, ge=1, le=16)


class ExampleOptions(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_examples: int = Field(default=3, ge=1, le=6, strict=True)
    max_chars: int = Field(default=6000, ge=1000, le=16000, strict=True)


class DatabaseConfig(StrictModel):
    """Plugin-owned database parameters, following the original NL2SQL layout."""

    engine: str = "sqlite"
    dialect: str = "sqlite"
    db_id: str | None = None
    description: str = ""
    path: str = Field(min_length=1)
    descriptions_dir: str | None = None

    @field_validator("path", "descriptions_dir")
    @classmethod
    def absolute_path(cls, value):
        if value is None:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(
                "Use an absolute database path (or ~/...); plugin parameters have no config-file path binding"
            )
        return str(path.resolve())


class Config(StrictModel):
    database: DatabaseConfig
    schema_prefetch: bool = True
    schema_prefetch_strategy: Literal["catalog", "question"] = "catalog"
    schema_sample_rows: int = Field(default=0, ge=0, le=3, strict=True)
    schema_value_hints: bool = False
    # Deterministic, non-blocking warnings returned with answer-SQL results.
    sql_lint: bool = True
    projection_check: bool = True
    # Deliver the sole successful answer for the current contract if finish_query is omitted.
    deliver_unfinished_answer: bool = True
    # Compatibility field for older configs; the root uses its ordinary delegation loop.
    direct_result_delivery: bool = False
    independent_initial_exploration: bool = False
    examples: ExampleOptions | None = None
    user_sql_rules: Literal["sql_rules_bird"] | None = None
    review_sql: bool = True
    review_mode: Literal["critique", "independent"] = "critique"
    review_timeout_seconds: float = Field(default=180, gt=0, le=180)
    review_max_retries: int = Field(default=3, ge=0, le=3, strict=True)
    max_rows: int = Field(default=1000, ge=1, le=10000, strict=True)
    query_timeout_seconds: float = Field(default=30, gt=0, le=600, allow_inf_nan=False)
    dbexplore: DBExploreOptions = Field(default_factory=DBExploreOptions)
    control: ControlPolicy = Field(default_factory=ControlPolicy)


def configuration(context):
    config = Config.model_validate(context.get_plugin_config("nl2sql-react"))
    database = config.database
    source = {
        "engine": database.engine,
        "dialect": database.dialect,
        "db_id": database.db_id,
        "description": database.description,
        "connection": {"path": database.path},
    }
    if database.descriptions_dir is not None:
        source["connection"]["descriptions_dir"] = database.descriptions_dir
    if source["engine"] not in {"sqlite", "sqlite3"}:
        raise ValueError("nl2sql-react currently supports SQLite read-only tools")
    return config, source


def redaction_secrets(context):
    """Keep database connection values out of Local diagnostics without host changes."""
    secrets = list(getattr(context, "redaction_secrets", ()))
    getter = getattr(context, "get_plugin_config", lambda _: {})
    raw = getter("nl2sql-react").get("database", {})
    for name in ("path", "descriptions_dir"):
        value = raw.get(name)
        if isinstance(value, str):
            secrets.append(value)
            secrets.append(str(Path(value).expanduser().resolve()))
    return tuple(dict.fromkeys(secrets))


def validate_config(state, runtime, *, params):
    configuration(runtime.context)


def _run(config, source, operation, args, cancelled):
    with build_sql_service(
        source["engine"],
        source["connection"],
        timeout_seconds=config.query_timeout_seconds,
        cancelled=cancelled.is_set,
    ) as service:
        if operation == "search_objects":
            detail = args.pop("detail_level")
            result = service.search_objects(**args)
            if detail == "full":
                metadata = {}
                for item in result["objects"]:
                    table = item["table_name"]
                    if table not in metadata:
                        metadata[table] = service.describe_table(table)
                result["tables"] = list(metadata.values())
            return {**result, "truncated": result["next_offset"] is not None}
        sql = args["sql"]
        if not sql.strip() or len(sql) > 64000:
            raise ValueError("SQL must contain 1 to 64000 characters")
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("Exactly one read-only SELECT query is required")
        # Execute the original SQL under the service's engine-level authorizer.
        # Unlike basic data-access tools, JOINs, CTEs and subqueries are allowed.
        columns, rows, error = service.execute_readonly(sql, limit=config.max_rows)
        if error:
            raise ValueError(error)
        return {
            "sql": sql,
            "columns": columns,
            "rows": [
                ["0x" + v.hex() if isinstance(v, bytes) else v for v in row]
                for row in rows[: config.max_rows]
            ],
            "row_count": min(len(rows), config.max_rows),
            "truncated": len(rows) > config.max_rows,
        }


async def _invoke(runtime, operation, **args):
    config, source = configuration(runtime.context)
    cancelled = Event()
    base = {
        "source": (source.get("db_id") or "database"),
        "db_id": source.get("db_id"),
        "dialect": source["dialect"],
        "operation": operation,
    }
    try:
        result = await asyncio.to_thread(_run, config, source, operation, args, cancelled)
        return {**base, **result, "error": None}
    except Exception as error:
        return {**base, "error": safe_error(error, redaction_secrets(runtime.context))}
    finally:
        cancelled.set()


@tool(response_format="content_and_artifact")
async def search_objects(
    runtime: ToolRuntime[AgentContext],
    object_type: Literal["table", "column"] = "table",
    pattern: str = "%",
    detail_level: Literal["names", "full"] = "names",
    offset: int = 0,
    limit: int = 100,
    issue_id: str = "",
) -> tuple[dict, dict]:
    """Discover tables/views or columns in the configured SQLite database using SQL LIKE.

    Use detail_level='full' to include columns, foreign keys, indexes and DDL for
    matching tables. In the subagent, issue_id must reference an open database issue
    registered with update_query_state. Follow next_offset for additional matches. No source argument
    is needed: the plugin is bound to one configured datasource. limit is 1..1000.
    """
    if not 1 <= limit <= 1000 or offset < 0:
        result = {
            "error": {"code": "invalid_pagination", "message": "limit must be 1..1000; offset >= 0"}
        }
    else:
        result = await _invoke(
            runtime,
            "search_objects",
            object_type=object_type,
            pattern=pattern,
            detail_level=detail_level,
            offset=offset,
            limit=limit,
        )
    return result, {"nl2sql_observation": result}


@tool(response_format="content_and_artifact")
async def execute_sql(
    sql: str,
    runtime: ToolRuntime[AgentContext],
    purpose: Literal["probe", "answer"] = "probe",
    issue_id: str = "",
    contract_id: str = "",
    repair_issue_id: str = "",
    repair_reason: str = "",
) -> tuple[dict, dict]:
    """Execute one read-only SQLite SELECT, including JOINs, CTEs and subqueries.

    Set purpose='probe' for diagnostics and purpose='answer' for a checked answer
    candidate. The subagent's result hook saves successful executions and returns
    query_id/execution_id. A probe needs an open issue_id; an answer requires the
    current locked contract_id. Finish with finish_query(execution_id=...). No file-writing call is
    needed. Returns columns, rows,
    row_count and an exact truncated flag; row_count is returned rows, not a total
    when truncated. BLOB values are hex strings. Inspect error before using rows.
    Source, row cap and query timeout come from plugin configuration.
    After a successful answer, a corrected answer with the same contract requires
    repair_issue_id for a newly resolved issue and repair_reason; its evidence must
    be newer than that answer. This is not permission to enumerate interpretations.
    """
    result = {**await _invoke(runtime, "execute_sql", sql=sql), "purpose": purpose}
    # Native filesystem middleware may offload visible content before after_tool.
    # artifact is retained by that middleware and is never sent to the model.
    return result, {"nl2sql_execution": result, "nl2sql_observation": result}


@tool(response_format="content_and_artifact")
async def db_explore(
    question: Annotated[str, Field(min_length=1, max_length=16000)],
    runtime: ToolRuntime[AgentContext],
    evidence: Annotated[str, Field(max_length=32000)] = "",
    focus: Annotated[str, Field(max_length=8000)] = "",
    issue_id: str = "",
) -> tuple[dict, dict]:
    """Ask the bounded database exploration agent to gather query context, not an answer.

    Associate the call with a registered database issue_id. Call before generating SQL when relevant tables/columns, join grain, stored values
    or time encodings remain unclear, or after an error reveals a missing database fact.
    For known schema and clear semantics, use search_objects/execute_sql directly.
    Pass the original question and all supplied evidence unchanged; focus names specific
    gaps and prior observations/errors. Do not delegate SQL generation or execution of
    the final answer. The source and exploration budgets come from configuration.
    A ready result contains observed schema, joins, DDL and an evidence report.
    Inspect status, report.unresolved, assumptions and truncation in observations;
    unresolved/error is not permission to guess. Reuse results; avoid duplicate exploration.
    """
    config, source = configuration(runtime.context)
    base = {
        "source": (source.get("db_id") or "database"),
        "db_id": source.get("db_id"),
        "dialect": source["dialect"],
        "operation": "db_explore",
        "limits": config.dbexplore.model_dump(),
    }
    if config.independent_initial_exploration and not ledger(runtime.state)["contract"]:
        # The initial task carries the original question and supplied evidence.
        # Do not feed the actor's tentative mappings back as authoritative premises.
        question = next(m.text for m in runtime.state["messages"] if isinstance(m, HumanMessage))
        evidence = ""
        focus = (
            "Ground the original task independently, including its supplied evidence. "
            "Identify the requested population before choosing table/column mappings and join paths. "
            "Do not substitute a related population merely because it is easy to probe. "
            "Report any missing business choice separately from observable database facts."
        )
        base["input_mode"] = "original_task"
    try:
        recalled = await recall_database(
            question,
            evidence=evidence,
            focus=focus,
            model=runtime.context.get_model(),
            database={"engine": source["engine"], **source["connection"]},
            options=config.dbexplore,
            reserve_description_round="max_tool_rounds" not in config.dbexplore.model_fields_set,
        )
        result = {
            **base,
            "status": "ready",
            "schema": recalled.context.schema,
            "schema_str": recalled.context.schema_str,
            "joins": recalled.context.joins,
            "report": {k: v for k, v in recalled.report.items() if k != "schema"},
            "error": None,
        }
    except RecallError as error:
        result = {
            **base,
            "status": "unresolved" if error.recall_report.get("unresolved") else "error",
            "schema": error.recall_report.get("schema", {}),
            "schema_str": "",
            "joins": [],
            "report": {k: v for k, v in error.recall_report.items() if k != "schema"},
            "error": safe_error(error, redaction_secrets(runtime.context)),
        }
    except Exception as error:
        result = {
            **base,
            "status": "error",
            "schema": {},
            "schema_str": "",
            "joins": [],
            "report": {},
            "error": safe_error(error, redaction_secrets(runtime.context)),
        }
    return result, {"nl2sql_observation": result}
