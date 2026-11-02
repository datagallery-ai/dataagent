# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Workspace-scoped xlsx tools for spreadsheet evaluation.

Register only from eval YAML via ``TOOLS.local_functions``. Not a default builtin.
Paths go through the same sandbox as ``read_file``. ``execute_xlsx`` injects a
restricted openpyxl facade and a sandbox-backed ``Path``; COM / ``Dispatch`` /
out-of-workspace writes are blocked.
"""

from __future__ import annotations

import builtins as _builtins
import contextlib
import datetime
import io
import types
from pathlib import Path
from typing import Any

from dataagent.actions.tools.local_tool.tools import _resolve_and_authorize

_ALLOWED_SUFFIXES = {".xlsx", ".xlsm"}
_DEFAULT_READ_MAX_CELLS = 4000
_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "copy",
        "datetime",
        "math",
        "re",
        "json",
        "collections",
        "itertools",
        "decimal",
        "statistics",
        "functools",
        "operator",
        "string",
        "numbers",
        "typing",
        "pprint",
        "textwrap",
        "unicodedata",
        "calendar",
        "fractions",
    }
)
_SAFE_BUILTIN_NAMES = (
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "Exception",
    "False",
    "IndexError",
    "KeyError",
    "LookupError",
    "None",
    "RuntimeError",
    "StopIteration",
    "True",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
    "abs",
    "all",
    "any",
    "bool",
    "callable",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "hasattr",
    "hash",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "ord",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "setattr",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
)


def _ensure_openpyxl():
    """Import openpyxl or raise a clear install hint."""
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl is not installed. In runtime/dataagent run: uv pip install '.[wps]'") from exc
    return openpyxl


def _authorize_xlsx(path: str, *, operation: str, mode: str) -> Path:
    """Resolve, authorize, and check that the path is an xlsx/xlsm file."""
    p = _resolve_and_authorize(path, "path", operation=operation, mode=mode)
    if p.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError(f"path must be an .xlsx or .xlsm file, got {p.suffix or 'no suffix'}")
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if p.is_dir():
        raise IsADirectoryError(f"path is a directory, not a workbook: {path}")
    return p


def _load_workbook(path: Path, *, data_only: bool = False):
    """Open a workbook; wrap openpyxl errors. Keep VBA when the file is ``.xlsm``."""
    openpyxl = _ensure_openpyxl()
    kwargs: dict[str, Any] = {"filename": str(path), "data_only": data_only}
    if path.suffix.lower() == ".xlsm":
        kwargs["keep_vba"] = True
    try:
        return openpyxl.load_workbook(**kwargs)
    except Exception as exc:
        raise ValueError(f"failed to open workbook {path}: {exc}") from exc


def _workbook_cell_snapshot(wb: Any) -> tuple[Any, ...]:
    """Capture sheet names and non-empty cell values for dirty detection."""
    sheets: list[tuple[str, tuple[tuple[str, Any], ...]]] = []
    for name in wb.sheetnames:
        ws = wb[name]
        cells = tuple(
            (cell.coordinate, cell.value)
            for row in ws.iter_rows(
                min_row=1,
                max_row=int(ws.max_row or 1),
                min_col=1,
                max_col=int(ws.max_column or 1),
            )
            for cell in row
            if cell.value is not None
        )
        sheets.append((name, cells))
    return tuple(sheets)


def _guard_workbook_save(workbook: Any, default_path: Path | None = None) -> Any:
    """Patch ``workbook.save`` so every path goes through workspace authorization."""
    original_save = workbook.save

    def _guarded_save(filename=None, *args, **kwargs):
        raw = filename if filename is not None else default_path
        if raw is None:
            raise ValueError("save() requires a path under the workspace root")
        target = _resolve_and_authorize(str(raw), "path", operation="execute_xlsx", mode="write")
        if target.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError("save path must be an .xlsx or .xlsm file")
        return original_save(str(target), *args, **kwargs)

    workbook.save = _guarded_save  # type: ignore[method-assign]
    return workbook


class GuardedPath(Path):
    """``pathlib.Path`` whose read/write methods stay inside the workspace."""

    def _authorize(self, *, mode: str) -> Path:
        return _resolve_and_authorize(str(self), "path", operation="execute_xlsx", mode=mode)

    def write_text(self, data, encoding=None, errors=None, newline=None):
        return Path(self._authorize(mode="write")).write_text(data, encoding=encoding, errors=errors, newline=newline)

    def write_bytes(self, data):
        return Path(self._authorize(mode="write")).write_bytes(data)

    def read_text(self, encoding=None, errors=None):
        return Path(self._authorize(mode="read")).read_text(encoding=encoding, errors=errors)

    def read_bytes(self):
        return Path(self._authorize(mode="read")).read_bytes()

    def touch(self, mode=0o666, exist_ok=True):
        return Path(self._authorize(mode="write")).touch(mode=mode, exist_ok=exist_ok)

    def unlink(self, missing_ok=False):
        return Path(self._authorize(mode="write")).unlink(missing_ok=missing_ok)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        return Path(self._authorize(mode="write")).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    def rmdir(self):
        return Path(self._authorize(mode="write")).rmdir()

    def replace(self, target):
        authorized_src = self._authorize(mode="write")
        authorized_dst = _resolve_and_authorize(str(target), "path", operation="execute_xlsx", mode="write")
        return Path(authorized_src).replace(authorized_dst)

    def rename(self, target):
        authorized_src = self._authorize(mode="write")
        authorized_dst = _resolve_and_authorize(str(target), "path", operation="execute_xlsx", mode="write")
        return Path(authorized_src).rename(authorized_dst)

    def chmod(self, mode):
        return Path(self._authorize(mode="write")).chmod(mode)

    def symlink_to(self, target, target_is_directory=False):
        authorized = self._authorize(mode="write")
        return Path(authorized).symlink_to(target, target_is_directory=target_is_directory)

    def hardlink_to(self, target):
        authorized = self._authorize(mode="write")
        return Path(authorized).hardlink_to(target)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        mode_text = str(mode or "r")
        write_mode = any(flag in mode_text for flag in "wax+")
        authorized = self._authorize(mode="write" if write_mode else "read")
        return Path(authorized).open(mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)


def _sheet_used_bounds(ws: Any) -> tuple[int, int, int, int]:
    """Return (min_col, min_row, max_col, max_row) for the used area."""
    dim = getattr(ws, "dimensions", None)
    if dim and ":" in str(dim):
        from openpyxl.utils import range_boundaries

        try:
            return range_boundaries(str(dim))
        except Exception:
            pass
    max_row = int(ws.max_row or 1)
    max_col = int(ws.max_column or 1)
    return 1, 1, max_col, max_row


def _display_cell(value: Any) -> str:
    """Render a cell value for the model-facing table."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat(timespec="seconds")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _is_formula_cell(cell: Any) -> bool:
    """True when openpyxl stored a formula in this cell."""
    if getattr(cell, "data_type", None) == "f":
        return True
    value = getattr(cell, "value", None)
    return isinstance(value, str) and value.startswith("=")


def _render_grid(ws: Any, min_col: int, min_row: int, max_col: int, max_row: int) -> str:
    """Render a TSV grid with a column-letter header and row numbers."""
    from openpyxl.utils import get_column_letter

    header = [""] + [get_column_letter(col) for col in range(min_col, max_col + 1)]
    lines = ["\t".join(header)]
    for row in range(min_row, max_row + 1):
        cells = [str(row)]
        for col in range(min_col, max_col + 1):
            cells.append(_display_cell(ws.cell(row=row, column=col).value))
        lines.append("\t".join(cells))
    return "\n".join(lines)


def _collect_formulas(ws: Any, min_col: int, min_row: int, max_col: int, max_row: int) -> list[str]:
    """List formula cells in the window as ``A1\\t=SUM(...)`` lines."""
    lines: list[str] = []
    for row in range(min_row, max_row + 1):
        for col in range(min_col, max_col + 1):
            cell = ws.cell(row=row, column=col)
            if _is_formula_cell(cell):
                lines.append(f"{cell.coordinate}\t{_display_cell(cell.value)}")
    return lines


def inspect_xlsx(path: str, purpose: str | None = None) -> dict[str, Any]:
    """Inspect workbook structure: sheet names, used range, row/column counts.

    xlsx files are binary; use this instead of read_file to see what is in the
    workbook before reading a region or editing it.

    Args:
        path (str): Absolute path under the workspace root to an .xlsx / .xlsm file.
        purpose (str | None): Brief description of why this workbook is being inspected.
    """
    del purpose
    p = _authorize_xlsx(path, operation="inspect_xlsx", mode="read")
    from openpyxl.utils import get_column_letter

    wb = _load_workbook(p)
    try:
        active_name = wb.active.title
        sheets: list[dict[str, Any]] = []
        lines = [f"file: {p}", f"sheets ({len(wb.sheetnames)}):"]
        for name in wb.sheetnames:
            ws = wb[name]
            min_col, min_row, max_col, max_row = _sheet_used_bounds(ws)
            used = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}"
            rows = max_row - min_row + 1
            cols = max_col - min_col + 1
            sheets.append(
                {
                    "name": name,
                    "used_range": used,
                    "min_row": min_row,
                    "max_row": max_row,
                    "min_column": min_col,
                    "max_column": max_col,
                    "rows": rows,
                    "cols": cols,
                }
            )
            marker = " (active)" if name == active_name else ""
            lines.append(f"  - {name}{marker}  used={used}  rows={rows}  cols={cols}")
        original_msg = "\n".join(lines)
    finally:
        wb.close()

    return {
        "original_msg": original_msg,
        "frontend_msg": f"\n\ninspect_xlsx 工具执行完成\n\n```text\n{original_msg}\n```",
        "data": {"path": str(p), "sheets": sheets, "active": active_name},
    }


def read_xlsx(
    path: str,
    purpose: str | None = None,
    sheet: str | None = None,
    cell_range: str | None = None,
    include_formulas: bool = True,
    max_cells: int | None = None,
) -> dict[str, Any]:
    """Read a sheet region. Returns stored values and, by default, formulas.

    Formula cells show the formula text (not a cached result). Use this to
    understand existing formulas before writing new ones with execute_xlsx.

    Args:
        path (str): Absolute path under the workspace root to an .xlsx / .xlsm file.
        purpose (str | None): Brief description of why this region is being read.
        sheet (str | None): Worksheet name. Defaults to the first sheet.
        cell_range (str | None): Excel range such as A1:D20. Defaults to the used range.
        include_formulas (bool): If true, also list formula cells in the region.
        max_cells (int | None): Maximum cells to return (default 4000). Narrow cell_range if truncated.
    """
    del purpose
    p = _authorize_xlsx(path, operation="read_xlsx", mode="read")
    cap = _DEFAULT_READ_MAX_CELLS if max_cells is None else int(max_cells)
    if cap <= 0:
        raise ValueError("max_cells must be a positive integer.")

    wb = _load_workbook(p)
    try:
        sheet_name = (sheet or "").strip() or wb.sheetnames[0]
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"worksheet {sheet_name!r} not found; available: {wb.sheetnames}")
        ws = wb[sheet_name]

        used_min_col, used_min_row, used_max_col, used_max_row = _sheet_used_bounds(ws)
        if cell_range and str(cell_range).strip():
            from openpyxl.utils import range_boundaries

            try:
                min_col, min_row, max_col, max_row = range_boundaries(str(cell_range).strip())
            except Exception as exc:
                raise ValueError(f"invalid cell_range {cell_range!r}: {exc}") from exc
        else:
            min_col, min_row, max_col, max_row = used_min_col, used_min_row, used_max_col, used_max_row

        width = max_col - min_col + 1
        height = max_row - min_row + 1
        total = width * height
        truncated = False
        if total > cap:
            truncated = True
            height = max(1, cap // max(width, 1))
            max_row = min_row + height - 1
            total = width * height

        from openpyxl.utils import get_column_letter

        shown = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}"
        parts = [
            f"file: {p}",
            f"sheet: {sheet_name}",
            f"range: {shown}",
            f"cells: {total}",
            "",
            _render_grid(ws, min_col, min_row, max_col, max_row),
        ]
        formulas: list[str] = []
        if include_formulas:
            formulas = _collect_formulas(ws, min_col, min_row, max_col, max_row)
            parts.append("")
            if formulas:
                parts.append("formulas:")
                parts.extend(formulas)
            else:
                parts.append("formulas: (none in this range)")
        if truncated:
            parts.append("")
            parts.append(
                f"truncated to {total} cells. Pass a smaller cell_range "
                f"(used range is {get_column_letter(used_min_col)}{used_min_row}:"
                f"{get_column_letter(used_max_col)}{used_max_row})."
            )
        original_msg = "\n".join(parts)
    finally:
        wb.close()

    return {
        "original_msg": original_msg,
        "frontend_msg": f"\n\nread_xlsx 工具执行完成\n\n```text\n{original_msg}\n```",
        "data": {
            "path": str(p),
            "sheet": sheet_name,
            "range": shown,
            "cells": total,
            "truncated": truncated,
            "formula_count": len(formulas),
        },
    }


def execute_xlsx(path: str, code: str, purpose: str | None = None) -> dict[str, Any]:
    """Run Python against a workspace workbook with a restricted openpyxl facade.

    The workbook at ``path`` is loaded as ``wb`` (active sheet as ``ws``).
    After the script finishes, the workbook is saved back only if cells changed.
    Use this for cell edits, formulas, row/column operations, and multi-sheet work.

    Injected names: ``openpyxl`` (``Workbook`` / ``load_workbook`` / ``utils``),
    ``wb``, ``ws``, ``xlsx_path``, ``Path``, ``load_workbook``.
    ``import os`` / ``openpyxl`` / ``subprocess`` / ``win32com`` / ``Dispatch``
    are blocked. ``Path`` and workbook ``save`` / ``load_workbook`` stay inside
    the workspace root.

    Example::

        ws['B7'] = '=A1+A2'
        ws.append([1, 2, 3])

    Args:
        path (str): Absolute path under the workspace root to the workbook to edit.
        code (str): Python script. Do not wrap in markdown fences.
        purpose (str | None): Brief description of the edit.
    """
    normalized_purpose = str(purpose or "").strip()
    source = str(code or "").strip()
    if not source:
        raise ValueError("code must not be empty.")
    if source.startswith("```"):
        raise ValueError("code must be plain Python, not a markdown fenced block.")

    p = _authorize_xlsx(path, operation="execute_xlsx", mode="write")
    openpyxl = _ensure_openpyxl()
    wb = _guard_workbook_save(_load_workbook(p), default_path=p)
    before = _workbook_cell_snapshot(wb)
    captured = io.StringIO()

    def _safe_print(*args: Any, **kwargs: Any) -> None:
        kwargs = dict(kwargs)
        kwargs["file"] = captured
        _builtins.print(*args, **kwargs)

    def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = str(name).split(".", 1)[0]
        if root not in _ALLOWED_IMPORT_ROOTS:
            raise ImportError(
                f"import of {name!r} is not allowed in execute_xlsx. "
                "Use the injected openpyxl workbook (wb / ws) and save in place."
            )
        return _builtins.__import__(name, globals, locals, fromlist, level)

    def _safe_open(file, mode="r", *args, **kwargs):
        mode_text = str(mode or "r")
        write_mode = any(flag in mode_text for flag in "wax+")
        authorized = _resolve_and_authorize(
            str(file),
            "path",
            operation="execute_xlsx",
            mode="write" if write_mode else "read",
        )
        return _builtins.open(authorized, mode, *args, **kwargs)

    def _safe_load_workbook(filename, *args, **kwargs):
        del args
        authorized = _authorize_xlsx(str(filename), operation="execute_xlsx", mode="read")
        loaded = _load_workbook(authorized, data_only=bool(kwargs.get("data_only", False)))
        return _guard_workbook_save(loaded)

    def _workbook_factory(*args, **kwargs):
        return _guard_workbook_save(openpyxl.Workbook(*args, **kwargs))

    def _save_to_target(workbook: Any, target: Path) -> None:
        _guard_workbook_save(workbook)
        workbook.save(str(target))

    openpyxl_proxy = types.SimpleNamespace(
        Workbook=_workbook_factory,
        load_workbook=_safe_load_workbook,
        utils=openpyxl.utils,
    )

    safe_builtins: dict[str, Any] = {name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES}
    safe_builtins["print"] = _safe_print
    safe_builtins["open"] = _safe_open
    safe_builtins["__import__"] = _restricted_import
    safe_builtins["__build_class__"] = _builtins.__build_class__
    safe_builtins["__name__"] = "__xlsx__"

    namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "openpyxl": openpyxl_proxy,
        "load_workbook": _safe_load_workbook,
        "Path": GuardedPath,
        "xlsx_path": str(p),
        "wb": wb,
        "ws": wb.active,
    }

    try:
        compiled = compile(source, "<execute_xlsx>", "exec")
        exec(compiled, namespace, namespace)  # noqa: S102 — intentional restricted exec
        final_wb = namespace.get("wb", wb)
        if final_wb is None:
            raise RuntimeError("workbook object wb is missing after execute_xlsx")
        mutated = final_wb is not wb or _workbook_cell_snapshot(final_wb) != before
        if mutated:
            _save_to_target(final_wb, p)
            status = f"saved: {p}"
        else:
            status = f"unchanged: {p}"
    except Exception:
        with contextlib.suppress(Exception):
            wb.close()
        raise
    else:
        with contextlib.suppress(Exception):
            wb.close()

    printed = captured.getvalue().rstrip()
    parts = [status]
    if printed:
        parts.append(printed)
    original_msg = "\n".join(parts)
    purpose_note = f" — {normalized_purpose}" if normalized_purpose else ""
    return {
        "original_msg": original_msg,
        "frontend_msg": f"\n\nexecute_xlsx 工具执行完成{purpose_note}\n\n```text\n{original_msg}\n```",
        "data": {"path": str(p), "printed": printed, "saved": mutated},
    }
