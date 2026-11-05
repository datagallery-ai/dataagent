#!/usr/bin/env python3
"""Generate ``receipt.json`` for step2_5 from artifacts the pipeline already wrote.

``step2_5_finalize.md`` fixes the receipt shape: two top-level fields (``summary`` and
``artifacts``) and a file artifact list. Both are fully determined by files the earlier
stages produced, so the Agent never hand-assembles the JSON:

  * the artifact list is fixed by the pipeline contract
  * ``step2_4_wide_userfiltered.csv`` supplies row count and column count (header)
  * the label ratio comes from ``step2_4_user_filter_report.json`` (``n_pos`` / ``n_neg``)
  * the source-table count comes from ``schema_resolution.json``

The deployment contract is registered only when ``validate_deployment_contract.py`` set
``validation.structural_validation.passed=true``; otherwise the entry is omitted and the
omission is reported on stderr, matching ``step2_5_finalize.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

WIDE_CSV = "step2_4_wide_userfiltered.csv"
CONTRACT = "step2_2_deployment_feature_contract.json"
DERIVATION = "step2_2_feature_derivation.md"
SCHEMA = "schema_resolution.json"
USER_FILTER_REPORT = "step2_4_user_filter_report.json"
RECEIPT = "receipt.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _csv_shape(path: Path) -> tuple[int, int]:
    """Return ``(data_row_count, column_count)`` from a CSV header + body."""
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            return 0, 0
        return sum(1 for _ in reader), len(header)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ratio_text(n_pos: int, n_neg: int) -> str:
    if n_pos <= 0 or n_neg <= 0:
        return f"{n_pos}:{n_neg}"
    return f"1:{round(n_neg / n_pos)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="generate step2_5 receipt.json")
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument("--wide-csv", type=Path, default=None)
    parser.add_argument("--schema", type=Path, default=None)
    parser.add_argument("--user-filter-report", type=Path, default=None)
    parser.add_argument("--contract", type=Path, default=None)
    parser.add_argument("--derivation", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None, help="defaults to <out-dir>/receipt.json")
    parser.add_argument("--summary", type=str, default=None, help="override the generated summary string")
    parser.add_argument(
        "--register-contract",
        action="store_true",
        help="register the deployment contract even when structural validation is not passed",
    )
    args = parser.parse_args()

    out_dir = args.out_dir
    wide_csv = args.wide_csv or out_dir / WIDE_CSV
    schema_path = args.schema or out_dir / SCHEMA
    derivation_path = args.derivation or out_dir / DERIVATION
    contract_path = args.contract or out_dir / CONTRACT
    filter_path = args.user_filter_report or out_dir / USER_FILTER_REPORT
    output = args.output or out_dir / RECEIPT

    if not wide_csv.is_file() or wide_csv.stat().st_size == 0:
        raise SystemExit(f"未找到非空 {wide_csv}；先完成 step2_4 CSV 导出再生成 receipt")
    if not derivation_path.is_file():
        raise SystemExit(f"未找到 {derivation_path}；step2_2 尚未产出特征衍生文档")
    if not schema_path.is_file():
        raise SystemExit(f"未找到 {schema_path}；step2_0 尚未完成")

    row_count, column_count = _csv_shape(wide_csv)

    schema = _read_json(schema_path) or {}
    table_count = len(schema.get("source_tables") or [])
    output_database = str(schema.get("output_database") or "").strip() or "<output_database>"

    filter_report = _read_json(filter_path)
    if filter_report is None:
        raise SystemExit(f"未找到 {filter_path}；先跑 step2_4 门禁写出过滤报告")
    n_pos = _as_int(filter_report.get("n_pos"))
    n_neg = _as_int(filter_report.get("n_neg"))
    if n_pos is None or n_neg is None:
        raise SystemExit(f"{filter_path} 缺少 n_pos/n_neg，无法生成 summary 的正负样本比")

    artifacts: list[dict[str, str]] = [
        {"kind": "file", "path": SCHEMA, "type": "json"},
        {"kind": "file", "path": DERIVATION, "type": "markdown"},
    ]

    contract = _read_json(contract_path)
    structural = ((contract or {}).get("validation") or {}).get("structural_validation") or {}
    contract_registered = bool(structural.get("passed")) or (contract is not None and args.register_contract)
    if contract_registered:
        artifacts.append({"kind": "file", "path": CONTRACT, "type": "json"})

    artifacts.append({"kind": "file", "path": WIDE_CSV, "type": "csv"})

    summary = args.summary or (
        f"特征工程完成：基于 {table_count} 张交付表，产出 {row_count} 行 {column_count} 列宽表。"
        f"正 {n_pos} : 负 {n_neg} = {_ratio_text(n_pos, n_neg)}，库 {output_database}"
    )

    output.write_text(
        json.dumps({"summary": summary, "artifacts": artifacts}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"已写出 {output}")
    print(f"  summary: {summary}")
    for item in artifacts:
        print(f"  - {item['kind']}: {item['path']}")
    if contract is None:
        print(f"  未找到 {contract_path.name}，receipt 不含契约条目。", file=sys.stderr)
    elif not contract_registered:
        print(
            "  契约未登记：validation.structural_validation.passed != true。"
            "先跑 step2_2_validate_deployment_contract.py；仍不通过则保持不登记。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
