#!/usr/bin/env python3
"""Driver for step2_1 (1:1 merge).

Collapses "expand join SQL, expand the validation gate SELECT, remember which file is
which, hand-replace the warehouse catalog placeholder, then run the gate" into two
commands that the run manifest spells out:

  * ``prepare`` -- expand ``step2_1_join_expanded.sql`` + ``step2_1_validation_expanded.sql``
    (both already placeholder-bound) and write ``step2_1_merge_plan.json`` + ``step2_1_run.json``.
  * ``gate``    -- run ``step2_1_verify_gate.py`` (the gate algorithm is unchanged).

The SQL expansion algorithm lives in ``step2_1_expand_sql.py``; this driver only
orchestrates it and binds the warehouse placeholders.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_1_expand_sql import _assert_expanded, _non_comment_text, expand  # noqa: E402
from step2_driver_common import (  # noqa: E402
    bind_placeholders,
    load_data_source,
    run_script,
    unbound_placeholders,
    write_manifest,
)

MANIFEST = "step2_1_run.json"
MERGE_SQL = "step2_1_join_expanded.sql"
VALIDATION_SQL = "step2_1_validation_expanded.sql"
MERGE_PLAN = "step2_1_merge_plan.json"
RAW_VALIDATION = "raw_step2_1_validation.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _prepare(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    meta = _read_json(args.meta)
    merge_sql, validation_sql, plan, joined = expand(schema, meta)

    data_source = load_data_source(out_dir)
    merge_sql = bind_placeholders(merge_sql, data_source)
    validation_sql = bind_placeholders(validation_sql, data_source)

    out_merge = out_dir / MERGE_SQL
    _assert_expanded(out_merge, merge_sql)
    out_merge.write_text(_non_comment_text(merge_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_merge}")

    out_validation = out_dir / VALIDATION_SQL
    _assert_expanded(out_validation, validation_sql)
    out_validation.write_text(_non_comment_text(validation_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_validation}")

    leftovers = unbound_placeholders(validation_sql)
    if leftovers:
        print(
            f"警告：{VALIDATION_SQL} 仍有未绑定占位符 {leftovers}；"
            "工作区缺 data_source.json，已回退套件 references/data_source.json；"
            "仍有未绑定占位符，提交前需按 references/data_source.md 手工替换。",
            file=sys.stderr,
        )

    out_plan = out_dir / MERGE_PLAN
    out_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写出 {out_plan}：1:1 右表 {len(joined)} 张（{plan['join_nesting']}），预期列数 {plan['expected_column_count']}")
    if joined:
        print("  右表（按 JOIN 顺序）：" + ", ".join(joined))

    manifest = {
        "step": "step2_1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sql_binding": {
            "source": "data_source.json" if data_source else "(缺失)",
            "catalog_columns": data_source.get("catalog_columns"),
        },
        "jobs": [
            {"name": "step2_1_join", "command_file": MERGE_SQL, "collect_as": None},
            {"name": "step2_1_validation", "command_file": VALIDATION_SQL, "collect_as": RAW_VALIDATION},
        ],
        "gate": (
            f"python skill/feature-engineer/scripts/step2_driver_common.py check-collect "
            f"--manifest {MANIFEST} --expect-rows --out-dir {out_dir} && "
            f"python skill/feature-engineer/scripts/step2_1_driver.py gate "
            f"--schema {args.schema} --raw-validation {RAW_VALIDATION} --plan {MERGE_PLAN}"
        ),
    }
    write_manifest(out_dir / MANIFEST, manifest)


def _gate(args: argparse.Namespace) -> None:
    """Delegate to ``step2_1_verify_gate.py`` so the gate algorithm stays single-sourced."""
    argv = ["--schema", str(args.schema)]
    for raw in args.raw_validation:
        argv += ["--raw-validation", str(raw)]
    if args.plan is not None:
        argv += ["--plan", str(args.plan)]
    raise SystemExit(run_script(SCRIPTS_DIR / "step2_1_verify_gate.py", argv))


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_1 driver (1:1 merge)")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="expand + bind the join and validation SQL")
    prepare.add_argument("--schema", type=Path, required=True, help="path to schema_resolution.json")
    prepare.add_argument("--meta", type=Path, required=True, help="path to step1_output_meta.json")
    prepare.add_argument("--out-dir", type=Path, default=Path("."), help="directory for expanded SQL")
    prepare.set_defaults(func=_prepare)

    gate = sub.add_parser("gate", help="run the step2_1 gate")
    gate.add_argument("--schema", type=Path, required=True)
    gate.add_argument("--raw-validation", type=Path, action="append", required=True)
    gate.add_argument("--plan", type=Path, default=None)
    gate.set_defaults(func=_gate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
