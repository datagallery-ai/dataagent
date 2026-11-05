#!/usr/bin/env python3
"""Final consistency check across step2_0 ~ step2_5.

The per-stage gates (``step2_0_3_verify_gate.py``, ``step2_1_verify_gate.py``,
``step2_2_verify_gate.py``, ``step2_3_verify_gate.py``) already decided whether each
stage passed and wrote their markers/artifacts. This script does **not** re-derive any of
those decisions: it only reads the artifacts those gates left behind and renders the
closing checklist, so the Agent never hand-verifies "file exists / gate passed / column
count matches" by reading markdown.

Sources, per stage (markers are authoritative; this script does not re-derive gate rules):

  * step2_0 -- ``gate_data_verified.txt``, ``schema_resolution.json``, profile md, ``list_fields.txt``
  * step2_1 -- ``progress_step2_1.txt`` (``status=done``; this file *is* the step2_1 gate marker)
  * step2_2 -- ``step2_2_feature_gate.json`` (all 2a~2l), ``gate_data_verified_2_2.txt``,
    derivation/contract/plan presence
  * step2_3 -- ``gate_data_verified_2_3.txt``, ``step2_3_cleaning_report.json``
  * step2_4 -- ``gate_data_verified_2_4.txt``, ``step2_4_wide_userfiltered.csv``,
    ``step2_4_user_filter_report.json``; CSV 表头列名唯一，且 header SHA256
    （``sha256("\\x1f".join(header))``）与契约 ``wide_csv_header_sha256`` 逐位比对
  * step2_5 -- ``receipt.json``, contract ``validation.structural_validation.passed``

Warehouse-side facts (the validation SELECTs returning zero rows) cannot be checked from
local files; they are reported as ``warehouse`` items so the Agent confirms them before
submitting the receipt.

Writes ``step2_final_check.json`` and exits non-zero when any local item fails.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
SKIP = "--"

NON_STANDARD_PREFIXES = ("_tmp_", "_ft_", "fe_")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class Checklist:
    """Collects ``(stage, item, status, detail)`` rows and tracks local failures."""

    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []
        self.failed = 0

    def add(self, stage: str, item: str, ok: bool, detail: str = "") -> None:
        status = PASS if ok else FAIL
        if not ok:
            self.failed += 1
        self.rows.append({"stage": stage, "item": item, "status": status, "detail": detail})

    def note(self, stage: str, item: str, detail: str = "") -> None:
        self.rows.append({"stage": stage, "item": item, "status": SKIP, "detail": detail})


def _check_present(check: Checklist, stage: str, path: Path, label: str) -> bool:
    exists = path.is_file() and path.stat().st_size > 0
    check.add(stage, label, exists, "" if exists else f"缺少 {path.name}")
    return exists


def check_step2_0(check: Checklist, out_dir: Path) -> None:
    has_marker = _check_present(check, "step2_0", out_dir / "gate_data_verified.txt", "格式门禁标记")
    _check_present(check, "step2_0", out_dir / "schema_resolution.json", "schema_resolution.json")
    md = out_dir / "step2_0_profile_report.md"
    if _check_present(check, "step2_0", md, "step2_0_profile_report.md"):
        if has_marker:
            check.note("step2_0", "constant:TODO", "由 step2_0 gate 已判定，此处不重扫")
        else:
            pending = md.read_text(encoding="utf-8").count("constant:TODO")
            check.add(
                "step2_0",
                "无残留 constant:TODO",
                pending == 0,
                f"残留 {pending} 行" if pending else "",
            )
    _check_present(check, "step2_0", out_dir / "list_fields.txt", "list_fields.txt")


def check_step2_1(check: Checklist, out_dir: Path) -> None:
    marker = out_dir / "progress_step2_1.txt"
    if _check_present(check, "step2_1", marker, "progress_step2_1.txt"):
        text = marker.read_text(encoding="utf-8")
        check.add("step2_1", "status=done", "status=done" in text, "标记缺 status=done")


def check_step2_2(check: Checklist, out_dir: Path) -> None:
    gate = _read_json(out_dir / "step2_2_feature_gate.json")
    gate_passed = False
    if gate is None:
        check.add("step2_2", "step2_2_feature_gate.json", False, "缺少门禁结果")
    else:
        checks = gate.get("checks") or {}
        failed = sorted(key for key, value in checks.items() if not value.get("passed"))
        gate_passed = bool(checks) and not failed
        check.add(
            "step2_2",
            "门禁 2a~2l 全通过",
            gate_passed,
            "失败项 " + ", ".join(failed) if failed else f"{len(checks)} 组",
        )
    has_marker = _check_present(check, "step2_2", out_dir / "gate_data_verified_2_2.txt", "门禁标记")
    _check_present(check, "step2_2", out_dir / "step2_2_feature_derivation.md", "step2_2_feature_derivation.md")
    _check_present(check, "step2_2", out_dir / "step2_2_deployment_feature_contract.json", "部署契约")
    _check_present(check, "step2_2", out_dir / "step2_2_feature_plan.json", "step2_2_feature_plan.json")
    if has_marker and gate_passed:
        check.note("step2_2", "plan 字段完整性", "由 step2_2 gate 2e/2f/2k 已判定，此处不重推")
    check.note(
        "step2_2",
        "仓内门禁：高基数与地理原列 validation 返回 0 行",
        "本项由提交 step2_2_validation_expanded.sql 后返回 0 行确认，本地无法校验",
    )


def check_step2_3(check: Checklist, out_dir: Path) -> None:
    has_marker = _check_present(check, "step2_3", out_dir / "gate_data_verified_2_3.txt", "门禁标记")
    _check_present(check, "step2_3", out_dir / "step2_3_cleaning_report.json", "step2_3_cleaning_report.json")
    if has_marker:
        check.note("step2_3", "DROP 命中宽表列", "由 step2_3 finalize/gate 已判定，此处不重推")
    else:
        plan = _read_json(out_dir / "step2_3_cleaning_plan.json")
        if plan is not None:
            orphan = plan.get("drop_not_in_wide") or []
            check.add("step2_3", "DROP 决策全部命中真实宽表列", not orphan, ", ".join(orphan[:5]))


def check_step2_4(check: Checklist, out_dir: Path) -> None:
    has_marker = _check_present(check, "step2_4", out_dir / "gate_data_verified_2_4.txt", "门禁标记")
    csv_path = out_dir / "step2_4_wide_userfiltered.csv"
    header: list[str] | None = None
    if _check_present(check, "step2_4", csv_path, "step2_4_wide_userfiltered.csv"):
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream)
                header = next(reader, None)
                data_rows = list(reader)
            if header is None:
                check.add("step2_4", "CSV 含表头与数据行", False, "空文件")
            else:
                total_lines = len(data_rows) + 1
                check.add(
                    "step2_4",
                    "CSV 含表头与数据行",
                    len(data_rows) > 0,
                    f"{total_lines} 行（含表头）",
                )
                unique = len(header) == len(set(header))
                dupes = sorted({name for name in header if header.count(name) > 1})
                check.add(
                    "step2_4",
                    "CSV 表头列名唯一",
                    unique,
                    "" if unique else "重复列 " + ", ".join(dupes[:5]),
                )
                digest = hashlib.sha256("\x1f".join(header).encode("utf-8")).hexdigest()
                contract = _read_json(out_dir / "step2_2_deployment_feature_contract.json")
                structural = (
                    ((contract or {}).get("validation") or {}).get("structural_validation") or {}
                )
                expected = structural.get("wide_csv_header_sha256")
                if not expected:
                    check.add(
                        "step2_4",
                        "CSV header SHA256 == 契约 wide_csv_header_sha256",
                        False,
                        "契约缺少 wide_csv_header_sha256；请先跑 "
                        "step2_2_validate_deployment_contract.py / step2_5_driver.py validate-contract",
                    )
                else:
                    match = str(expected) == digest
                    check.add(
                        "step2_4",
                        "CSV header SHA256 == 契约 wide_csv_header_sha256",
                        match,
                        "" if match else f"csv={digest[:12]}… contract={str(expected)[:12]}…",
                    )
        except (OSError, csv.Error) as exc:
            check.add("step2_4", "CSV 可读", False, str(exc))
    report = _read_json(out_dir / "step2_4_user_filter_report.json")
    if report is None:
        check.add("step2_4", "step2_4_user_filter_report.json", False, "缺少过滤报告")
    elif has_marker:
        check.note("step2_4", "label/用户键门禁", "由 step2_4 gate 已判定，此处不重推")
    else:
        invalid = report.get("n_label_invalid")
        try:
            invalid_count = int(invalid) if invalid not in (None, "") else 0
        except (TypeError, ValueError):
            invalid_count = -1
        check.add("step2_4", "label 仅含 0/1", invalid_count == 0, f"n_label_invalid={invalid}")
        check.add("step2_4", "用户键唯一", bool(report.get("user_key_unique", True)), "")


def check_receipt(check: Checklist, out_dir: Path) -> None:
    receipt = _read_json(out_dir / "receipt.json")
    if receipt is None:
        check.add("step2_5", "receipt.json", False, "缺少 receipt")
    else:
        artifacts = receipt.get("artifacts") or []
        missing = [
            str(item.get("path"))
            for item in artifacts
            if isinstance(item, dict)
            and str(item.get("kind") or "file") == "file"
            and not (out_dir / str(item.get("path"))).is_file()
        ]
        check.add("step2_5", "receipt 每条 artifact 指向真实文件", not missing, ", ".join(missing[:5]))
        check.add("step2_5", "receipt.summary 非空", bool(str(receipt.get("summary") or "").strip()), "")

    contract = _read_json(out_dir / "step2_2_deployment_feature_contract.json")
    if contract is not None:
        validation = contract.get("validation") or {}
        structural = validation.get("structural_validation") or {}
        registered = contract.get("registered_in_receipt", None)
        if registered is False:
            check.note("step2_5", "契约结构检查", "本次未登记契约")
        else:
            check.add(
                "step2_5",
                "契约 structural_validation.passed=true",
                bool(structural.get("passed")),
                "未通过或未运行 validate_deployment_contract.py",
            )


def check_naming(check: Checklist, out_dir: Path) -> None:
    offenders = sorted(
        path.name
        for path in out_dir.iterdir()
        if path.is_file() and path.name.startswith(NON_STANDARD_PREFIXES)
    )
    check.add("naming", "无非标准前缀文件", not offenders, ", ".join(offenders[:5]))


def main() -> None:
    parser = argparse.ArgumentParser(description="step2 final consistency check (step2_0~step2_5)")
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument("--report-json", type=Path, default=Path("step2_final_check.json"))
    parser.add_argument(
        "--require-receipt",
        action="store_true",
        help="treat a missing receipt.json as a failure (run this after step2_5)",
    )
    args = parser.parse_args()
    out_dir = args.out_dir

    check = Checklist()
    check_step2_0(check, out_dir)
    check_step2_1(check, out_dir)
    check_step2_2(check, out_dir)
    check_step2_3(check, out_dir)
    check_step2_4(check, out_dir)
    if args.require_receipt:
        check_receipt(check, out_dir)
    else:
        check.note("step2_5", "receipt 校验", "未启用 --require-receipt；定稿后请带该开关重跑")
    check_naming(check, out_dir)

    report = {
        "step": "step2_final_check",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": check.failed == 0,
        "failed_count": check.failed,
        "items": check.rows,
    }
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    for row in check.rows:
        line = f"{row['status']:4s} {row['stage']:8s} {row['item']}"
        if row["detail"] and row["status"] != PASS:
            line += f"  ({row['detail']})"
        print(line)
    print(f"\n已写出 {args.report_json}；{'全部通过' if report['passed'] else f'{check.failed} 项未通过'}")
    if check.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
