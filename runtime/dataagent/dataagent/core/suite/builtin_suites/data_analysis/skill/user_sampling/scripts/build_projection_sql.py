#!/usr/bin/env python3
"""Generate step1_4 CTAS SQL from the sampling plan and table schema.

Reads ``step1_0_sampling_plan.json`` + ``step1_0_table_schema.json`` and writes
one ``CREATE OR REPLACE TABLE ... AS SELECT`` file per ``projections[]`` item,
plus a combined gate query and a manifest. The sampling subagent submits the
generated files; it should not hand-write projection SQL.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from scene_config import SceneConfig, SceneConfigError, add_config_argument, load_scene_config

PROJECTION_TYPES = ("user_table", "user_keyed", "game_keyed")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")
_WRAPPED_TYPE = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)\s*$", re.I)


class ProjectionSqlError(ValueError):
    """Invalid plan/schema combination that the agent can fix and rerun."""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    value_type: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: dict[str, ColumnInfo]
    user_id_names: tuple[str, ...] = ()
    game_names: tuple[str, ...] = ()

    def column(self, name: str) -> ColumnInfo | None:
        if name in self.columns:
            return self.columns[name]
        lowered = name.lower()
        for col in self.columns.values():
            if col.name.lower() == lowered:
                return col
        return None

    def has_column(self, name: str) -> bool:
        return self.column(name) is not None

    def first_named(self, candidates: tuple[str, ...]) -> ColumnInfo | None:
        for candidate in candidates:
            found = self.column(candidate)
            if found is not None:
                return found
        return None

    def user_id_columns(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for candidate in self.user_id_names:
            col = self.column(candidate)
            if col is not None and col.name.lower() not in seen:
                seen.add(col.name.lower())
                found.append(col.name)
        return found


@dataclass(frozen=True)
class Projection:
    index: int
    table: str
    type: str
    user_key: str
    game_key: str


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionSqlError(f"Cannot read JSON {path}: {exc}") from exc


def quote_ident(name: str) -> str:
    ident = str(name or "").strip()
    if not ident:
        raise ProjectionSqlError("empty identifier")
    if _IDENT.match(ident):
        return ident
    return f"`{ident.replace('`', '``')}`"


def qualify(database: str, table: str) -> str:
    return f"{quote_ident(database)}.{quote_ident(table)}"


def unwrap_type(value_type: str) -> str:
    current = str(value_type or "").strip()
    for _ in range(4):
        match = _WRAPPED_TYPE.match(current)
        if not match:
            break
        current = match.group(1).strip()
    return current


def is_string_type(value_type: str) -> bool:
    inner = unwrap_type(value_type).lower()
    return inner.startswith("string") or inner.startswith("fixedstring")


def type_family(value_type: str) -> str:
    inner = unwrap_type(value_type).lower()
    if not inner:
        return ""
    if inner.startswith("string") or inner.startswith("fixedstring"):
        return "string"
    if inner.startswith("uuid"):
        return "uuid"
    if inner.startswith("int") or inner.startswith("uint"):
        return "int"
    if inner.startswith("enum"):
        return "enum"
    if inner.startswith("float") or inner.startswith("decimal"):
        return "float"
    return inner.split("(", 1)[0]


def types_need_cast(left_type: str, right_type: str) -> bool:
    left = type_family(left_type)
    right = type_family(right_type)
    if not left or not right:
        return True
    return left != right


def not_empty_predicate(alias: str, column: str, value_type: str) -> str:
    qualified = f"{alias}.{quote_ident(column)}"
    if is_string_type(value_type):
        return f"{qualified} IS NOT NULL AND {qualified} != ''"
    return f"{qualified} IS NOT NULL"


def join_on(alias: str, column: str, source_type: str, sampled_type: str) -> str:
    left = f"{alias}.{quote_ident(column)}"
    right = "s.user_key"
    if types_need_cast(source_type, sampled_type):
        return f"toString({left}) = toString({right})"
    return f"{left} = {right}"


def label_in_values(value_type: str) -> str:
    if is_string_type(value_type) or type_family(value_type) == "enum":
        return "'0', '1'"
    return "0, 1"


def filename_stem(index: int, table: str) -> str:
    cleaned = _SAFE_FILENAME.sub("_", table).strip("._") or "table"
    return f"{index:02d}_{cleaned}"


def sql_value_literal(value: str, value_type: str) -> str:
    if is_string_type(value_type) or type_family(value_type) in {"enum", "uuid", ""}:
        return "'" + str(value).replace("'", "''") + "'"
    return str(value)


def target_game_value(plan: dict[str, Any]) -> str:
    scope = plan.get("game_scope") if isinstance(plan.get("game_scope"), dict) else {}
    return str(scope.get("target") or "").strip()


def is_catalog_table(table: str, config: SceneConfig) -> bool:
    """Catalog dims listed in config.yaml projection.catalog_copy: copy whole."""
    return config.is_catalog_copy(table)


def is_game_filter_target(table: str, config: SceneConfig) -> bool:
    """game_keyed tables that are not catalog_copy get the target_game filter."""
    return not config.is_catalog_copy(table)


def resolve_game_key(info: TableInfo, explicit: str, default: str) -> str:
    for candidate in (explicit, default, *info.game_names):
        name = str(candidate or "").strip()
        if not name:
            continue
        column = info.column(name)
        if column is not None:
            return column.name
    return ""


def _index_tables(schema: dict[str, Any], config: SceneConfig) -> dict[str, TableInfo]:
    config.assert_schema_compatible(schema)
    tables: dict[str, TableInfo] = {}
    user_id_names = config.aliases_for(schema, "user_id")
    game_names = config.aliases_for(schema, "game")
    for raw in schema.get("tables") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        columns: dict[str, ColumnInfo] = {}
        for col in raw.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = str(col.get("name") or "").strip()
            if not col_name:
                continue
            columns[col_name] = ColumnInfo(
                name=col_name,
                value_type=str(col.get("valueType") or col.get("type") or ""),
            )
        tables[name] = TableInfo(
            name=name,
            columns=columns,
            user_id_names=user_id_names,
            game_names=game_names,
        )
    return tables


def _sampled_user_key_type(plan: dict[str, Any], tables: dict[str, TableInfo]) -> str:
    keys = plan.get("keys") if isinstance(plan.get("keys"), dict) else {}
    default_key = str(keys.get("user_key_default") or "").strip()
    sources = plan.get("sampling_sources") if isinstance(plan.get("sampling_sources"), dict) else {}
    user_table_name = str(sources.get("user_table") or "").strip()
    if user_table_name and user_table_name in tables and default_key:
        column = tables[user_table_name].column(default_key)
        if column is not None:
            return column.value_type
    for table in tables.values():
        if default_key:
            column = table.column(default_key)
            if column is not None:
                return column.value_type
        user_cols = table.user_id_columns()
        if user_cols:
            column = table.column(user_cols[0])
            if column is not None:
                return column.value_type
    return ""


def parse_projections(plan: dict[str, Any], tables: dict[str, TableInfo], config: SceneConfig) -> list[Projection]:
    raw_projections = plan.get("projections")
    if not isinstance(raw_projections, list) or not raw_projections:
        raise ProjectionSqlError("plan.projections[] is required and must be non-empty")

    keys = plan.get("keys") if isinstance(plan.get("keys"), dict) else {}
    default_user_key = str(keys.get("user_key_default") or "").strip()
    default_game_key = str(keys.get("game_key_default") or "").strip()
    seen: set[str] = set()
    projections: list[Projection] = []

    for index, item in enumerate(raw_projections):
        if not isinstance(item, dict):
            raise ProjectionSqlError(f"projections[{index}] must be an object")
        table = str(item.get("table") or "").strip()
        proj_type = str(item.get("type") or "").strip()
        if not table:
            raise ProjectionSqlError(f"projections[{index}].table is required")
        if table in seen:
            raise ProjectionSqlError(f"duplicate projections table: {table}")
        seen.add(table)
        if proj_type not in PROJECTION_TYPES:
            raise ProjectionSqlError(
                f"projections[{index}] ({table}) type must be one of {PROJECTION_TYPES}, got {proj_type!r}"
            )
        user_key = str(item.get("user_key") or default_user_key).strip()
        explicit_game_key = str(item.get("game_key") or "").strip()
        game_key = explicit_game_key or default_game_key
        if table not in tables:
            raise ProjectionSqlError(f"projections[{index}] table {table!r} is missing from schema.tables")
        info = tables[table]
        if proj_type in {"user_table", "user_keyed"}:
            if not user_key:
                raise ProjectionSqlError(f"projections[{index}] ({table}) needs user_key")
            if info.column(user_key) is None:
                raise ProjectionSqlError(
                    f"projections[{index}] ({table}) user_key {user_key!r} is not in schema columns"
                )
        if proj_type == "game_keyed":
            user_cols = info.user_id_columns()
            if user_cols:
                raise ProjectionSqlError(
                    f"projections[{index}] ({table}) is game_keyed but schema has user key column(s) "
                    f"{user_cols}; set type to user_keyed"
                )
            if explicit_game_key and info.column(explicit_game_key) is None:
                raise ProjectionSqlError(
                    f"projections[{index}] ({table}) game_key {explicit_game_key!r} is not in schema columns"
                )
            game_key = resolve_game_key(info, explicit_game_key, default_game_key)
            if is_game_filter_target(table, config) and not game_key:
                raise ProjectionSqlError(
                    f"projections[{index}] ({table}) is a 1-row-per-game dim and needs "
                    f"game_key / keys.game_key_default (or a column in {info.game_names})"
                )
        projections.append(
            Projection(
                index=index,
                table=table,
                type=proj_type,
                user_key=user_key,
                game_key=game_key,
            )
        )

    inventory = plan.get("inventory_check") if isinstance(plan.get("inventory_check"), dict) else {}
    expected = inventory.get("table_count")
    if expected is not None and int(expected) != len(projections):
        raise ProjectionSqlError(
            f"inventory_check.table_count={expected} != len(projections)={len(projections)}"
        )
    return projections


def _user_table_select(table: TableInfo, mode: str, label_names: tuple[str, ...]) -> str:
    if mode == "prelabeled":
        return "src.*"
    label_col = table.first_named(label_names)
    if label_col is not None:
        return f"src.* EXCEPT ({quote_ident(label_col.name)}), s.label"
    return "src.*, s.label"


def render_ctas(
    projection: Projection,
    *,
    plan: dict[str, Any],
    tables: dict[str, TableInfo],
    sampled_type: str,
    config: SceneConfig,
    label_names: tuple[str, ...],
) -> str:
    source_db = str(plan.get("source_database") or "").strip()
    output_db = str(plan.get("output_database") or "").strip()
    if not source_db or not output_db:
        raise ProjectionSqlError("plan.source_database and plan.output_database are required")

    mode = str(plan.get("mode") or "regular").strip() or "regular"
    keys = plan.get("keys") if isinstance(plan.get("keys"), dict) else {}
    info = tables[projection.table]
    src = qualify(source_db, projection.table)
    dst = qualify(output_db, projection.table)
    sampled = qualify(output_db, "step1_temp_sampled_users")
    header = (
        f"CREATE OR REPLACE TABLE {dst}\n"
        "ENGINE = MergeTree()\n"
        "ORDER BY tuple()\n"
        "AS\n"
    )

    if projection.type == "game_keyed":
        if is_catalog_table(projection.table, config):
            return (
                f"{header}"
                "SELECT *\n"
                f"FROM {src} AS t;\n"
            )
        game_col_info = info.column(projection.game_key) if projection.game_key else None
        target = target_game_value(plan)
        if game_col_info is not None and target:
            empty = not_empty_predicate("t", game_col_info.name, game_col_info.value_type)
            literal = sql_value_literal(target, game_col_info.value_type)
            return (
                f"{header}"
                "SELECT *\n"
                f"FROM {src} AS t\n"
                f"WHERE t.{quote_ident(game_col_info.name)} = {literal}\n"
                f"  AND {empty};\n"
            )
        raise ProjectionSqlError(
            f"{projection.table} requires game_scope.target and a valid per-table game_key; "
            "only projection.catalog_copy tables may be copied without a target filter"
        )

    column = info.column(projection.user_key)
    assert column is not None
    on_clause = join_on("src" if projection.type == "user_table" else "t", column.name, column.value_type, sampled_type)
    empty_alias = "src" if projection.type == "user_table" else "t"
    empty = not_empty_predicate(empty_alias, column.name, column.value_type)
    limit_by = f"LIMIT 1 BY src.{quote_ident(column.name)}"

    if projection.type == "user_keyed":
        return (
            f"{header}"
            "SELECT t.*\n"
            f"FROM {src} AS t\n"
            f"INNER JOIN {sampled} AS s\n"
            f"  ON {on_clause}\n"
            f"WHERE {empty};\n"
        )

    if mode == "prelabeled":
        label_column = str(keys.get("label_column") or "").strip()
        if not label_column:
            raise ProjectionSqlError("mode=prelabeled requires keys.label_column")
        label_info = info.column(label_column)
        if label_info is None:
            raise ProjectionSqlError(
                f"keys.label_column {label_column!r} is not in {projection.table} columns"
            )
        values = label_in_values(label_info.value_type)
        return (
            f"{header}"
            "SELECT src.*\n"
            f"FROM {src} AS src\n"
            f"INNER JOIN {sampled} AS s\n"
            f"  ON {on_clause}\n"
            f"WHERE {empty}\n"
            f"  AND src.{quote_ident(label_info.name)} IN ({values})\n"
            f"{limit_by};\n"
        )

    select_clause = _user_table_select(info, mode, label_names)
    return (
        f"{header}"
        f"SELECT {select_clause}\n"
        f"FROM {src} AS src\n"
        f"INNER JOIN {sampled} AS s\n"
        f"  ON {on_clause}\n"
        f"WHERE {empty}\n"
        f"{limit_by};\n"
    )


def render_gate_sql(projections: list[Projection], plan: dict[str, Any], tables: dict[str, TableInfo]) -> str:
    source_db = str(plan.get("source_database") or "").strip()
    output_db = str(plan.get("output_database") or "").strip()
    sampled = qualify(output_db, "step1_temp_sampled_users")
    gate = qualify(output_db, "step1_temp_step1_4_gate")
    branches: list[str] = []
    for projection in projections:
        dst = qualify(output_db, projection.table)
        table_literal = projection.table.replace("'", "''")
        if projection.type == "game_keyed":
            out_users_sql = "toUInt64(0)"
        else:
            column = tables[projection.table].column(projection.user_key)
            assert column is not None
            out_users_sql = f"(SELECT uniqExact({quote_ident(column.name)}) FROM {dst})"
        branch = (
            "SELECT\n"
            f"  '{table_literal}' AS proj_table,\n"
            f"  '{projection.type}' AS proj_type,\n"
            f"  {out_users_sql} AS out_users,\n"
            f"  (SELECT count() FROM {sampled}) AS sampled_n,\n"
            f"  (SELECT count() FROM {dst}) AS out_rows,\n"
            "  (\n"
            "    SELECT total_rows\n"
            "    FROM system.tables\n"
            f"    WHERE database = '{source_db.replace(chr(39), chr(39)+chr(39))}'\n"
            f"      AND name = '{table_literal}'\n"
            "  ) AS src_rows"
        )
        branches.append(branch)
    unioned = "\nUNION ALL\n".join(branches)
    return (
        f"CREATE OR REPLACE TABLE {gate}\n"
        "ENGINE = MergeTree()\n"
        "ORDER BY tuple()\n"
        "AS\n"
        f"{unioned};\n"
    )


def render_count_check_sql(plan: dict[str, Any]) -> str:
    output_db = str(plan.get("output_database") or "").strip()
    return (
        "SELECT count() AS actual\n"
        "FROM system.tables\n"
        f"WHERE database = '{output_db.replace(chr(39), chr(39)+chr(39))}'\n"
        "  AND name NOT LIKE 'step1_%';\n"
    )


def build_projection_sql(
    plan: dict[str, Any],
    schema: dict[str, Any],
    config: SceneConfig,
) -> tuple[list[Projection], dict[str, str], str, str]:
    tables = _index_tables(schema, config)
    if not tables:
        raise ProjectionSqlError("schema.tables is empty")
    projections = parse_projections(plan, tables, config)
    sampled_type = _sampled_user_key_type(plan, tables)
    label_names = config.aliases_for(schema, "label")
    files: dict[str, str] = {}
    for projection in projections:
        stem = filename_stem(projection.index, projection.table)
        files[f"{stem}.sql"] = render_ctas(
            projection,
            plan=plan,
            tables=tables,
            sampled_type=sampled_type,
            config=config,
            label_names=label_names,
        )
    gate_sql = render_gate_sql(projections, plan, tables)
    count_sql = render_count_check_sql(plan)
    return projections, files, gate_sql, count_sql


def write_outputs(
    *,
    out_dir: Path,
    projections: list[Projection],
    files: dict[str, str],
    gate_sql: str,
    count_sql: str,
    plan: dict[str, Any],
    config: SceneConfig,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = target_game_value(plan)
    ctas_entries: list[dict[str, Any]] = []
    for projection in projections:
        filename = f"{filename_stem(projection.index, projection.table)}.sql"
        path = out_dir / filename
        path.write_text(files[filename], encoding="utf-8")
        filter_applied = (
            projection.type == "game_keyed"
            and is_game_filter_target(projection.table, config)
            and bool(projection.game_key)
            and bool(target)
        )
        ctas_entries.append(
            {
                "index": projection.index,
                "table": projection.table,
                "type": projection.type,
                "file": filename,
                "user_key": projection.user_key if projection.type != "game_keyed" else None,
                "game_key": projection.game_key if projection.type == "game_keyed" else None,
                "filter_applied": filter_applied if projection.type == "game_keyed" else None,
            }
        )
    (out_dir / "step1_4_gate.sql").write_text(gate_sql, encoding="utf-8")
    (out_dir / "step1_4_count_check.sql").write_text(count_sql, encoding="utf-8")
    manifest = {
        "source_database": plan.get("source_database"),
        "output_database": plan.get("output_database"),
        "mode": plan.get("mode"),
        "table_count": len(projections),
        "ctas": ctas_entries,
        "gate_file": "step1_4_gate.sql",
        "count_check_file": "step1_4_count_check.sql",
        "batch_size": 8,
    }
    (out_dir / "step1_4_ctas_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate step1_4 projection CTAS SQL from plan + schema")
    parser.add_argument("--plan", type=Path, required=True, help="Path to step1_0_sampling_plan.json")
    parser.add_argument("--schema", type=Path, required=True, help="Path to step1_0_table_schema.json")
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory for generated .sql files")
    add_config_argument(parser)
    args = parser.parse_args()

    try:
        config = load_scene_config(args.config)
        plan = _load_json(args.plan)
        schema = _load_json(args.schema)
        if not isinstance(plan, dict) or not isinstance(schema, dict):
            raise ProjectionSqlError("plan and schema must be JSON objects")
        projections, files, gate_sql, count_sql = build_projection_sql(plan, schema, config)
        manifest = write_outputs(
            out_dir=args.out_dir,
            projections=projections,
            files=files,
            gate_sql=gate_sql,
            count_sql=count_sql,
            plan=plan,
            config=config,
        )
    except (ProjectionSqlError, SceneConfigError) as exc:
        print(f"build_projection_sql failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(
        f"wrote {args.out_dir}: {manifest['table_count']} ctas, "
        f"gate={manifest['gate_file']}, manifest=step1_4_ctas_manifest.json"
    )


if __name__ == "__main__":
    main()
