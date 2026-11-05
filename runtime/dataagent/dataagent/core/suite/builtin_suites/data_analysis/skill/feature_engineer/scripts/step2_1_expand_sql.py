#!/usr/bin/env python3
"""Expand the step2_1 1:1 merge into a submit-ready single statement.

Reads, from local files only:

  * ``schema_resolution.json``
    - ``output_database`` / ``user_table`` / ``user_id``
    - ``source_tables``   -> table order + per-table ``user_key``
    - ``key_validation``  -> ``classification`` == ``1:1用户表`` selects the right tables,
                             ``max_duplication_factor`` gates them
  * ``step1_output_meta.json``
    - ``tables[].columns[]`` -> physical column order per table (authoritative spelling)

Rules enforced here (see SKILL.md ``## step2_1``):

  1. right tables = every ``1:1用户表`` except the base ``<user_table>``;
  2. ``max_duplication_factor`` must be ``1.0`` — anything else **blocks** (a 1:1 table
     whose key is not unique is a classification defect, never silently de-duplicated);
  3. ``u.*`` keeps the base columns in place; every right-table column that is not the
     join key is prefixed ``<table>_<column>`` (the wide-table spelling the step2_3
     EXCEPT intersection relies on);
  4. joins are never flat: >1 right table becomes a strictly-1-JOIN-per-layer nested
     subquery chain, ``ON`` is alias-qualified, ``USING`` is never emitted.

Outputs (local files only):
  * ``step2_1_join_expanded.sql`` -- the CREATE, ready to submit (one statement)
  * ``step2_1_validation_expanded.sql``   -- row/user-key/column gate SELECT (expected
    column names inlined as a literal; reconciled against the column catalog in-warehouse)
  * ``step2_1_merge_plan.json``           -- joined tables, aliases, and the authoritative
    ``column_sources`` / ``bare_to_wide`` mapping step2_3 reuses to resolve DROP features
"""

from __future__ import annotations

import argparse
import json
import re
import textwrap
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
TEMPLATE_MERGE = SCRIPTS_DIR / "step2_1_join.sql"
TEMPLATE_VALIDATION = SCRIPTS_DIR / "step2_1_validation.sql"

MERGE_BODY_BLOCK = "/*__ONE_TO_ONE_MERGE_BODY__*/"
# Inlined expected-column literal; resolved against the warehouse's column catalog at
# submit time (see step2_1_validation.sql), never used as a SQL block of its own.
EXPECTED_COLUMNS_BLOCK = "/*__EXPECTED_COLUMNS_LITERAL__*/"

ONE_TO_ONE = "1:1用户表"
BASE_ALIAS = "u"

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


def _columns_by_table(meta: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for table in meta.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        result[name] = [
            str(col.get("name") or "").strip()
            for col in (table.get("columns") or [])
            if isinstance(col, dict) and str(col.get("name") or "").strip()
        ]
    return result


def _source_tables(schema: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for item in schema.get("source_tables") or []:
        if isinstance(item, dict) and item.get("table_name"):
            tables.append(item)
        elif isinstance(item, str) and item.strip():
            tables.append({"table_name": item.strip()})
    return tables


def _key_validation(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = schema.get("key_validation")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


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


def _resolve_user_key(table: dict[str, Any], columns: list[str], aliases: list[str]) -> str | None:
    explicit = str(table.get("user_key") or "").strip()
    if explicit:
        return explicit
    column_set = set(columns)
    for alias in aliases:
        if alias in column_set:
            return alias
    return None


def select_right_tables(
    schema: dict[str, Any],
    columns_by_table: dict[str, list[str]],
    aliases: list[str],
    errors: list[str],
) -> list[dict[str, Any]]:
    """Return the ordered 1:1 right tables (base excluded), appending blocking errors."""
    user_table = str(schema.get("user_table") or "").strip()
    key_validation = _key_validation(schema)
    if not key_validation:
        raise SystemExit(
            "schema_resolution.key_validation 为空，无法判定 1:1 表；请先运行 step2_0_2_generate_schema.py"
        )

    rights: list[dict[str, Any]] = []
    seen: set[str] = set()
    for table in _source_tables(schema):
        name = str(table.get("table_name") or "").strip()
        if not name or name == user_table or name in seen:
            continue
        validation = key_validation.get(name)
        if not validation:
            continue
        if str(validation.get("classification") or "").strip() != ONE_TO_ONE:
            continue
        if int(validation.get("total_rows") or 0) == 0:
            # 0 行空表降级为维度表（step2_0_2 已重分类）；对旧 schema 兜底跳过。
            continue
        seen.add(name)

        duplication = validation.get("max_duplication_factor")
        if duplication is None:
            errors.append(
                f"{name}：key_validation 缺少 max_duplication_factor，无法确认用户键唯一"
            )
            continue
        try:
            duplication = float(duplication)
        except (TypeError, ValueError):
            errors.append(f"{name}：max_duplication_factor 非数值（{duplication}）")
            continue
        if duplication != 1.0:
            errors.append(
                f"{name}：classified '1:1用户表' 但 max_duplication_factor={duplication}（≠1）"
                "——键不唯一的 1:1 表必须阻塞，禁止静默去重"
            )
            continue

        columns = columns_by_table.get(name)
        if not columns:
            errors.append(f"{name}：step1_output_meta.json 中找不到该表的列清单")
            continue
        key = _resolve_user_key(table, columns, aliases)
        if not key:
            errors.append(f"{name}：无法解析用户键（user_key 为空且列中无别名匹配），无法生成 JOIN")
            continue
        if key not in columns:
            errors.append(f"{name}：解析出的用户键 {key} 不在该表列清单中")
            continue
        rights.append({"table": name, "user_key": key, "columns": columns})
    return rights


def build_merge_body(
    output_database: str,
    base_table: str,
    user_id: str,
    rights: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(body_sql, joined_plan)`` where ``body_sql`` is the SELECT..FROM..JOIN."""
    user_table_ref = f"{output_database}.{base_table}"
    if not rights:
        return f"SELECT\n    {BASE_ALIAS}.*\nFROM {user_table_ref} AS {BASE_ALIAS}", []

    joined_plan: list[dict[str, Any]] = []
    sql = ""
    left_key = f"{BASE_ALIAS}.{user_id}"
    for index, right in enumerate(rights, start=1):
        table = right["table"]
        key = right["user_key"]
        alias = f"t{index}"
        prefix = f"{table}_"

        select_items: list[str] = [f"{BASE_ALIAS}.*" if index == 1 else f"j{index - 1}.*"]
        for column in right["columns"]:
            if column == key:
                continue
            select_items.append(f"{alias}.{column} AS {prefix}{column}")

        from_expr = (
            f"{user_table_ref} AS {BASE_ALIAS}"
            if index == 1
            else f"(\n{textwrap.indent(sql, '    ')}\n) AS j{index - 1}"
        )
        sql = (
            "SELECT\n    "
            + ",\n    ".join(select_items)
            + f"\nFROM {from_expr}\nLEFT JOIN {output_database}.{table} AS {alias} ON {left_key} = {alias}.{key}"
        )
        left_key = f"j{index}.{user_id}"
        joined_plan.append(
            {
                "table": table,
                "alias": alias,
                "user_key": key,
                "prefix": prefix,
                "select_columns": [f"{prefix}{col}" for col in right["columns"] if col != key],
                "select_column_count": len([col for col in right["columns"] if col != key]),
            }
        )
    return sql, joined_plan


def _replace_outside_comments(template: str, replacements: dict[str, str]) -> str:
    """Replace ``{{name}}`` and ``<name>`` placeholders on non-comment lines only."""
    lines: list[str] = []
    for line in template.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue

        def _sub(match: re.Match[str]) -> str:
            return replacements.get(match.group(1), match.group(0))

        replaced = _PLACEHOLDER_RE.sub(_sub, line)
        replaced = _ANGLE_RE.sub(_sub, replaced)
        lines.append(replaced)
    return "\n".join(lines)


def _non_comment_text(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


# Placeholders intentionally left for submit-time binding (per references/data_source.md), not
# expand-time: the warehouse's column-catalog table name.
DEFERRED_PLACEHOLDERS = {"catalog_columns", "catalog_tables"}


def _assert_expanded(path: Path, text: str) -> None:
    active = _non_comment_text(text)
    if "/*__" in active:
        raise SystemExit(f"{path} 仍含未展开的 /*__...__*/ 动态块")
    leftover = _PLACEHOLDER_RE.search(active)
    if leftover:
        raise SystemExit(f"{path} 仍含未替换占位符 {leftover.group(0)}")
    for match in _ANGLE_RE.finditer(active):
        if match.group(1) not in DEFERRED_PLACEHOLDERS:
            raise SystemExit(f"{path} 仍含未替换占位符 {match.group(0)}")


def _sql_string_array(values: list[str]) -> str:
    """Render a ClickHouse array-of-strings literal for inlining expected names."""
    items = ", ".join("'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'" for value in values)
    return f"[{items}]"


def expand(
    schema: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[str, str, dict[str, Any], list[str]]:
    errors: list[str] = []

    output_database = str(schema.get("output_database") or "").strip()
    base_table = str(schema.get("user_table") or "").strip()
    user_id = str(schema.get("user_id") or "").strip()
    if not output_database:
        raise SystemExit("schema_resolution.output_database 为空，无法填充 {{output_database}}")
    if not base_table:
        raise SystemExit("schema_resolution.user_table 为空，无法确定基表")
    if not user_id:
        raise SystemExit("schema_resolution.user_id 为空，无法填充 ORDER BY <user_id>")

    columns_by_table = _columns_by_table(meta)
    base_columns = columns_by_table.get(base_table)
    if not base_columns:
        raise SystemExit(f"step1_output_meta.json 中找不到基表 {base_table} 的列清单")
    if user_id not in base_columns:
        raise SystemExit(f"基表 {base_table} 的列清单中找不到用户键 {user_id}")

    aliases = _user_key_columns(meta, schema)
    rights = select_right_tables(schema, columns_by_table, aliases, errors)
    if errors:
        raise SystemExit("step2_1 展开被阻塞：\n- " + "\n- ".join(errors))

    body, joined_plan = build_merge_body(output_database, base_table, user_id, rights)

    expected_columns = list(base_columns)
    for right in joined_plan:
        expected_columns.extend(right["select_columns"])

    # Wide-table spelling -> owning source table, plus a bare-name index.  Kept as the
    # authoritative 1:1 column-spelling record for downstream diagnostics.
    column_sources: dict[str, str] = {column: base_table for column in base_columns}
    for right in joined_plan:
        for column in right["select_columns"]:
            column_sources[column] = right["table"]

    bare_to_wide: dict[str, list[str]] = {}
    for wide in expected_columns:
        owner = column_sources.get(wide)
        # Only right-table columns are prefixed; base columns stay verbatim, so a base
        # column literally named ``<base_table>_x`` must not expose a bare ``x``.
        if owner is not None and owner != base_table and wide.startswith(f"{owner}_"):
            bare = wide[len(owner) + 1 :]
            bare_to_wide.setdefault(bare, []).append(wide)
        bare_to_wide.setdefault(wide, []).append(wide)
    ambiguous = sorted(bare for bare, wides in bare_to_wide.items() if len(wides) > 1)

    # Every source table's column spelling once the step2_3 cleaning rule is applied:
    # base table keeps the bare name, every other table prefixes ``<table>_``.  This is
    # the index step2_3 uses to turn a per-table DROP into that table's cleaned spelling,
    # including the 1:N / dimension tables that never enter the 1:1 wide table.
    one_to_one_tables = {base_table, *[item["table"] for item in joined_plan]}
    column_spellings: dict[str, dict[str, str]] = {}
    for table_name, columns in columns_by_table.items():
        prefix = "" if table_name == base_table else f"{table_name}_"
        column_spellings[table_name] = {column: f"{prefix}{column}" for column in columns}

    common = {
        "output_database": output_database,
        "user_id": user_id,
        "user_table": base_table,
        "expected_columns_literal": EXPECTED_COLUMNS_BLOCK,
    }
    merge_sql = _replace_outside_comments(TEMPLATE_MERGE.read_text(encoding="utf-8"), common)
    merge_sql = merge_sql.replace(MERGE_BODY_BLOCK, body)

    validation_sql = _replace_outside_comments(
        TEMPLATE_VALIDATION.read_text(encoding="utf-8"), common
    )
    validation_sql = validation_sql.replace(EXPECTED_COLUMNS_BLOCK, _sql_string_array(expected_columns))

    plan = {
        "output_table": "step2_1_wide_joined",
        "base_table": base_table,
        "base_alias": BASE_ALIAS,
        "user_id": user_id,
        "base_column_count": len(base_columns),
        "expected_column_count": len(expected_columns),
        "expected_columns": expected_columns,
        "column_sources": column_sources,
        "bare_to_wide": bare_to_wide,
        "ambiguous_bare_names": ambiguous,
        "one_to_one_tables": sorted(one_to_one_tables),
        "column_spellings": column_spellings,
        "joined_tables": joined_plan,
        "joined_table_count": len(joined_plan),
        "join_nesting": "flat" if len(joined_plan) <= 1 else "nested",
    }
    return merge_sql, validation_sql, plan, [right["table"] for right in joined_plan]


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand the step2_1 1:1 merge dynamic block")
    parser.add_argument("--schema", type=Path, required=True, help="path to schema_resolution.json")
    parser.add_argument("--meta", type=Path, required=True, help="path to step1_output_meta.json")
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="directory for expanded SQL")
    args = parser.parse_args()

    schema = _read_json(args.schema)
    meta = _read_json(args.meta)
    merge_sql, validation_sql, plan, joined = expand(schema, meta)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    out_merge = args.out_dir / "step2_1_join_expanded.sql"
    _assert_expanded(out_merge, merge_sql)
    out_merge.write_text(_non_comment_text(merge_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_merge}")

    out_validation = args.out_dir / "step2_1_validation_expanded.sql"
    _assert_expanded(out_validation, validation_sql)
    out_validation.write_text(_non_comment_text(validation_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_validation}")

    out_plan = args.out_dir / "step2_1_merge_plan.json"
    out_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    nesting = plan["join_nesting"]
    print(
        f"已写出 {out_plan}：1:1 右表 {len(joined)} 张（{nesting}），"
        f"预期列数 {plan['expected_column_count']}"
    )
    if joined:
        print("  右表（按 JOIN 顺序）：" + ", ".join(joined))


if __name__ == "__main__":
    main()
