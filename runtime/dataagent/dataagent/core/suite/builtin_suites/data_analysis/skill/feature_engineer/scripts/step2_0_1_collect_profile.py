#!/usr/bin/env python3
"""Normalize step2_0 warehouse profile results into ``step2_0_profile_dump.json``.

step2_0_1 materializes two warehouse tables (``step2_0_table_profile`` and
``step2_0_column_profile``). The Agent collects each result through the warehouse MCP
(``collect_job``) and saves the payload as a local file. This script turns those
payloads into one canonical dump that the render/gate scripts consume.

This script is pure local file processing: it never connects to a warehouse, never
generates SQL, and never knows scene-specific table or column names. It only reads
JSON/JSONL payloads and writes a normalized JSON file.

Accepted raw inputs (auto-detected):
  * a ``collect_job`` payload object: ``{"outputs": [{"columns": [...], "rows": [[...]]}]}``
  * any nested ``{"data": ...}`` / ``{"original_msg": {...}}`` envelope
  * a bare list of row objects ``[{"table_name": ...}, ...]``
  * a JSONL / TSVWithNames text file written by the truncator hook

Kind is inferred from the columns present in each result:
  * ``classification``             -> table_profile
  * ``missing_rate`` + ``n_unique`` -> column_profile
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TABLE_PROFILE_COLUMNS = ("table_name", "total_rows", "unique_user_id", "classification")
COLUMN_PROFILE_COLUMNS = (
    "table_name",
    "column_name",
    "data_type",
    "missing_rate",
    "n_unique",
    "sample_values",
)


def _coerce_scalar(value: Any) -> Any:
    """Convert numeric-looking strings to int/float; keep everything else as-is."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == "":
        return value
    try:
        if text.lstrip("-+").isdigit():
            return int(text)
    except ValueError:
        return value
    try:
        return float(text)
    except ValueError:
        return value


def _rows_to_records(columns: list[str], rows: list[Any]) -> list[dict[str, Any]]:
    """Join a column header with row values, coercing numerics."""
    records: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            records.append({key: _coerce_scalar(val) for key, val in row.items()})
            continue
        if not isinstance(row, (list, tuple)):
            continue
        record: dict[str, Any] = {}
        for index, name in enumerate(columns):
            value = row[index] if index < len(row) else None
            record[str(name)] = _coerce_scalar(value)
        records.append(record)
    return records


def _extract_results(payload: Any) -> list[tuple[list[str], list[Any]]]:
    """Yield ``(columns, rows)`` pairs found anywhere in a collect payload."""
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
            # A bare list of row objects is handled by the caller (no header).
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def _load_text_payloads(path: Path) -> list[Any]:
    """Load one payload from a JSON, JSONL, or truncator text file."""
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
    # Fall back to a JSON array/object embedded in a larger text blob.
    start = min(
        (index for index in (text.find("["), text.find("{")) if index >= 0),
        default=-1,
    )
    if start >= 0:
        try:
            return [json.loads(text[start:])]
        except json.JSONDecodeError:
            return []
    return []


def _classify(columns: list[str]) -> str | None:
    header = {str(col).strip() for col in columns}
    if "classification" in header and "table_name" in header:
        return "table_profile"
    if {"missing_rate", "n_unique"}.issubset(header) and "column_name" in header:
        return "column_profile"
    return None


def _detect_bare_kind(records: list[dict[str, Any]]) -> str | None:
    if not records:
        return None
    keys = set(records[0].keys())
    if "classification" in keys:
        return "table_profile"
    if {"missing_rate", "n_unique"}.issubset(keys):
        return "column_profile"
    return None


def _normalize_record(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    if kind == "table_profile":
        total = record.get("total_rows")
        unique = record.get("unique_user_id")
        classification = str(record.get("classification") or "").strip()
        if not classification:
            classification = _hard_classification(unique, total)
        return {
            "table_name": str(record.get("table_name") or "").strip(),
            "total_rows": _as_int(total),
            "unique_user_id": None if unique in (None, "") else _as_int(unique),
            "classification": classification,
        }
    if kind == "column_profile":
        return {
            "table_name": str(record.get("table_name") or "").strip(),
            "column_name": str(record.get("column_name") or "").strip(),
            "data_type": str(record.get("data_type") or "").strip(),
            "missing_rate": _as_float(record.get("missing_rate")),
            "n_unique": _as_int(record.get("n_unique")),
            "sample_values": "" if record.get("sample_values") is None else str(record.get("sample_values")),
        }
    return {str(key): _coerce_scalar(val) for key, val in record.items()}


def _hard_classification(unique: Any, total: Any) -> str:
    if unique in (None, ""):
        return "维度表"
    try:
        if int(total) == 0:
            # 0 行空表对用户键无意义，一律降级为维度表（否则 0==0 被误判 1:1 用户表）
            return "维度表"
        return "1:1用户表" if int(unique) == int(total) else "1:N行为表"
    except (TypeError, ValueError):
        return "未知"


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_dump(payload_files: list[Path]) -> dict[str, Any]:
    table_profile: dict[str, dict[str, Any]] = {}
    column_profile: dict[tuple[str, str], dict[str, Any]] = {}

    for path in payload_files:
        if not path.is_file():
            raise SystemExit(f"未找到原始 payload 文件：{path}")
        for payload in _load_text_payloads(path):
            results = _extract_results(payload)
            if results:
                for columns, rows in results:
                    kind = _classify(columns)
                    if kind is None:
                        continue
                    for record in _rows_to_records(columns, rows):
                        _store(kind, record, table_profile, column_profile)
                continue
            # Bare list of row objects without a header.
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                kind = _detect_bare_kind(payload)
                if kind is None:
                    continue
                for record in payload:
                    _store(kind, record, table_profile, column_profile)

    return {
        "table_profile": sorted(table_profile.values(), key=lambda item: item["table_name"]),
        "column_profile": sorted(
            column_profile.values(), key=lambda item: (item["table_name"], item["column_name"])
        ),
    }


def _store(
    kind: str,
    record: dict[str, Any],
    table_profile: dict[str, dict[str, Any]],
    column_profile: dict[tuple[str, str], dict[str, Any]],
) -> None:
    if kind == "table_profile":
        normalized = _normalize_record(kind, record)
        if normalized["table_name"]:
            table_profile[normalized["table_name"]] = normalized
        return
    if kind == "column_profile":
        normalized = _normalize_record(kind, record)
        if normalized["table_name"] and normalized["column_name"]:
            column_profile[(normalized["table_name"], normalized["column_name"])] = normalized
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize step2_0 collect_job payloads into step2_0_profile_dump.json"
    )
    parser.add_argument(
        "--raw",
        type=Path,
        action="append",
        required=True,
        help="raw collect payload file (JSON, JSONL, or truncator text); repeatable",
    )
    parser.add_argument("--out", type=Path, required=True, help="path to step2_0_profile_dump.json")
    args = parser.parse_args()

    dump = build_dump(list(args.raw))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dump, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tables = len(dump["table_profile"])
    columns = len(dump["column_profile"])
    print(f"已写出 {args.out}：table_profile={tables} column_profile={columns}")
    if tables == 0 or columns == 0:
        print("警告：table_profile 或 column_profile 为空，请检查传入的 raw payload", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
