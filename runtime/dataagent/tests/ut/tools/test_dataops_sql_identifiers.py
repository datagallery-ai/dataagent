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
"""DataOps SQL 标识符 / 分区过滤条件的白名单校验。"""

from __future__ import annotations

import pytest

from dataagent.actions.tools.local_tool.dataops_sql_identifiers import (
    UnsafeIdentifierError,
    is_safe_column_name,
    is_safe_identifier,
    is_safe_table_name,
    quote_column,
    quote_table,
    validate_partition_filter,
)


class TestIdentifierWhitelist:
    @pytest.mark.parametrize(
        "name",
        ["udid", "_x", "a1", "pay_cnt_7d", "A_B_9"],
    )
    def test_accepts_plain_identifiers(self, name):
        assert is_safe_identifier(name)
        assert is_safe_column_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "",           # 空
            "   ",        # 纯空白
            "1abc",       # 数字开头
            "a-b",        # 连字符
            "a b",        # 空格
            "a;b",        # 分号
            "a`b",        # 反引号
            "a.b",        # 带库表前缀（列名不允许）
            "*",          # 通配符
            "db.tbl.col", # 多段
            "a)",         # 括号
            "a/*x*/",     # 注释
            "a\nb",       # 换行
        ],
    )
    def test_rejects_unsafe_columns(self, name):
        assert not is_safe_column_name(name)

    @pytest.mark.parametrize(
        "name",
        ["a", "db.tbl", "db.schema.tbl", "_db._tbl"],
    )
    def test_accepts_multi_segment_tables(self, name):
        assert is_safe_table_name(name)

    @pytest.mark.parametrize(
        "name",
        ["", "  ", "db.", ".tbl", "db..tbl", "a b", "a;b", "a`b", "a-b", "*"],
    )
    def test_rejects_unsafe_tables(self, name):
        assert not is_safe_table_name(name)

    def test_non_string_is_rejected(self):
        assert not is_safe_identifier(None)
        assert not is_safe_column_name(None)
        assert not is_safe_table_name(None)


class TestQuoting:
    def test_quotes_column_with_backticks(self):
        assert quote_column("udid") == "`udid`"

    def test_quotes_table_per_segment(self):
        # 整体加反引号（`db.tbl`）在 Hive 里会被当成单个标识符 —— 必须按段加。
        assert quote_table("db.tbl") == "`db`.`tbl`"
        assert quote_table("db.schema.tbl") == "`db`.`schema`.`tbl`"

    def test_rejects_unsafe_column(self):
        with pytest.raises(UnsafeIdentifierError):
            quote_column("a; DROP TABLE x")

    def test_rejects_unsafe_table(self):
        with pytest.raises(UnsafeIdentifierError):
            quote_table("db.tbl; DROP TABLE x")


class TestPartitionFilter:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("pt_d = '20261020'", "pt_d = '20261020'"),
            ("pt_d='20261020'", "pt_d = '20261020'"),
            ("  pt_d = '20261020'  ", "pt_d = '20261020'"),
            ("ds = '2026-10-20'", "ds = '2026-10-20'"),
        ],
    )
    def test_accepts_the_single_comparison_shape(self, raw, expected):
        assert validate_partition_filter(raw) == expected

    def test_empty_string_is_allowed(self):
        # 空串表达「明确不过滤」—— 那是合法的三态语义。
        assert validate_partition_filter("") == ""
        assert validate_partition_filter("   ") == ""
        assert validate_partition_filter(None) == ""

    @pytest.mark.parametrize(
        "raw",
        [
            "pt_d = '20261020'; DROP TABLE x",
            "pt_d = '20261020' OR 1=1",
            "pt_d = 20261020",            # 没有引号
            "1 = 1",                      # 不是列名比较
            "pt_d = $date",               # 占位符（后端不替换）
            "pt_d = 'a' AND pt_h = 'b'",  # 多条件
            "pt_d = '20261020' -- c",      # 注释
            "pt_d = 'a' UNION SELECT 1",
            "func(pt_d) = 'x'",
        ],
    )
    def test_rejects_anything_else(self, raw):
        with pytest.raises(UnsafeIdentifierError):
            validate_partition_filter(raw)

    def test_rejects_non_string(self):
        with pytest.raises(UnsafeIdentifierError):
            validate_partition_filter(123)
