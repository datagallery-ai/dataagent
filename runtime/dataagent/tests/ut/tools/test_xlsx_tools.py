# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# limitations under the License.
# ============================================================================
"""Unit tests for inspect_xlsx / read_xlsx / execute_xlsx."""

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from dataagent.actions.tools.local_tool import xlsx_tools
from dataagent.actions.tools.local_tool.sandbox import (
    NoopSandbox,
    WorkspaceAccessError,
    reset_current_sandbox,
    set_current_sandbox,
)


@pytest.fixture()
def workspace(tmp_path: Path):
    """Bind a sandbox rooted at tmp_path."""
    root = tmp_path.resolve()
    token = set_current_sandbox(NoopSandbox(workspace_root=root))
    yield root
    reset_current_sandbox(token)


def _make_workbook(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Main"
    ws["A1"] = "item"
    ws["B1"] = "qty"
    ws["A2"] = "apple"
    ws["B2"] = 2
    ws["C2"] = "=B2*10"
    extra = wb.create_sheet("Extra")
    extra["A1"] = "ok"
    wb.save(path)
    wb.close()
    return path


class TestInspectXlsx:
    def test_lists_sheets_and_used_range(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        result = xlsx_tools.inspect_xlsx(str(path), purpose="structure")
        assert "Main" in result["original_msg"]
        assert "Extra" in result["original_msg"]
        names = [sheet["name"] for sheet in result["data"]["sheets"]]
        assert names == ["Main", "Extra"]
        assert result["data"]["active"] == "Main"

    def test_rejects_outside_workspace(self, workspace: Path, tmp_path: Path):
        outsider = tmp_path.parent / "outside.xlsx"
        _make_workbook(outsider)
        with pytest.raises(WorkspaceAccessError):
            xlsx_tools.inspect_xlsx(str(outsider))


class TestReadXlsx:
    def test_returns_values_and_formulas(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        result = xlsx_tools.read_xlsx(str(path), sheet="Main", cell_range="A1:C2")
        message = result["original_msg"]
        assert "apple" in message
        assert "=B2*10" in message
        assert result["data"]["formula_count"] == 1

    def test_unknown_sheet_lists_available(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(ValueError, match="Extra"):
            xlsx_tools.read_xlsx(str(path), sheet="Missing")


class TestExecuteXlsx:
    def test_writes_formula_and_saves(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        result = xlsx_tools.execute_xlsx(
            str(path),
            "ws['C3'] = '=B2+1'\nprint(ws['A2'].value)",
            purpose="add formula",
        )
        assert "saved:" in result["original_msg"]
        assert "apple" in result["original_msg"]
        wb = load_workbook(path)
        try:
            assert wb["Main"]["C3"].value == "=B2+1"
        finally:
            wb.close()

    def test_blocks_os_import(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(ImportError, match="not allowed"):
            xlsx_tools.execute_xlsx(str(path), "import os")

    def test_blocks_dispatch_import(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(ImportError, match="not allowed"):
            xlsx_tools.execute_xlsx(str(path), "from win32com.client import Dispatch")

    def test_blocks_save_outside_workspace(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(WorkspaceAccessError):
            xlsx_tools.execute_xlsx(str(path), "wb.save('/tmp/xlsx_tools_escape.xlsx')")

    def test_blocks_path_write_text_outside_workspace(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(WorkspaceAccessError):
            xlsx_tools.execute_xlsx(
                str(path),
                "Path('/tmp/xlsx_tools_escape.txt').write_text('pwn')",
            )

    def test_blocks_new_workbook_save_outside_workspace(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(WorkspaceAccessError):
            xlsx_tools.execute_xlsx(
                str(path),
                "openpyxl.Workbook().save('/tmp/xlsx_tools_escape.xlsx')",
            )

    def test_blocks_openpyxl_import(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        with pytest.raises(ImportError, match="not allowed"):
            xlsx_tools.execute_xlsx(str(path), "import openpyxl")

    def test_skips_save_when_script_does_not_modify(self, workspace: Path):
        path = _make_workbook(workspace / "book.xlsx")
        original = path.read_bytes()
        result = xlsx_tools.execute_xlsx(str(path), "print(ws['A1'].value)")
        assert "unchanged:" in result["original_msg"]
        assert result["data"]["saved"] is False
        assert path.read_bytes() == original

    def test_loads_xlsm_with_keep_vba(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        path = _make_workbook(workspace / "macro.xlsm")
        captured: dict[str, object] = {}
        real_loader = xlsx_tools._ensure_openpyxl().load_workbook

        def wrapped(**kwargs):
            captured.update(kwargs)
            return real_loader(**kwargs)

        monkeypatch.setattr(xlsx_tools._ensure_openpyxl(), "load_workbook", wrapped)
        xlsx_tools.inspect_xlsx(str(path))
        assert captured.get("keep_vba") is True
