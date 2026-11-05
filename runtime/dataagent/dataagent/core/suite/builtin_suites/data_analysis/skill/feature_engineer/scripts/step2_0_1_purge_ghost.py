#!/usr/bin/env python3
"""Purge ghost columns declared in meta but absent from the live warehouse schema.

Pure local JSON processing: never connects to a warehouse. Two sub-commands:

  * ``gen``   -- emit a ClickHouse ``system.columns`` describe query from
    ``schema_resolution.json`` so the Agent can submit + collect it.
  * ``purge`` -- parse the describe payload, drop ghost columns from meta in place
    (backing up ``<meta文件名>.orig.json`` only when ghosts exist), record a
    ``ghost_columns`` audit list on schema, and rebuild ``schema.tables`` physical
    columns for later contract validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DESCRIBE_SQL = "step2_0_1_describe_columns_expanded.sql"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _quote(value: str) -> str:
    """SQL string literal with standard ``''`` escaping (no backslash escapes)."""
    return "'" + str(value).replace("'", "''") + "'"


def _source_table_names(schema: dict[str, Any]) -> list[str]:
    """Return ordered source table names from ``source_tables`` only."""
    names: list[str] = []
    seen: set[str] = set()
    for item in schema.get("source_tables") or []:
        if isinstance(item, dict):
            name = str(item.get("table_name") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _output_database(schema: dict[str, Any]) -> str:
    """Require ``schema_resolution.json`` ``output_database`` (no fallbacks)."""
    value = schema.get("output_database")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise SystemExit(
        "schema_resolution.json 缺少 output_database，无法生成 describe 查询"
    )


def build_describe_sql(schema: dict[str, Any]) -> str:
    tables = _source_table_names(schema)
    if not tables:
        raise SystemExit("schema 中没有 source_tables，无法生成 describe 查询")
    database = _output_database(schema)
    in_list = ", ".join(_quote(name) for name in tables)
    # First line must be SELECT; no FORMAT JSON; ORDER BY table, name.
    return (
        "SELECT database, table, name, type\n"
        "FROM system.columns\n"
        f"WHERE database = {_quote(database)} AND table IN ({in_list})\n"
        "ORDER BY table, name\n"
    )


def gen(schema_path: Path, out_dir: Path) -> None:
    """Write ``step2_0_1_describe_columns_expanded.sql`` from the schema skeleton."""
    schema = _read_json(schema_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    sql = build_describe_sql(schema)
    out = out_dir / DESCRIBE_SQL
    out.write_text(sql, encoding="utf-8")
    print(f"已写出 {out}：tables={len(_source_table_names(schema))}")


def _load_payload_objects(path: Path) -> list[Any]:
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


def _rows_to_dicts(columns: list[str], rows: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            records.append(dict(row))
            continue
        if not isinstance(row, (list, tuple)):
            continue
        record: dict[str, Any] = {}
        for index, name in enumerate(columns):
            record[str(name)] = row[index] if index < len(row) else None
        records.append(record)
    return records


def _is_columns_rows_block(item: Any) -> bool:
    return isinstance(item, dict) and "columns" in item and "rows" in item


def _walk_column_rows(node: Any) -> list[dict[str, Any]]:
    """Yield row dicts that look like ``system.columns`` records from nested payloads."""
    found: list[dict[str, Any]] = []

    def _consider(rows: list[Any], columns: list[str] | None = None) -> None:
        if columns is not None:
            found.extend(_rows_to_dicts(columns, rows))
            return
        for item in rows:
            if isinstance(item, dict):
                found.append(dict(item))

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            columns = obj.get("columns")
            rows = obj.get("rows")
            if isinstance(rows, list):
                if isinstance(columns, list):
                    _consider(rows, [str(col) for col in columns])
                else:
                    _consider(rows)
                return
            for key in ("outputs", "data", "result", "payload", "original_msg", "content"):
                if key in obj:
                    _walk(obj[key])
            return
        if isinstance(obj, list):
            # Multi-block [{columns, rows}, …] — do not misread as flat rows.
            if obj and _is_columns_rows_block(obj[0]):
                for item in obj:
                    _walk(item)
                return
            if obj and all(isinstance(item, dict) for item in obj) and not _is_columns_rows_block(obj[0]):
                _consider(obj)
                return
            for item in obj:
                _walk(item)

    _walk(node)
    return found


def _normalize_column_record(row: dict[str, Any]) -> dict[str, str] | None:
    table = str(row.get("table") or row.get("table_name") or "").strip()
    name = str(row.get("name") or row.get("column") or row.get("column_name") or "").strip()
    col_type = str(row.get("type") or row.get("data_type") or "").strip()
    if not table or not name:
        return None
    return {"table": table, "name": name, "type": col_type or "Unknown"}


def parse_describe_records(paths: list[Path]) -> list[dict[str, str]]:
    """Parse one or more describe payloads into ``{table, name, type}`` records."""
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"未找到 describe payload：{path}")
        for payload in _load_payload_objects(path):
            for row in _walk_column_rows(payload):
                normalized = _normalize_column_record(row)
                if normalized is None:
                    continue
                key = (normalized["table"], normalized["name"])
                if key in seen:
                    continue
                seen.add(key)
                records.append(normalized)
    return records


def _actual_columns(records: list[dict[str, str]]) -> dict[str, set[str]]:
    """Group real column names by table from describe records."""
    result: dict[str, set[str]] = {}
    for record in records:
        result.setdefault(record["table"], set()).add(record["name"])
    return result


def _meta_columns_by_table(meta: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in meta.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        result[name] = [
            col for col in (table.get("columns") or []) if isinstance(col, dict) and col.get("name")
        ]
    return result


def _find_ghosts(
    meta: dict[str, Any],
    actual: dict[str, set[str]],
    source_tables: list[str],
) -> dict[str, list[str]]:
    """Return ghost columns per source table that the describe payload covered."""
    ghosts: dict[str, list[str]] = {}
    meta_by_table = _meta_columns_by_table(meta)
    # Ghost detection only walks schema source_tables (table_names).
    for table_name in source_tables:
        if table_name not in actual:
            # Describe did not return this table — do not treat every meta column as ghost.
            continue
        columns = meta_by_table.get(table_name) or []
        real = actual[table_name]
        missing = sorted(
            {
                str(col.get("name") or "").strip()
                for col in columns
                if str(col.get("name") or "").strip() and str(col.get("name") or "").strip() not in real
            }
        )
        if missing:
            ghosts[table_name] = missing
    return ghosts


def _purge_meta(meta: dict[str, Any], ghosts: dict[str, list[str]]) -> int:
    """Drop ghost columns from meta tables in place; return the number removed."""
    removed = 0
    for table in meta.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name") or "").strip()
        drop = set(ghosts.get(name) or [])
        if not drop:
            continue
        before = table.get("columns") or []
        kept = [
            col
            for col in before
            if not (isinstance(col, dict) and str(col.get("name") or "").strip() in drop)
        ]
        removed += len(before) - len(kept)
        table["columns"] = kept
    return removed


def _rebuild_schema_tables(
    records: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Rebuild ``schema.tables`` physical columns; tables sorted by name dict order."""
    by_table: dict[str, list[dict[str, str]]] = {}
    for record in records:
        by_table.setdefault(record["table"], []).append(
            {"name": record["name"], "type": record["type"]}
        )

    tables: list[dict[str, Any]] = []
    for name in sorted(by_table):
        tables.append({"name": name, "columns": by_table[name]})
    return tables


def _append_ghost_audit(
    schema: dict[str, Any],
    ghosts: dict[str, list[str]],
) -> list[dict[str, str]]:
    """Merge new ghost entries into schema.ghost_columns list; dedupe by (table, column)."""
    existing = schema.get("ghost_columns")
    audit: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            table = str(item.get("table") or "").strip()
            column = str(item.get("column") or "").strip()
            if not table or not column:
                continue
            key = (table, column)
            if key in seen:
                continue
            seen.add(key)
            audit.append({"table": table, "column": column})

    for table_name, cols in sorted(ghosts.items()):
        for column in cols:
            key = (table_name, column)
            if key in seen:
                continue
            seen.add(key)
            audit.append({"table": table_name, "column": column})

    return audit


def _backup_meta_path(meta_path: Path) -> Path:
    """``<meta文件名>.orig.json`` — do not use ``Path.with_suffix`` (drops ``.json`` stem)."""
    return Path(str(meta_path) + ".orig.json")


def purge(
    describe_paths: list[Path],
    meta_path: Path,
    schema_path: Path,
    out_dir: Path,
) -> None:
    """Apply ghost-column purge to meta + schema using describe payload(s)."""
    del out_dir  # accepted for CLI compatibility; purge writes in place beside inputs
    records = parse_describe_records(describe_paths)
    if not records:
        raise SystemExit(
            "describe payload 解析出 0 条列记录；勿把「无记录」当成「无幽灵列」。"
            "请确认 collect 落盘了 system.columns 的结果后再重跑 purge。"
        )

    meta = _read_json(meta_path)
    schema = _read_json(schema_path)
    source_tables = _source_table_names(schema)
    if not source_tables:
        raise SystemExit("schema 中没有 source_tables，无法判定幽灵列")

    actual = _actual_columns(records)
    ghosts = _find_ghosts(meta, actual, source_tables)

    if ghosts:
        backup = _backup_meta_path(meta_path)
        if not backup.is_file():
            backup.write_text(meta_path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"已备份 meta -> {backup}")
        removed = _purge_meta(meta, ghosts)
        _write_json(meta_path, meta)
        print(
            f"已就地修正 {meta_path}：移除幽灵列 {removed} 个 "
            f"（涉及表 {', '.join(sorted(ghosts))}）"
        )
        for table_name, cols in sorted(ghosts.items()):
            print(f"  {table_name}: {', '.join(cols)}")
    else:
        # No ghosts: pass through without backup or meta rewrite.
        print("无幽灵列：meta 零改动通过")

    schema["tables"] = _rebuild_schema_tables(records)
    schema["ghost_columns"] = _append_ghost_audit(schema, ghosts)
    _write_json(schema_path, schema)
    print(
        f"已更新 {schema_path}：schema.tables={len(schema['tables'])}，"
        f"ghost_columns={len(schema['ghost_columns'])}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="purge ghost columns before step2_0 profiling")
    sub = parser.add_subparsers(dest="command", required=True)

    gen_cmd = sub.add_parser("gen", help="emit system.columns describe SQL from schema")
    gen_cmd.add_argument("--schema", type=Path, required=True, help="schema_resolution.json")
    gen_cmd.add_argument("--out-dir", type=Path, default=Path("."))
    gen_cmd.set_defaults(func=lambda args: gen(args.schema, args.out_dir))

    purge_cmd = sub.add_parser("purge", help="drop ghost columns using describe payload(s)")
    purge_cmd.add_argument(
        "--describe",
        type=Path,
        action="append",
        required=True,
        help="describe collect payload; repeatable",
    )
    purge_cmd.add_argument("--meta", type=Path, required=True, help="step1_output_meta.json")
    purge_cmd.add_argument("--schema", type=Path, required=True, help="schema_resolution.json")
    purge_cmd.add_argument("--out-dir", type=Path, default=Path("."))
    purge_cmd.set_defaults(
        func=lambda args: purge(list(args.describe), args.meta, args.schema, args.out_dir)
    )

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
