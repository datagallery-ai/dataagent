#!/usr/bin/env python3
"""Build a sampling-plan skeleton from table schema + task params.

Fills only fields that follow from schema/column names and explicit task
params. The agent still:

1. Submits the generated mode-probe SQL (if a label column exists).
2. Reruns this script with ``--mode prelabeled`` or ``--mode regular``.
3. For regular: fills ``y_label.family``, ``sql_fragments.positive_label``,
   and ``negative_populations`` from real table values.

It does not invent y-label families, positive-label predicates, similar-game
dimensions, or event-time columns that are not listed in config.yaml.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from scene_config import SceneConfig, SceneConfigError, add_config_argument, load_scene_config

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WRAPPED_TYPE = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)\s*$", re.I)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class SamplingPlanError(ValueError):
    """Invalid schema/params combination that the agent can fix and rerun."""


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

    def first_named(self, candidates: tuple[str, ...]) -> ColumnInfo | None:
        for candidate in candidates:
            found = self.column(candidate)
            if found is not None:
                return found
        return None

    def user_id_columns(self) -> list[ColumnInfo]:
        found: list[ColumnInfo] = []
        seen: set[str] = set()
        for candidate in self.user_id_names:
            column = self.column(candidate)
            if column is not None and column.name.lower() not in seen:
                seen.add(column.name.lower())
                found.append(column)
        return found


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SamplingPlanError(f"Cannot read JSON {path}: {exc}") from exc


def quote_ident(name: str) -> str:
    ident = str(name or "").strip()
    if not ident:
        raise SamplingPlanError("empty identifier")
    if _IDENT.match(ident):
        return ident
    return f"`{ident.replace('`', '``')}`"


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
    if inner.startswith("date"):
        return "date"
    if inner.startswith("float") or inner.startswith("decimal"):
        return "float"
    return inner.split("(", 1)[0]


def sql_literal(value: str, value_type: str) -> str:
    if is_string_type(value_type) or type_family(value_type) in {"enum", "uuid", "", "date"}:
        return "'" + str(value).replace("'", "''") + "'"
    return str(value)


def target_game_filter(column: ColumnInfo | None, target_game: str) -> str | None:
    if column is None or not target_game:
        return None
    return f"{quote_ident(column.name)} = {sql_literal(target_game, column.value_type)}"


def key_expr(column: ColumnInfo) -> str:
    ident = quote_ident(column.name)
    if is_string_type(column.value_type):
        return f"assumeNotNull({ident})"
    return ident


def valid_predicate(column: ColumnInfo) -> str:
    ident = quote_ident(column.name)
    if is_string_type(column.value_type):
        return f"{ident} IS NOT NULL AND {ident} != ''"
    return f"{ident} IS NOT NULL"


def label_values(value_type: str) -> tuple[str, str]:
    if is_string_type(value_type) or type_family(value_type) == "enum":
        return "'1'", "'0'"
    return "1", "0"


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


def _first_role(schema: dict[str, Any], role: str) -> str:
    roles = schema.get("role_candidates") if isinstance(schema.get("role_candidates"), dict) else {}
    values = roles.get(role) or []
    if isinstance(values, list):
        for item in values:
            name = str(item or "").strip()
            if name:
                return name
    return ""


def _table_names(schema: dict[str, Any], tables: dict[str, TableInfo]) -> list[str]:
    names = [str(name).strip() for name in (schema.get("table_names") or []) if str(name).strip()]
    if names:
        return names
    return list(tables.keys())


def _pick_user_table(schema: dict[str, Any], tables: dict[str, TableInfo]) -> TableInfo:
    name = _first_role(schema, "user_table")
    if name and name in tables:
        info = tables[name]
        if info.user_id_columns():
            return info
        raise SamplingPlanError(
            f"role_candidates.user_table {name!r} has no user-id column; fix schema and rerun"
        )
    for info in tables.values():
        if info.user_id_columns():
            return info
    raise SamplingPlanError("no table with a user-id column; cannot build projections")


def _pick_game_dim(schema: dict[str, Any], tables: dict[str, TableInfo], user_table: str) -> TableInfo | None:
    name = _first_role(schema, "game_dim")
    if name and name in tables and name != user_table:
        return tables[name]
    for info in tables.values():
        if info.name == user_table:
            continue
        if info.user_id_columns():
            continue
        if info.first_named(info.game_names) is not None:
            return info
    return None


def _pick_user_key(table: TableInfo, default: ColumnInfo | None) -> ColumnInfo | None:
    own = table.user_id_columns()
    if own:
        return own[0]
    return default


def build_projections(
    table_names: list[str],
    tables: dict[str, TableInfo],
    user_table: TableInfo,
    config: SceneConfig,
) -> list[dict[str, Any]]:
    default_user = user_table.user_id_columns()[0]
    projections: list[dict[str, Any]] = []
    for name in table_names:
        if name not in tables:
            raise SamplingPlanError(f"schema.table_names has {name!r} but tables[] does not")
        info = tables[name]
        if name == user_table.name:
            projections.append(
                {"table": name, "type": "user_table", "user_key": default_user.name}
            )
            continue
        user_cols = info.user_id_columns()
        if user_cols:
            projections.append(
                {"table": name, "type": "user_keyed", "user_key": user_cols[0].name}
            )
            continue
        game_col = info.first_named(info.game_names)
        if game_col is None and not config.is_catalog_copy(name):
            raise SamplingPlanError(
                f"table {name!r} has neither a configured user-id nor game column; "
                "add the column alias or list the table under projection.catalog_copy"
            )
        item: dict[str, Any] = {"table": name, "type": "game_keyed"}
        if game_col is not None:
            item["game_key"] = game_col.name
        projections.append(item)
    return projections


def _time_column(
    tables: dict[str, TableInfo],
    preferred: list[str],
    time_names: tuple[str, ...],
) -> ColumnInfo | None:
    for name in preferred:
        if name and name in tables:
            found = tables[name].first_named(time_names)
            if found is not None:
                return found
    for info in tables.values():
        found = info.first_named(time_names)
        if found is not None:
            return found
    return None


def _parse_iso_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text or not _ISO_DATE.match(text):
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def time_fragments(column: ColumnInfo, t0: str, label_window_days: Any, lookback_days: Any) -> dict[str, str] | None:
    start = _parse_iso_date(t0)
    try:
        window = int(label_window_days)
        lookback = int(lookback_days)
    except (TypeError, ValueError):
        return None
    if start is None or window <= 0 or lookback <= 0:
        return None
    end = start + timedelta(days=window)
    lookback_start = start - timedelta(days=lookback)
    ident = quote_ident(column.name)
    family = type_family(column.value_type)
    if is_string_type(column.value_type) or family not in {"date", "date32", "datetime", "datetime64"}:
        expr = f"parseDateTimeBestEffortOrNull({ident})"
        t0_sql = f"parseDateTimeBestEffort('{start.isoformat()}')"
        end_sql = f"parseDateTimeBestEffort('{end.isoformat()}')"
        look_sql = f"parseDateTimeBestEffort('{lookback_start.isoformat()}')"
    else:
        expr = ident
        t0_sql = f"toDate('{start.isoformat()}')"
        end_sql = f"toDate('{end.isoformat()}')"
        look_sql = f"toDate('{lookback_start.isoformat()}')"
    return {
        "label_window": f"{expr} > {t0_sql} AND {expr} <= {end_sql}",
        "pre_t0_lookback": f"{expr} > {look_sql} AND {expr} <= {t0_sql}",
        "through_t0": f"{expr} <= {t0_sql}",
    }


def collect_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.params:
        loaded = _load_json(args.params)
        if not isinstance(loaded, dict):
            raise SamplingPlanError("--params must be a JSON object")
        params.update(loaded)
    cli = {
        "source_database": args.source_database,
        "output_database": args.output_database,
        "target_game": args.target_game,
        "run_id": args.run_id,
        "T0": args.t0,
        "label_window_days": args.label_window_days,
        "lookback_days": args.lookback_days,
        "sample_size": args.sample_size,
        "cold_start_threshold": args.cold_start_threshold,
    }
    for key, value in cli.items():
        if value is not None:
            params[key] = value
    source = str(params.get("source_database") or "").strip()
    output = str(params.get("output_database") or "").strip()
    if not source or not output:
        raise SamplingPlanError("source_database and output_database are required")
    params["source_database"] = source
    params["output_database"] = output
    params["target_game"] = str(params.get("target_game") or "").strip()
    params["run_id"] = str(params.get("run_id") or "").strip() or None
    params["T0"] = str(params.get("T0") or "").strip() or None
    if params.get("cold_start_threshold") in (None, ""):
        params["cold_start_threshold"] = 500
    return params


def build_skeleton(
    schema: dict[str, Any],
    params: dict[str, Any],
    config: SceneConfig,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    tables = _index_tables(schema, config)
    if not tables:
        raise SamplingPlanError("schema.tables is empty")
    table_names = _table_names(schema, tables)
    missing = [name for name in table_names if name not in tables]
    if missing:
        raise SamplingPlanError(f"schema.table_names missing from tables[]: {missing}")

    user_table = _pick_user_table(schema, tables)
    game_dim = _pick_game_dim(schema, tables, user_table.name)
    user_key = user_table.user_id_columns()[0]
    label_col = user_table.first_named(config.aliases_for(schema, "label"))
    game_key = game_dim.first_named(game_dim.game_names) if game_dim is not None else None
    projections = build_projections(table_names, tables, user_table, config)
    has_game_keyed = any(item["type"] == "game_keyed" for item in projections)

    behavior_table = (
        _first_role(schema, "conversion_event")
        or _first_role(schema, "label_event")
        or _first_role(schema, "activity_event")
    )
    behavior_key = user_key
    behavior_game_key = game_key
    if behavior_table and behavior_table in tables:
        picked = _pick_user_key(tables[behavior_table], user_key)
        if picked is not None:
            behavior_key = picked
        picked_game = tables[behavior_table].first_named(tables[behavior_table].game_names)
        if picked_game is not None:
            behavior_game_key = picked_game

    fragments: dict[str, Any] = {
        "user_key_expr": key_expr(user_key),
        "valid_user": valid_predicate(user_key),
        "game_key_expr": key_expr(game_key) if game_key is not None else None,
        "game_filter": None,
        "label_window": None,
        "positive_label": None,
        "pre_t0_lookback": None,
        "through_t0": None,
    }
    if has_game_keyed and params["target_game"]:
        fragments["game_filter"] = target_game_filter(
            behavior_game_key or game_key,
            params["target_game"],
        )
    elif has_game_keyed and not params["target_game"]:
        # Keep projections; agent must supply target_game before game_keyed CTAS.
        fragments["game_filter"] = None

    plan = {
        "source_database": params["source_database"],
        "output_database": params["output_database"],
        "run_id": params.get("run_id"),
        "T0": params.get("T0"),
        "label_window_days": params.get("label_window_days"),
        "lookback_days": params.get("lookback_days"),
        "sample_size": params.get("sample_size"),
        "cold_start_threshold": params.get("cold_start_threshold"),
        "mode": None,
        "game_scope": {"target": params["target_game"] or None, "similar_games": []},
        "y_label": {
            "family": None,
            "task_type": "binary_classification",
            "event_table": None,
        },
        "sampling_sources": {
            "user_table": user_table.name,
            "label_event": None,
            "activity_event": None,
            "conversion_event": None,
            "game_dim": game_dim.name if game_dim is not None else None,
        },
        "keys": {
            "user_key_default": user_key.name,
            "user_key_behavior": behavior_key.name,
            "game_key_default": game_key.name if game_key is not None else None,
            "game_key_behavior": behavior_game_key.name if behavior_game_key is not None else None,
            "event_time": None,
            "similar_dim": None,
            "label_column": label_col.name if label_col is not None else None,
        },
        "sql_fragments": fragments,
        "negative_populations": [],
        "source_table_inventory": {"tables": list(table_names)},
        "inventory_check": {
            "ok": True,
            "table_count": len(table_names),
        },
        "projections": projections,
    }

    probe_sql = None
    if label_col is not None:
        pos, neg = label_values(label_col.value_type)
        probe_sql = (
            "SELECT\n"
            f"  uniqExactIf({fragments['user_key_expr']}, "
            f"{quote_ident(label_col.name)} = {pos}) AS pos_users,\n"
            f"  uniqExactIf({fragments['user_key_expr']}, "
            f"{quote_ident(label_col.name)} = {neg}) AS neg_users\n"
            f"FROM {quote_ident(params['source_database'])}.{quote_ident(user_table.name)}\n"
            f"WHERE {fragments['valid_user']};\n"
        )

    notes = {
        "generated_by": "build_sampling_plan.py",
        "inferred": {
            "user_table": user_table.name,
            "label_column": label_col.name if label_col is not None else None,
            "game_dim": game_dim.name if game_dim is not None else None,
            "game_key_default": game_key.name if game_key is not None else None,
            "projection_count": len(projections),
            "role_candidates": schema.get("role_candidates") or {},
        },
        "model_must": [],
    }
    if probe_sql:
        notes["model_must"].append(
            "submit step1_0_sql/mode_probe.sql, then rerun with "
            "--mode prelabeled (pos_users>0 and neg_users>0) or --mode regular"
        )
    else:
        notes["model_must"].append(
            "user table has no label column; rerun with --mode regular and fill y_label / positive_label"
        )
    if has_game_keyed and not params["target_game"]:
        notes["model_must"].append("target_game is missing; game_filter was left null")
    elif has_game_keyed and not fragments["game_filter"]:
        notes["model_must"].append(
            "no configured game column found for the behavior source; set keys.game_key_behavior "
            "and sql_fragments.game_filter"
        )
    return plan, notes, probe_sql


def apply_mode(
    plan: dict[str, Any],
    schema: dict[str, Any],
    params: dict[str, Any],
    mode: str,
    config: SceneConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode not in {"prelabeled", "regular"}:
        raise SamplingPlanError("--mode must be prelabeled or regular (cold_start is decided in step1_1)")
    tables = _index_tables(schema, config)
    updated = json.loads(json.dumps(plan))
    updated["mode"] = mode
    for key in (
        "source_database",
        "output_database",
        "run_id",
        "T0",
        "label_window_days",
        "lookback_days",
        "sample_size",
        "cold_start_threshold",
    ):
        if params.get(key) not in (None, ""):
            updated[key] = params[key]
    if params.get("target_game"):
        scope = updated.get("game_scope") if isinstance(updated.get("game_scope"), dict) else {}
        scope["target"] = params["target_game"]
        updated["game_scope"] = scope

    fragments = updated.get("sql_fragments") if isinstance(updated.get("sql_fragments"), dict) else {}
    keys = updated.get("keys") if isinstance(updated.get("keys"), dict) else {}
    sources = updated.get("sampling_sources") if isinstance(updated.get("sampling_sources"), dict) else {}
    y_label = updated.get("y_label") if isinstance(updated.get("y_label"), dict) else {}
    notes: dict[str, Any] = {"generated_by": "build_sampling_plan.py", "mode": mode, "model_must": []}

    if mode == "prelabeled":
        if not keys.get("label_column"):
            raise SamplingPlanError("mode=prelabeled requires keys.label_column; check user-table columns")
        y_label["family"] = None
        y_label["event_table"] = None
        y_label["task_type"] = "binary_classification"
        sources["label_event"] = None
        sources["activity_event"] = None
        sources["conversion_event"] = None
        for name in ("label_window", "positive_label", "pre_t0_lookback", "through_t0"):
            fragments[name] = None
        keys["event_time"] = None
        updated["negative_populations"] = []
        notes["model_must"].append("projections / user_key / valid_user / game_filter are complete; go to step1_3")
    else:
        sources["label_event"] = sources.get("label_event") or _first_role(schema, "label_event") or None
        sources["activity_event"] = sources.get("activity_event") or _first_role(schema, "activity_event") or None
        sources["conversion_event"] = sources.get("conversion_event") or _first_role(schema, "conversion_event") or None
        filter_table_name = (
            sources.get("conversion_event")
            or sources.get("label_event")
            or sources.get("activity_event")
            or sources.get("game_dim")
        )
        filter_game_key = None
        if filter_table_name and filter_table_name in tables:
            filter_table = tables[filter_table_name]
            filter_game_key = filter_table.first_named(filter_table.game_names)
        if filter_game_key is None:
            game_dim_name = str(sources.get("game_dim") or "")
            if game_dim_name in tables:
                game_dim_table = tables[game_dim_name]
                filter_game_key = game_dim_table.first_named(game_dim_table.game_names)
        updated_scope = (
            updated.get("game_scope")
            if isinstance(updated.get("game_scope"), dict)
            else {}
        )
        target = str(updated_scope.get("target") or "").strip()
        keys["game_key_behavior"] = filter_game_key.name if filter_game_key is not None else None
        fragments["game_filter"] = target_game_filter(filter_game_key, target)
        preferred = [
            sources.get("label_event") or "",
            sources.get("conversion_event") or "",
            sources.get("activity_event") or "",
        ]
        time_col = _time_column(tables, preferred, config.aliases_for(schema, "event_time"))
        if not keys.get("event_time") and time_col is not None:
            keys["event_time"] = time_col.name
        if time_col is not None and not fragments.get("label_window"):
            built = time_fragments(
                time_col,
                str(updated.get("T0") or ""),
                updated.get("label_window_days"),
                updated.get("lookback_days"),
            )
            if built:
                fragments.update(built)
            else:
                notes["model_must"].append(
                    "event_time inferred but T0 / label_window_days / lookback_days missing; fill time fragments"
                )
        elif not keys.get("event_time"):
            notes["model_must"].append(
                "no event_time column from config.yaml; set keys.event_time and time fragments"
            )
        if not y_label.get("event_table"):
            y_label["event_table"] = sources.get("conversion_event") or sources.get("label_event")
        y_label.setdefault("family", None)
        y_label.setdefault("task_type", "binary_classification")
        fragments.setdefault("positive_label", None)
        if not y_label.get("family") or not fragments.get("positive_label"):
            notes["model_must"].extend(
                [
                    "set y_label.family from the task objective",
                    "query real enum values then set sql_fragments.positive_label",
                    "fill negative_populations for that family",
                ]
            )

    updated["sql_fragments"] = fragments
    updated["keys"] = keys
    updated["sampling_sources"] = sources
    updated["y_label"] = y_label
    return updated, notes


def refresh_generated_fields(
    existing: dict[str, Any],
    schema: dict[str, Any],
    params: dict[str, Any],
    config: SceneConfig,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Refresh schema-derived fields while preserving model-authored decisions."""
    effective = dict(params)
    for key in (
        "source_database",
        "output_database",
        "run_id",
        "T0",
        "label_window_days",
        "lookback_days",
        "sample_size",
        "cold_start_threshold",
    ):
        if effective.get(key) in (None, "") and existing.get(key) not in (None, ""):
            effective[key] = existing[key]
    existing_scope = existing.get("game_scope") if isinstance(existing.get("game_scope"), dict) else {}
    if not effective.get("target_game"):
        effective["target_game"] = str(existing_scope.get("target") or "").strip()

    refreshed, notes, probe_sql = build_skeleton(schema, effective, config)
    refreshed_scope = refreshed["game_scope"]
    refreshed_scope["similar_games"] = list(existing_scope.get("similar_games") or [])

    existing_sources = (
        existing.get("sampling_sources")
        if isinstance(existing.get("sampling_sources"), dict)
        else {}
    )
    available_tables = set(refreshed["source_table_inventory"]["tables"])
    for key in ("label_event", "activity_event", "conversion_event"):
        if existing_sources.get(key) in available_tables:
            refreshed["sampling_sources"][key] = existing_sources[key]

    existing_keys = existing.get("keys") if isinstance(existing.get("keys"), dict) else {}
    for key in ("event_time", "similar_dim"):
        if existing_keys.get(key):
            refreshed["keys"][key] = existing_keys[key]

    existing_fragments = (
        existing.get("sql_fragments")
        if isinstance(existing.get("sql_fragments"), dict)
        else {}
    )
    for key in ("label_window", "positive_label", "pre_t0_lookback", "through_t0"):
        if existing_fragments.get(key):
            refreshed["sql_fragments"][key] = existing_fragments[key]

    existing_label = existing.get("y_label") if isinstance(existing.get("y_label"), dict) else {}
    for key, value in existing_label.items():
        if value not in (None, ""):
            refreshed["y_label"][key] = value
    refreshed["negative_populations"] = list(existing.get("negative_populations") or [])
    return refreshed, notes, probe_sql


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate or finalize step1_0_sampling_plan.json")
    parser.add_argument("--schema", type=Path, required=True, help="Path to step1_0_table_schema.json")
    parser.add_argument("--out", type=Path, required=True, help="Path to step1_0_sampling_plan.json")
    parser.add_argument("--params", type=Path, default=None, help="Optional task-params JSON")
    parser.add_argument("--notes", type=Path, default=None, help="Path to step1_0_plan_notes.json")
    parser.add_argument("--sql-dir", type=Path, default=None, help="Directory for mode_probe.sql")
    parser.add_argument("--mode", choices=("prelabeled", "regular"), default=None)
    parser.add_argument("--source-database", default=None)
    parser.add_argument("--output-database", default=None)
    parser.add_argument("--target-game", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--t0", default=None)
    parser.add_argument("--label-window-days", type=int, default=None)
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--cold-start-threshold", type=int, default=None)
    add_config_argument(parser)
    args = parser.parse_args()

    try:
        config = load_scene_config(args.config)
        schema = _load_json(args.schema)
        if not isinstance(schema, dict):
            raise SamplingPlanError("schema must be a JSON object")
        params = collect_params(args)
        notes_path = args.notes or args.out.with_name("step1_0_plan_notes.json")
        sql_dir = args.sql_dir or args.out.parent / "step1_0_sql"

        if args.mode:
            if not args.out.exists():
                plan, notes, probe_sql = build_skeleton(schema, params, config)
            else:
                existing = _load_json(args.out)
                if not isinstance(existing, dict):
                    raise SamplingPlanError("existing plan must be a JSON object")
                plan, notes, probe_sql = refresh_generated_fields(
                    existing,
                    schema,
                    params,
                    config,
                )
            plan, mode_notes = apply_mode(plan, schema, params, args.mode, config)
            notes.update(mode_notes)
        else:
            plan, notes, probe_sql = build_skeleton(schema, params, config)

        _write_json(args.out, plan)
        _write_json(notes_path, notes)
        if probe_sql and not args.mode:
            sql_dir.mkdir(parents=True, exist_ok=True)
            (sql_dir / "mode_probe.sql").write_text(probe_sql, encoding="utf-8")
    except (SamplingPlanError, SceneConfigError) as exc:
        print(f"build_sampling_plan failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    mode = plan.get("mode") or "unresolved"
    print(
        f"wrote {args.out}: mode={mode}, "
        f"projections={len(plan.get('projections') or [])}, notes={notes_path.name}"
    )


if __name__ == "__main__":
    main()
