#!/usr/bin/env python3
"""Convert a semantic ontology download dump into step1_0_table_schema.json.

The output is isomorphic to step1_output_meta.json: table_names, tables[].columns,
join_hints, role_candidates, column_aliases. Reads the dump file that
semantic_download wrote under .dataagent/tool_outputs/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from scene_config import SceneConfig, SceneConfigError, add_config_argument, load_scene_config

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EQ_SPLIT = re.compile(r"\s*=\s*")
_CH_TYPE = re.compile(
    r"^(?:Nullable\()?("
    r"UInt\d+|Int\d+|Float\d+|Decimal(?:\([^)]+\))?|String|FixedString\(\d+\)|"
    r"Date|Date32|DateTime(?:64)?(?:\([^)]+\))?|UUID|Boolean|Bool|"
    r"Array\([^)]+\)|LowCardinality\([^)]+\)|Enum\d*\([^)]+\)"
    r")\)?$",
    re.I,
)


def _load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _maybe_parse_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _unwrap_payload(payload: Any) -> Any:
    current = _maybe_parse_json(payload)
    for _ in range(8):
        if not isinstance(current, dict):
            return current
        if "structuredContent" in current:
            current = _maybe_parse_json(current["structuredContent"])
            continue
        content = current.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and "text" in first:
                current = _maybe_parse_json(first["text"])
                continue
        for key in ("result", "data", "ontology", "payload"):
            nested = current.get(key)
            if isinstance(nested, (dict, list)):
                current = nested
                break
        else:
            return current
    return current


def _assert_download_ok(payload: Any) -> None:
    if not isinstance(payload, dict) or "code" not in payload:
        return
    if str(payload.get("code")) not in {"200", "0"}:
        message = payload.get("message") or payload.get("msg") or f"download failed code={payload.get('code')}"
        raise ValueError(str(message))


def _ontology_root(payload: Any) -> dict[str, Any]:
    _assert_download_ok(payload)
    current = _unwrap_payload(payload)
    if not isinstance(current, dict):
        raise ValueError("ontology dump must be a JSON object")
    if "entities" in current or "dataAccessPlan" in current:
        return current
    scene_roots = [
        value
        for value in current.values()
        if isinstance(value, dict) and ("entities" in value or "dataAccessPlan" in value)
    ]
    if len(scene_roots) == 1:
        return scene_roots[0]
    if len(scene_roots) > 1:
        for value in scene_roots:
            if value.get("entities"):
                return value
        return scene_roots[0]
    return current


def _table_name_from_full(full_table_name: Any) -> str:
    text = str(full_table_name or "").strip()
    if not text:
        return ""
    return text.split(".")[-1]


def _identifier_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    name = text.split(".")[-1]
    return name if _IDENT.match(name) else ""


def _entity_table_name(entity: dict[str, Any]) -> str:
    sources = entity.get("data_sources")
    source_alias_keys: set[str] = set()
    if isinstance(sources, dict):
        source_alias_keys = {str(key) for key in sources}
        for source in sources.values():
            if isinstance(source, dict):
                name = _identifier_name(source.get("full_table_name")) or _table_name_from_full(
                    source.get("full_table_name")
                )
                if name:
                    return name
    alias_counts: dict[str, int] = {}
    for prop in entity.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        alias = _identifier_name(prop.get("alias_table_name"))
        if alias and alias not in source_alias_keys:
            alias_counts[alias] = alias_counts.get(alias, 0) + 1
    if alias_counts:
        return max(alias_counts, key=alias_counts.get)
    for key in ("api_name", "table_name", "name", "display_name"):
        name = _identifier_name(entity.get(key))
        if name:
            return name
    return str(entity.get("api_name") or entity.get("display_name") or "").strip()


def _primary_key_columns(entity: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def _add(col: Any) -> None:
        text = str(col or "").strip()
        if text and text not in seen:
            seen.add(text)
            keys.append(text)

    for col in entity.get("primary_properties") or []:
        _add(col)
    sources = entity.get("data_sources")
    if isinstance(sources, dict):
        for source in sources.values():
            if isinstance(source, dict):
                for col in source.get("primary_key_columns") or []:
                    _add(col)
    return keys


def _order_pk_first(columns: list[dict[str, Any]], primary_keys: list[str]) -> list[dict[str, Any]]:
    by_name = {str(col.get("name") or ""): col for col in columns}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in primary_keys:
        column = by_name.get(name)
        if column is not None and name not in seen:
            ordered.append(column)
            seen.add(name)
    for column in columns:
        name = str(column.get("name") or "")
        if name not in seen:
            ordered.append(column)
            seen.add(name)
    return ordered


def _map_value_type(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.lower().replace(" ", "")
    mapping = {
        "bigint": "Int64",
        "int": "Int32",
        "integer": "Int32",
        "smallint": "Int16",
        "tinyint": "Int8",
        "float": "Float64",
        "double": "Float64",
        "real": "Float64",
        "str": "String",
        "string": "String",
        "varchar": "String",
        "text": "String",
        "bool": "UInt8",
        "boolean": "UInt8",
        "date": "Date",
        "datetime": "DateTime",
        "timestamp": "DateTime",
        "list[str]": "Array(String)",
        "list[string]": "Array(String)",
        "array(string)": "Array(String)",
    }
    mapped = mapping.get(lowered)
    if mapped:
        return mapped
    if _CH_TYPE.match(text):
        return text
    return text


def _column_from_property(prop: dict[str, Any], primary_keys: list[str]) -> dict[str, Any] | None:
    name = (
        _identifier_name(prop.get("column_name"))
        or _identifier_name(prop.get("api_name"))
        or _identifier_name(prop.get("name"))
        or str(prop.get("column_name") or prop.get("api_name") or prop.get("name") or "").strip()
    )
    if not name:
        return None
    api_name = str(prop.get("api_name") or "").strip()
    description = prop.get("description")
    if description is None or str(description).strip() == "":
        description = None
    else:
        description = str(description)
    is_pk = bool(prop.get("isPrimaryKey")) or name in primary_keys or api_name in primary_keys
    return {
        "name": name,
        "valueType": _map_value_type(prop.get("data_type") or prop.get("valueType") or prop.get("type")),
        "description": description,
        "isPrimaryKey": is_pk,
    }


def _tables_from_entities(root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    entities = root.get("entities")
    if not isinstance(entities, list):
        return tables
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        table_name = _entity_table_name(entity)
        if not table_name:
            continue
        primary_keys = _primary_key_columns(entity)
        columns: list[dict[str, Any]] = []
        seen: set[str] = set()
        for prop in entity.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            column = _column_from_property(prop, primary_keys)
            if column is None or column["name"] in seen:
                continue
            seen.add(column["name"])
            columns.append(column)
        columns = _order_pk_first(columns, primary_keys)
        property_columns = {
            str(prop.get("api_name") or "").strip(): str(
                _identifier_name(prop.get("column_name"))
                or prop.get("column_name")
                or prop.get("api_name")
                or ""
            ).strip()
            for prop in (entity.get("properties") or [])
            if isinstance(prop, dict) and str(prop.get("api_name") or "").strip()
        }
        existing = tables.get(table_name)
        if existing is None:
            tables[table_name] = {
                "name": table_name,
                "description": entity.get("description") if entity.get("description") else None,
                "columns": columns,
                "api_name": str(entity.get("api_name") or ""),
                "property_columns": property_columns,
            }
            continue
        seen_names = {str(col.get("name") or "") for col in existing["columns"]}
        for column in columns:
            if column["name"] not in seen_names:
                existing["columns"].append(column)
                seen_names.add(column["name"])
        existing["property_columns"].update(property_columns)
        if not existing.get("description") and entity.get("description"):
            existing["description"] = entity.get("description")
    return tables


def _tables_from_data_access_plan(root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plan = root.get("dataAccessPlan")
    if not isinstance(plan, dict):
        return {}
    tables: dict[str, dict[str, Any]] = {}
    for item in plan.get("tables") or []:
        if not isinstance(item, dict):
            continue
        name = _table_name_from_full(item.get("name") or item.get("table") or item.get("tableName"))
        if not name:
            continue
        columns: list[dict[str, Any]] = []
        seen: set[str] = set()
        for col in item.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = str(col.get("name") or col.get("column_name") or "").strip()
            if not col_name or col_name in seen:
                continue
            seen.add(col_name)
            description = col.get("description")
            if description is None or str(description).strip() == "":
                description = None
            else:
                description = str(description)
            columns.append(
                {
                    "name": col_name,
                    "valueType": _map_value_type(col.get("valueType") or col.get("data_type") or col.get("type")),
                    "description": description,
                    "isPrimaryKey": bool(col.get("isPrimaryKey") or col.get("primaryKey")),
                }
            )
        tables[name] = {
            "name": name,
            "description": item.get("description") if item.get("description") else None,
            "columns": columns,
            "api_name": "",
            "property_columns": {},
        }
    return tables


def _ref_to_table_col(ref: str) -> str | None:
    parts = [part for part in str(ref or "").strip().split(".") if part]
    if len(parts) >= 2:
        return f"{parts[-2]}.{parts[-1]}"
    return None


def _join_note(rel: dict[str, Any]) -> str:
    card = str(rel.get("cardinality") or "").strip().upper()
    title = str(rel.get("display_name") or rel.get("description") or "").strip()
    if card == "ONE_TO_ONE":
        kind = "INNER JOIN 1:1"
    elif card == "ONE_TO_MANY":
        extra = "聚合前需对 N 端去重，否则度量翻倍"
        if title:
            return f"LEFT JOIN 1:N，{title}；{extra}"
        return f"LEFT JOIN 1:N，{extra}"
    elif card == "MANY_TO_ONE":
        kind = "INNER JOIN N:1"
    elif card == "MANY_TO_MANY":
        kind = "INNER JOIN N:N"
    else:
        kind = ""
    if kind and title:
        return f"{kind}，{title}"
    return kind or title


def _join_hints_from_relations(
    root: dict[str, Any],
    tables: dict[str, dict[str, Any]],
    allowed: set[str] | None,
) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    api_to_table = {name: name for name in tables}
    for name, item in tables.items():
        api_name = str(item.get("api_name") or "").strip()
        if api_name:
            api_to_table[api_name] = name

    def _add(left: str | None, right: str | None, note: Any) -> None:
        if not left or not right:
            return
        left_table = left.split(".", 1)[0]
        right_table = right.split(".", 1)[0]
        if allowed is not None and (left_table not in allowed or right_table not in allowed):
            return
        key = (left, right)
        if key in seen:
            return
        seen.add(key)
        hint = {"left": left, "right": right}
        if note is None or str(note).strip() == "":
            hint["note"] = ""
        else:
            hint["note"] = str(note)
        hints.append(hint)

    relations = root.get("relations")
    if isinstance(relations, list):
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            description = rel.get("description")
            parsed = False
            if isinstance(description, str) and "=" in description:
                sides = _EQ_SPLIT.split(description.strip(), maxsplit=1)
                if len(sides) == 2:
                    _add(_ref_to_table_col(sides[0]), _ref_to_table_col(sides[1]), description)
                    parsed = True
            if parsed:
                continue
            mapping = rel.get("mapping_keys") if isinstance(rel.get("mapping_keys"), dict) else {}
            source_table = api_to_table.get(str(rel.get("source_entity_type") or ""))
            target_table = api_to_table.get(str(rel.get("target_entity_type") or ""))
            source_props = mapping.get("source_properties") or []
            target_props = mapping.get("target_properties") or []
            if source_table and target_table and source_props and target_props:
                source_col = tables[source_table]["property_columns"].get(str(source_props[0])) or str(
                    source_props[0]
                )
                target_col = tables[target_table]["property_columns"].get(str(target_props[0])) or str(
                    target_props[0]
                )
                if source_col and target_col:
                    _add(
                        f"{source_table}.{source_col}",
                        f"{target_table}.{target_col}",
                        _join_note(rel),
                    )

    plan = root.get("dataAccessPlan")
    if isinstance(plan, dict):
        for path in plan.get("joinPaths") or plan.get("joins") or []:
            if not isinstance(path, dict):
                continue
            left = path.get("left")
            right = path.get("right")
            if isinstance(left, str) and isinstance(right, str) and "." in left and "." in right:
                _add(_ref_to_table_col(left), _ref_to_table_col(right), path.get("note") or path.get("on"))
                continue
            on_text = str(path.get("on") or path.get("condition") or "")
            if "=" in on_text:
                sides = _EQ_SPLIT.split(on_text.strip(), maxsplit=1)
                if len(sides) == 2:
                    _add(_ref_to_table_col(sides[0]), _ref_to_table_col(sides[1]), path.get("note") or on_text)
    return hints


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("tables", "rows", "data", "items", "result"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _load_inventory(path: Path | None, inline: str | None) -> list[str] | None:
    names: list[str] = []
    if inline:
        names.extend(part.strip() for part in inline.split(",") if part.strip())
    if path is not None:
        text = path.read_text(encoding="utf-8")
        try:
            raw = _unwrap_payload(json.loads(text))
        except json.JSONDecodeError:
            raw = text
        if isinstance(raw, str):
            names.extend(part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip())
        else:
            for item in _as_list(raw) or (raw if isinstance(raw, list) else []):
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, dict):
                    name = str(item.get("name") or item.get("table") or "").strip()
                    if name:
                        names.append(_table_name_from_full(name))
    if not names:
        return None
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _load_columns_file(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    raw = _unwrap_payload(_load_json(path))
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[str]] = {}
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        table = _table_name_from_full(item.get("table") or item.get("table_name"))
        name = str(item.get("name") or item.get("column") or "").strip()
        if not table or not name:
            continue
        if name in seen.setdefault(table, set()):
            continue
        seen[table].add(name)
        grouped.setdefault(table, []).append(
            {
                "name": name,
                "valueType": _map_value_type(item.get("type") or item.get("valueType")),
                "description": None,
                "isPrimaryKey": False,
            }
        )
    return grouped


def _merge_columns(
    ontology_cols: list[dict[str, Any]],
    ch_cols: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_name = {col["name"]: dict(col) for col in ontology_cols}
    order = [col["name"] for col in ontology_cols]
    for col in ch_cols:
        existing = by_name.get(col["name"])
        if existing is None:
            by_name[col["name"]] = dict(col)
            order.append(col["name"])
            continue
        if not existing.get("valueType") and col.get("valueType"):
            existing["valueType"] = col["valueType"]
    if not ontology_cols:
        return [dict(col) for col in ch_cols]
    return [by_name[name] for name in order]


def _missing_columns(tables: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for table in tables:
        columns = table.get("columns") or []
        if not columns:
            missing.append(table["name"])
            continue
        if any(not str(col.get("name") or "").strip() for col in columns):
            missing.append(table["name"])
    return missing


def build_schema(
    dump: Any,
    source_database: str,
    inventory: list[str] | None,
    extra_columns: dict[str, list[dict[str, Any]]],
    config: SceneConfig,
) -> dict[str, Any]:
    root = _ontology_root(dump)
    ontology_tables = _tables_from_entities(root)
    if not ontology_tables:
        ontology_tables = _tables_from_data_access_plan(root)

    table_names = list(inventory) if inventory is not None else sorted(ontology_tables.keys())
    tables: list[dict[str, Any]] = []
    for name in table_names:
        source = ontology_tables.get(name, {"name": name, "description": None, "columns": []})
        columns = _merge_columns(list(source.get("columns") or []), extra_columns.get(name, []))
        tables.append(
            {
                "name": name,
                "description": source.get("description"),
                "columns": columns,
            }
        )

    allowed = set(table_names)
    return {
        "source_database": source_database,
        "scene_config": config.schema_metadata(),
        "table_names": table_names,
        "tables": tables,
        "join_hints": _join_hints_from_relations(root, ontology_tables, allowed),
        "role_candidates": config.role_candidates(tables),
        "column_aliases": config.column_aliases(tables),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build step1_0_table_schema.json from a semantic_download dump"
    )
    parser.add_argument("--dump", type=Path, required=True, help="Path from semantic_download receipt")
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--out", type=Path, required=True, help="Path to step1_0_table_schema.json")
    parser.add_argument("--inventory-file", type=Path, default=None)
    parser.add_argument("--inventory", default=None, help="Comma-separated table names")
    parser.add_argument("--columns-file", type=Path, default=None, help="CH system.columns JSON dump")
    add_config_argument(parser)
    args = parser.parse_args()

    try:
        config = load_scene_config(args.config)
        dump = _load_json(args.dump)
        inventory = _load_inventory(args.inventory_file, args.inventory)
        extra_columns = _load_columns_file(args.columns_file)
        schema = build_schema(dump, args.source_database, inventory, extra_columns, config)
    except (SceneConfigError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"build_table_schema failed: {exc}") from exc

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    missing = _missing_columns(schema["tables"])
    table_count = len(schema["table_names"])
    if missing:
        print(
            f"wrote {args.out}: {table_count} tables, missing_columns={missing}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"wrote {args.out}: {table_count} tables, columns complete")


if __name__ == "__main__":
    main()
