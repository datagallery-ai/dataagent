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

When ``progress_step2_3b.txt`` exists (semantic prior features were applied), the receipt
additionally registers the ``step2_3b_wide_semantic`` warehouse table plus the step2_3b
file artifacts (``semantic_apply.json``, feature doc, gate reports), and the summary gains
the semantic feature counts.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_driver_common import load_data_source  # noqa: E402

WIDE_CSV = "step2_4_wide_userfiltered.csv"
CONTRACT = "step2_2_deployment_feature_contract.json"
DERIVATION = "step2_2_feature_derivation.md"
SCHEMA = "schema_resolution.json"
USER_FILTER_REPORT = "step2_4_user_filter_report.json"
RECEIPT = "receipt.json"

# step2_3b（语义先验特征）产物：marker 由 step2_3b gate 写出，存在即视为该阶段生效。
SEMANTIC_MARKER = "progress_step2_3b.txt"
SEMANTIC_TABLE = "step2_3b_wide_semantic"
SEMANTIC_APPLY = "semantic_apply.json"
SEMANTIC_MD = "step2_3b_semantic_features.md"
SEMANTIC_GATE_REPORT_JSON = "step2_3b_gate_report.json"
SEMANTIC_GATE_REPORT_MD = "step2_3b_gate_report.md"


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


def _semantic_artifacts(out_dir: Path, output_database: str) -> tuple[list[dict[str, str]], str]:
    """Return ``(artifacts, summary_suffix)`` for step2_3b, or ``([], "")`` when inactive.

    Active means ``progress_step2_3b.txt`` exists. The warehouse-table entry needs
    ``table_kind`` / ``uri_scheme`` from data_source.json; missing files are skipped with a
    stderr warning so a half-finished step2_3b is visible instead of silently omitted.
    """
    if not (out_dir / SEMANTIC_MARKER).is_file():
        return [], ""

    apply_path = out_dir / SEMANTIC_APPLY
    apply_payload = _read_json(apply_path)
    if apply_payload is None:
        raise SystemExit(
            f"存在 {SEMANTIC_MARKER} 但找不到 {apply_path}；step2_3b 产物不完整，"
            "请从 generate 重新执行 step2_3b。"
        )

    artifacts: list[dict[str, str]] = []

    data_source = load_data_source(out_dir)
    table_kind = str(data_source.get("table_kind") or "").strip()
    uri_scheme = str(data_source.get("uri_scheme") or "").strip()
    if table_kind and uri_scheme:
        artifacts.append(
            {
                "kind": table_kind,
                "uri": f"{uri_scheme}://{output_database}/{SEMANTIC_TABLE}",
                "name": f"{output_database}.{SEMANTIC_TABLE}",
            }
        )
    else:
        print(
            "警告：data_source.json 缺少 table_kind/uri_scheme，receipt 未登记 "
            f"{SEMANTIC_TABLE} 表条目（文件产物照常登记）。",
            file=sys.stderr,
        )

    artifacts.append({"kind": "file", "path": SEMANTIC_APPLY, "type": "json"})
    if (out_dir / SEMANTIC_MD).is_file():
        artifacts.append({"kind": "file", "path": SEMANTIC_MD, "type": "markdown"})
    else:
        print(f"警告：存在 {SEMANTIC_MARKER} 但缺少 {SEMANTIC_MD}。", file=sys.stderr)

    gate = apply_payload.get("gate") if isinstance(apply_payload.get("gate"), dict) else {}
    gate_report_missing = not (out_dir / SEMANTIC_GATE_REPORT_JSON).is_file()
    if not gate_report_missing:
        artifacts.append({"kind": "file", "path": SEMANTIC_GATE_REPORT_JSON, "type": "json"})
        if (out_dir / SEMANTIC_GATE_REPORT_MD).is_file():
            artifacts.append({"kind": "file", "path": SEMANTIC_GATE_REPORT_MD, "type": "markdown"})
    if not gate:
        print(
            "警告：semantic_apply.json 尚无 gate 段（spearman-gate 未运行？），"
            "receipt 摘要只登记启用的语义特征数。",
            file=sys.stderr,
        )
    elif gate_report_missing:
        print(f"警告：semantic_apply.json 有 gate 段但缺少 {SEMANTIC_GATE_REPORT_JSON}。", file=sys.stderr)

    enabled = len(apply_payload.get("enabled_features") or [])
    if gate:
        kept = len(gate.get("kept") or [])
        dropped = len(gate.get("dropped") or [])
        suffix = f"语义先验特征 {enabled} 个（入选 {kept}、剔除 {dropped}）"
    else:
        suffix = f"语义先验特征 {enabled} 个"
    return artifacts, suffix


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

    semantic_artifacts, semantic_suffix = _semantic_artifacts(out_dir, output_database)
    artifacts.extend(semantic_artifacts)

    artifacts.append({"kind": "file", "path": WIDE_CSV, "type": "csv"})

    summary = args.summary or (
        f"特征工程完成：基于 {table_count} 张交付表，产出 {row_count} 行 {column_count} 列宽表。"
        f"正 {n_pos} : 负 {n_neg} = {_ratio_text(n_pos, n_neg)}，库 {output_database}"
        + (f"；{semantic_suffix}" if semantic_suffix else "")
    )

    output.write_text(
        json.dumps({"summary": summary, "artifacts": artifacts}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"已写出 {output}")
    print(f"  summary: {summary}")
    for item in artifacts:
        location = item.get("path") or item.get("uri") or ""
        print(f"  - {item['kind']}: {location}")
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
