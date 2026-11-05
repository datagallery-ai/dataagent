#!/usr/bin/env python3
"""Expand step2_0_1 profile SQL dynamic blocks into submit-ready single statements.

Inputs (local files only):
  * ``schema_resolution.json``   -- ``source_tables`` with roles + user keys
  * ``step1_output_meta.json``   -- physical columns/types per source table

Outputs (one SQL statement per file):
  * ``step2_0_1_table_profile_expanded.sql``   -> step2_0_table_profile
  * ``step2_0_1_column_profile_expanded.sql``  -> step2_0_column_profile
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
TEMPLATE_TABLE_PROFILE = SCRIPTS_DIR / "step2_0_1_table_profile.sql"
TEMPLATE_COLUMN_PROFILE = SCRIPTS_DIR / "step2_0_1_column_profile.sql"

TABLE_BLOCK = "/*__SOURCE_TABLE_PROFILE_SELECTS__*/"
COLUMN_BLOCK = "/*__SOURCE_COLUMN_PROFILE_SELECTS__*/"

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


def _quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _ident(name: str) -> str:
    return str(name).strip()


def _columns_by_table(meta: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in meta.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        columns = [col for col in (table.get("columns") or []) if isinstance(col, dict) and col.get("name")]
        result[name] = columns
    return result


def _user_key_columns(meta: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    aliases = schema.get("user_id_columns")
    if not isinstance(aliases, list):
        aliases = ((meta.get("column_aliases") or {}).get("user_id_columns")) or []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in aliases:
        name = str(item).strip()
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _source_tables(schema: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for item in schema.get("source_tables") or []:
        if isinstance(item, dict) and item.get("table_name"):
            tables.append(item)
        elif isinstance(item, str) and item.strip():
            tables.append({"table_name": item.strip()})
    return tables


def _resolve_user_key(table: dict[str, Any], columns: list[dict[str, Any]], aliases: list[str]) -> str | None:
    explicit = str(table.get("user_key") or "").strip()
    if explicit:
        return explicit
    column_names = {str(col.get("name") or "").strip() for col in columns}
    for alias in aliases:
        if alias in column_names:
            return alias
    return None


def _table_profile_select(
    output_database: str,
    table_name: str,
    user_key: str | None,
) -> str:
    if user_key:
        key = _ident(user_key)
        classification = (
            "CASE\n"
            "        WHEN count() = 0 THEN '维度表'\n"
            f"        WHEN uniqExact({key}) IS NULL THEN '维度表'\n"
            f"        WHEN uniqExact({key}) = count() THEN '1:1用户表'\n"
            "        ELSE '1:N行为表'\n"
            "    END"
        )
        unique_expr = f"uniqExact({key})"
    else:
        classification = "'维度表'"
        unique_expr = "NULL"
    return (
        "SELECT\n"
        f"    {_quote(table_name)} AS table_name,\n"
        "    count() AS total_rows,\n"
        f"    {unique_expr} AS unique_user_id,\n"
        f"    {classification} AS classification\n"
        f"FROM {output_database}.{_ident(table_name)}"
    )


def _column_profile_select(
    output_database: str,
    table_name: str,
    column: dict[str, Any],
) -> str:
    column_name = _ident(column.get("name"))
    data_type = str(column.get("valueType") or column.get("type") or "Unknown").strip() or "Unknown"
    return (
        "SELECT\n"
        f"    {_quote(table_name)} AS table_name,\n"
        f"    {_quote(column_name)} AS column_name,\n"
        f"    {_quote(data_type)} AS data_type,\n"
        f"    round((count() - count({column_name})) * 100.0 / count(), 4) AS missing_rate,\n"
        f"    uniqExact({column_name}) AS n_unique,\n"
        f"    arrayStringConcat(arraySlice(arrayDistinct(groupArray(toString({column_name}))), 1, 5), ',') AS sample_values\n"
        f"FROM {output_database}.{_ident(table_name)}"
    )


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


def expand(
    schema: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[str, str]:
    output_database = str(schema.get("output_database") or "{{output_database}}").strip()
    columns_by_table = _columns_by_table(meta)
    aliases = _user_key_columns(meta, schema)
    tables = _source_tables(schema)

    user_id = str(schema.get("user_id") or (aliases[0] if aliases else "<user_id>")).strip()
    label = str(schema.get("label") or "<label>").strip()
    user_table = str(schema.get("user_table") or "<user_table>").strip()

    table_selects: list[str] = []
    column_selects: list[str] = []
    for table in tables:
        table_name = _ident(table.get("table_name"))
        columns = columns_by_table.get(table_name, [])
        user_key = _resolve_user_key(table, columns, aliases)
        table_selects.append(_table_profile_select(output_database, table_name, user_key))
        for column in columns:
            column_selects.append(_column_profile_select(output_database, table_name, column))

    if not table_selects:
        raise SystemExit("schema_resolution.source_tables 为空，无法展开表画像 SQL")
    if not column_selects:
        raise SystemExit("step1_output_meta.json 中未找到任何列，无法展开字段画像 SQL")

    common = {"output_database": output_database, "user_id": user_id, "label": label, "user_table": user_table}

    table_sql = TEMPLATE_TABLE_PROFILE.read_text(encoding="utf-8")
    table_sql = _replace_outside_comments(table_sql, common)
    table_sql = table_sql.replace(TABLE_BLOCK, "\nUNION ALL\n".join(table_selects))

    column_sql = TEMPLATE_COLUMN_PROFILE.read_text(encoding="utf-8")
    column_sql = _replace_outside_comments(column_sql, common)
    column_sql = column_sql.replace(COLUMN_BLOCK, "\nUNION ALL\n".join(column_selects))

    return table_sql, column_sql


def _non_comment_text(text: str) -> str:
    """Drop ``--`` comment lines so template documentation does not trip the checks."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def _assert_expanded(path: Path, text: str) -> None:
    active = _non_comment_text(text)
    if "/*__" in active:
        raise SystemExit(f"{path} 仍含未展开的 /*__...__*/ 动态块")
    leftover = _PLACEHOLDER_RE.search(active)
    if leftover:
        raise SystemExit(f"{path} 仍含未替换占位符 {leftover.group(0)}")
    angle = _ANGLE_RE.search(active)
    if angle:
        raise SystemExit(f"{path} 仍含未替换占位符 {angle.group(0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand step2_0 profile SQL dynamic blocks")
    parser.add_argument("--schema", type=Path, required=True, help="path to schema_resolution.json")
    parser.add_argument("--meta", type=Path, required=True, help="path to step1_output_meta.json")
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="directory for expanded SQL")
    args = parser.parse_args()

    schema = _read_json(args.schema)
    meta = _read_json(args.meta)
    table_sql, column_sql = expand(schema, meta)

    outputs = {
        "step2_0_1_table_profile_expanded.sql": table_sql,
        "step2_0_1_column_profile_expanded.sql": column_sql,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        path = args.out_dir / name
        _assert_expanded(path, text)
        path.write_text(_non_comment_text(text).rstrip() + "\n", encoding="utf-8")
        print(f"已写出 {path}")

    classification_hits = _non_comment_text(table_sql).count("AS classification")
    if classification_hits < len(_source_tables(schema)):
        raise SystemExit(
            f"AS classification 命中数 {classification_hits} < 源表数，动态块展开不完整"
        )
    print(f"已展开 {len(_source_tables(schema))} 张源表，AS classification 命中 {classification_hits} 处")


if __name__ == "__main__":
    main()
