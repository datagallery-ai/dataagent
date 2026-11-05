#!/usr/bin/env python3
"""Driver for step2_3 (user-grain cleaning of the aggregated wide table).

The cleaning has a two-pass expand (the EXCEPT list needs the cleaning report first).
This driver hides that shape behind one chain of commands and folds the chain into
``step2_3_run.json`` with ``after_collect``, so the Agent never picks the next command:

  * ``prepare``  -- pass 1: expand report SQL (auto-batch when over size limit) + bind +
    combined SELECT for collect. Writes ``step2_3_run.json``; next is ``finalize``.
  * ``finalize`` -- pass 2: expand + bind ``step2_3_feature_cleaning_expanded.sql`` and
    ``step2_3_validation_expanded.sql`` (EXCEPT from the collected report) plus
    ``step2_3_cleaning_plan.json``. Appends both jobs; next is ``gate``.
  * ``gate``     -- ``step2_3_verify_gate.py``. On success clears ``after_collect`` so the chain stops.
  * ``rescue-collect`` -- diagnose a failed report collect; optional loose main-table SELECT.

The expansion algorithm lives in ``step2_3_expand_sql.py``; the driver only orchestrates
and binds the warehouse catalog placeholder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_3_expand_sql import (  # noqa: E402
    _assert_expanded,
    _non_comment_text,
    _read_json,
    _rows,
    _wide_columns,
    build_report_batches,
    expand,
)
from step2_driver_common import (  # noqa: E402
    bind_placeholders,
    check_collect,
    load_data_source,
    run_script,
    unbound_placeholders,
    write_manifest,
)

MANIFEST = "step2_3_run.json"
REPORT_SQL = "step2_3_cleaning_report_expanded.sql"
REPORT_COMBINE_SQL = "step2_3_cleaning_report_combine.sql"
COMBINED_SELECT_SQL = "step2_3_cleaning_report_combined_select.sql"
REPORT_PAYLOADS_LIST = "cleaning_report_payloads.txt"
CLEANING_SQL = "step2_3_feature_cleaning_expanded.sql"
VALIDATION_SQL = "step2_3_validation_expanded.sql"
PLAN = "step2_3_cleaning_plan.json"
RAW_CLEANING_REPORT = "raw_cleaning_report.json"
RAW_WIDE_STATS = "raw_wide_stats.json"

_FROM_REPORT_RE = re.compile(
    r"FROM\s+([`\"\w.]+)\.step2_3_cleaning_report\b",
    re.IGNORECASE,
)

_SCRIPT = "python skill/feature-engineer/scripts/step2_3_driver.py"
_CHECK = "python skill/feature-engineer/scripts/step2_driver_common.py check-collect"


def _read_columns(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"未找到 step2_2_wide_aggregated 列清单 payload 文件：{path}")
        rows.extend(_rows(path))
    return rows


def _warn_unbound(path: str, text: str) -> None:
    leftovers = unbound_placeholders(text)
    if leftovers:
        print(
            f"警告：{path} 仍有未绑定占位符 {leftovers}；"
            "工作区缺 data_source.json，已回退套件 references/data_source.json；"
            "仍有未绑定占位符，提交前需按 references/data_source.md 手工替换。",
            file=sys.stderr,
        )


def _write_sql(path: Path, sql: str) -> None:
    _assert_expanded(path, sql)
    path.write_text(_non_comment_text(sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {path}")


def _write_report_payloads(out_dir: Path, names: list[str]) -> None:
    path = out_dir / REPORT_PAYLOADS_LIST
    path.write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"已写出 {path}：{len(names)} 个 report payload")


def _read_report_payloads(out_dir: Path) -> list[str]:
    path = out_dir / REPORT_PAYLOADS_LIST
    if not path.is_file():
        return [RAW_CLEANING_REPORT]
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return names or [RAW_CLEANING_REPORT]


def _collect_guard(next_command: str, out_dir: Path) -> str:
    return f"{_CHECK} --manifest {MANIFEST} --expect-rows --out-dir {out_dir} && {next_command}"


def _combined_select_sql(output_database: str, column_count: int) -> str:
    limit = max(column_count + 1, 1)
    return (
        f"SELECT *\n"
        f"FROM {output_database}.step2_3_cleaning_report\n"
        f"ORDER BY feature\n"
        f"LIMIT {limit};\n"
    )


def _prepare(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    columns = _read_columns(args.raw_columns)
    column_count = len(_wide_columns(columns))
    data_source = load_data_source(out_dir)
    output_database = str(schema.get("output_database") or "{{output_database}}").strip()

    batches, combine = build_report_batches(columns, schema)
    jobs: list[dict] = []
    batched = combine is not None
    if batched:
        for index, batch_sql in enumerate(batches):
            command_file = f"step2_3_cleaning_report_expanded_batch_{index}.sql"
            name = f"step2_3_cleaning_report_batch_{index}"
            sql = bind_placeholders(batch_sql, data_source)
            _write_sql(out_dir / command_file, sql)
            _warn_unbound(command_file, sql)
            jobs.append({"name": name, "command_file": command_file, "collect_as": None})
    else:
        if len(batches) != 1:
            raise SystemExit(f"未拆批时期望恰好 1 条 SQL，实际 {len(batches)}")
        sql = bind_placeholders(batches[0], data_source)
        _write_sql(out_dir / REPORT_SQL, sql)
        _warn_unbound(REPORT_SQL, sql)
        jobs.append(
            {"name": "step2_3_cleaning_report", "command_file": REPORT_SQL, "collect_as": None}
        )

    combine_job = None
    if combine is not None:
        combine_sql = bind_placeholders(combine, data_source)
        _write_sql(out_dir / REPORT_COMBINE_SQL, combine_sql)
        _warn_unbound(REPORT_COMBINE_SQL, combine_sql)
        combine_job = {
            "name": "step2_3_cleaning_report_combine",
            "command_file": REPORT_COMBINE_SQL,
            "collect_as": None,
        }

    select_sql = bind_placeholders(
        _combined_select_sql(output_database, column_count),
        data_source,
    )
    select_path = out_dir / COMBINED_SELECT_SQL
    _write_sql(select_path, select_sql)
    _warn_unbound(COMBINED_SELECT_SQL, select_sql)

    _write_report_payloads(out_dir, [RAW_CLEANING_REPORT])

    raw_columns_args = " ".join(f"--raw-columns {p}" for p in args.raw_columns)
    finalize_cmd = (
        f"{_SCRIPT} finalize --schema {args.schema} {raw_columns_args} "
        f"--raw-report {RAW_CLEANING_REPORT} --out-dir {out_dir}"
    )
    manifest: dict = {
        "step": "step2_3",
        "stage": "prepare",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "column_count": column_count,
        "batched": batched,
        "jobs": jobs,
        "collect": {
            "name": "step2_3_cleaning_report_combined_select",
            "command_file": COMBINED_SELECT_SQL,
            "collect_as": RAW_CLEANING_REPORT,
        },
        "report_payloads": [RAW_CLEANING_REPORT],
        "save_note": (
            "collect_job 后必须用 step2_driver_common.py save-collect "
            f"--out {RAW_CLEANING_REPORT} [--extract-rows] 落盘；禁止手工复制 payload。"
        ),
        "after_collect": _collect_guard(finalize_cmd, out_dir),
    }
    if combine_job is not None:
        manifest["combine"] = combine_job
    write_manifest(out_dir / MANIFEST, manifest)


def _dedupe_report_rows(report_rows: list[dict]) -> list[dict]:
    """Drop exact duplicate rows (full-field hash); warn when duplicates appear."""
    seen: set[str] = set()
    deduped: list[dict] = []
    duplicates = 0
    for row in report_rows:
        digest = hashlib.sha256(
            json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        deduped.append(row)
    if duplicates:
        print(
            f"警告：raw cleaning report 检测到 {duplicates} 条全字段重复行，已去重"
            f"（保留 {len(deduped)} / 原始 {len(report_rows)}）",
            file=sys.stderr,
        )
    return deduped


def _finalize(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # after_collect already chains check-collect; only re-guard while still on prepare stage
    # (re-running finalize after the manifest flipped to finalize would look for raw_wide_stats).
    manifest_path = out_dir / MANIFEST
    if manifest_path.is_file():
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and current.get("stage") == "prepare":
            code = check_collect(manifest_path, expect_rows=True, out_dir=out_dir)
            if code:
                raise SystemExit(code)

    schema = _read_json(args.schema)
    columns = _read_columns(args.raw_columns)
    report_paths = list(args.raw_report)
    report_rows: list[dict] = []
    for path in report_paths:
        if not path.is_file():
            raise SystemExit(f"未找到 cleaning_report payload 文件：{path}")
        report_rows.extend(_rows(path))
    report_rows = _dedupe_report_rows(report_rows)
    data_source = load_data_source(out_dir)

    report_sql, cleaning_sql, validation_sql, plan = expand(columns, report_rows, schema)
    if plan is None or cleaning_sql is None or validation_sql is None:
        raise SystemExit("finalize 需要 --raw-report（清洗画像结果），否则 EXCEPT 无法确定")

    report_sql = bind_placeholders(report_sql, data_source)
    cleaning_sql = bind_placeholders(cleaning_sql, data_source)
    validation_sql = bind_placeholders(validation_sql, data_source)

    out_report = out_dir / REPORT_SQL
    _write_sql(out_report, report_sql)

    out_cleaning = out_dir / CLEANING_SQL
    _write_sql(out_cleaning, cleaning_sql)

    out_validation = out_dir / VALIDATION_SQL
    _write_sql(out_validation, validation_sql)
    _warn_unbound(VALIDATION_SQL, validation_sql)

    out_plan = out_dir / PLAN
    out_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写出 {out_plan}：EXCEPT {len(plan['except_columns'])} 列")
    if plan["drop_not_in_wide"]:
        raise SystemExit(
            "以下 DROP 字段不是 step2_2_wide_aggregated 的列，无法清洗：\n- "
            + "\n- ".join(plan["drop_not_in_wide"])
            + f"\n诊断清单见 {out_plan}。请检查 step2_3_cleaning_report.sql 是否与宽表列一致。"
        )

    raw_report_args = " ".join(f"--raw-report {p}" for p in report_paths) or f"--raw-report {RAW_CLEANING_REPORT}"
    gate_cmd = (
        f"{_SCRIPT} gate --schema {args.schema} {raw_report_args} --raw-wide-stats {RAW_WIDE_STATS}"
    )
    guarded = _collect_guard(gate_cmd, out_dir)
    manifest = {
        "step": "step2_3",
        "stage": "finalize",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "except_columns": plan["except_columns"],
        "jobs": [
            {"name": "step2_3_feature_cleaning", "command_file": CLEANING_SQL, "collect_as": None},
            {"name": "step2_3_validation", "command_file": VALIDATION_SQL, "collect_as": RAW_WIDE_STATS},
        ],
        "gate": guarded,
        "after_collect": guarded,
        "save_note": (
            "validation collect_job 后必须用 step2_driver_common.py save-collect "
            f"--out {RAW_WIDE_STATS} 落盘；禁止手工复制 payload。"
        ),
    }
    write_manifest(out_dir / MANIFEST, manifest)


def _clear_after_collect(out_dir: Path) -> None:
    path = out_dir / MANIFEST
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or "after_collect" not in payload:
        return
    payload.pop("after_collect", None)
    payload["stage"] = "gate"
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    write_manifest(path, payload)


def _gate(args: argparse.Namespace) -> None:
    argv = ["--schema", str(args.schema)]
    for raw in args.raw_report:
        argv += ["--raw-report", str(raw)]
    for raw in args.raw_wide_stats:
        argv += ["--raw-wide-stats", str(raw)]
    if args.marker is not None:
        argv += ["--marker", str(args.marker)]
    if args.report_json is not None:
        argv += ["--report-json", str(args.report_json)]
    code = run_script(SCRIPTS_DIR / "step2_3_verify_gate.py", argv)
    if code == 0:
        _clear_after_collect(Path("."))
    raise SystemExit(code)


def _infer_output_database(out_dir: Path, schema_path: Path | None) -> str:
    """Infer ``output_database`` from existing combined SELECT, else ``--schema``."""
    select_path = out_dir / COMBINED_SELECT_SQL
    if select_path.is_file():
        text = select_path.read_text(encoding="utf-8")
        match = _FROM_REPORT_RE.search(text)
        if match:
            return match.group(1).strip("`\"")
    if schema_path is not None and schema_path.is_file():
        schema = _read_json(schema_path)
        return str(schema.get("output_database") or "{{output_database}}").strip()
    raise SystemExit(
        "--rescue-main-table 无法推断 output_database：请提供 --schema，"
        f"或先确保 {COMBINED_SELECT_SQL} 存在且含 FROM <db>.step2_3_cleaning_report"
    )


def _rescue_collect(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    print("=== step2_3 rescue-collect 诊断清单 ===")
    checks = [
        ("manifest", out_dir / MANIFEST),
        ("combined SELECT", out_dir / COMBINED_SELECT_SQL),
        ("combine SQL", out_dir / REPORT_COMBINE_SQL),
        ("raw cleaning report", out_dir / RAW_CLEANING_REPORT),
        ("report payloads list", out_dir / REPORT_PAYLOADS_LIST),
        ("save-collect 标记", out_dir / f"{RAW_CLEANING_REPORT}.via-save-collect"),
    ]
    for label, path in checks:
        status = "存在" if path.is_file() and path.stat().st_size > 0 else "缺失/空"
        print(f"- {label}: {path} → {status}")

    print("\n=== 重提 collect / save 命令序列 ===")
    print(f"1. 提交 warehouse job：{COMBINED_SELECT_SQL}")
    print("2. poll_job → collect_job")
    print(
        "3. python skill/feature-engineer/scripts/step2_driver_common.py save-collect "
        f"--source <tool-results/id.txt> --out {RAW_CLEANING_REPORT} --extract-rows"
    )
    print(
        f"   （大结果可用 --latest 代替 --source；落盘后应出现 {RAW_CLEANING_REPORT}.via-save-collect）"
    )
    print(
        f"4. {_CHECK} --manifest {MANIFEST} --expect-rows --out-dir {out_dir}"
    )
    print(
        f"5. 通过后继续 finalize（见 manifest.after_collect），或重跑：\n"
        f"   {_SCRIPT} finalize --schema <schema_resolution.json> "
        f"--raw-columns <...> --raw-report {RAW_CLEANING_REPORT} --out-dir {out_dir}"
    )

    print("\n=== check-collect 现状 ===")
    check_code = 1
    if (out_dir / MANIFEST).is_file():
        check_code = check_collect(out_dir / MANIFEST, expect_rows=True, out_dir=out_dir)
    else:
        print(f"跳过：缺少 {MANIFEST}")

    if args.rescue_main_table:
        select_path = out_dir / COMBINED_SELECT_SQL
        if select_path.is_file():
            text = select_path.read_text(encoding="utf-8")
            # Drop ORDER BY ... (to end of line) then force a loose LIMIT.
            text = re.sub(r"(?im)^\s*ORDER\s+BY\s+.+$", "", text)
            if re.search(r"(?i)\bLIMIT\b", text):
                text = re.sub(r"(?i)\bLIMIT\s+\d+", "LIMIT 100000", text)
            else:
                text = text.rstrip().rstrip(";") + "\nLIMIT 100000;\n"
            # Collapse leftover blank lines from ORDER BY removal.
            text = re.sub(r"\n{3,}", "\n\n", text)
        else:
            output_database = _infer_output_database(out_dir, args.schema)
            text = (
                f"SELECT *\nFROM {output_database}.step2_3_cleaning_report\nLIMIT 100000;\n"
            )
        data_source = load_data_source(out_dir)
        text = bind_placeholders(text, data_source)
        select_path.write_text(_non_comment_text(text).rstrip() + "\n", encoding="utf-8")
        print(
            f"\n已用宽松主表 SELECT（去 ORDER BY、LIMIT 100000）覆盖 {select_path}；"
            "请重新提交该文件并 save-collect。"
        )

    # Not ready → non-zero so `rescue-collect && finalize` chains do not proceed.
    return 0 if check_code == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_3 driver (user-grain cleaning)")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="pass 1: expand + bind the cleaning-report SQL")
    prepare.add_argument("--schema", type=Path, required=True)
    prepare.add_argument("--raw-columns", type=Path, action="append", required=True, help="wide-table column payload; repeatable")
    prepare.add_argument("--out-dir", type=Path, default=Path("."))
    prepare.set_defaults(func=_prepare)

    finalize = sub.add_parser("finalize", help="pass 2: expand + bind cleaning/validation SQL")
    finalize.add_argument("--schema", type=Path, required=True)
    finalize.add_argument("--raw-columns", type=Path, action="append", required=True)
    finalize.add_argument(
        "--raw-report",
        type=Path,
        action="append",
        required=True,
        help="cleaning-report payload; repeatable (duplicates across the pack are warned)",
    )
    finalize.add_argument("--out-dir", type=Path, default=Path("."))
    finalize.set_defaults(func=_finalize)

    gate = sub.add_parser("gate", help="run the step2_3 gate")
    gate.add_argument("--schema", type=Path, required=True)
    gate.add_argument("--raw-report", type=Path, action="append", required=True)
    gate.add_argument("--raw-wide-stats", type=Path, action="append", required=True)
    gate.add_argument("--marker", type=Path, default=Path("gate_data_verified_2_3.txt"))
    gate.add_argument("--report-json", type=Path, default=Path("step2_3_cleaning_report.json"))
    gate.set_defaults(func=_gate)

    rescue = sub.add_parser(
        "rescue-collect",
        help="diagnose a failed cleaning-report collect; optional loose main-table SELECT",
    )
    rescue.add_argument(
        "--schema",
        type=Path,
        required=False,
        help="schema_resolution.json (optional; used to infer output_database for --rescue-main-table)",
    )
    rescue.add_argument("--out-dir", type=Path, default=Path("."))
    rescue.add_argument(
        "--rescue-main-table",
        action="store_true",
        help="overwrite combined SELECT with a loose SELECT (no ORDER BY, LIMIT 100000)",
    )
    rescue.set_defaults(func=_rescue_collect)

    args = parser.parse_args()
    raise SystemExit(args.func(args) or 0)


if __name__ == "__main__":
    main()
