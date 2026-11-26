"""Local BIRD result comparison; standard library only, no model/runtime imports.

Ported from bird-wheel-merged-successful/eval_script/evaluate_sql.py:
strict comparison is set(tuple(row)), ignoring row order and duplicate rows;
adjusted comparison additionally allows a fixed, injective column projection
(including column reordering), ONLY when predicted has more columns. Column
names are not compared. As in the historical evaluator, two empty result sets
are a strict match even if their column counts differ.

Both SQL statements execute completely on separate read-only connections.
This independent runner disables SQL/projection wall-clock deadlines by default.
The existing SQLite busy-lock wait (not a query execution deadline) stays 5s.
Read-only authorization and bounded projection search fail closed: an
execution/comparison error or timeout never counts as correct. Callers must
also exclude upstream agent failures, even if those failures left an SQL string.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any


def _execute(database: Path, sql: str, timeout: float | None) -> tuple[list[tuple], int, float]:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Empty SQL")
    started = time.monotonic()
    deadline = None if timeout is None else started + timeout
    connection = sqlite3.connect(
        database.as_uri() + "?mode=ro",
        uri=True,
        timeout=5.0 if timeout is None else min(5.0, timeout),
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }

        def authorize(action, first, second, db_name, trigger):
            if action not in allowed:
                return sqlite3.SQLITE_DENY
            if (
                action == sqlite3.SQLITE_FUNCTION
                and (second or first or "").lower() == "load_extension"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        if deadline is not None:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            cursor = connection.execute(sql)
            if cursor.description is None:
                raise ValueError("SQL did not return a result set")
            width = len(cursor.description)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"SQL execution exceeded {timeout:g} seconds") from exc
            raise
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"SQL execution exceeded {timeout:g} seconds")
        return rows, width, time.monotonic() - started
    finally:
        connection.close()


def _projection(
    expected: set[tuple],
    actual: set[tuple],
    width: int,
    predicted_width: int,
    search_limit: int,
    timeout: float | None,
) -> list[int] | None:
    """Historical fixed-mapping algorithm, with a wall-clock comparison budget."""
    deadline = None if timeout is None else time.monotonic() + timeout

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"Column projection comparison exceeded {timeout:g} seconds")

    def project(rows, indices):
        values = set()
        for index, row in enumerate(rows):
            if index % 1024 == 0:
                check_deadline()
            values.add(tuple(row[column] for column in indices))
        return values

    expected_values = [project(expected, [i]) for i in range(width)]
    actual_values = [project(actual, [i]) for i in range(predicted_width)]
    candidates = [
        [j for j in range(predicted_width) if values == actual_values[j]]
        for values in expected_values
    ]
    if any(not choices for choices in candidates):
        return None
    order = sorted(range(width), key=lambda i: len(candidates[i]))
    expected_prefixes = [project(expected, order[:depth]) for depth in range(1, width + 1)]
    mapping = [None] * width
    visited = 0

    def search(depth, used):
        nonlocal visited
        visited += 1
        check_deadline()
        if visited > search_limit:
            raise RuntimeError("projection_search_limit")
        if depth == width:
            return list(mapping) if project(actual, mapping) == expected else None
        column = order[depth]
        for candidate in candidates[column]:
            if candidate in used:
                continue
            mapping[column] = candidate
            # Marginals alone are insufficient: preserve row-wise correlations.
            if (
                project(actual, [mapping[i] for i in order[: depth + 1]])
                != expected_prefixes[depth]
            ):
                continue
            found = search(depth + 1, used | {candidate})
            if found is not None:
                return found
        return None

    return search(0, set())


def compare(
    db_path: str | Path,
    pred_sql: str,
    gold_sql: str,
    *,
    sql_timeout_seconds: float | None = None,
    projection_timeout_seconds: float | None = None,
    search_limit: int = 100_000,
) -> dict[str, Any]:
    """Compare full query results. ``extra_columns_correct`` includes strict matches.

    Returns booleans, an optional structured ``error``, comparison status, result
    dimensions and the adjusted projection (predicted indices in gold order).
    Normal wrong answers have ``error=None``. No rows or SQL text are returned.
    """
    result: dict[str, Any] = {
        "strict_correct": False,
        "extra_columns_correct": False,
        "extra_columns_only": False,
        "error": None,
        "comparison_status": "different",
        "projection_predicted_indices_for_gold": None,
        "gold_row_count": None,
        "gold_column_count": None,
        "predicted_row_count": None,
        "predicted_column_count": None,
    }
    started = time.monotonic()
    stage = "configuration"
    try:
        for name, timeout in (
            ("sql_timeout_seconds", sql_timeout_seconds),
            ("projection_timeout_seconds", projection_timeout_seconds),
        ):
            if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
                raise ValueError(f"{name} must be finite and positive")
        if isinstance(search_limit, bool) or not isinstance(search_limit, int) or search_limit < 1:
            raise ValueError("search_limit must be a positive integer")
        database = Path(db_path).resolve(strict=True)
        if not database.is_file():
            raise ValueError("db_path must be a SQLite file")
        executed = {}
        for stage, sql in (("gold", gold_sql), ("predicted", pred_sql)):
            rows, width, elapsed = _execute(database, sql, sql_timeout_seconds)
            result[f"{stage}_row_count"] = len(rows)
            result[f"{stage}_column_count"] = width
            result[f"{stage}_elapsed_seconds"] = elapsed
            executed[stage] = rows
        stage = "comparison"
        expected, actual = set(executed["gold"]), set(executed["predicted"])
        if expected == actual:
            result.update(
                strict_correct=True, extra_columns_correct=True, comparison_status="strict_match"
            )
        elif result["predicted_column_count"] > result["gold_column_count"]:
            mapping = _projection(
                expected,
                actual,
                result["gold_column_count"],
                result["predicted_column_count"],
                search_limit,
                projection_timeout_seconds,
            )
            if mapping is not None:
                result.update(
                    extra_columns_correct=True,
                    extra_columns_only=True,
                    comparison_status="extra_columns_only",
                    projection_predicted_indices_for_gold=mapping,
                )
    except Exception as exc:
        result.update(
            strict_correct=False,
            extra_columns_correct=False,
            extra_columns_only=False,
            comparison_status=(
                "execution_error" if stage in {"gold", "predicted"} else "comparison_error"
            ),
            error={"stage": stage, "type": type(exc).__name__, "message": str(exc)},
        )
    result["elapsed_seconds"] = time.monotonic() - started
    return result
