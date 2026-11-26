"""Run the metavisor v2 checker chain after generation and feed the native Selector."""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import HumanMessage

from dataagent import build_sql_service, check_read_only_sql, check_sql, collect_model_stream
from nl2sql.constants import DEFAULT_NL2SQL_PREVIEW_LIMIT
from nl2sql.errors import DataAgentError
from nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from nl2sql.nodes.checkers.base import DEFAULT_REPAIR_ATTEMPTS
from nl2sql.nodes.checkers.checkers import DEFAULT_CHECKERS, RepairError, validate_sql
from nl2sql.state import NL2SQLState, Result
from nl2sql.utils.nl2sql_utils import truncate

logger = logging.getLogger(__name__)


@dataclass
class _Execution:
    sql: str
    columns: list[str] | None
    rows: list[tuple[Any, ...]] | None
    error: str | None
    blocked: bool = False


async def _collect(awaitables):
    """Return in candidate order, cancelling sibling requests when one fails."""
    tasks = [asyncio.create_task(call) for call in awaitables]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BirdValidatorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="bird-validator", **kwargs)
        self.checkers = tuple(kwargs.get("checkers", DEFAULT_CHECKERS))
        self.repair_attempts = kwargs.get("repair_attempts", DEFAULT_REPAIR_ATTEMPTS)
        self.preview_limit = kwargs.get("preview_limit", DEFAULT_NL2SQL_PREVIEW_LIMIT)
        self.sql_security_enabled = kwargs.get("sql_security_enabled", False)

    def _emit_reasoning_tag(self):
        explicit = self._config.get("emit_reasoning_tag")
        if explicit is not None:
            return explicit
        extra = getattr(self.model, "extra_body", None) or {}
        reasoning = extra.get("reasoning") or {}
        thinking = extra.get("thinking") or {}
        return not (
            isinstance(reasoning, dict)
            and (
                reasoning.get("enabled") is True
                or reasoning.get("effort") in ("low", "medium", "high", "max", "xhigh")
            )
            or isinstance(thinking, dict)
            and thinking.get("type") == "enabled"
            or extra.get("enable_thinking") is True
        )

    async def _ask(self, prompt: str, *, count: int, stop: list[str]):
        return await _collect(
            collect_model_stream(self.model, [HumanMessage(content=prompt)], stop=stop)
            for _ in range(count)
        )

    def _execute(self, sql: str, schema: dict) -> _Execution:
        if self.sql_security_enabled:
            checked = check_sql(sql, dialect=self.dialect, schema=schema, policy="nl2sql")
        else:
            checked = check_read_only_sql(sql, dialect=self.dialect)
        if checked.violations:
            return _Execution(
                sql, None, None, "; ".join(v.message for v in checked.violations), True
            )
        sql = checked.normalized_sql or sql
        with build_sql_service(self.engine, self._config.connection_config()) as service:
            columns, rows, error = service.execute(sql, limit=-1)
        if self.engine == "sqlite" and error == "interrupted":
            error = f"Query timed out after {service.TIME_OUT} seconds."
        return _Execution(sql, columns, rows, error)

    async def _aprocess(self, state: NL2SQLState) -> NL2SQLState:
        context = {
            "dialect": self.dialect,
            "schema": state["schema_str"],
            "question": state["question"],
            "evidence": state["evidence"],
            "sql_guidance": state["sql_rules"],
            "emit_reasoning_tag": self._emit_reasoning_tag(),
        }
        # Results (including errors) belong to this question and this database.
        cache: dict[str, asyncio.Task[_Execution]] = {}

        async def execution(sql):
            if sql not in cache:
                cache[sql] = asyncio.create_task(
                    asyncio.to_thread(self._execute, sql, state["schema"])
                )
            return await cache[sql]

        async def execute(sql):
            output = await execution(sql)
            return output.columns, output.rows, output.error

        async def validate(candidate: Result):
            try:
                sql = await validate_sql(
                    candidate.sql,
                    execute=execute,
                    ask=self._ask,
                    context=context,
                    checkers=self.checkers,
                    max_parse_retries=self.repair_attempts,
                )
            except RepairError as exc:
                logger.warning(
                    "Skipping BIRD candidate %s after repair failed: %s", candidate.id, exc
                )
                return None
            output = await execution(sql)
            if output.blocked:
                return None
            rows = output.rows
            preview = rows if rows is None or self.preview_limit < 0 else rows[: self.preview_limit]
            return replace(
                candidate,
                sql=output.sql,
                columns=output.columns,
                rows=rows,
                rows_preview=None
                if preview is None
                else [tuple(truncate(v) for v in row) for row in preview],
                error=output.error,
                row_count=len(rows or ()),
                total_known=rows is not None,
                truncated=False,
                security_checked=self.sql_security_enabled,
                security_violations=[],
            )

        try:
            results = await _collect(
                validate(candidate) for candidate in state["generation_results"]
            )
        finally:
            for task in cache.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*cache.values(), return_exceptions=True)
        safe = [r for r in results if r is not None]
        if not safe:
            raise DataAgentError(
                source="constraint",
                component="nl2sql",
                fact="No eligible SQL candidate after BIRD validation.",
            )
        state["generation_results"] = []
        state["execution_results"] = safe
        state["security_sql_approved"] = True
        state["proceed"] = True
        state["stream_message"] = f"=== BIRD Validator ===\nChecked {len(results)} candidates."
        return state
