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
"""Unit tests for :mod:`dataagent.actions.tools.local_tool.dataops_quality_check`.

The module owns the extensible data quality checker registry (null-rate and
single-value coverage). These tests pin down the four contracts that matter:

1. Field extraction from ``INSERT ... SELECT`` (aliases, constants, expressions)
2. SQL generation — one statement, one row, one alias per checker/field pair
3. Result parsing — threshold comparison in both directions
4. Configuration resolution — ``tool_config`` > resource metadata > env > default
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import dataops_quality_check as qc
from dataagent.actions.tools.local_tool.dataops_validate_tool import (
    _extract_fields_from_insert_sql,
)

# Re-exported for convenience — the field extractor lives in the validate tool
# (it is shared with the INSERT rewrite) while everything else lives here.
_EXCLUDE = qc.DEFAULT_EXCLUDE_FIELDS


def _extract(sql: str) -> list[str]:
    return _extract_fields_from_insert_sql(sql, _EXCLUDE)


# ============================================================================
# _extract_fields_from_insert_sql
# ============================================================================
class TestExtractFields:
    def test_basic_fields(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT col1, col2, col3 FROM src"
        assert _extract(sql) == ["col1", "col2", "col3"]

    def test_aliases_with_as(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT col1 AS c1, col2 AS c2, col3 FROM src"
        assert _extract(sql) == ["c1", "c2", "col3"]

    def test_aliases_without_as(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT col1 c1, col2 c2, col3 FROM src"
        assert _extract(sql) == ["c1", "c2", "col3"]

    def test_filters_excluded_fields(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT pt_d, col1, udid, create_time FROM src"
        assert _extract(sql) == ["col1"]

    def test_skips_constant_with_alias(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT 1 AS dummy, col1 FROM src"
        assert _extract(sql) == ["col1"]

    def test_keeps_expression_alias(self):
        """带别名的表达式是目标表里真实存在的列，必须纳入校验。"""
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT col1 + col2 AS sum_val, col3 FROM src"
        assert _extract(sql) == ["sum_val", "col3"]

    def test_keeps_aggregate_alias(self):
        """聚合列（最常见）同样要校验：别名就是临时表里的列名。"""
        sql = (
            "INSERT OVERWRITE TABLE adhoctemp.tmp "
            "SELECT uid, SUM(IF(flag = '1', 1, 0)) AS cnt, COUNT(1) AS total FROM src"
        )
        assert _extract(sql) == ["uid", "cnt", "total"]

    def test_skips_unaliased_expression(self):
        """没有别名的表达式推不出列名，跳过。"""
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT col1 + col2 FROM src"
        assert _extract(sql) == []

    def test_cte_projection_uses_insert_columns(self):
        """WITH CTE 时必须取 INSERT 自己的投影，而不是 CTE 的列。

        CTE 的列在目标表里不存在；INSERT 的投影才是临时表里落地的列。
        """
        sql = (
            "WITH filtered AS (SELECT uid, flag FROM src WHERE pt_d = '$date') "
            "INSERT OVERWRITE TABLE biads.t PARTITION(pt_d = '$date') "
            "SELECT uid, SUM(IF(flag = '1', 1, 0)) AS cnt FROM filtered GROUP BY uid"
        )
        assert _extract(sql) == ["uid", "cnt"]

    def test_create_sql_returns_empty(self):
        sql = "CREATE TABLE adhoctemp.tmp (col1 string, col2 int)"
        assert _extract(sql) == []

    def test_dedup_preserves_order(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp SELECT col1 AS c, col1 AS c, col2 FROM src"
        assert _extract(sql) == ["c", "col2"]


# ============================================================================
# Checker registry
# ============================================================================
class TestCheckerRegistry:
    def test_default_builtin_checkers(self):
        assert [c.name for c in qc._BUILTIN_CHECKERS] == [
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
        ]

    def test_select_checkers_by_name(self):
        selected = qc.select_checkers(["null_rate"])
        assert [c.name for c in selected] == ["null_rate"]

    def test_select_checkers_unknown_name_falls_back_to_all(self):
        selected = qc.select_checkers(["does_not_exist"])
        assert [c.name for c in selected] == [
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
        ]

    def test_select_checkers_none_returns_all(self):
        assert [c.name for c in qc.select_checkers(None)] == [
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
        ]

    def test_alias_format(self):
        assert qc.NULL_RATE_CHECKER.alias("col1") == "null_rate__col1"
        assert qc.SINGLE_VALUE_CHECKER.alias("col1") == "single_value_ratio__col1"
        assert qc.OUTLIER_RATIO_CHECKER.alias("col1") == "outlier_ratio__col1"

    def test_column_is_backtick_quoted(self):
        ctx = qc.CheckContext(
            temp_table="adhoctemp.tmp_x",
            fields=["col1"],
            checkers=qc._BUILTIN_CHECKERS,
        )
        assert ctx.column("col1") == "`col1`"

    def test_column_does_not_double_quote(self):
        ctx = qc.CheckContext(
            temp_table="adhoctemp.tmp_x",
            fields=["col1"],
            checkers=qc._BUILTIN_CHECKERS,
        )
        assert ctx.column("`col1`") == "`col1`"


# ============================================================================
# SQL generation — 一个 checker 一条 SQL
# ============================================================================
class TestBuildQualityCheckSqls:
    def test_one_statement_per_checker(self):
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1", "x_ratio"], "pt_d", "20261010", 10000
        )
        assert [name for name, _ in statements] == [
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
        ]

    def test_no_union_all_monster(self):
        """不能再出现「checker × 字段」的 UNION ALL 大查询。"""
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1", "col2", "col3"], "pt_d", "20261010", 10000
        )
        for _name, sql in statements:
            assert "UNION ALL" not in sql
            assert "_per_field" not in sql

    def test_null_rate_sql_matches_reference_shape(self):
        """空值率 SQL 对齐业务示例：SUM(IF(有值,1,0))/COUNT(1)。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )[0][1]
        assert "SUM(IF(`col1` IS NOT NULL, 1, 0)) / COUNT(1) AS col1" in sql
        assert "FROM adhoctemp.tmp_x" in sql
        assert "WHERE pt_d = '20261010'" in sql
        assert "LIMIT 10000" in sql

    def test_single_value_ratio_sql_matches_reference_shape(self):
        """单一值比率 SQL 对齐业务示例：max(各值次数)/总样本数。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )[1][1]
        assert "SELECT MAX(cnt) FROM (SELECT COUNT(*) AS cnt FROM" in sql
        assert "GROUP BY `col1`) AS t) / COUNT(1), 4) AS col1" in sql

    def test_no_cte_and_no_bare_alias_in_from(self):
        """生产 adHoc 不认 CTE —— 也不能在 FROM 位置裸写别名。

        生产解析阶段会把 ``WITH _src AS (...)`` 里的 ``FROM _src`` 当成
        **物理表名**，报 ``table(s) agapdw._src do not exist``。
        派生表是唯一安全的写法：``FROM (SELECT ...) AS _src``。
        """
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1", "x_ratio"], "pt_d", "20261010", 10000
        )
        for _name, sql in statements:
            assert "WITH " not in sql.upper()
            # FROM 后面必须跟 '(' （派生表）或真实表名，不能是裸别名。
            assert "FROM _src" not in sql
            assert "FROM _s " not in sql
            assert "FROM _s\n" not in sql

    def test_limit_lives_inside_derived_table(self):
        """LIMIT 必须落在派生表里才真正限制扫描量。

        ``SELECT COUNT(1) FROM t WHERE ... LIMIT 10000`` 的 LIMIT 作用在
        **聚合结果**上（1 行），全表照样扫 —— 采样是假的。
        正确写法是把 LIMIT 放进派生表：``FROM (SELECT ... LIMIT 10000) AS _src``。
        """
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )
        for _name, sql in statements:
            assert "LIMIT 10000" in sql
            # LIMIT 出现时，括号还没闭合 → 说明它在派生表内部。
            head = sql[: sql.index("LIMIT 10000")]
            assert head.count("(") > head.count(")")

    def test_every_from_targets_a_real_table_or_inline_derived_table(self):
        """生产 adHoc 的表名解析会把 FROM 后的名字当物理表去找。

        所以 FROM 后面只能是：真实表名，或内联派生表 ``(SELECT ...) AS alias``；
        绝不能是裸别名（``FROM _src``）—— 那会被解析成
        ``agapdw._src`` 这种不存在的物理表。
        """
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1", "x_ratio"], "pt_d", "20261010", 10000
        )
        for _name, sql in statements:
            for after_from in re.findall(r"\bFROM\s+([^\s(]+)", sql, flags=re.IGNORECASE):
                # 只允许真实表名（adhoctemp.*）出现在 FROM 后面。
                assert after_from.startswith("adhoctemp."), (
                    f"FROM 后面出现了非物理表引用: {after_from!r}"
                )

    def test_outlier_ratio_sql_matches_reference_shape(self):
        """异常值占比 SQL 对齐业务示例：SUM(IF(比率<0 OR 比率>1,1,0))/COUNT(1)。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["x_ratio"], "pt_d", "20261010", 10000
        )[2][1]
        assert "SUM(IF((`x_ratio` < 0 OR `x_ratio` > 1), 1, 0)) / COUNT(1) AS x_ratio" in sql

    def test_string_field_uses_is_empty_udf(self):
        """STRING 存储的字段用 bicoredata.IsEmpty 判空（空字符串也算空）。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x",
            ["col1"],
            "pt_d",
            "20261010",
            10000,
            column_types={"col1": "string"},
        )[0][1]
        assert "SUM(IF(!bicoredata.IsEmpty(`col1`), 1, 0)) / COUNT(1) AS col1" in sql

    def test_numeric_field_uses_is_not_null(self):
        """数值存储的字段不适用 IsEmpty，用 IS NOT NULL。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x",
            ["cnt"],
            "pt_d",
            "20261010",
            10000,
            column_types={"cnt": "bigint"},
        )[0][1]
        assert "IS NOT NULL" in sql
        assert "bicoredata.IsEmpty" not in sql

    def test_all_fields_in_one_statement(self):
        """一条 SQL 里一次算完所有字段（条件聚合，不做字段×校验项展开）。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1", "col2", "col3"], "pt_d", "20261010", 10000
        )[0][1]
        for field in ("col1", "col2", "col3"):
            assert f"AS {field}" in sql

    def test_empty_fields_returns_no_statements(self):
        assert qc.build_quality_check_sqls("adhoctemp.tmp_x", [], "pt_d", "20261010", 10000) == []

    def test_empty_checkers_returns_no_statements(self):
        assert qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000, checkers=()
        ) == []

    def test_single_checker_limits_statements(self):
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x",
            ["col1"],
            "pt_d",
            "20261010",
            10000,
            checkers=(qc.NULL_RATE_CHECKER,),
        )
        assert [name for name, _ in statements] == ["null_rate"]

    def test_deprecated_single_sql_returns_first_statement(self):
        """旧的单 SQL 入口保留兼容 —— 返回第一条 checker 的 SQL。"""
        sql = qc.build_quality_check_sql("adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000)
        assert sql == qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )[0][1]

    def test_outlier_ratio_skips_non_ratio_fields(self):
        """非比率类字段不出现在异常值占比 SQL 里。"""
        sql = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["x_ratio", "pay_cnt"], "pt_d", "20261010", 10000
        )[2][1]
        assert "x_ratio" in sql
        assert "pay_cnt" not in sql

    def test_outlier_ratio_statement_omitted_without_ratio_fields(self):
        """没有比率类字段时，异常值占比这条 SQL 不生成（也就不会执行）。"""
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["pay_cnt"], "pt_d", "20261010", 10000
        )
        assert [name for name, _ in statements] == ["null_rate", "single_value_ratio"]


# ============================================================================
# DDL column type parsing
# ============================================================================
class TestParseDdlColumnTypes:
    def test_parses_hive_ddl(self):
        ddl = (
            "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_foo (\n"
            "  udid_sha256 STRING COMMENT 'sha256',\n"
            "  retain_cnt_30d BIGINT COMMENT '次数',\n"
            "  retain_ratio_30d DECIMAL(38,6) COMMENT '比率'\n"
            ") COMMENT 'xx'\n"
            "PARTITIONED BY (pt_d STRING COMMENT '天分区')\n"
            "STORED AS ORC;"
        )
        types = qc.parse_ddl_column_types(ddl)
        assert types["udid_sha256"] == "string"
        assert types["retain_cnt_30d"] == "bigint"
        assert types["retain_ratio_30d"] == "decimal"

    def test_empty_ddl_returns_empty(self):
        assert qc.parse_ddl_column_types("") == {}

    def test_ddl_without_columns_returns_empty(self):
        assert qc.parse_ddl_column_types("CREATE TABLE t") == {}


# ============================================================================
# Result parsing
# ============================================================================
class TestParseCheckerResult:
    """单个 checker 的结果行：``{字段名: 指标值}``。"""

    def test_reads_value_by_field_name(self):
        result = {"data": [{"col1": 0.95, "col2": 0.90}]}
        parsed = qc.parse_checker_result(result, qc.NULL_RATE_CHECKER, ["col1", "col2"])
        assert parsed["ok"] is True
        assert parsed["details"]["col1"]["null_rate"] == 0.95
        assert parsed["details"]["col2"]["null_rate"] == 0.90

    def test_below_threshold_warns(self):
        result = {"data": [{"col1": 0.50}]}
        parsed = qc.parse_checker_result(result, qc.NULL_RATE_CHECKER, ["col1"])
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1
        assert "col1" in parsed["warnings"][0]

    def test_non_applicable_field_is_none(self):
        """非比率类字段在异常值占比里是 None，且不告警。"""
        result = {"data": [{"pay_cnt": 0.9}]}
        parsed = qc.parse_checker_result(result, qc.OUTLIER_RATIO_CHECKER, ["pay_cnt"])
        assert parsed["details"]["pay_cnt"]["outlier_ratio"] is None
        assert parsed["warnings"] == []

    def test_legacy_alias_is_still_readable(self):
        """旧格式（列名带 checker 前缀）仍能解析。"""
        result = {"data": [{"null_rate__col1": 0.95}]}
        parsed = qc.parse_checker_result(result, qc.NULL_RATE_CHECKER, ["col1"])
        assert parsed["details"]["col1"]["null_rate"] == 0.95

    def test_string_values_are_coerced(self):
        result = {"data": [{"col1": "0.95"}]}
        parsed = qc.parse_checker_result(result, qc.NULL_RATE_CHECKER, ["col1"])
        assert parsed["details"]["col1"]["null_rate"] == 0.95


class TestMergeCheckerResults:
    def test_merges_metrics_per_field(self):
        parsed_results = [
            (
                "null_rate",
                qc.parse_checker_result({"data": [{"col1": 0.95}]}, qc.NULL_RATE_CHECKER, ["col1"]),
            ),
            (
                "single_value_ratio",
                qc.parse_checker_result(
                    {"data": [{"col1": 0.10}]}, qc.SINGLE_VALUE_CHECKER, ["col1"]
                ),
            ),
        ]
        merged = qc.merge_checker_results(parsed_results, ["col1"])
        assert merged["details"]["col1"]["null_rate"] == 0.95
        assert merged["details"]["col1"]["single_value_ratio"] == 0.10
        assert merged["ok"] is True

    def test_collects_warnings_from_all_checkers(self):
        parsed_results = [
            (
                "null_rate",
                qc.parse_checker_result({"data": [{"col1": 0.10}]}, qc.NULL_RATE_CHECKER, ["col1"]),
            ),
            (
                "single_value_ratio",
                qc.parse_checker_result(
                    {"data": [{"col1": 0.99}]}, qc.SINGLE_VALUE_CHECKER, ["col1"]
                ),
            ),
        ]
        merged = qc.merge_checker_results(parsed_results, ["col1"])
        assert merged["ok"] is False
        assert len(merged["warnings"]) == 2

    def test_non_applicable_metric_is_none(self):
        parsed_results = [
            (
                "null_rate",
                qc.parse_checker_result({"data": [{"col1": 0.95}]}, qc.NULL_RATE_CHECKER, ["col1"]),
            ),
            (
                "outlier_ratio",
                qc.parse_checker_result(
                    {"data": [{"col1": 0.0}]}, qc.OUTLIER_RATIO_CHECKER, ["col1"]
                ),
            ),
        ]
        merged = qc.merge_checker_results(parsed_results, ["col1"])
        assert merged["details"]["col1"]["outlier_ratio"] is None

    def test_details_shape_is_stable(self):
        """没跑的 checker 也要占位（None），不能缺 key —— 下游按 key 取值。"""
        merged = qc.merge_checker_results([], ["col1"])
        assert set(merged["details"]["col1"]) == {
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
        }
        assert all(v is None for v in merged["details"]["col1"].values())


# ============================================================================
# 旧格式解析（向后兼容）
# ============================================================================
class TestParseQualityCheckResult:
    def test_all_pass(self):
        result = {
            "data": [
                {
                    "null_rate__col1": 0.95,
                    "null_rate__col2": 0.90,
                    "single_value_ratio__col1": 0.10,
                    "single_value_ratio__col2": 0.20,
                }
            ]
        }
        parsed = qc.parse_quality_check_result(result, ["col1", "col2"])
        assert parsed["ok"] is True
        assert parsed["warnings"] == []
        assert parsed["details"]["col1"]["null_rate"] == 0.95

    def test_null_rate_below_threshold_fails(self):
        result = {"data": [{"null_rate__col1": 0.50, "single_value_ratio__col1": 0.10}]}
        parsed = qc.parse_quality_check_result(result, ["col1"])
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1
        assert "col1" in parsed["warnings"][0]

    def test_single_value_above_threshold_fails(self):
        result = {"data": [{"null_rate__col1": 0.95, "single_value_ratio__col1": 0.99}]}
        parsed = qc.parse_quality_check_result(result, ["col1"])
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1
        assert "col1" in parsed["warnings"][0]

    def test_empty_data_is_not_a_failure(self):
        parsed = qc.parse_quality_check_result({"data": []}, ["col1"])
        assert parsed["ok"] is True
        assert parsed["warnings"] == []

    def test_missing_column_is_not_a_failure(self):
        parsed = qc.parse_quality_check_result({"data": [{}]}, ["col1"])
        assert parsed["ok"] is True
        assert parsed["warnings"] == []

    def test_string_values_are_coerced(self):
        result = {"data": [{"null_rate__col1": "0.95", "single_value_ratio__col1": "0.10"}]}
        parsed = qc.parse_quality_check_result(result, ["col1"])
        assert parsed["details"]["col1"]["null_rate"] == 0.95
        assert parsed["ok"] is True

    def test_custom_threshold_is_respected(self):
        config = qc.QualityCheckConfig(
            thresholds={
                qc.NULL_RATE_CHECKER.threshold_key: 0.99,
                qc.SINGLE_VALUE_CHECKER.threshold_key: 0.95,
            }
        )
        result = {"data": [{"null_rate__col1": 0.95, "single_value_ratio__col1": 0.10}]}
        parsed = qc.parse_quality_check_result(result, ["col1"], config)
        # 0.95 < 0.99 → violates the stricter threshold
        assert parsed["ok"] is False


# ============================================================================
# 异常值占比 outlier_ratio
# ============================================================================
class TestOutlierRatioChecker:
    """异常值占比只对比率类字段生效,参考值 0。"""

    @staticmethod
    def _ctx(fields, **kwargs):
        return qc.CheckContext(
            temp_table="adhoctemp.tmp_x",
            fields=list(fields),
            checkers=qc._BUILTIN_CHECKERS,
            **kwargs,
        )

    # --- 比率类字段识别 ---
    def test_ratio_field_detected_by_name_pattern(self):
        ctx = self._ctx(["app_dl_retention_ratio_1d_udid"])
        assert ctx.is_ratio_field("app_dl_retention_ratio_1d_udid") is True

    def test_non_ratio_field_not_detected(self):
        ctx = self._ctx(["pay_cnt_udid"])
        assert ctx.is_ratio_field("pay_cnt_udid") is False

    def test_explicit_ratio_fields_take_precedence(self):
        ctx = self._ctx(["x_ratio", "y"], ratio_fields=frozenset({"y"}))
        # 显式清单命中 → 是;清单外的 ratio 字段 → 否
        assert ctx.is_ratio_field("y") is True
        assert ctx.is_ratio_field("x_ratio") is False

    def test_custom_patterns(self):
        ctx = self._ctx(["rate_col"], ratio_field_patterns=("rate",))
        assert ctx.is_ratio_field("rate_col") is True

    # --- SQL 生成 ---
    def test_metric_sql_uses_out_of_range_predicate(self):
        ctx = self._ctx(
            ["app_dl_retention_ratio_1d_udid"],
            partition_col="pt_d",
            partition_date="20261010",
        )
        sql = qc.OUTLIER_RATIO_CHECKER.build_sql(ctx)
        assert "app_dl_retention_ratio_1d_udid" in sql
        assert "< 0 OR" in sql
        assert "> 1" in sql

    def test_non_ratio_field_is_not_in_sql(self):
        """非比率类字段不出现在这条 SQL 里（不再用占位子查询）。"""
        ctx = self._ctx(
            ["pay_cnt_udid"],
            partition_col="pt_d",
            partition_date="20261010",
        )
        sql = qc.OUTLIER_RATIO_CHECKER.build_sql(ctx)
        assert sql == ""

    def test_ratio_field_sql_filters_partition(self):
        """SQL 必须带分区条件。

        Spark 会拒绝扫描分区表却没有分区条件的语句
        （partition condition not found in where or on node）。
        """
        ctx = self._ctx(["x_ratio"], partition_col="pt_d", partition_date="20261010")
        sql = qc.OUTLIER_RATIO_CHECKER.build_sql(ctx)
        assert "pt_d = '20261010'" in sql
        assert "LIMIT 10000" in sql

    def test_build_quality_check_sqls_includes_outlier_field(self):
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["x_ratio", "pay_cnt"], "pt_d", "20261010", 10000
        )
        outlier_sql = dict(statements)["outlier_ratio"]
        assert "x_ratio" in outlier_sql
        assert "pay_cnt" not in outlier_sql

    # --- 结果解析 ---
    def test_outlier_ratio_above_zero_fails(self):
        result = {
            "data": [
                {
                    "null_rate__x_ratio": 1.0,
                    "single_value_ratio__x_ratio": 0.1,
                    "outlier_ratio__x_ratio": 0.05,
                }
            ]
        }
        parsed = qc.parse_quality_check_result(result, ["x_ratio"])
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1
        assert "x_ratio" in parsed["warnings"][0]
        assert parsed["details"]["x_ratio"]["outlier_ratio"] == 0.05

    def test_zero_outlier_ratio_passes(self):
        result = {
            "data": [
                {
                    "null_rate__x_ratio": 1.0,
                    "single_value_ratio__x_ratio": 0.1,
                    "outlier_ratio__x_ratio": 0.0,
                }
            ]
        }
        parsed = qc.parse_quality_check_result(result, ["x_ratio"])
        assert parsed["ok"] is True
        assert parsed["warnings"] == []

    def test_non_ratio_field_never_warns(self):
        """非比率类字段即使回传了异常值占比也不应告警。"""
        result = {
            "data": [
                {
                    "null_rate__pay_cnt": 1.0,
                    "single_value_ratio__pay_cnt": 0.1,
                    "outlier_ratio__pay_cnt": 0.9,
                }
            ]
        }
        parsed = qc.parse_quality_check_result(result, ["pay_cnt"])
        assert parsed["ok"] is True
        assert parsed["warnings"] == []
        assert parsed["details"]["pay_cnt"]["outlier_ratio"] is None

    def test_custom_threshold_is_respected(self):
        config = qc.QualityCheckConfig(
            thresholds={qc.OUTLIER_RATIO_CHECKER.threshold_key: 0.1},
        )
        result = {
            "data": [
                {
                    "null_rate__x_ratio": 1.0,
                    "single_value_ratio__x_ratio": 0.1,
                    "outlier_ratio__x_ratio": 0.05,
                }
            ]
        }
        parsed = qc.parse_quality_check_result(result, ["x_ratio"], config)
        # 0.05 <= 0.1 → 不告警
        assert parsed["ok"] is True

    # --- 配置 ---
    def test_ratio_fields_config_is_resolved(self):
        tool_cfg = {"quality_check": {"ratio_fields": ["x_ratio"]}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.ratio_fields == frozenset({"x_ratio"})

    def test_ratio_field_patterns_config_is_resolved(self):
        tool_cfg = {"quality_check": {"ratio_field_patterns": ["rate", "pct"]}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.ratio_field_patterns == ("rate", "pct")

    def test_outlier_threshold_config_is_resolved(self):
        tool_cfg = {"quality_check": {"thresholds": {"outlier_ratio_threshold": 0.05}}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.threshold_for(qc.OUTLIER_RATIO_CHECKER) == 0.05


# ============================================================================
# Configuration resolution
# ============================================================================
class TestResolveConfig:
    def test_defaults_without_any_config(self):
        config = qc.resolve_config(None)
        assert config.enabled is True
        assert config.sample_size == qc._DEFAULT_SAMPLE_SIZE
        assert config.partition_col == qc._DEFAULT_PARTITION_COL
        assert config.exclude_fields == qc.DEFAULT_EXCLUDE_FIELDS
        assert config.checker_names == ("null_rate", "single_value_ratio", "outlier_ratio")
        assert config.ratio_fields == frozenset()
        assert config.ratio_field_patterns == ("ratio",)

    def test_tool_config_wins_over_resource_metadata(self):
        tool_cfg = {"quality_check": {"sample_size": 42, "thresholds": {"null_rate_threshold": 0.5}}}
        resource_cfg = {"quality_check": {"sample_size": 99, "thresholds": {"null_rate_threshold": 0.9}}}
        config = qc.resolve_config(
            _StubContext(tool_cfg=tool_cfg, resource_cfg=resource_cfg)
        )
        assert config.sample_size == 42
        assert config.threshold_for(qc.NULL_RATE_CHECKER) == 0.5

    def test_flat_threshold_keys_are_supported(self):
        tool_cfg = {"quality_check": {"null_rate_threshold": 0.7}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.threshold_for(qc.NULL_RATE_CHECKER) == 0.7

    def test_checks_subset_is_respected(self):
        tool_cfg = {"quality_check": {"checks": ["single_value_ratio"]}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.checker_names == ("single_value_ratio",)

    def test_exclude_fields_override(self):
        tool_cfg = {"quality_check": {"exclude_fields": ["custom_col"]}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.exclude_fields == frozenset({"custom_col"})

    def test_disabled_via_tool_config(self):
        tool_cfg = {"quality_check": {"enabled": False}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.enabled is False

    def test_env_fallback_when_no_yaml_config(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_ENABLE_REASONABLE_CHECK", "false")
        config = qc.resolve_config(None)
        assert config.enabled is False

    def test_env_checks_fallback(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_QUALITY_CHECK_CHECKS", "null_rate")
        config = qc.resolve_config(None)
        assert config.checker_names == ("null_rate",)


# ============================================================================
# Backward-compatible shims
# ============================================================================
class TestBackwardCompatibleShims:
    def test_build_shim_delegates(self):
        sql = qc._build_reasonable_check_sql(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )
        assert sql == qc.build_quality_check_sql(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )

    def test_parse_shim_delegates(self):
        result = {"data": [{"null_rate__col1": 0.50, "single_value_ratio__col1": 0.10}]}
        parsed = qc._parse_reasonable_check_result(result, ["col1"], 0.80, 0.95)
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1

    def test_enabled_shim_default(self, monkeypatch):
        monkeypatch.delenv("DATAOPS_ENABLE_REASONABLE_CHECK", raising=False)
        assert qc._is_reasonable_check_enabled() is True

    def test_enabled_shim_disabled(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_ENABLE_REASONABLE_CHECK", "false")
        assert qc._is_reasonable_check_enabled() is False

    def test_exclude_cols_alias(self):
        assert qc._REASONABLE_CHECK_DEFAULT_EXCLUDE_COLS == qc.DEFAULT_EXCLUDE_FIELDS

    def test_validate_tool_reexports_shims(self):
        from dataagent.actions.tools.local_tool import dataops_validate_tool as vt

        assert vt._build_reasonable_check_sql is qc._build_reasonable_check_sql
        assert vt._parse_reasonable_check_result is qc._parse_reasonable_check_result
        assert vt._is_reasonable_check_enabled is qc._is_reasonable_check_enabled
        assert vt.run_quality_check is qc.run_quality_check

    def test_validate_tool_unknown_attribute_raises(self):
        from dataagent.actions.tools.local_tool import dataops_validate_tool as vt

        with pytest.raises(AttributeError):
            _ = vt.definitely_not_a_real_export


# ============================================================================
# run_quality_check — end-to-end wiring
#
# Regression guard: ``run_quality_check`` used to forward ``config=config``
# (i.e. ``None``) instead of the resolved config, and to omit ``temp_table``
# altogether. Every call therefore blew up with a TypeError that was swallowed
# by the broad ``except`` and reported as "no warnings" — the check silently
# never ran.
# ============================================================================
class TestRunQualityCheckWiring:
    @staticmethod
    def _fake_runtime():
        runtime = MagicMock()
        coordinator = MagicMock()
        coordinator.catalog = {"dataops": MagicMock(metadata={"exec_user": "u", "table_suffix": ""})}
        runtime.ensure_resource_coordinator.return_value = coordinator
        # ``_find_ddl_in_workspace`` 只认真实的 str/Path —— MagicMock 路径必须置空。
        runtime.workspace_dir = None
        return runtime

    def test_check_timeout_cap_is_30_minutes(self):
        """校验 SQL 的超时上限必须是 30 分钟。

        回归防护：这里曾是 180s（3 分钟）。质量校验 SQL 在 Spark 上聚合多个指标，
        千万级分区下单条要跑十几分钟 —— 3 分钟的帽子会让校验在跑完前被判定
        timed_out，整个校验报「未能执行」，一个质量结论都拿不到。表现上像是
        「agent 已经超时」，实际是校验子任务先超时了。
        """
        assert qc._MAX_CHECK_TIMEOUT_SEC == 30 * 60
        assert qc._MAX_CHECK_TIMEOUT_SEC == qc._MAX_MAIN_TIMEOUT_SEC

    @pytest.mark.asyncio
    async def test_check_sql_receives_the_full_timeout_budget(self, monkeypatch):
        """传给校验 SQL 的 timeout 必须是 30 分钟，不能被 3 分钟的旧帽子砍掉。"""
        monkeypatch.setenv("DATAOPS_ENV", "integration")

        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        seen_timeouts: list[int] = []

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            seen_timeouts.append(timeout_sec)
            return "completed", f"job-{len(seen_timeouts)}", {"data": [{"col1": 1.0}]}

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT col1 FROM src",
                _tool_context=ctx,
            )

        assert seen_timeouts, "check SQL must have been executed"
        assert all(t == 30 * 60 for t in seen_timeouts), seen_timeouts

    @pytest.mark.asyncio
    async def test_check_actually_executes_and_reports_warnings(self, monkeypatch):
        """A threshold violation must surface as a warning, not a silent pass."""
        monkeypatch.setenv("DATAOPS_ENV", "integration")

        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        executed: list[str] = []

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            executed.append(check_sql)
            # 每个 checker 一条 SQL；结果行的列名 = 字段名。
            if "IsEmpty" in check_sql or "IS NOT NULL" in check_sql:
                return "completed", "job-1", {"data": [{"col1": 0.10}]}
            if "MAX(cnt)" in check_sql:
                return "completed", "job-2", {"data": [{"col1": 0.20}]}
            return "completed", "job-3", {"data": [{"col1": 0.0}]}

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT col1 FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        assert result["sampled"] is True
        assert result["ok"] is False
        assert result["warnings"], "a null_rate of 0.10 must raise a warning"
        # 三条 SQL 逐条执行（异常值占比没有比率字段时不生成）。
        assert len(executed) == 2
        assert "UNION ALL" not in "".join(executed)

    @pytest.mark.asyncio
    async def test_all_three_checks_run_when_ratio_field_present(self, monkeypatch):
        """有比率类字段时，三条校验 SQL 各执行一次。"""
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        executed: list[str] = []

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            executed.append(check_sql)
            return "completed", f"job-{len(executed)}", {"data": [{"x_ratio": 1.0}]}

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT x_ratio FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        assert len(executed) == 3
        assert result["details"]["x_ratio"]["null_rate"] == 1.0
        assert result["details"]["x_ratio"]["single_value_ratio"] == 1.0
        assert result["details"]["x_ratio"]["outlier_ratio"] == 1.0
        assert result["checks"] == ["null_rate", "single_value_ratio", "outlier_ratio"]

    @pytest.mark.asyncio
    async def test_checks_are_submitted_concurrently(self, monkeypatch):
        """三条校验 SQL 必须**并发**提交，不能顺序跑。

        DataOps 不支持把多个校验项合并成一条语句；顺序跑的话第一条挂了就
        后面的全不跑，整轮校验直接被判成「未执行」。
        并发提交后三条各自独立轮询，互不影响。
        """
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        in_flight = 0
        max_in_flight = 0

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            # 让出控制权，确保三条任务都能同时在飞。
            await asyncio.sleep(0.05)
            in_flight -= 1
            return "completed", "job-x", {"data": [{"x_ratio": 1.0}]}

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT x_ratio FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        assert result["sampled"] is True
        assert max_in_flight == 3, (
            f"三条校验 SQL 应并发提交，实际最大并发数 {max_in_flight}"
        )

    @pytest.mark.asyncio
    async def test_one_failed_check_does_not_cancel_the_others(self, monkeypatch):
        """一条挂了不影响另外两条 —— 已跑出来的指标照常给结论，不整体丢弃。

        回归防护：早先这里一条失败就直接 return ``_empty_result``，
        已经算出来的指标全丢了，前端只能看到「未能执行」，看不到任何结果。
        """
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            if "MAX(cnt)" in check_sql:
                return "failed", "job-bad", None
            return "completed", "job-ok", {"data": [{"x_ratio": 1.0}]}

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT x_ratio FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        # 有一条没跑完 → 不能说「全部通过」，但已跑出来的结果必须保留。
        assert result["ok"] is False
        assert result["sampled"] is True
        assert result["partial"] is True
        # 失败的那条被记下来。
        failed_names = [item["name"] for item in result["failed_checks"]]
        assert "single_value_ratio" in failed_names
        # 跑成功的那条指标照常有值。
        assert result["details"]["x_ratio"]["null_rate"] == 1.0
        # 跑成功的那条不该被记成失败。
        assert "null_rate" not in failed_names

    @pytest.mark.asyncio
    async def test_all_checks_failed_reports_no_conclusion(self, monkeypatch):
        """一条都没跑成 → 确实没有质量结论，报 sampled=False + 原因。"""
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            return "timed_out", "job-bad", None

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT col1 FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        assert result["sampled"] is False
        assert result["ok"] is False
        assert "timed_out" in result["error"]

    @pytest.mark.asyncio
    async def test_checks_reports_only_executed_statements(self, monkeypatch):
        """没有比率类字段时，异常值占比那条 SQL 不会执行，也不出现在 checks 里。"""
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        async def fake_exec(check_sql, coordinator, timeout_sec, poll_interval):
            return "completed", "job-1", {"data": [{"col1": 1.0}]}

        with patch.object(qc, "_execute_check_sql_integration", side_effect=fake_exec):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT col1 FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        assert result["checks"] == ["null_rate", "single_value_ratio"]
        assert result["details"]["col1"]["outlier_ratio"] is None

    @pytest.mark.asyncio
    async def test_non_insert_sql_is_skipped(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        result = await qc.run_quality_check(
            "CREATE TABLE biads.t (a int)", _tool_context=ctx
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_config_skips(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(
            runtime=self._fake_runtime(),
            tool_config={"quality_check": {"enabled": False}},
        )

        result = await qc.run_quality_check(
            "INSERT OVERWRITE TABLE biads.tmp SELECT col1 FROM src",
            _tool_context=ctx,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_execution_failure_does_not_raise(self, monkeypatch):
        """Best-effort contract: a failing check must not raise at the caller.

        但**不能**报成通过：校验没跑起来 ≠ 数据没问题。
        ``ok`` 必须是 False，且 ``sampled`` 为 False，让调用方能区分
        「查过了、没问题」和「根本没查成」。
        """
        monkeypatch.setenv("DATAOPS_ENV", "integration")
        ctx = ToolExecutionContext(runtime=self._fake_runtime(), tool_config={})

        async def boom(*_args, **_kwargs):
            raise RuntimeError("dataops exploded")

        with patch.object(qc, "_execute_check_sql_integration", side_effect=boom):
            result = await qc.run_quality_check(
                "INSERT OVERWRITE TABLE biads.tmp SELECT col1 FROM src",
                _tool_context=ctx,
            )

        assert result is not None
        assert result["ok"] is False
        assert result["sampled"] is False
        assert result["warnings"] == []
        assert "dataops exploded" in result["error"]


# ============================================================================
# render_metrics_table — Markdown 表格渲染（前端展示）
# ============================================================================
class TestRenderMetricsTable:
    def test_empty_details_placeholder(self):
        assert qc.render_metrics_table({}, []) == "（无可展示的指标）"
        assert qc.render_metrics_table(None, []) == "（无可展示的指标）"

    def test_header_row(self):
        table = qc.render_metrics_table({"a": {"null_rate": 1.0}}, [])
        lines = table.splitlines()
        assert lines[0] == "| 字段名 | 非空率(覆盖率) | 单一值比率 | 异常值占比 | 状态 |"
        assert lines[1] == "|---|---|---|---|---|"

    def test_metrics_rendered_with_four_decimals(self):
        table = qc.render_metrics_table(
            {"a": {"null_rate": 0.4567, "single_value_ratio": 0.1, "outlier_ratio": 0.0}},
            [],
        )
        assert "| a | 0.4567 | 0.1000 | 0.0000 | ✅ 通过 |" in table

    def test_none_metric_renders_as_dash(self):
        """不适用的指标是 ``None`` —— 必须渲染成 ``—``，不能是 0。

        0 会被读成「算出来就是 0」，``—`` 才是「这一项不适用」。
        """
        table = qc.render_metrics_table(
            {"a": {"null_rate": 0.9, "single_value_ratio": 0.2, "outlier_ratio": None}},
            [],
        )
        assert "| a | 0.9000 | 0.2000 | — | ✅ 通过 |" in table

    def test_warned_field_is_flagged(self):
        table = qc.render_metrics_table(
            {"a": {"null_rate": 0.4, "single_value_ratio": 0.2, "outlier_ratio": None}},
            ["字段 'a' 非空率 0.4000 < 阈值 0.80"],
        )
        assert "⚠️ 告警" in table
        assert "✅ 通过" not in table

    def test_field_with_all_none_metrics_shows_dash_status(self):
        """该字段所有指标都不适用 —— 状态列不能写「通过」。"""
        table = qc.render_metrics_table(
            {"a": {"null_rate": None, "single_value_ratio": None, "outlier_ratio": None}},
            [],
        )
        assert "| a | — | — | — | — |" in table

    def test_non_numeric_value_renders_as_dash(self):
        table = qc.render_metrics_table({"a": {"null_rate": "n/a"}}, [])
        assert "| a | — | — | — |" in table

    def test_details_shape_is_stable(self):
        """``merge_checker_results`` 保证每个字段都带全部 metric key。"""
        details = {"a": {"null_rate": 1.0, "single_value_ratio": 0.1, "outlier_ratio": None}}
        table = qc.render_metrics_table(details, [])
        # 三列都在，顺序固定
        assert table.splitlines()[2].count("|") == 6

    def test_failed_metric_renders_as_not_completed(self):
        """没算出来的指标渲染成「未完成」，不能渲染成「—」（那读成「不适用」）。"""
        table = qc.render_metrics_table(
            {"a": {"null_rate": 0.9, "single_value_ratio": None, "outlier_ratio": None}},
            [],
            {"a": ["single_value_ratio"]},
        )
        assert "| a | 0.9000 | 未完成 | — | ⚠️ 部分未完成 |" in table

    def test_failed_metric_does_not_override_warning_status(self):
        """字段同时有告警和未完成指标时，状态列仍标「告警」。"""
        table = qc.render_metrics_table(
            {"a": {"null_rate": 0.4, "single_value_ratio": None, "outlier_ratio": None}},
            ["字段 'a' 非空率 0.4000 < 阈值 0.80"],
            {"a": ["single_value_ratio"]},
        )
        assert "⚠️ 告警" in table
        assert "未完成" in table


# ============================================================================
# Helpers
# ============================================================================
class _StubContext:
    """Minimal ``ToolExecutionContext`` stand-in for config resolution.

    ``dataops_quality_check`` reads ``tool_config`` off the context and the
    ``quality_check`` section off the dataops resource metadata; both are
    stubbed here without needing a real Runtime.
    """

    def __init__(self, *, tool_cfg: dict, resource_cfg: dict) -> None:
        self.tool_config = tool_cfg
        self._resource_cfg = resource_cfg
        self.runtime = None

    # ``_resource_quality_check_metadata`` swallows exceptions and returns {},
    # so a resource-less stub is enough to exercise the tool_config path.
    def __getattr__(self, name):  # pragma: no cover - defensive
        raise AttributeError(name)


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("", True)],
)
def test_as_bool(raw, expected):
    assert qc._as_bool(raw, True) is expected
