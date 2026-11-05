#!/usr/bin/env python3
"""Set one ``constant:TODO`` cell in ``step2_0_profile_report.md`` to semantic/false.

Locates the ``### <table>`` section, then the field row inside it, and replaces
``constant:TODO`` with ``constant:semantic`` or ``constant:false``. Exits non-zero
when the section/field is missing or the row is no longer ``TODO``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Contract: section headings must carry a full-width annotation, e.g. ``### foo（N 列）``.
_HEADING_RE = re.compile(r"^###\s+(?P<table>\S+?)（")
_CONSTANT_TODO_RE = re.compile(r"constant:TODO\b")


def set_constant(markdown: str, table: str, field: str, value: str) -> tuple[str, int]:
    """Replace ``constant:TODO`` for *field* under *table*; return (text, replacements)."""
    if value not in {"semantic", "false"}:
        raise SystemExit(f"--value 必须是 semantic 或 false，收到：{value}")

    lines = markdown.splitlines(keepends=True)
    table_name = table.strip()
    field_name = field.strip()

    in_section = False
    section_found = False
    field_line_index: int | None = None

    for index, line in enumerate(lines):
        heading = _HEADING_RE.match(line.rstrip("\n"))
        if heading:
            current = heading.group("table").strip()
            in_section = current == table_name
            if in_section:
                section_found = True
            continue
        if not in_section:
            continue
        # Stop at the next markdown heading of same/higher level.
        if line.startswith("#"):
            in_section = False
            continue
        # Table rows: ``| field | ... | constant:TODO, delimiter:... |``
        if not line.lstrip().startswith("|"):
            continue
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if cells[0] == field_name:
            field_line_index = index
            break

    if not section_found:
        raise SystemExit(f"未找到小节 ### {table_name}（…）")
    if field_line_index is None:
        raise SystemExit(f"在 ### {table_name} 中未找到字段行 {field_name}")

    line = lines[field_line_index]
    if not _CONSTANT_TODO_RE.search(line):
        raise SystemExit(
            f"{table_name}.{field_name} 的分布分析列已不是 constant:TODO（可能已判定）："
            f"{line.strip()}"
        )

    lines[field_line_index] = _CONSTANT_TODO_RE.sub(f"constant:{value}", line, count=1)
    return "".join(lines), 1


def main() -> None:
    parser = argparse.ArgumentParser(description="set one constant:TODO to semantic/false")
    parser.add_argument("--md", type=Path, required=True, help="step2_0_profile_report.md")
    parser.add_argument("--table", required=True, help="table name under ### heading")
    parser.add_argument("--field", required=True, help="field name in the markdown table")
    parser.add_argument("--value", required=True, choices=("semantic", "false"))
    args = parser.parse_args()
    if not args.md.is_file():
        raise SystemExit(f"未找到 {args.md}")
    updated, _count = set_constant(args.md.read_text(encoding="utf-8"), args.table, args.field, args.value)
    args.md.write_text(updated, encoding="utf-8")
    print(f"已更新 {args.md}：{args.table.strip()}.{args.field.strip()} -> constant:{args.value}")


if __name__ == "__main__":
    main()
