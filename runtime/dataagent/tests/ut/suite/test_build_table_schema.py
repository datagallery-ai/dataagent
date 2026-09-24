from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "dataagent"
    / "core"
    / "suite"
    / "builtin_suites"
    / "data_analysis"
    / "skill"
    / "user_sampling"
    / "scripts"
    / "build_table_schema.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("build_table_schema", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_schema_from_download_dump(tmp_path: Path) -> None:
    mod = _load_module()
    dump = {
        "entities": [
            {
                "api_name": "user_info",
                "description": "用户主表",
                "properties": [
                    {
                        "column_name": "usid",
                        "data_type": "bigint",
                        "description": "用户ID（含枚举 A/B）",
                        "isPrimaryKey": True,
                    },
                    {"column_name": "city", "data_type": "string", "description": None},
                ],
            },
            {
                "api_name": "game_info",
                "description": "实体维表",
                "properties": [
                    {"column_name": "game_id", "data_type": "int", "description": "游戏ID"},
                ],
            },
        ],
        "dataAccessPlan": {
            "joinPaths": [
                {
                    "left": "db.user_info.usid",
                    "right": "db.game_info.usid",
                    "on": "user_info.usid = game_info.usid",
                }
            ]
        },
    }
    dump_path = tmp_path / "semantic_download.txt"
    dump_path.write_text(__import__("json").dumps(dump, ensure_ascii=False), encoding="utf-8")
    inventory_path = tmp_path / "step1_source_tables.json"
    inventory_path.write_text('["user_info", "game_info"]\n', encoding="utf-8")
    out = tmp_path / "step1_0_table_schema.json"

    schema = mod.build_schema(
        dump,
        "src_db",
        mod._load_inventory(inventory_path, None),
        {},
        mod.load_scene_config(),
    )
    out.write_text(__import__("json").dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

    assert schema["source_database"] == "src_db"
    assert schema["table_names"] == ["user_info", "game_info"]
    user_cols = {col["name"]: col for col in schema["tables"][0]["columns"]}
    assert user_cols["usid"]["description"] == "用户ID（含枚举 A/B）"
    assert user_cols["usid"]["valueType"] == "Int64"
    assert user_cols["city"]["description"] is None
    assert schema["join_hints"] == [
        {"left": "user_info.usid", "right": "game_info.usid", "note": "user_info.usid = game_info.usid"}
    ]
    assert set(schema["role_candidates"]) == {
        "user_table",
        "label_event",
        "activity_event",
        "conversion_event",
        "game_dim",
    }


def test_missing_columns_uses_catalog_file() -> None:
    mod = _load_module()
    dump = {"entities": [], "dataAccessPlan": {"tables": [], "joinPaths": []}}
    extra = {
        "only_in_warehouse": [
            {"name": "id", "valueType": "Int64", "description": None, "isPrimaryKey": False},
        ]
    }
    schema = mod.build_schema(dump, "src_db", ["only_in_warehouse"], extra, mod.load_scene_config())
    assert schema["tables"][0]["columns"][0]["name"] == "id"
    assert mod._missing_columns(schema["tables"]) == []


def test_exit_code_2_when_columns_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import sys

    mod = _load_module()
    dump_path = tmp_path / "dump.json"
    dump_path.write_text(json.dumps({"entities": [], "dataAccessPlan": {}}), encoding="utf-8")
    inventory = tmp_path / "tables.json"
    inventory.write_text('["missing_table"]\n', encoding="utf-8")
    out = tmp_path / "schema.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_table_schema.py",
            "--dump",
            str(dump_path),
            "--source-database",
            "src",
            "--inventory-file",
            str(inventory),
            "--out",
            str(out),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        mod.main()
    assert caught.value.code == 2
    assert out.is_file()
