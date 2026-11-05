#!/usr/bin/env python3
"""Automated gate for step2_1 (1:1 merge completeness).

Reads the raw warehouse payloads collected through the MCP and checks:

  * the base rows did not multiply: ``n_rows == expected_rows`` (``row_count_unchanged``);
  * the user key is unique after the merge: ``count() == uniqExact(user_id)``;
  * the merged schema matches the plan exactly: the self-certifying validation SELECT
    already compared ``step2_1_wide_joined`` against the inlined expected column list,
    so a silently dropped or mis-prefixed column surfaces as
    ``column_reconciliation_ok = false``.

On success it writes ``progress_step2_1.txt`` and exits 0. On failure it prints every
offending field and exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    return records


def _rows_from(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"未找到 payload 文件：{path}")
        records.extend(_rows(path))
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


def check_merge(
    validation_rows: list[dict[str, Any]],
    plan: dict[str, Any],
    errors: list[str],
) -> None:
    if not validation_rows:
        errors.append("step2_1_wide_joined: 未提供合表门禁结果（n_rows/expected_rows/user_key_unique）")
        return

    row = validation_rows[0]
    n_rows = _as_int(row.get("n_rows"))
    n_user_id = _as_int(row.get("n_user_id"))
    expected_rows = _as_int(row.get("expected_rows"))
    if _boolish(row.get("user_key_unique")) is False or (
        n_rows is not None and n_user_id is not None and n_rows != n_user_id
    ):
        errors.append(f"step2_1_wide_joined: 用户键不唯一（n_rows={n_rows}, n_user_id={n_user_id}）")
    if _boolish(row.get("row_count_unchanged")) is False or (
        n_rows is not None and expected_rows is not None and n_rows != expected_rows
    ):
        errors.append(
            f"step2_1_wide_joined: 行数膨胀（n_rows={n_rows} != expected_rows={expected_rows}）"
            "——1:1 JOIN 不应改变行数"
        )

    # Column reconciliation is asserted in-warehouse against the inlined expected list.
    expected_columns = [str(name) for name in plan.get("expected_columns") or []]
    expected_count = plan.get("expected_column_count")
    if not expected_columns:
        errors.append("step2_1_merge_plan.json 缺少 expected_columns，请先运行 step2_1_expand_sql.py")
        return

    actual_count = _as_int(row.get("actual_column_count"))
    if isinstance(actual_count, int) and isinstance(expected_count, int) and actual_count != expected_count:
        errors.append(
            f"step2_1_wide_joined: 列数 {actual_count} != 预期 {expected_count}"
            "——仅允许 u.* 基表列 + 每张 1:1 表的 <表名>_<列>"
        )
    if _boolish(row.get("column_reconciliation_ok")) is False:
        errors.append(
            "step2_1_wide_joined: 列对账失败（仓内列集合 != 内联期望列集合）"
            "——存在缺失列或计划外列"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated step2_1 gate")
    parser.add_argument("--schema", type=Path, required=True, help="schema_resolution.json")
    parser.add_argument(
        "--raw-validation",
        type=Path,
        action="append",
        required=True,
        help="step2_1_validation collect payload (n_rows/expected_rows/user_key_unique); repeatable",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("step2_1_merge_plan.json"),
        help="merge plan from step2_1_expand_sql.py",
    )
    parser.add_argument("--marker", type=Path, default=Path("progress_step2_1.txt"))
    args = parser.parse_args()

    _read_json(args.schema)
    if not args.plan.is_file():
        raise SystemExit(f"未找到 {args.plan}（请先运行 step2_1_expand_sql.py）")
    plan = _read_json(args.plan)

    validation_rows = _rows_from(args.raw_validation)
    if not validation_rows:
        raise SystemExit("step2_1 validation 结果为空，无法执行门禁")

    errors: list[str] = []
    check_merge(validation_rows, plan, errors)

    if errors:
        print("step2_1 门禁未通过：", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        print("\n修正后重跑本脚本。禁止手工创建 progress_step2_1.txt 绕过门禁。", file=sys.stderr)
        raise SystemExit(1)

    joined = plan.get("joined_table_count") or 0
    column_count = plan.get("expected_column_count") or 0
    if args.marker.exists():
        args.marker.unlink()
    timestamp = datetime.now(timezone.utc).isoformat()
    args.marker.write_text(
        f"status=done\ntodo=all\nupdated={timestamp}\n"
        f"summary=step2_1_wide_joined 校验通过（{column_count} 列，1:1 右表 {joined} 张）\n",
        encoding="utf-8",
    )
    print(f"step2_1 门禁通过，已写出 {args.marker}")


if __name__ == "__main__":
    main()
