#!/usr/bin/env python3
"""Driver for step2_4 (user filtering) and the final CSV export.

step2_0~step2_3 all hide their SQL expansion behind a driver; step2_4 used to be the one
stage where the Agent hand-bound ``<age>`` / ``<gender>`` / ``<user_id>`` / ``<label>`` and
hand-transcribed the validation rows into ``step2_4_user_filter_report.json``. This driver
removes both:

  * ``prepare`` -- bind the two validation templates + the user-cleaning CREATE from
    ``schema_resolution.json`` and ``references/data_source.json``, write
    ``step2_4_run.json`` naming the three jobs in submit order.
  * ``gate``    -- read the two collect payloads, decide every gate rule, and write
    ``step2_4_user_filter_report.json`` plus ``gate_data_verified_2_4.txt``. The report is
    a script artifact, never a hand-copied table.
  * ``export-plan``  -- turn the collected row count into per-batch ``SELECT`` files and
    append them to the manifest, so the Agent never invents the OFFSET loop.
  * ``append-csv``   -- append collected batches into ``step2_4_wide_userfiltered.csv``,
    header on the first batch only.

The gate algorithm lives here (it is small and step2_4-specific); the SQL binding and
manifest helpers are shared with the other drivers.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_driver_common import (  # noqa: E402
    bind_placeholders,
    check_collect,
    load_data_source,
    unbound_placeholders,
    write_manifest,
)
from step2_3_expand_sql import (  # noqa: E402
    _extract_results,
    _load_payloads,
    _non_comment_text,
    _plain_records,
    _read_json,
)

MANIFEST = "step2_4_run.json"
CLEANING_TEMPLATE = SCRIPTS_DIR / "step2_4_user_cleaning.sql"
STATS_TEMPLATE = SCRIPTS_DIR / "step2_4_stats_validation.sql"
COLUMNS_TEMPLATE = SCRIPTS_DIR / "step2_4_columns_validation.sql"

COUNT_TEMPLATE = SCRIPTS_DIR / "step2_4_count.sql"

CLEANING_SQL = "step2_4_user_cleaning_expanded.sql"
STATS_SQL = "step2_4_stats_validation_expanded.sql"
COLUMNS_SQL = "step2_4_columns_validation_expanded.sql"
COUNT_SQL = "step2_4_count_expanded.sql"
RAW_STATS = "raw_step2_4_stats.json"
RAW_COLUMNS = "raw_step2_4_columns.json"
RAW_COUNT = "raw_step2_4_count.json"
RAW_EXPORT_COUNT = "raw_export_count.json"
REPORT_JSON = "step2_4_user_filter_report.json"
MARKER = "gate_data_verified_2_4.txt"
WIDE_CSV = "step2_4_wide_userfiltered.csv"
BATCH_FILE = "step2_4_export_batch_{index:04d}.sql"

# Batch SELECT template. ``LIMIT``/``OFFSET`` are formatted in, the role placeholders are
# bound from schema_resolution.json exactly like the other step2_4 templates.
BATCH_SQL = (
    "-- step2_4 CSV 导出批次（单条 SELECT，由 step2_4_driver.py export-plan 生成）\n"
    "-- 本文件禁止手工改写；OFFSET 由驱动按 total_rows / batch_size 递增。\n"
    "SELECT *\n"
    "FROM {{output_database}}.step2_4_wide_userfiltered\n"
    "ORDER BY <user_id>\n"
    "LIMIT @LIMIT@ OFFSET @OFFSET@;\n"
)

# Role columns the templates reference. Every one of them comes from the step2_0 skeleton.
REQUIRED_ROLES = ("user_id", "label", "age", "gender")

_ANGLE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")
_BRACE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_DEFERRED = {"catalog_columns", "catalog_tables"}
_CHECK = "python skill/feature-engineer/scripts/step2_driver_common.py check-collect"


def _assert_expanded(path: Path, text: str) -> None:
    active = _non_comment_text(text)
    leftover = _BRACE_RE.search(active)
    if leftover:
        raise SystemExit(f"{path} 仍含未替换占位符 {leftover.group(0)}")
    angle = _ANGLE_RE.search(active)
    if angle and angle.group(1) not in _DEFERRED:
        raise SystemExit(f"{path} 仍含未替换占位符 {angle.group(0)}")


def _bind(text: str, schema: dict[str, Any], data_source: dict[str, Any]) -> str:
    """Replace ``{{role}}`` / ``<role>`` on non-comment lines, then the catalog placeholders."""
    replacements: dict[str, str] = {}
    for key, value in schema.items():
        text_value = str(value or "").strip()
        if text_value:
            replacements[key] = text_value
    if not str(replacements.get("output_database") or "").strip():
        raise SystemExit("schema_resolution.output_database 为空，无法填充 {{output_database}}")

    lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue

        def _sub(match: re.Match[str]) -> str:
            return replacements.get(match.group(1), match.group(0))

        lines.append(_ANGLE_RE.sub(_sub, _BRACE_RE.sub(_sub, line)))
    return bind_placeholders("\n".join(lines), data_source)


def _write(path: Path, sql: str, label: str) -> None:
    _assert_expanded(path, sql)
    path.write_text(_non_comment_text(sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {path}")
    leftovers = unbound_placeholders(sql)
    if leftovers:
        print(
            f"警告：{label} 仍有未绑定占位符 {leftovers}；工作区缺 data_source.json，"
            "已回退套件 references/data_source.json。",
            file=sys.stderr,
        )


def _collect_guard(next_command: str, out_dir: Path) -> str:
    return f"{_CHECK} --manifest {MANIFEST} --expect-rows --out-dir {out_dir} && {next_command}"


def _role_columns(schema: dict[str, Any]) -> dict[str, str]:
    roles: dict[str, str] = {}
    missing: list[str] = []
    for role in REQUIRED_ROLES:
        value = str(schema.get(role) or "").strip()
        if value:
            roles[role] = value
        else:
            missing.append(role)
    if missing:
        raise SystemExit(
            "schema_resolution.json 缺少角色列 "
            + ", ".join(missing)
            + "；step2_4 无法确定过滤列与门禁列。请回到 step2_0 补全后重跑 prepare。"
        )
    return roles


def _prepare(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    _role_columns(schema)
    data_source = load_data_source(out_dir)

    _write(out_dir / CLEANING_SQL, _bind(CLEANING_TEMPLATE.read_text(encoding="utf-8"), schema, data_source), CLEANING_SQL)
    _write(out_dir / STATS_SQL, _bind(STATS_TEMPLATE.read_text(encoding="utf-8"), schema, data_source), STATS_SQL)
    _write(
        out_dir / COLUMNS_SQL,
        _bind(COLUMNS_TEMPLATE.read_text(encoding="utf-8"), schema, data_source),
        COLUMNS_SQL,
    )
    _write(out_dir / COUNT_SQL, _bind(COUNT_TEMPLATE.read_text(encoding="utf-8"), schema, data_source), COUNT_SQL)

    manifest = {
        "step": "step2_4",
        "stage": "prepare",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": [
            {"name": "step2_4_wide_userfiltered", "command_file": CLEANING_SQL, "collect_as": None},
            {"name": "step2_4_stats_validation", "command_file": STATS_SQL, "collect_as": RAW_STATS},
            {"name": "step2_4_columns_validation", "command_file": COLUMNS_SQL, "collect_as": RAW_COLUMNS},
            {"name": "step2_4_count", "command_file": COUNT_SQL, "collect_as": RAW_COUNT},
        ],
        "after_collect": _collect_guard(
            f"python skill/feature-engineer/scripts/step2_4_driver.py gate "
            f"--stats {RAW_STATS} --columns {RAW_COLUMNS} --out-dir {out_dir}",
            out_dir,
        ),
        "export": (
            f"python skill/feature-engineer/scripts/step2_4_driver.py export-plan "
            f"--raw-count {RAW_COUNT} --schema {args.schema} --out-dir {out_dir}"
        ),
        "save_note": (
            "collect_job 后必须用 step2_driver_common.py save-collect 落盘为清单 collect_as；"
            "禁止手工复制 payload。"
        ),
    }
    write_manifest(out_dir / MANIFEST, manifest)


def _rows(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for payload in _load_payloads(path):
        results = _extract_results(payload)
        if results:
            for columns, rows in results:
                for row in rows:
                    if isinstance(row, dict):
                        records.append(dict(row))
                    elif isinstance(row, (list, tuple)):
                        records.append(
                            {
                                str(columns[index]): (row[index] if index < len(row) else None)
                                for index in range(len(columns))
                            }
                        )
            continue
        records.extend(_plain_records(payload))
    return records


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _gate(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    stats_rows: list[dict[str, Any]] = []
    for path in args.stats:
        if not path.is_file():
            raise SystemExit(f"未找到 stats validation payload：{path}")
        stats_rows.extend(_rows(path))
    if not stats_rows:
        raise SystemExit("step2_4 stats validation 结果为空，无法执行门禁")
    stats = stats_rows[0]

    columns_rows: list[dict[str, Any]] = []
    for path in args.columns:
        if not path.is_file():
            raise SystemExit(f"未找到 columns validation payload：{path}")
        columns_rows.extend(_rows(path))
    if not columns_rows:
        raise SystemExit("step2_4 columns validation 结果为空，无法执行门禁")
    columns = columns_rows[0]

    errors: list[str] = []

    n_rows = _as_int(stats.get("n_rows"))
    n_users = _as_int(stats.get("n_users"))
    user_key_unique = _boolish(stats.get("user_key_unique"))
    n_label_missing = _as_int(stats.get("n_label_missing"))
    n_label_invalid = _as_int(stats.get("n_label_invalid"))
    n_pos = _as_int(stats.get("n_pos"))
    n_neg = _as_int(stats.get("n_neg"))

    if n_rows is None or n_rows <= 0:
        errors.append(f"n_rows 必须为正整数，实际 {stats.get('n_rows')!r}")
    if user_key_unique is not True:
        errors.append(f"user_key_unique 必须为 true（<user_id> 唯一），实际 {stats.get('user_key_unique')!r}")
    if n_users is not None and n_rows is not None and n_users != n_rows:
        errors.append(f"n_users 必须等于 n_rows（n_users={n_users}, n_rows={n_rows}）")
    if n_label_invalid is None or n_label_invalid != 0:
        errors.append(f"n_label_invalid 必须为 0（<label> 只能是 0/1），实际 {stats.get('n_label_invalid')!r}")

    column_reconciled = _boolish(columns.get("column_reconciled"))
    if column_reconciled is not True:
        errors.append(
            "column_reconciled 必须为 true（step2_4_wide_userfiltered 列数必须等于 "
            f"step2_3_wide_cleaned），实际 {columns.get('column_reconciled')!r}"
        )

    if errors:
        print("step2_4 门禁未通过：", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        print("\n修正后重跑本脚本。禁止手工创建 gate_data_verified_2_4.txt 绕过门禁。", file=sys.stderr)
        marker = args.marker or (out_dir / MARKER)
        if marker.exists():
            marker.unlink()
        raise SystemExit(1)

    report = {
        "step": "step2_4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "n_rows": n_rows,
        "n_users": n_users,
        "user_key_unique": True,
        "n_label_missing": n_label_missing,
        "n_label_invalid": n_label_invalid,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "actual_cols": _as_int(columns.get("actual_cols")),
        "expected_cols": _as_int(columns.get("expected_cols")),
        "column_reconciled": True,
    }
    out_report = args.report_json or (out_dir / REPORT_JSON)
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写出 {out_report}")

    marker = args.marker or (out_dir / MARKER)
    if marker.exists():
        marker.unlink()
    marker.write_text(
        f"PASSED: {report['generated_at']}（{n_rows} 行，正 {n_pos} : 负 {n_neg}）\n",
        encoding="utf-8",
    )
    print(f"step2_4 门禁通过，已写出 {marker}")

    # Drop after_collect so the Agent stops the prepare→gate chain; next step is the
    # separate ``export`` field (export-plan), not another after_collect hop.
    manifest_path = out_dir / MANIFEST
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and "after_collect" in payload:
            payload.pop("after_collect", None)
            payload["stage"] = "gate"
            payload["generated_at"] = report["generated_at"]
            write_manifest(manifest_path, payload)


def _export_plan(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for path in args.raw_count:
        if not path.is_file():
            raise SystemExit(f"未找到总行数 payload：{path}")
        rows.extend(_rows(path))
    if not rows:
        raise SystemExit("总行数 payload 为空，无法生成导出批次")
    n_rows = _as_int(rows[0].get("n_rows"))
    if n_rows is None or n_rows <= 0:
        raise SystemExit(f"n_rows 必须为正整数，实际 {rows[0].get('n_rows')!r}")

    if args.batch_size <= 0:
        raise SystemExit(f"--batch-size 必须为正整数，实际 {args.batch_size}")
    if args.batch_size > 999:
        print(
            "警告：--batch-size > 999，MCP 单次 collect 结果行数硬上限约 1000，"
            "超限可能触发 Code 396；建议使用默认 999。",
            file=sys.stderr,
        )

    schema = _read_json(args.schema)
    _role_columns(schema)
    data_source = load_data_source(out_dir)

    jobs: list[dict[str, Any]] = []
    count = 0
    offset = 0
    while offset < n_rows:
        limit = min(args.batch_size, n_rows - offset)
        count += 1
        name = BATCH_FILE.format(index=count)
        sql = BATCH_SQL.replace("@LIMIT@", str(limit)).replace("@OFFSET@", str(offset))
        sql = _bind(sql, schema, data_source)
        path = out_dir / name
        _assert_expanded(path, sql)
        path.write_text(_non_comment_text(sql).rstrip() + "\n", encoding="utf-8")
        jobs.append(
            {
                "name": f"step2_4_export_batch_{count:04d}",
                "command_file": name,
                "collect_as": f"raw_step2_4_export_batch_{count:04d}.json",
            }
        )
        offset += limit

    manifest_path = out_dir / MANIFEST
    previous: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    manifest = {
        **previous,
        "step": "step2_4",
        "stage": "export",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": n_rows,
        "batch_size": args.batch_size,
        "jobs": jobs,
        "after_collect": _collect_guard(
            "python skill/feature-engineer/scripts/step2_4_driver.py append-csv "
            + " ".join(f"--raw-batch {job['collect_as']}" for job in jobs)
            + f" --out-dir {out_dir}",
            out_dir,
        ),
        "save_note": (
            "每批 collect_job 后必须用 step2_driver_common.py save-collect 落盘；"
            "可用 --batch / --source-glob 批量模式。"
        ),
    }
    write_manifest(manifest_path, manifest)
    print(f"已生成 {len(jobs)} 个导出批次（n_rows={n_rows}, batch_size={args.batch_size}）")


def _append_csv(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_path = args.csv or (out_dir / WIDE_CSV)

    header_written = False
    total_rows = 0
    batches = 0
    with out_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        for path in args.raw_batch:
            if not path.is_file():
                raise SystemExit(f"未找到导出批次 payload：{path}")
            payloads = _load_payloads(path)
            found = False
            for payload in payloads:
                extracted = _extract_results(payload)
                if extracted:
                    for columns, rows in extracted:
                        found = True
                        if not header_written:
                            writer.writerow(columns)
                            header_written = True
                        for row in rows:
                            if isinstance(row, dict):
                                writer.writerow([row.get(str(col)) for col in columns])
                            elif isinstance(row, (list, tuple)):
                                writer.writerow(list(row))
                            total_rows += 1
                        batches += 1
                    continue
                plain = _plain_records(payload)
                if plain:
                    columns = [str(key) for key in plain[0].keys()]
                    found = True
                    if not header_written:
                        writer.writerow(columns)
                        header_written = True
                    for row in plain:
                        writer.writerow([row.get(str(col)) for col in columns])
                        total_rows += 1
                    batches += 1
            if not found:
                raise SystemExit(
                    f"{path} 中没有可识别的结果行（兼容 {{columns, rows}} 与扁平 rows 列表）；"
                    "可能是截断预览或未走 save-collect。请用 "
                    "step2_driver_common.py save-collect --source <tool-results/id.txt> "
                    f"--out {path.name} 重新落盘后重试"
                )

    if not header_written:
        raise SystemExit(
            "所有批次都没有表头，无法写出 CSV；请检查 collect payload"
            "（兼容 {columns, rows} 与扁平 rows），并用 save-collect 重落盘"
        )
    if total_rows <= 0:
        raise SystemExit(
            f"{out_path} 只有表头没有数据行；请检查 collect payload，"
            "并用 step2_driver_common.py save-collect 重新落盘"
        )
    print(f"已写出 {out_path}：{total_rows} 行，{batches} 批")


def _check_collect(args: argparse.Namespace) -> int:
    return check_collect(args.manifest, expect_rows=args.expect_rows, out_dir=args.out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_4 driver (user filtering + CSV export)")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="bind the cleaning + two validation SQL and write the manifest")
    prepare.add_argument("--schema", type=Path, default=Path("schema_resolution.json"))
    prepare.add_argument("--out-dir", type=Path, default=Path("."))
    prepare.set_defaults(func=_prepare)

    gate = sub.add_parser("gate", help="decide the step2_4 gate and write the filter report + marker")
    gate.add_argument("--stats", type=Path, action="append", required=True, help="stats validation payload; repeatable")
    gate.add_argument("--columns", type=Path, action="append", required=True, help="columns validation payload; repeatable")
    gate.add_argument("--out-dir", type=Path, default=Path("."))
    gate.add_argument("--report-json", type=Path, default=None)
    gate.add_argument("--marker", type=Path, default=None)
    gate.set_defaults(func=_gate)

    export = sub.add_parser("export-plan", help="turn the collected row count into per-batch SELECT files")
    export.add_argument("--raw-count", type=Path, action="append", required=True, help="count payload; repeatable")
    export.add_argument("--schema", type=Path, default=Path("schema_resolution.json"))
    export.add_argument("--batch-size", type=int, default=999)
    export.add_argument("--out-dir", type=Path, default=Path("."))
    export.set_defaults(func=_export_plan)

    append = sub.add_parser("append-csv", help="append collected batches into the final CSV")
    append.add_argument("--raw-batch", type=Path, action="append", required=True, help="batch payload; repeatable")
    append.add_argument("--out-dir", type=Path, default=Path("."))
    append.add_argument("--csv", type=Path, default=None)
    append.set_defaults(func=_append_csv)

    check = sub.add_parser("check-collect", help="forward to step2_driver_common.check_collect")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--expect-rows", action="store_true")
    check.add_argument("--out-dir", type=Path, default=Path("."))
    check.set_defaults(func=_check_collect)

    args = parser.parse_args()
    raise SystemExit(args.func(args) or 0)


if __name__ == "__main__":
    main()
