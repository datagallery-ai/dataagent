#!/usr/bin/env python3
"""Automated gate for the step2_3 user-grain cleaning of the aggregated wide table.

Reads the raw warehouse payloads collected through the MCP and checks:

  * cleaning_report correctness: no ``recommendation='KEEP'`` row has ``null_rate`` above
    ``null_rate_drop_threshold_percent`` (a KEEP on a high-missing column means the CASE
    WHEN was bypassed by a literal)
  * protected columns are KEEP and constant columns (``n_unique <= constant_drop_max_unique``)
    are DROP
  * wide_cleaned completeness, read from the self-certifying gate SELECT: ``n_rows =
    uniqExact(user_id)``, ``column_coverage_ok``, and zero ``surviving_except_columns`` /
    ``surviving_drop_features`` (the EXCEPT clause dropped what it promised)

Both thresholds are read from ``references/cleaning_policy.json`` — the same file
``step2_3_expand_sql.py`` injects into the report SQL, so decision and gate cannot drift.

On success it writes ``gate_data_verified_2_3.txt`` and ``step2_3_cleaning_report.json``
and exits 0. On failure it prints every offending field and exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTECTED_ROLE_KEYS = ("user_id", "label", "age", "gender")
POLICY_JSON = Path(__file__).resolve().parent.parent / "references" / "cleaning_policy.json"


def _load_policy() -> dict[str, Any]:
    """Read the same ``references/cleaning_policy.json`` the expander used."""
    try:
        policy = json.loads(POLICY_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {POLICY_JSON}：{exc}") from exc
    if not isinstance(policy, dict):
        raise SystemExit(f"{POLICY_JSON} 必须包含一个 JSON 对象")
    try:
        threshold = float(policy["null_rate_drop_threshold_percent"])
        max_unique = int(policy["constant_drop_max_unique"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"{POLICY_JSON} 缺少合法的阈值字段：{exc}") from exc
    return {
        "null_rate_drop_threshold_percent": threshold,
        "constant_drop_max_unique": max_unique,
    }


POLICY = _load_policy()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _extract_results(payload: Any) -> list[tuple[list[str], list[Any]]]:
    found: list[tuple[list[str], list[Any]]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            columns = node.get("columns")
            rows = node.get("rows")
            if isinstance(columns, list) and isinstance(rows, list):
                found.append(([str(col) for col in columns], rows))
                return
            for key in ("outputs", "data", "result", "payload", "original_msg"):
                if key in node:
                    _walk(node[key])
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def _load_payloads(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    payloads: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if payloads:
        return payloads
    start = min((index for index in (text.find("["), text.find("{")) if index >= 0), default=-1)
    if start >= 0:
        try:
            return [json.loads(text[start:])]
        except json.JSONDecodeError:
            return []
    return []


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
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    records.append(dict(item))
        elif isinstance(payload, dict):
            # 扁平 dict（如 raw_wide_stats.json 直接写的 n_rows/column_coverage_ok 等字段）：
            # 视为单条记录（否则扁平 dict 会被 _extract_results 漏掉而失败）。
            records.append(dict(payload))
    return records


def _rows_from(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"未找到 payload 文件：{path}")
        records.extend(_rows(path))
    return records


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    return None if parsed is None else int(parsed)


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _feature(row: dict[str, Any]) -> str:
    return str(row.get("feature") or row.get("column_name") or "").strip()


def _protected_columns(schema: dict[str, Any]) -> set[str]:
    return {str(schema.get(key) or "").strip() for key in PROTECTED_ROLE_KEYS} - {""}


def check_report(
    report_rows: list[dict[str, Any]],
    schema: dict[str, Any],
    errors: list[str],
) -> None:
    protected = _protected_columns(schema)
    threshold = POLICY["null_rate_drop_threshold_percent"]
    max_unique = POLICY["constant_drop_max_unique"]

    for row in report_rows:
        feature = _feature(row)
        if not feature:
            continue
        recommendation = str(row.get("recommendation") or "").strip().upper()
        null_rate = _as_float(row.get("null_rate"))
        n_unique = _as_int(row.get("n_unique"))

        # Correctness gate: KEEP + null_rate > threshold means CASE WHEN was bypassed.
        if recommendation == "KEEP" and null_rate is not None and null_rate > threshold:
            errors.append(
                f"cleaning_report: {feature} 标 KEEP 但 null_rate={null_rate}%（>{threshold:g}）"
                "——CASE WHEN 被绕过或阈值不一致"
            )
        if feature in protected and recommendation != "KEEP":
            errors.append(f"cleaning_report: 保护列 {feature} 被标 {recommendation or '空'}，必须 KEEP")
        if (
            n_unique is not None
            and n_unique <= max_unique
            and recommendation == "KEEP"
            and feature not in protected
        ):
            errors.append(f"cleaning_report: 常量字段 {feature}（n_unique={n_unique}）未被 DROP")


def check_wide(
    wide_rows: list[dict[str, Any]],
    errors: list[str],
) -> None:
    if not wide_rows:
        errors.append("wide_cleaned: 未提供完整性门禁结果（n_rows/n_user_id/column_coverage_ok）")
        return

    row = wide_rows[0]
    n_rows = _as_int(row.get("n_rows"))
    n_user_id = _as_int(row.get("n_user_id"))
    if _boolish(row.get("user_key_unique")) is False or (
        n_rows is not None and n_user_id is not None and n_rows != n_user_id
    ):
        errors.append(f"wide_cleaned: n_rows={n_rows} != n_user_id={n_user_id}，用户键不唯一")
    if _boolish(row.get("column_coverage_ok")) is False:
        profiled = _as_int(row.get("profiled_columns"))
        expected = _as_int(row.get("expected_columns"))
        errors.append(
            f"wide_cleaned: column_coverage_ok=false（profiled={profiled}, expected={expected}）"
        )
    survivors = _as_int(row.get("surviving_except_columns"))
    if survivors:
        errors.append(f"wide_cleaned: EXCEPT 声明的 {survivors} 列仍残留在宽表中")
    drop_survivors = _as_int(row.get("surviving_drop_features"))
    if drop_survivors:
        errors.append(f"wide_cleaned: {drop_survivors} 个 DROP 字段仍残留在宽表中")
    dropped_protected = _as_int(row.get("dropped_protected_columns"))
    if dropped_protected:
        errors.append(f"wide_cleaned: {dropped_protected} 个保护列被标为 DROP")


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated step2_3 gate")
    parser.add_argument("--schema", type=Path, required=True, help="schema_resolution.json")
    parser.add_argument(
        "--raw-report",
        type=Path,
        action="append",
        required=True,
        metavar="RAW_REPORT [RAW_REPORT ...]",
        help=(
            "cleaning_report collect payload (feature/null_rate/n_unique/recommendation); "
            "可重复传多个（一个 batch 一个文件），脚本自动合并"
        ),
    )
    parser.add_argument(
        "--raw-wide-stats",
        type=Path,
        action="append",
        required=True,
        metavar="RAW_WIDE_STATS [RAW_WIDE_STATS ...]",
        help=(
            "step2_3_validation_expanded.sql collect payload "
            "(n_rows/n_user_id/column_coverage_ok/EXCEPT survival); 可重复传多个"
        ),
    )
    parser.add_argument("--marker", type=Path, default=Path("gate_data_verified_2_3.txt"))
    parser.add_argument(
        "--report-json",
        type=Path,
        default=Path("step2_3_cleaning_report.json"),
        help="cleaning decision summary written on success",
    )
    args = parser.parse_args()

    schema = _read_json(args.schema)
    report_rows = _rows_from(args.raw_report)
    if not report_rows:
        raise SystemExit("cleaning_report 结果为空，无法执行门禁")
    wide_rows = _rows_from(args.raw_wide_stats)

    errors: list[str] = []
    check_report(report_rows, schema, errors)
    check_wide(wide_rows, errors)

    if errors:
        print("step2_3 门禁未通过：", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        print(
            "\n修正后重跑本脚本。禁止手工创建 gate_data_verified_2_3.txt 绕过门禁。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    decisions = [
        {
            "feature": _feature(row),
            "null_rate": row.get("null_rate"),
            "n_unique": row.get("n_unique"),
            "recommendation": str(row.get("recommendation") or "").strip().upper(),
        }
        for row in report_rows
        if _feature(row)
    ]
    drop = [item["feature"] for item in decisions if item["recommendation"] == "DROP"]
    keep = [item["feature"] for item in decisions if item["recommendation"] == "KEEP"]
    args.report_json.write_text(
        json.dumps(
            {
                "wide_table": "step2_2_wide_aggregated",
                "output_table": "step2_3_wide_cleaned",
                "total_features": len(decisions),
                "keep_count": len(keep),
                "drop_count": len(drop),
                "drop_features": drop,
                "decisions": decisions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"已写出 {args.report_json}：{len(keep)} KEEP / {len(drop)} DROP")

    if args.marker.exists():
        args.marker.unlink()
    timestamp = datetime.now(timezone.utc).isoformat()
    args.marker.write_text(f"PASSED: {timestamp}\n", encoding="utf-8")
    print(f"step2_3 门禁通过，已写出 {args.marker}")


if __name__ == "__main__":
    main()
