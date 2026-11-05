#!/usr/bin/env python3
"""Driver for step2_0 (source-data profiling).

The profiling step is four sub-stages. This driver runs all the local stages in the
right order so the Agent does not have to remember the sequence, and writes a run
manifest naming the SQL files to submit and where to drop each collect payload.
The single semantic task (deciding ``constant:TODO`` rows) stays with the Agent and is
surfaced explicitly between ``render`` and ``gate``.

  * ``prepare``      -- ``step2_0_0_generate_schema.py`` build the skeleton, then
    ``step2_0_1_purge_ghost.py gen`` write the describe SQL. Writes ``step2_0_run.json``
    (stage ``0_1a_describe``).
  * ``purge``        -- ``step2_0_1_purge_ghost.py purge`` strip ghost columns, then
    ``step2_0_1_expand_sql.py`` write both profile SQL. Writes stage ``0_1b_profile``.
  * ``render``       -- ``step2_0_1_collect_profile.py`` -> ``step2_0_2_generate_schema.py``
    -> ``step2_0_2_render_md.py``; prints the ``constant:TODO`` rows awaiting a decision.
  * ``set-constant`` -- ``step2_0_2_set_constant.py`` flip one ``constant:TODO`` row.
  * ``gate``         -- ``step2_0_3_verify_gate.py`` (re-render diff + semantic-constant checks).

Every sub-stage algorithm stays in its own script; the driver only orders them.
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

from step2_driver_common import run_script, write_manifest  # noqa: E402

MANIFEST = "step2_0_run.json"
SCHEMA = "schema_resolution.json"
DUMP = "step2_0_profile_dump.json"
MD = "step2_0_profile_report.md"
SEMANTIC_TODO = "step2_0_semantic_todo.json"
LIST_FIELDS = "list_fields.txt"
TABLE_PROFILE_SQL = "step2_0_1_table_profile_expanded.sql"
COLUMN_PROFILE_SQL = "step2_0_1_column_profile_expanded.sql"
RAW_TABLE = "raw_table_profile.json"
RAW_COLUMN = "raw_column_profile.json"
DESCRIBE_SQL = "step2_0_1_describe_columns_expanded.sql"
RAW_DESCRIBE = "raw_describe_columns.json"


def _run(script: str, argv: list[str]) -> None:
    code = run_script(SCRIPTS_DIR / script, argv)
    if code != 0:
        raise SystemExit(code)


def _ghost_audit(schema_path: Path) -> dict[str, Any]:
    """Read ghost_columns audit; always expose ``ghost_columns`` (list, possibly empty)."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"ghost_columns": []}
    if not isinstance(schema, dict):
        return {"ghost_columns": []}
    ghost = schema.get("ghost_columns")
    if not isinstance(ghost, list):
        ghost = []
    audit: dict[str, Any] = {"ghost_columns": ghost}
    for key in ("ghost_columns_purged", "ghost_column_count"):
        if key in schema:
            audit[key] = schema[key]
    return audit


def _prepare(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    _run(
        "step2_0_0_generate_schema.py",
        ["--meta", str(args.meta), "--stats", str(args.stats), "--schema", str(args.schema)],
    )
    _run(
        "step2_0_1_purge_ghost.py",
        ["gen", "--schema", str(args.schema), "--out-dir", str(out_dir)],
    )

    manifest = {
        "step": "step2_0",
        "stage": "0_1a_describe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": [
            {
                "name": "step2_0_describe_columns",
                "command_file": DESCRIBE_SQL,
                "collect_as": RAW_DESCRIBE,
            },
        ],
        "after_collect": (
            f"python skill/feature-engineer/scripts/step2_0_driver.py purge "
            f"--describe {RAW_DESCRIBE} --meta {args.meta} "
            f"--schema {args.schema} --out-dir {out_dir}"
        ),
    }
    write_manifest(out_dir / MANIFEST, manifest)


def _purge(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    argv_purge: list[str] = [
        "purge",
        "--meta", str(args.meta),
        "--schema", str(args.schema),
        "--out-dir", str(out_dir),
    ]
    for describe in args.describe:
        argv_purge += ["--describe", str(describe)]
    _run("step2_0_1_purge_ghost.py", argv_purge)

    _run(
        "step2_0_1_expand_sql.py",
        ["--schema", str(args.schema), "--meta", str(args.meta), "--out-dir", str(out_dir)],
    )

    manifest = {
        "step": "step2_0",
        "stage": "0_1b_profile",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": [
            {"name": "step2_0_table_profile", "command_file": TABLE_PROFILE_SQL, "collect_as": RAW_TABLE},
            {"name": "step2_0_column_profile", "command_file": COLUMN_PROFILE_SQL, "collect_as": RAW_COLUMN},
        ],
        "after_collect": (
            f"python skill/feature-engineer/scripts/step2_0_driver.py render "
            f"--raw {RAW_TABLE} --raw {RAW_COLUMN} --meta {args.meta} "
            f"--schema {args.schema} --dump {DUMP} --out-dir {out_dir}"
        ),
        **_ghost_audit(args.schema),
    }
    write_manifest(out_dir / MANIFEST, manifest)


def _render(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    argv_collect: list[str] = ["--out", str(args.dump)]
    for raw in args.raw:
        argv_collect += ["--raw", str(raw)]
    _run("step2_0_1_collect_profile.py", argv_collect)

    _run(
        "step2_0_2_generate_schema.py",
        ["--schema", str(args.schema), "--dump", str(args.dump)],
    )
    _run(
        "step2_0_2_render_md.py",
        [
            "--dump", str(args.dump),
            "--meta", str(args.meta),
            "--schema", str(args.schema),
            "--out", str(out_dir / MD),
            "--semantic-todo", str(out_dir / SEMANTIC_TODO),
            "--list-fields", str(out_dir / LIST_FIELDS),
        ],
    )

    todo_path = out_dir / SEMANTIC_TODO
    todo_count = 0
    if todo_path.is_file():
        try:
            todo_count = len((json.loads(todo_path.read_text(encoding="utf-8")).get("candidates") or []))
        except (OSError, json.JSONDecodeError):
            todo_count = 0

    manifest = {
        "step": "step2_0",
        "stage": "0_2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "semantic_todo_count": todo_count,
        "semantic_todo_file": SEMANTIC_TODO,
        "next": (
            "逐个判定 semantic_todo_file 中列出的 constant:TODO 行（就地改为 "
            "constant:semantic 或 constant:false，只改分布分析列），然后运行 gate。"
            if todo_count
            else "无语义常量候选，直接运行 gate。"
        ),
        "gate": (
            f"python skill/feature-engineer/scripts/step2_0_driver.py gate "
            f"--md {out_dir / MD} --dump {args.dump} --meta {args.meta} "
            f"--schema {args.schema} --list-fields {out_dir / LIST_FIELDS}"
        ),
    }
    write_manifest(out_dir / MANIFEST, manifest)


def _set_constant(args: argparse.Namespace) -> None:
    _run(
        "step2_0_2_set_constant.py",
        [
            "--md", str(args.md),
            "--table", args.table,
            "--field", args.field,
            "--value", args.value,
        ],
    )


def _gate(args: argparse.Namespace) -> None:
    argv = [
        "--md", str(args.md),
        "--dump", str(args.dump),
        "--meta", str(args.meta),
        "--schema", str(args.schema),
        "--list-fields", str(args.list_fields),
    ]
    if args.marker is not None:
        argv += ["--marker", str(args.marker)]
    _run("step2_0_3_verify_gate.py", argv)


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_0 driver (source-data profiling)")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="build skeleton + generate describe SQL")
    prepare.add_argument("--meta", type=Path, required=True)
    prepare.add_argument("--stats", type=Path, required=True)
    prepare.add_argument("--schema", type=Path, default=Path(SCHEMA))
    prepare.add_argument("--out-dir", type=Path, default=Path("."))
    prepare.set_defaults(func=_prepare)

    purge = sub.add_parser("purge", help="purge ghost columns then expand the two profile SQL")
    purge.add_argument("--describe", type=Path, action="append", required=True, help="describe collect payload; repeatable")
    purge.add_argument("--meta", type=Path, required=True)
    purge.add_argument("--schema", type=Path, default=Path(SCHEMA))
    purge.add_argument("--out-dir", type=Path, default=Path("."))
    purge.set_defaults(func=_purge)

    render = sub.add_parser("render", help="collect profile -> complete schema -> render md")
    render.add_argument("--raw", type=Path, action="append", required=True, help="collect payload; repeatable")
    render.add_argument("--meta", type=Path, required=True)
    render.add_argument("--schema", type=Path, default=Path(SCHEMA))
    render.add_argument("--dump", type=Path, default=Path(DUMP))
    render.add_argument("--out-dir", type=Path, default=Path("."))
    render.set_defaults(func=_render)

    set_constant = sub.add_parser("set-constant", help="set one constant:TODO to semantic/false")
    set_constant.add_argument("--md", type=Path, default=Path(MD))
    set_constant.add_argument("--table", required=True)
    set_constant.add_argument("--field", required=True)
    set_constant.add_argument("--value", required=True, choices=("semantic", "false"))
    set_constant.set_defaults(func=_set_constant)

    gate = sub.add_parser("gate", help="run the step2_0 gate")
    gate.add_argument("--md", type=Path, default=Path(MD))
    gate.add_argument("--dump", type=Path, default=Path(DUMP))
    gate.add_argument("--meta", type=Path, required=True)
    gate.add_argument("--schema", type=Path, default=Path(SCHEMA))
    gate.add_argument("--list-fields", type=Path, default=Path(LIST_FIELDS))
    gate.add_argument("--marker", type=Path, default=Path("gate_data_verified.txt"))
    gate.set_defaults(func=_gate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
