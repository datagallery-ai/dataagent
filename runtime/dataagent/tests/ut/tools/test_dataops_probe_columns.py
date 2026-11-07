# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for the ``columns`` contract of ``dataops_probe_table``.

DataOps does **not** support ``SELECT *`` — every probe query must name its
columns explicitly. These tests pin down the three layers that enforce that:

1. the **schema** the LLM sees (``columns`` is required, and its description
   says so — the description is what GLM-class models actually read);
2. the **runtime validation** (missing / empty / malformed columns are
   rejected instead of silently degrading to ``SELECT *``);
3. the **SQL builder** (no ``*`` fallback branch survives).

They also cover the docstring-parser hardening in
``ToolSchema._parse_docstring_param_descriptions``, which used to truncate
parameter descriptions and inject phantom CJK parameters.
"""

from __future__ import annotations

import pytest

from dataagent.actions.tools.local import LocalToolWrapper
from dataagent.actions.tools.local_tool.dataops_probe_tool import (
    _build_probe_sql,
    _is_safe_column_name,
    dataops_probe_table,
)
from dataagent.core.managers.action_manager.schemas import ToolSchema

# ============================================================================
# 1. LLM-facing schema
# ============================================================================


class TestProbeColumnsSchema:
    def _schema(self) -> dict:
        wrapper = LocalToolWrapper(dataops_probe_table, name="dataops_probe_table")
        return wrapper.to_langchain_tool().args_schema.model_json_schema()

    def test_columns_is_required(self):
        """``columns`` must be in the ``required`` list — the LLM has to pass it."""
        assert "columns" in self._schema()["required"]

    def test_columns_is_a_plain_string_array(self):
        """No ``anyOf: [..., null]`` — the parameter is not optional any more."""
        columns = self._schema()["properties"]["columns"]
        assert columns["type"] == "array"
        assert columns["items"] == {"type": "string"}
        assert "anyOf" not in columns

    def test_columns_description_reaches_the_llm(self):
        """Per-parameter descriptions must survive into the LLM-bound schema.

        This is the actual root cause of the model omitting ``columns``: the
        description used to be dropped by ``to_pydantic_model()``, so the model
        only saw a bare ``{"type": "array"}`` with no hint that the value was
        mandatory.
        """
        desc = self._schema()["properties"]["columns"]["description"]
        assert desc
        assert "SELECT *" in desc

    def test_tool_description_does_not_advertise_select_star(self):
        """The old docstring literally said ``None 或空列表 → SELECT *``."""
        schema = ToolSchema.from_function(dataops_probe_table)
        assert "SELECT *" not in schema.description
        for param in schema.parameters:
            assert "→ ``SELECT *``" not in param.description

    def test_table_description_is_not_truncated(self):
        """Regression: the CJK continuation line used to eat the tail of the
        ``table`` description and spawn a phantom ``多段`` parameter."""
        params = {p.name: p for p in ToolSchema.from_function(dataops_probe_table).parameters}
        assert "多段" not in params
        assert "白名单" in params["table"].description


# ============================================================================
# 2. Runtime validation
# ============================================================================


class TestProbeColumnValidation:
    def test_none_is_rejected(self):
        assert _is_safe_column_name(None) is False

    def test_empty_string_is_rejected(self):
        assert _is_safe_column_name("") is False
        assert _is_safe_column_name("   ") is False

    def test_plain_column_is_accepted(self):
        assert _is_safe_column_name("user_id") is True
        assert _is_safe_column_name("col_a") is True

    def test_star_is_rejected(self):
        assert _is_safe_column_name("*") is False

    def test_qualified_column_is_rejected(self):
        """``db.tbl.col`` would be quoted as `` `db.tbl.col` `` — unparsable."""
        assert _is_safe_column_name("db.tbl.col") is False
        assert _is_safe_column_name("tbl.col") is False

    def test_leading_digit_is_rejected(self):
        assert _is_safe_column_name("1col") is False

    def test_injection_attempt_is_rejected(self):
        assert _is_safe_column_name("col`; DROP TABLE t; --") is False


# ============================================================================
# 3. SQL builder
# ============================================================================


class TestProbeSqlHasNoSelectStar:
    def test_empty_columns_raise_instead_of_emitting_star(self):
        """The ``else "*"`` fallback is what silently produced ``SELECT *``."""
        with pytest.raises(ValueError):
            _build_probe_sql("db.t", [], 5, "pt_d = '20261012'")

    def test_none_columns_raise(self):
        with pytest.raises(ValueError):
            _build_probe_sql("db.t", None, 5, "pt_d = '20261012'")

    def test_explicit_columns_render_select_list(self):
        sql = _build_probe_sql("db.t", ["a", "b"], 5, "pt_d = '20261012'")
        assert sql == "SELECT `a`, `b` FROM `db`.`t` WHERE pt_d = '20261012' LIMIT 5"
        assert "*" not in sql


# ============================================================================
# 4. Docstring param parser hardening
# ============================================================================


class TestDocstringParamParser:
    def test_cjk_continuation_line_is_not_a_phantom_param(self):
        args = "\n".join(
            [
                "table: 目标表名。",
                "多段(用 . 分隔)时每段都需通过白名单。",
                "columns: 列名清单。",
            ]
        )
        parsed = ToolSchema._parse_docstring_param_descriptions(args)
        assert "多段" not in parsed
        assert set(parsed) == {"table", "columns"}

    def test_cjk_continuation_line_does_not_truncate_previous_param(self):
        args = "\n".join(
            [
                "table: 目标表名。",
                "多段(用 . 分隔)时每段都需通过白名单。",
            ]
        )
        parsed = ToolSchema._parse_docstring_param_descriptions(args)
        assert "白名单" in parsed["table"]

    def test_unclosed_parenthesis_does_not_crash(self):
        """The old parser raised ``ValueError`` on a continuation line with an
        unclosed ``(`` — which broke ``ToolSchema.from_function`` entirely."""
        args = "\n".join(
            [
                "path: Target path.",
                "state (including HITL requests whose ToolMessage hasn't arrived",
            ]
        )
        parsed = ToolSchema._parse_docstring_param_descriptions(args)
        assert "path" in parsed

    @pytest.mark.parametrize(
        "line,expected_name",
        [
            ("a (str): The first parameter.", "a"),
            ("b (int): The second parameter.", "b"),
            ("c: The third parameter.", "c"),
        ],
    )
    def test_supported_param_header_formats(self, line, expected_name):
        parsed = ToolSchema._parse_docstring_param_descriptions(line)
        assert expected_name in parsed
