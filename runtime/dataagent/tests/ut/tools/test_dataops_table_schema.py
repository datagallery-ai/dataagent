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
"""Unit tests for :mod:`dataagent.actions.tools.local_tool.dataops_table_schema`.

The contracts under test:

1. **User-supplied fields** — the escape hatch for "the table is not in the
   metadata but it does exist in the database".  The user hands the tool the
   column list (or the DDL) and the validation must proceed.
2. **Source priority** — user fields > user DDL > semantic metadata.
3. **Field-name validation** — column names are whitelisted (they end up
   interpolated into SQL), and the errors are human-readable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.local_tool import dataops_table_schema as schema


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.runtime = None
    return ctx


_DDL = (
    "CREATE TABLE biads.ads_x ("
    "  udid STRING COMMENT 'user id',"
    "  pay_cnt BIGINT COMMENT 'pay count'"
    ") PARTITIONED BY (pt_d STRING)"
)


# ============================================================================
# User field validation
# ============================================================================
class TestValidateUserFields:
    def test_accepts_a_plain_list(self):
        assert schema.validate_user_fields(["udid", "pay_cnt"]) == ["udid", "pay_cnt"]

    def test_accepts_a_comma_separated_string(self):
        """LLMs frequently pass a string instead of a list."""
        assert schema.validate_user_fields("udid, pay_cnt") == ["udid", "pay_cnt"]

    def test_accepts_whitespace_and_newline_separated_string(self):
        assert schema.validate_user_fields("udid pay_cnt\ngmv") == ["udid", "pay_cnt", "gmv"]

    def test_deduplicates_case_insensitively(self):
        assert schema.validate_user_fields(["udid", "UDID"]) == ["udid"]

    def test_strips_backticks(self):
        assert schema.validate_user_fields(["`udid`"]) == ["udid"]

    def test_none_is_empty(self):
        assert schema.validate_user_fields(None) == []

    def test_empty_list_is_empty(self):
        assert schema.validate_user_fields([]) == []

    def test_rejects_a_star(self):
        """``SELECT *`` is hard-blocked by the DataOps platform."""
        with pytest.raises(schema.UserFieldError, match="不能是 `*`"):
            schema.validate_user_fields(["*"])

    def test_rejects_a_qualified_column_name(self):
        """``db.tbl.col`` would be back-quoted verbatim and fail to parse."""
        with pytest.raises(schema.UserFieldError, match="不能带库/表前缀"):
            schema.validate_user_fields(["biads.ads_x.udid"])

    def test_rejects_an_illegal_identifier(self):
        with pytest.raises(schema.UserFieldError, match="非法字段名"):
            schema.validate_user_fields(["udid; drop table x"])

    def test_rejects_a_non_string_item(self):
        with pytest.raises(schema.UserFieldError, match="必须是字符串"):
            schema.validate_user_fields([123])

    def test_rejects_a_wrong_container_type(self):
        with pytest.raises(schema.UserFieldError, match="必须是字段名列表"):
            schema.validate_user_fields({"udid": 1})

    def test_rejects_too_many_fields(self):
        with pytest.raises(schema.UserFieldError, match="超过上限"):
            schema.validate_user_fields([f"c{i}" for i in range(schema._MAX_USER_FIELDS + 1)])


# ============================================================================
# Source priority
# ============================================================================
class TestResolveColumns:
    def test_user_fields_win_over_semantic(self):
        """The user's own column list beats whatever the metadata says."""
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            return_value=(["semantic_col"], {"semantic_col": "string"}, ()),
        ):
            fields, types, _pks, source = schema.resolve_columns(
                target_table="biads.ads_x",
                user_fields=["udid", "pay_cnt"],
                _tool_context=_ctx(),
            )

        assert fields == ["udid", "pay_cnt"]
        assert source == "user_fields"

    def test_user_fields_survive_a_missing_metadata_table(self):
        """The whole point: metadata has no such table, yet the check can run."""
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            side_effect=schema.SchemaResolutionError("表 `biads.ads_x` 在元数据中不存在"),
        ):
            fields, _types, _pks, source = schema.resolve_columns(
                target_table="biads.ads_x",
                user_fields=["udid", "pay_cnt"],
                _tool_context=_ctx(),
            )

        assert fields == ["udid", "pay_cnt"]
        assert source == "user_fields"

    def test_user_ddl_is_used_when_metadata_misses(self):
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            side_effect=schema.SchemaResolutionError("表不存在"),
        ):
            fields, types, _pks, source = schema.resolve_columns(
                target_table="biads.ads_x",
                ddl_sql=_DDL,
                _tool_context=_ctx(),
            )

        assert source == "ddl"
        assert fields == ["udid", "pay_cnt"]
        # 列类型来自 DDL —— 它决定判空写法（STRING 用 bicoredata.IsEmpty）。
        assert types["udid"] == "string"
        assert types["pay_cnt"] == "bigint"

    def test_user_ddl_supplies_types_for_user_fields(self):
        """fields + ddl → the field list wins, the types still come from the DDL."""
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            side_effect=schema.SchemaResolutionError("表不存在"),
        ):
            fields, types, _pks, source = schema.resolve_columns(
                target_table="biads.ads_x",
                user_fields=["udid"],
                ddl_sql=_DDL,
                _tool_context=_ctx(),
            )

        assert source == "user_fields"
        assert fields == ["udid"]
        assert types == {"udid": "string"}

    def test_user_ddl_types_match_case_insensitively(self):
        """DDL may spell the column differently from what the user typed."""
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            side_effect=schema.SchemaResolutionError("表不存在"),
        ):
            _fields, types, _pks, _source = schema.resolve_columns(
                target_table="biads.ads_x",
                user_fields=["UDID"],
                ddl_sql=_DDL,
                _tool_context=_ctx(),
            )

        assert types == {"UDID": "string"}

    def test_semantic_is_the_last_resort(self):
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            return_value=(["udid"], {"udid": "string"}, ("udid",)),
        ):
            fields, _types, pks, source = schema.resolve_columns(
                target_table="biads.ads_x",
                _tool_context=_ctx(),
            )

        assert source == "semantic"
        assert fields == ["udid"]
        assert pks == ("udid",)

    def test_all_sources_failing_raises(self):
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            side_effect=schema.SchemaResolutionError("表不存在"),
        ), pytest.raises(schema.SchemaResolutionError):
            schema.resolve_columns(
                target_table="biads.ads_x",
                _tool_context=_ctx(),
            )

    def test_invalid_user_fields_raise_user_field_error(self):
        with pytest.raises(schema.UserFieldError):
            schema.resolve_columns(
                target_table="biads.ads_x",
                user_fields=["*"],
                _tool_context=_ctx(),
            )

    def test_semantic_primary_keys_are_ignored_when_the_table_is_absent(self):
        """A missing table must not produce a bogus primary key."""
        with patch.object(
            schema,
            "fetch_semantic_columns_and_primary_keys",
            side_effect=schema.SchemaResolutionError("表不存在"),
        ), patch.object(
            schema,
            "fetch_semantic_primary_key_columns",
            return_value=(),
        ):
            _fields, _types, pks, _source = schema.resolve_columns(
                target_table="biads.ads_x",
                user_fields=["udid"],
                _tool_context=_ctx(),
            )

        assert pks == ()
