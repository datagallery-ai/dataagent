"""Bind metavisor's v2 checker classes to DataAgent SQL and model callbacks."""

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage

from nl2sql.errors import DataAgentError

from .base import DEFAULT_REPAIR_ATTEMPTS, RepairError
from .join_checker import JoinChecker
from .max_min_checker import MaxMinChecker
from .order_by_limit_checker import OrderByLimitChecker
from .order_by_null_checker import OrderByNullChecker
from .prompts import build_prompt
from .result_checker import ResultChecker
from .select_checker import SelectChecker
from .syntax_checker import SyntaxChecker
from .time_checker import TimeChecker

logger = logging.getLogger(__name__)
ExecutionResult = tuple[Sequence[str] | None, Sequence[Sequence[Any]] | None, str | None]
Execute = Callable[[str], Awaitable[ExecutionResult]]


class Ask(Protocol):
    async def __call__(self, prompt: str, *, count: int, stop: list[str]) -> list[AIMessage]: ...


_CHECKERS = {
    "syntax": SyntaxChecker,
    "join": JoinChecker,
    "order_by_limit": OrderByLimitChecker,
    "time": TimeChecker,
    "select": SelectChecker,
    "max_min": MaxMinChecker,
    "order_by_null": OrderByNullChecker,
    "result": ResultChecker,
}
AVAILABLE_CHECKERS = tuple(_CHECKERS)
DEFAULT_CHECKERS = ("syntax", "result")


def classify_result(output: ExecutionResult) -> str:
    """Match metavisor's success/empty/all-NULL/error distinctions."""
    _, rows, error = output
    if error or rows is None:
        return (
            "timeout"
            if error and ("timed out" in error.lower() or "timeout" in error.lower())
            else "execution_error"
        )
    if not rows:
        return "empty_result"
    if not any(value is not None for row in rows for value in row):
        return "all_null_result"
    return "success"


def _format_execution(output: ExecutionResult) -> str:
    """Show the same psql result preview as the execution checker prompt."""
    columns, rows, error = output
    if error or rows is None:
        return error or "SQL execution returned no result."
    # Only empty/all-NULL results reach the execution prompt. Every displayed
    # data cell is blank, with an ellipsis row for results longer than five.
    headers = [str(column) for column in columns or ()]
    widths = [len(header) + 2 for header in headers]
    divider = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def row(cells):
        return "|" + "|".join(f" {cell:<{width}} " for cell, width in zip(cells, widths)) + "|"

    lines = [divider, row(headers), "|" + divider[1:-1] + "|"]
    lines.extend(row([""] * len(headers)) for _ in rows[:5])
    if len(rows) > 5:
        lines.append(row(["..."] * len(headers)))
    return "\n".join([*lines, divider])


@dataclass
class _Execution:
    result_type: str
    result_rows: Sequence[Sequence[Any]] | None
    output: ExecutionResult


@dataclass
class _CheckerRuntime:
    context: dict[str, Any]
    run_sql: Execute
    ask_model: Ask

    @property
    def question_id(self):
        return self.context.get("question_id")

    async def execute(self, sql):
        output = await self.run_sql(sql)
        return _Execution(classify_result(output), output[1], output)

    def execution_prompt(self, sql, result):
        return build_prompt(
            self.context, sql, "execution", [], _format_execution(result.output), result.result_type
        )

    def common_prompt(self, sql, suggestion):
        return build_prompt(self.context, sql, "common", [suggestion], "")

    async def ask(self, prompt, *, count, stop):
        try:
            replies = await self.ask_model(prompt, count=count, stop=stop)
        except (DataAgentError, RepairError):
            raise
        except Exception as exc:
            raise RepairError("Metavisor checker repair failed.") from exc
        usage = Counter()
        reasoning = []
        for reply in replies:
            token_usage = reply.usage_metadata or {}
            usage.update(
                {k: token_usage[k] for k in ("input_tokens", "output_tokens") if k in token_usage}
            )
            reasoning.append(reply.additional_kwargs.get("reasoning_content", ""))
        return replies, usage, reasoning


async def validate_sql(
    sql: str,
    *,
    execute: Execute,
    ask: Ask,
    context: dict[str, Any],
    checkers: Sequence[str] = DEFAULT_CHECKERS,
    max_parse_retries: int = DEFAULT_REPAIR_ATTEMPTS,
) -> str:
    """Dispatch the configured sequence, preserving each checker's sampling defaults."""
    runtime = _CheckerRuntime(context, execute, ask)
    for name in checkers:
        checker = _CHECKERS[name]()
        try:
            sql, _usage = await checker.check_and_revise(
                sql, runtime, max_parse_retries=max_parse_retries
            )
        except (DataAgentError, RepairError):
            raise
        except Exception as exc:
            logger.warning("Metavisor %s checker failed; retaining its input SQL: %s", name, exc)
    return sql
