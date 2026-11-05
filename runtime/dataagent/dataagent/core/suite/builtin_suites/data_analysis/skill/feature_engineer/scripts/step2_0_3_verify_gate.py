#!/usr/bin/env python3
"""Gate for step2_0_3: render consistency + semantic-constant validation.

``step2_0_profile_report.md``, ``list_fields.txt`` and ``step2_0_semantic_todo.json``
are all produced deterministically by ``step2_0_2_render_md.py`` from the profile dump,
the meta and the schema. The only edit the Agent may make is the ``constant:`` token on
the rows the render script flagged as semantic candidates
(``constant:TODO`` -> ``constant:semantic`` / ``constant:false``).

So this gate re-renders the markdown in memory from the same inputs and diffs it against
the file on disk, allowing only ``constant:`` token differences, then validates those
token edits:

  * no residual ``constant:TODO``
  * ``constant:semantic`` only on rows the render script flagged as candidates
  * ``constant:true`` / ``constant:false`` consistent with ``n_unique``

On success it writes ``gate_data_verified.txt`` and exits 0. On failure it prints every
offending field and exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from step2_0_2_render_md import _ROW_RE, render  # noqa: E402

_TABLE_HEADING_RE = re.compile(r"^###\s+(?P<table>.+?)（(?P<cols>\d+)\s*列）\s*$")
_CONSTANT_TOKEN_RE = re.compile(r"constant:[^\s,|]+")
_MAX_DIFF_REPORTS = 20


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _normalized_lines(markdown: str) -> list[str]:
    """Markdown lines with the ``constant:`` token masked, so only that value may differ."""
    lines = [_CONSTANT_TOKEN_RE.sub("constant:*", line).rstrip() for line in markdown.splitlines()]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def check_render_consistency(
    disk_md: str,
    rendered_md: str,
    disk_list_fields: str,
    rendered_list_fields: list[str],
    errors: list[str],
) -> None:
    """The disk md must equal a fresh render except for the ``constant:`` token."""
    disk = _normalized_lines(disk_md)
    fresh = _normalized_lines(rendered_md)
    if len(disk) != len(fresh):
        errors.append(
            f"格式: md 行数 {len(disk)} != 重新渲染的 {len(fresh)}"
            "（只允许 constant: 取值差异，禁止改动其他内容）"
        )
    reported = 0
    for index, (got, expected) in enumerate(zip(disk, fresh), start=1):
        if got == expected:
            continue
        errors.append(
            f"格式: md 第 {index} 行与重新渲染不一致\n  文件: {got.strip()}\n  期望: {expected.strip()}"
        )
        reported += 1
        if reported >= _MAX_DIFF_REPORTS:
            errors.append("格式: 差异过多，仅展示前若干行")
            break

    expected_list_fields = "".join(f"{field}\n" for field in rendered_list_fields)
    if disk_list_fields != expected_list_fields:
        errors.append(
            "list-fields: list_fields.txt 与重新渲染结果不一致（该文件由 step2_0_2_render_md.py 产出，禁止手工改动）"
        )


def _parse_rows(markdown: str) -> dict[str, list[dict[str, str]]]:
    rows_by_table: dict[str, list[dict[str, str]]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        heading = _TABLE_HEADING_RE.match(line)
        if heading:
            current = heading.group("table").strip()
            rows_by_table.setdefault(current, [])
            continue
        row = _ROW_RE.match(line)
        if row and current:
            rows_by_table.setdefault(current, []).append(
                {
                    "field": row.group("field").strip(),
                    "constant": row.group("constant").strip(),
                }
            )
    return rows_by_table


def _column_map(dump: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in dump.get("column_profile") or []:
        if isinstance(row, dict) and row.get("table_name") and row.get("column_name"):
            result[(str(row["table_name"]).strip(), str(row["column_name"]).strip())] = row
    return result


def check_semantic_constants(
    disk_md: str,
    candidates: set[tuple[str, str]],
    column_map: dict[tuple[str, str], dict[str, Any]],
    errors: list[str],
) -> None:
    todo_hits: list[str] = []
    semantic_violations: list[str] = []
    for table, rows in _parse_rows(disk_md).items():
        for row in rows:
            field = row["field"]
            constant = row["constant"]
            reference = column_map.get((table, field))
            n_unique = reference.get("n_unique") if reference else None
            if constant == "TODO":
                todo_hits.append(f"{table}.{field}")
            elif constant == "semantic":
                if (table, field) not in candidates:
                    semantic_violations.append(f"{table}.{field}")
            elif constant == "true" and isinstance(n_unique, int) and n_unique > 1:
                errors.append(
                    f"semantic: {table}.{field} 标了 constant:true，但 n_unique={n_unique}（>1）"
                )
            elif constant == "false" and isinstance(n_unique, int) and n_unique <= 1:
                errors.append(
                    f"semantic: {table}.{field} 标了 constant:false，但 n_unique={n_unique}（<=1）"
                )

    if todo_hits:
        errors.append(
            f"semantic: 仍有 {len(todo_hits)} 行是 'constant:TODO'，需你判定：{', '.join(todo_hits)}"
        )
    if semantic_violations:
        errors.append(
            "semantic: 'constant:semantic' 用在了非候选字段上：" + ", ".join(semantic_violations)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated step2_0_3 gate")
    parser.add_argument("--md", type=Path, default=Path("step2_0_profile_report.md"))
    parser.add_argument("--dump", type=Path, required=True, help="step2_0_profile_dump.json")
    parser.add_argument("--meta", type=Path, required=True, help="step1_output_meta.json")
    parser.add_argument("--schema", type=Path, required=True, help="schema_resolution.json")
    parser.add_argument("--marker", type=Path, default=Path("gate_data_verified.txt"))
    parser.add_argument("--list-fields", type=Path, default=Path("list_fields.txt"))
    parser.add_argument("--semantic-candidate-max-unique", type=int, default=10)
    args = parser.parse_args()

    if not args.md.is_file():
        raise SystemExit(f"未找到 markdown 文件：{args.md}")

    disk_md = args.md.read_text(encoding="utf-8")
    dump = _read_json(args.dump)
    meta = _read_json(args.meta)
    schema = _read_json(args.schema)

    rendered_md, semantic_todo, rendered_list_fields = render(
        dump, meta, schema, args.semantic_candidate_max_unique
    )
    disk_list_fields = (
        args.list_fields.read_text(encoding="utf-8") if args.list_fields.is_file() else ""
    )
    candidates = {
        (str(item.get("table") or "").strip(), str(item.get("field") or "").strip())
        for item in semantic_todo
    }

    errors: list[str] = []
    check_render_consistency(
        disk_md, rendered_md, disk_list_fields, rendered_list_fields, errors
    )
    check_semantic_constants(disk_md, candidates, _column_map(dump), errors)

    if errors:
        print("step2_0 门禁未通过：", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        print(
            "\n修正后重跑本脚本。禁止手工创建 gate_data_verified.txt 绕过门禁。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if args.marker.exists():
        args.marker.unlink()
    timestamp = datetime.now(timezone.utc).isoformat()
    args.marker.write_text(f"PASSED: {timestamp}\n", encoding="utf-8")
    print(f"step2_0 门禁通过，已写出 {args.marker}")


if __name__ == "__main__":
    main()
