"""Reject ClickHouse 23.8-unsafe flattened multi-JOIN SQL.

ClickHouse 23.8 mis-resolves a repeated key column (often ``usid``) when one
FROM clause has more than one LEFT/INNER/CROSS JOIN, and reports
``Missing columns: 'usid'`` even when the column exists. Proven workaround:
exactly one JOIN per SELECT layer (nested subqueries). Diagnostic extra tables
and ``USING`` do not fix it.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_FROM_RE = re.compile(r"\bFROM\b", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)
_USING_RE = re.compile(r"\bUSING\s*\(", re.IGNORECASE)
_DIAGNOSTIC_TABLE_RE = re.compile(
    r"\b(?:step2_3_test\w*|step2_3_with_\w+)\b",
    re.IGNORECASE,
)
_REGION_END_RE = re.compile(
    r"\b(?:WHERE|GROUP|HAVING|ORDER|UNION|LIMIT|SETTINGS|INTO|FORMAT)\b",
    re.IGNORECASE,
)
_ARRAY_PREFIX_RE = re.compile(r"(?:LEFT\s+)?ARRAY$", re.IGNORECASE)


def _strip_comments_and_literals(sql: str) -> str:
    """Replace comments and quoted literals with spaces so scans stay aligned."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        two = sql[i : i + 2]
        if two == "--":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if two == "/*":
            i += 2
            out.extend("  ")
            while i < n and sql[i : i + 2] != "*/":
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            if i < n:
                out.extend("  ")
                i += 2
            continue
        if sql[i] in {'"', "'", "`"}:
            quote = sql[i]
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == "\\" and quote != "`":
                    out.append(" ")
                    i += 1
                    if i < n:
                        out.append(" ")
                        i += 1
                    continue
                if sql[i] == quote:
                    out.append(" ")
                    i += 1
                    if quote == "'" and i < n and sql[i] == "'":
                        out.append(" ")
                        i += 1
                        continue
                    break
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def _is_array_join(sql: str, join_at: int) -> bool:
    """Return True when JOIN is part of ARRAY JOIN / LEFT ARRAY JOIN."""
    prefix = sql[:join_at].rstrip()
    return bool(_ARRAY_PREFIX_RE.search(prefix[-16:] if len(prefix) >= 16 else prefix))


def _region_end(sql: str, start: int, region_depth: int) -> int:
    """Scan from just after FROM until the clause ends at *region_depth*."""
    depth = region_depth
    j = start
    n = len(sql)
    while j < n:
        ch = sql[j]
        if ch == "(":
            depth += 1
            j += 1
            continue
        if ch == ")":
            if depth == region_depth:
                return j
            depth = max(0, depth - 1)
            j += 1
            continue
        if depth == region_depth and _REGION_END_RE.match(sql, j):
            return j
        j += 1
    return n


def _from_regions(sql: str) -> list[tuple[int, int]]:
    """Return (from_index, region_end) for each FROM clause."""
    regions: list[tuple[int, int]] = []
    depth = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        match = _FROM_RE.match(sql, i)
        if match:
            regions.append((match.start(), _region_end(sql, match.end(), depth)))
            i = match.end()
            continue
        i += 1
    return regions


def check_join_nesting(sql: str) -> list[str]:
    """Return human-readable errors; empty list means the SQL is nested safely."""
    errors: list[str] = []
    scanned = _strip_comments_and_literals(sql)
    if _USING_RE.search(scanned):
        errors.append(
            "USING(...) is forbidden: ClickHouse 23.8 reports Multiple USING / "
            "Missing columns. Use ON <left>.<user_id> = <right>.<user_id> inside "
            "a nested subquery with exactly one JOIN per layer"
        )
    diagnostic = {m.group(0) for m in _DIAGNOSTIC_TABLE_RE.finditer(scanned)}
    if diagnostic:
        names = ", ".join(sorted(diagnostic))
        errors.append(
            f"diagnostic/intermediate tables are forbidden ({names}): "
            "do not probe Missing columns with step2_3_test* tables; rewrite JOINs "
            "as nested subqueries (one JOIN per layer) in the single "
            "step2_3_wide_complete CREATE"
        )
    for start, end in _from_regions(scanned):
        region = scanned[start:end]
        join_count = 0
        comma_count = 0
        depth = 0
        k = 0
        while k < len(region):
            ch = region[k]
            if ch == "(":
                depth += 1
                k += 1
                continue
            if ch == ")":
                depth = max(0, depth - 1)
                k += 1
                continue
            if depth == 0:
                join_match = _JOIN_RE.match(region, k)
                if join_match and not _is_array_join(region, join_match.start()):
                    join_count += 1
                    k = join_match.end()
                    continue
                if ch == ",":
                    comma_count += 1
            k += 1
        if join_count > 1:
            snippet = re.sub(r"\s+", " ", region[:180]).strip()
            errors.append(
                "ClickHouse 23.8 flattened multi-JOIN is unsafe "
                f"(found {join_count} JOIN in one FROM clause). Nested 1-JOIN-per-layer "
                "workaround required. Offending FROM: "
                f"{snippet[:160]}"
            )
        if comma_count > 0 and join_count == 0:
            errors.append(
                "comma-style multi-table FROM is equivalent to a flattened CROSS JOIN "
                "and hits the ClickHouse 23.8 column-resolution bug; nest relations "
                "with exactly one JOIN per subquery layer"
            )
        elif comma_count > 0:
            errors.append(
                "FROM mixes JOIN and comma-separated tables; rewrite as nested "
                "subqueries with exactly one JOIN per layer"
            )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail expanded step2_3 SQL that ClickHouse 23.8 cannot resolve"
    )
    parser.add_argument("sql_path", type=Path)
    args = parser.parse_args()
    try:
        sql = args.sql_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Cannot read {args.sql_path}: {exc}") from exc
    errors = check_join_nesting(sql)
    if errors:
        raise SystemExit("JOIN nesting gate failed:\n- " + "\n- ".join(errors))
    print("JOIN nesting gate passed: at most one JOIN per FROM layer")


if __name__ == "__main__":
    main()
