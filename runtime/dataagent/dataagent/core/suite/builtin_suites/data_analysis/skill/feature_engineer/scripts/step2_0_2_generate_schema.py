#!/usr/bin/env python3
"""Complete ``schema_resolution.json`` for step2_0_2.

Consumes ``step2_0_profile_dump.json`` (written by ``step2_0_1_collect_profile.py``)
and completes the skeleton written by ``step2_0_0_generate_schema.py``:

  * per-table ``key_validation`` — rows, unique users, duplication factor, classification
  * ``entity_key`` (when the scene names a key column) and per-table ``user_key`` backfill
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
SCENE_YAML = SCRIPTS_DIR.parent / "references" / "scene.yaml"


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


def _table_profile_map(dump: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("table_name") or "").strip(): row
        for row in (dump.get("table_profile") or [])
        if isinstance(row, dict) and row.get("table_name")
    }


def complete_schema(schema: dict[str, Any], dump: dict[str, Any]) -> dict[str, Any]:
    table_profile = _table_profile_map(dump)

    key_validation: dict[str, Any] = {}
    for table in schema.get("source_tables") or []:
        name = str(table.get("table_name") or "").strip()
        profile = table_profile.get(name, {})
        total = profile.get("total_rows")
        unique = profile.get("unique_user_id")
        classification = profile.get("classification")
        duplication: float | None = None
        if isinstance(total, int) and total == 0:
            # 0 行空表对用户键无意义：降级为维度表，键唯一性校验视为通过
            # （否则会被分为 1:1 用户表且 max_duplication_factor 缺失，
            #  step2_1 prepare 被阻塞）。
            classification = "维度表"
            duplication = 1.0
        elif isinstance(total, int) and isinstance(unique, int) and unique > 0:
            duplication = round(total / unique, 6)
        key_validation[name] = {
            "table_type": table.get("table_type"),
            "classification": classification,
            "total_rows": total,
            "unique_user_id": unique,
            "max_duplication_factor": duplication,
        }
    schema["key_validation"] = key_validation

    # Infer a single entity key column when the scene names one.
    if not schema.get("entity_key"):
        scene = _scene_config()
        key_column = str(((scene.get("entity") or {}).get("key_column")) or "").strip()
        if key_column:
            schema["entity_key"] = key_column

    for table in schema.get("source_tables") or []:
        if table.get("table_type") == "user_table" and not table.get("user_key"):
            table["user_key"] = schema.get("user_id")

    return schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Complete schema_resolution.json for step2_0_2")
    parser.add_argument("--schema", type=Path, required=True, help="path to schema_resolution.json")
    parser.add_argument("--dump", type=Path, required=True, help="path to step2_0_profile_dump.json")
    args = parser.parse_args()

    if not args.schema.is_file():
        raise SystemExit(f"未找到骨架文件 {args.schema}，请先运行 step2_0_0_generate_schema.py")
    schema = complete_schema(_read_json(args.schema), _read_json(args.dump))

    args.schema.parent.mkdir(parents=True, exist_ok=True)
    args.schema.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tables = len(schema.get("source_tables") or [])
    print(
        f"已写出 {args.schema}：tables={tables} "
        f"user_table={schema.get('user_table')} user_id={schema.get('user_id')} label={schema.get('label')}"
    )


if __name__ == "__main__":
    main()
