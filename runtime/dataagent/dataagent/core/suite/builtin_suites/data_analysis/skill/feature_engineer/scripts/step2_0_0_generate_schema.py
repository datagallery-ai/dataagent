#!/usr/bin/env python3
"""Build the ``schema_resolution.json`` skeleton for step2_0_0.

Statically infers roles from ``step1_output_meta.json`` + ``step1_sample_stats.json``:
``source_tables`` (with per-table ``table_type`` / ``user_key``),
``user_table``, ``user_id``, ``label``, ``age``, ``gender`` and the user-id alias set.
Roles that require profiling stay unresolved until step2_0_2 completes the file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
SCENE_YAML = SCRIPTS_DIR.parent / "references" / "scene.yaml"

_DEFAULT_USER_ID_ALIASES = ("user_id", "usid", "rank_flg", "dsid", "uid")
_AGE_ALIASES = ("age", "age_range", "user_age", "年龄", "年龄段")
_GENDER_ALIASES = ("gender", "sex", "user_gender", "性别")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _scene_config() -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(SCENE_YAML.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _add_alias(aliases: list[str], seen: set[str], value: Any) -> None:
    name = str(value or "").strip()
    if name and name not in seen:
        seen.add(name)
        aliases.append(name)


def _role_aliases(meta: dict[str, Any], role: str) -> list[str]:
    """Return ordered, de-duplicated aliases for a role (meta → scene → built-in)."""
    aliases: list[str] = []
    seen: set[str] = set()

    if role == "user_id":
        for item in (meta.get("column_aliases") or {}).get("user_id_columns") or []:
            _add_alias(aliases, seen, item)
        for item in _scene_config().get("user_id_aliases") or []:
            _add_alias(aliases, seen, item)
        for item in _DEFAULT_USER_ID_ALIASES:
            _add_alias(aliases, seen, item)
    elif role == "label":
        _add_alias(aliases, seen, "label")
        scene = _scene_config()
        for item in ((scene.get("sampling") or {}).get("prelabeled") or {}).get("label_column_aliases") or []:
            _add_alias(aliases, seen, item)
    elif role == "age":
        for item in _AGE_ALIASES:
            _add_alias(aliases, seen, item)
    elif role == "gender":
        for item in _GENDER_ALIASES:
            _add_alias(aliases, seen, item)
    return aliases


def _user_id_aliases(meta: dict[str, Any]) -> list[str]:
    return _role_aliases(meta, "user_id")


def _columns(meta: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in meta.get("tables") or []:
        if not isinstance(table, dict) or not table.get("name"):
            continue
        result[str(table["name"]).strip()] = [
            col for col in (table.get("columns") or []) if isinstance(col, dict) and col.get("name")
        ]
    return result


def _column_alias(names: set[str], aliases: tuple[str, ...] | list[str]) -> str | None:
    lowered = {name.lower(): name for name in names}
    for alias in aliases:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    return None


def build_skeleton(meta: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    columns_by_table = _columns(meta)
    scene = _scene_config()

    projection_tables = [item for item in (stats.get("projection_tables") or []) if isinstance(item, dict)]
    source_tables: list[dict[str, Any]] = []
    user_table: str | None = None

    for item in projection_tables:
        table_name = str(item.get("table") or "").strip()
        if not table_name:
            continue
        table_type = str(item.get("type") or "").strip()
        source_tables.append(
            {
                "table_name": table_name,
                "table_type": table_type,
                "user_key": str(item.get("user_key") or "").strip() or None,
            }
        )
        if table_type == "user_table" and user_table is None:
            user_table = table_name

    if user_table is None:
        user_table = next(iter(columns_by_table), None)

    user_columns = columns_by_table.get(user_table or "", [])
    column_names = {str(col.get("name") or "").strip() for col in user_columns}

    user_id = _column_alias(column_names, _user_id_aliases(meta)) if column_names else None
    label = _column_alias(column_names, _role_aliases(meta, "label")) if column_names else None
    age = _column_alias(column_names, _role_aliases(meta, "age")) if column_names else None
    gender = _column_alias(column_names, _role_aliases(meta, "gender")) if column_names else None

    for table in source_tables:
        if table["table_name"] == user_table and not table.get("user_key"):
            table["user_key"] = user_id

    return {
        "scene_id": stats.get("scene_id") or str(scene.get("scene_id") or "").strip(),
        "source_database": stats.get("source_database"),
        "output_database": stats.get("output_database"),
        "user_table": user_table,
        "user_id": user_id,
        "label": label,
        "age": age,
        "gender": gender,
        "entity_key": None,
        "user_id_columns": _user_id_aliases(meta),
        "source_tables": source_tables,
        "key_validation": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the schema_resolution.json skeleton for step2_0_0")
    parser.add_argument("--meta", type=Path, required=True, help="path to step1_output_meta.json")
    parser.add_argument("--stats", type=Path, required=True, help="path to step1_sample_stats.json")
    parser.add_argument("--schema", type=Path, required=True, help="path to schema_resolution.json")
    args = parser.parse_args()

    meta = _read_json(args.meta)
    stats = _read_json(args.stats)
    schema = build_skeleton(meta, stats)

    args.schema.parent.mkdir(parents=True, exist_ok=True)
    args.schema.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tables = len(schema.get("source_tables") or [])
    todo_roles = [
        role
        for role in ("user_table", "user_id", "label", "age", "gender")
        if not schema.get(role)
    ]
    print(
        f"已写出 {args.schema}：tables={tables} "
        f"user_table={schema.get('user_table')} user_id={schema.get('user_id')} label={schema.get('label')}"
    )
    if not schema.get("user_table") or not schema.get("user_id") or not schema.get("label"):
        raise SystemExit(
            "骨架缺少必需角色"
            f"（未解析：{', '.join(todo_roles)}）；请检查 step1_output_meta.json 的别名"
        )


if __name__ == "__main__":
    main()
