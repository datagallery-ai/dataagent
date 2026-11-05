#!/usr/bin/env python3
"""Expand the step2_3 user-grain cleaning of the aggregated wide table.

Reads, from local files:

  * the ``step2_2_wide_aggregated`` column list collected from ``<catalog_columns>``
    (``name`` + ``type``) -> the wide-table columns to profile and the profile SELECTs
  * the ``step2_3_cleaning_report`` rows collected from the warehouse
    (``feature`` + ``recommendation``) -> the ``DROP`` set

step2_3 cleans exactly one relation, the aggregated wide table produced by step2_2, so
there is no cross-table routing: the profile is built from the wide table's own columns
at user grain, and every ``DROP`` maps back to a wide column verbatim. A ``DROP`` that is
not a real wide column blocks the gate.

Outputs (local files only):
  * ``step2_3_cleaning_report_expanded.sql``  -- the CREATE report, ready to submit
  * ``step2_3_feature_cleaning_expanded.sql`` -- the CREATE EXCEPT, ready to submit
  * ``step2_3_validation_expanded.sql``       -- the self-certifying gate SELECT
  * ``step2_3_cleaning_plan.json``            -- the resolved DROP set, for the gate
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
TEMPLATE_REPORT = SCRIPTS_DIR / "step2_3_cleaning_report.sql"
TEMPLATE_CLEANING = SCRIPTS_DIR / "step2_3_feature_cleaning.sql"
TEMPLATE_VALIDATION = SCRIPTS_DIR / "step2_3_validation.sql"
POLICY_JSON = SCRIPTS_DIR.parent / "references" / "cleaning_policy.json"

PROFILE_BLOCK = "/*__WIDE_COLUMN_PROFILE_SELECTS__*/"
EXCEPT_BLOCK = "/*__CLEANING_EXCEPT_CLAUSE__*/"
# Inlined EXCEPT-column literal; resolved against the warehouse's column catalog at
# submit time (see step2_3_validation.sql), never used as a SQL block of its own.
EXCEPT_LITERAL_BLOCK = "/*__EXCEPT_COLUMNS_LITERAL__*/"

# Placeholders intentionally left for submit-time binding (per references/data_source.md), not
# expand-time: the warehouse's column-catalog table name.
DEFERRED_PLACEHOLDERS = {"catalog_columns", "catalog_tables"}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ANGLE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _is_block_shaped(item: Any) -> bool:
    """True when ``item`` is a ``{columns, rows}`` collect block (not a data row)."""
    return (
        isinstance(item, dict)
        and isinstance(item.get("columns"), list)
        and isinstance(item.get("rows"), list)
    )


def _extract_results(payload: Any) -> list[tuple[list[str], list[Any]]]:
    """Yield ``(columns, rows)`` pairs found anywhere in a collect payload.

    Also accepts a flat list of row dicts (``--extract-rows`` save format with no
    header): column names are taken from the first row's keys.

    Multi-block payloads ``[{columns, rows}, ...]`` are walked as blocks — never
    treated as a flat row list.
    """
    found: list[tuple[list[str], list[Any]]] = []

    def _from_flat_rows(rows: list[Any]) -> bool:
        # Flat = list of dicts that are NOT {columns, rows} blocks.
        if not rows or not all(isinstance(item, dict) for item in rows):
            return False
        if any(_is_block_shaped(item) for item in rows):
            return False
        columns = [str(key) for key in rows[0].keys()]
        found.append((columns, rows))
        return True

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            columns = node.get("columns")
            rows = node.get("rows")
            if isinstance(columns, list) and isinstance(rows, list):
                found.append(([str(col) for col in columns], rows))
                return
            if columns is None and isinstance(rows, list) and _from_flat_rows(rows):
                return
            for key in ("outputs", "data", "result", "payload", "original_msg"):
                if key in node:
                    _walk(node[key])
            return
        if isinstance(node, list):
            # Multi-block: recurse into each {columns, rows} envelope.
            if node and all(_is_block_shaped(item) for item in node):
                for item in node:
                    _walk(item)
                return
            if _from_flat_rows(node):
                return
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def _load_payloads(path: Path) -> list[Any]:
    """Load one or more JSON objects from a JSON / JSONL / truncator text file."""
    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    payloads: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if payloads:
        return payloads
    start = min((index for index in (text.find("["), text.find("{")) if index >= 0), default=-1)
    if start >= 0:
        try:
            return [json.loads(text[start:])]
        except json.JSONDecodeError:
            return []
    return []


def _plain_records(payload: Any) -> list[dict[str, Any]]:
    """Return bare row objects (a JSONL list of dicts) that carry no header."""
    if isinstance(payload, list) and payload and all(isinstance(item, dict) for item in payload):
        if any(_is_block_shaped(item) for item in payload):
            return []
        return [dict(item) for item in payload]
    return []


def _rows(path: Path) -> list[dict[str, Any]]:
    """Flatten every payload file into a list of row dicts (header or bare)."""
    records: list[dict[str, Any]] = []
    for payload in _load_payloads(path):
        results = _extract_results(payload)
        if results:
            for columns, rows in results:
                for row in rows:
                    if isinstance(row, dict):
                        records.append(dict(row))
                    elif isinstance(row, (list, tuple)):
                        records.append(
                            {
                                str(columns[index]): (row[index] if index < len(row) else None)
                                for index in range(len(columns))
                            }
                        )
            continue
        records.extend(_plain_records(payload))
    return records


def _drop_features(report_rows: list[dict[str, Any]]) -> list[str]:
    """Features whose recommendation is DROP, in report order and de-duplicated."""
    ordered: list[str] = []
    seen: set[str] = set()
    for row in report_rows:
        feature = str(row.get("feature") or row.get("column_name") or "").strip()
        recommendation = str(row.get("recommendation") or "").strip().upper()
        if not feature or recommendation != "DROP":
            continue
        if feature not in seen:
            seen.add(feature)
            ordered.append(feature)
    return ordered


def _wide_columns(column_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """``[{'name': ..., 'type': ...}]`` for the aggregated wide table, order kept."""
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in column_rows:
        name = str(row.get("name") or row.get("column_name") or "").strip()
        data_type = str(row.get("type") or row.get("data_type") or "Unknown").strip() or "Unknown"
        if name and name not in seen:
            seen.add(name)
            ordered.append({"name": name, "type": data_type})
    return ordered


def _replace_outside_comments(template: str, replacements: dict[str, str]) -> str:
    """Replace ``{{name}}`` and ``<name>`` placeholders on non-comment lines only."""
    lines: list[str] = []
    for line in template.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue

        def _sub(match: re.Match[str]) -> str:
            key = match.group(1)
            return replacements.get(key, match.group(0))

        replaced = _PLACEHOLDER_RE.sub(_sub, line)
        replaced = _ANGLE_RE.sub(_sub, replaced)
        lines.append(replaced)
    return "\n".join(lines)


def _sql_string_array(values: list[str]) -> str:
    """Render a ClickHouse array-of-strings literal for inlining a name list."""
    items = ", ".join("'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'" for value in values)
    return f"[{items}]"


def _quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _load_policy() -> dict[str, Any]:
    """Read ``references/cleaning_policy.json`` — the single source of step2_3 thresholds."""
    policy = _read_json(POLICY_JSON)
    try:
        threshold = int(policy["null_rate_drop_threshold_percent"])
        max_unique = int(policy["constant_drop_max_unique"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"{POLICY_JSON} 缺少合法的 null_rate_drop_threshold_percent / "
            f"constant_drop_max_unique：{exc}"
        ) from exc
    # Missing / 0 => do not split report SQL (degrade gracefully).
    try:
        report_sql_max_bytes = int(policy.get("report_sql_max_bytes") or 0)
    except (TypeError, ValueError):
        report_sql_max_bytes = 0
    try:
        report_batch_columns = int(policy.get("report_batch_columns") or 0)
    except (TypeError, ValueError):
        report_batch_columns = 0
    roles = policy.get("protected_role_keys")
    if not isinstance(roles, list) or not roles:
        raise SystemExit(f"{POLICY_JSON} 的 protected_role_keys 必须是非空数组")
    return {
        "null_rate_drop_threshold_percent": threshold,
        "constant_drop_max_unique": max_unique,
        "protected_role_keys": [str(role) for role in roles],
        "report_sql_max_bytes": report_sql_max_bytes,
        "report_batch_columns": report_batch_columns,
    }


def _profile_select(wide_table: str, column: dict[str, str]) -> str:
    """One user-grain column profile SELECT (``missing_rate`` as a 0-100 percentage)."""
    name = column["name"]
    return (
        "SELECT\n"
        f"    {_quote(name)} AS column_name,\n"
        f"    {_quote(column['type'])} AS data_type,\n"
        f"    round((count() - count({name})) * 100.0 / count(), 4) AS null_rate,\n"
        f"    uniqExact({name}) AS n_unique,\n"
        "    arrayStringConcat(arraySlice(arrayDistinct(groupArray(toString("
        f"{name}))), 1, 5), ',') AS sample_values\n"
        f"FROM {wide_table}"
    )


def _except_ident(name: str) -> str:
    """Quote dotted column names for ClickHouse ``EXCEPT`` (bare ``a.b`` is invalid)."""
    if "." in name:
        return f"`{name}`"
    return name


def _report_batch_sql(
    *,
    output_database: str,
    wide_table: str,
    columns: list[dict[str, str]],
    protected_literal: str,
    policy: dict[str, Any],
    table_name: str,
) -> str:
    """One CREATE for ``table_name`` profiling the given wide-table column subset."""
    selections = "\nUNION ALL\n".join(_profile_select(wide_table, col) for col in columns)
    report_sql = _replace_outside_comments(
        TEMPLATE_REPORT.read_text(encoding="utf-8"),
        {"output_database": output_database},
    )
    if table_name != "step2_3_cleaning_report":
        report_sql = report_sql.replace(
            f"{output_database}.step2_3_cleaning_report",
            f"{output_database}.{table_name}",
        )
        # Template still has the bare name when output_database is a placeholder.
        report_sql = report_sql.replace(
            ".step2_3_cleaning_report\n",
            f".{table_name}\n",
        )
    report_sql = report_sql.replace(PROFILE_BLOCK, selections)
    report_sql = report_sql.replace("/*__PROTECTED_COLUMNS__*/", protected_literal)
    report_sql = report_sql.replace(
        "/*__NULL_RATE_DROP_THRESHOLD__*/",
        str(policy["null_rate_drop_threshold_percent"]),
    )
    report_sql = report_sql.replace(
        "/*__CONSTANT_MAX_UNIQUE__*/",
        str(policy["constant_drop_max_unique"]),
    )
    return report_sql


def _combine_report_sql(output_database: str, batch_tables: list[str]) -> str:
    """UNION ALL batch report tables into the final ``step2_3_cleaning_report``."""
    unions = "\nUNION ALL\n".join(
        f"SELECT * FROM {output_database}.{table}" for table in batch_tables
    )
    return (
        f"CREATE OR REPLACE TABLE {output_database}.step2_3_cleaning_report\n"
        "ENGINE = MergeTree\n"
        "ORDER BY feature\n"
        "AS\n"
        f"{unions};\n"
    )


def build_report_batches(
    column_rows: list[dict[str, Any]],
    schema: dict[str, Any],
) -> tuple[list[str], str | None]:
    """Build cleaning-report CREATE SQL, splitting when over ``report_sql_max_bytes``.

    Returns ``(batch_sqls, combine_sql_or_none)``. Batch table / file indices are
    0-based (``step2_3_cleaning_report_batch_0``, …). When ``report_sql_max_bytes``
    or ``report_batch_columns`` is missing/0, returns a single CREATE with no combine.
    """
    wide_columns = _wide_columns(column_rows)
    if not wide_columns:
        raise SystemExit("step2_2_wide_aggregated 列清单为空，无法展开清洗画像")

    policy = _load_policy()
    output_database = str(schema.get("output_database") or "{{output_database}}").strip()
    protected = [str(schema.get(role) or f"<{role}>").strip() for role in policy["protected_role_keys"]]
    protected_literal = ", ".join(_quote(name) for name in protected)
    wide_table = f"{output_database}.step2_2_wide_aggregated"
    max_bytes = int(policy["report_sql_max_bytes"] or 0)
    batch_size = int(policy["report_batch_columns"] or 0)

    single_sql = _report_batch_sql(
        output_database=output_database,
        wide_table=wide_table,
        columns=wide_columns,
        protected_literal=protected_literal,
        policy=policy,
        table_name="step2_3_cleaning_report",
    )
    # 0 / missing limits => never split; under limit => single CREATE.
    if max_bytes <= 0 or batch_size <= 0 or len(single_sql.encode("utf-8")) <= max_bytes:
        return [single_sql], None

    batch_sqls: list[str] = []
    batch_tables: list[str] = []
    for index, offset in enumerate(range(0, len(wide_columns), batch_size)):
        chunk = wide_columns[offset : offset + batch_size]
        table = f"step2_3_cleaning_report_batch_{index}"
        batch_sqls.append(
            _report_batch_sql(
                output_database=output_database,
                wide_table=wide_table,
                columns=chunk,
                protected_literal=protected_literal,
                policy=policy,
                table_name=table,
            )
        )
        batch_tables.append(table)

    return batch_sqls, _combine_report_sql(output_database, batch_tables)


def expand(
    column_rows: list[dict[str, Any]],
    report_rows: list[dict[str, Any]],
    schema: dict[str, Any],
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """Build the report SQL always; build the EXCEPT/validation/plan once DROP is known."""
    wide_columns = _wide_columns(column_rows)
    if not wide_columns:
        raise SystemExit("step2_2_wide_aggregated 列清单为空，无法展开清洗画像")

    policy = _load_policy()
    output_database = str(schema.get("output_database") or "{{output_database}}").strip()
    protected = [str(schema.get(role) or f"<{role}>").strip() for role in policy["protected_role_keys"]]
    protected_literal = ", ".join(_quote(name) for name in protected)
    wide_table = f"{output_database}.step2_2_wide_aggregated"

    report_sql = _report_batch_sql(
        output_database=output_database,
        wide_table=wide_table,
        columns=wide_columns,
        protected_literal=protected_literal,
        policy=policy,
        table_name="step2_3_cleaning_report",
    )

    # First pass: the report has not been collected yet, so DROP is unknown.
    if not report_rows:
        return report_sql, None, None, None

    wide_set = {column["name"] for column in wide_columns}
    drop = _drop_features(report_rows)
    drop_map = {feature: feature for feature in drop if feature in wide_set}
    orphans = [feature for feature in drop if feature not in wide_set]
    except_columns = [column["name"] for column in wide_columns if column["name"] in drop_map]

    clause = (
        ""
        if not except_columns
        else "EXCEPT (" + ", ".join(_except_ident(name) for name in except_columns) + ")"
    )
    role_map = {"output_database": output_database}
    role_map.update({role: name for role, name in zip(policy["protected_role_keys"], protected)})
    cleaning_sql = _replace_outside_comments(
        TEMPLATE_CLEANING.read_text(encoding="utf-8"),
        role_map,
    )
    cleaning_sql = cleaning_sql.replace(EXCEPT_BLOCK, clause)

    validation_sql = _replace_outside_comments(
        TEMPLATE_VALIDATION.read_text(encoding="utf-8"),
        role_map,
    )
    validation_sql = validation_sql.replace(EXCEPT_LITERAL_BLOCK, _sql_string_array(except_columns))
    validation_sql = validation_sql.replace("/*__PROTECTED_COLUMNS__*/", protected_literal)

    plan = {
        "wide_table": "step2_2_wide_aggregated",
        "output_table": "step2_3_wide_cleaned",
        "wide_column_count": len(wide_columns),
        "except_columns": except_columns,
        "drop_map": drop_map,
        "drop_not_in_wide": orphans,
    }
    return report_sql, cleaning_sql, validation_sql, plan


def _non_comment_text(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def _assert_expanded(path: Path, text: str) -> None:
    active = _non_comment_text(text)
    if "/*__" in active:
        raise SystemExit(f"{path} 仍含未展开的 /*__...__*/ 动态块")
    leftover = _PLACEHOLDER_RE.search(active)
    if leftover:
        raise SystemExit(f"{path} 仍含未替换占位符 {leftover.group(0)}")
    angle = _ANGLE_RE.search(active)
    if angle and angle.group(1) not in DEFERRED_PLACEHOLDERS:
        raise SystemExit(f"{path} 仍含未替换占位符 {angle.group(0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand step2_3 user-grain wide-table cleaning")
    parser.add_argument("--schema", type=Path, required=True, help="path to schema_resolution.json")
    parser.add_argument(
        "--raw-report",
        type=Path,
        action="append",
        help="step2_3_cleaning_report collect payload (feature + recommendation); "
        "omit on the first pass (which only emits the report SQL)",
    )
    parser.add_argument(
        "--raw-columns",
        type=Path,
        action="append",
        required=True,
        help="step2_2_wide_aggregated column collect payload (name + type); repeatable",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="directory for expanded SQL")
    args = parser.parse_args()

    schema = _read_json(args.schema)
    report_rows: list[dict[str, Any]] = []
    for path in args.raw_report or []:
        if not path.is_file():
            raise SystemExit(f"未找到 cleaning_report payload 文件：{path}")
        report_rows.extend(_rows(path))
    column_rows: list[dict[str, Any]] = []
    for path in args.raw_columns:
        if not path.is_file():
            raise SystemExit(f"未找到 step2_2_wide_aggregated 列清单 payload 文件：{path}")
        column_rows.extend(_rows(path))

    report_sql, cleaning_sql, validation_sql, plan = expand(column_rows, report_rows, schema)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_report = args.out_dir / "step2_3_cleaning_report_expanded.sql"
    _assert_expanded(out_report, report_sql)
    out_report.write_text(_non_comment_text(report_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_report}")

    if plan is None:
        print("第一遍（未提供 --raw-report）：只产出画像 SQL，提交建 step2_3_cleaning_report 后带 --raw-report 重跑。")
        return

    out_cleaning = args.out_dir / "step2_3_feature_cleaning_expanded.sql"
    _assert_expanded(out_cleaning, cleaning_sql)
    out_cleaning.write_text(_non_comment_text(cleaning_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_cleaning}")

    out_validation = args.out_dir / "step2_3_validation_expanded.sql"
    _assert_expanded(out_validation, validation_sql)
    out_validation.write_text(_non_comment_text(validation_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_validation}")

    out_plan = args.out_dir / "step2_3_cleaning_plan.json"
    out_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写出 {out_plan}：EXCEPT {len(plan['except_columns'])} 列")
    if plan["drop_not_in_wide"]:
        raise SystemExit(
            "以下 DROP 字段不是 step2_2_wide_aggregated 的列，无法清洗：\n- "
            + "\n- ".join(plan["drop_not_in_wide"])
            + f"\n诊断清单见 {out_plan}。请检查 step2_3_cleaning_report.sql 是否与宽表列一致。"
        )


if __name__ == "__main__":
    main()
