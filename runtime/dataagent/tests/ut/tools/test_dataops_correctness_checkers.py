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
"""Unit tests for the **data correctness** checkers.

覆盖需求 1.1 / 1.2 的两条确定性规则，以及它们与既有校验器的隔离契约：

- 主键重复率（表级，全表算，必须为 0）；
- 计数型字段非负（字段级，必须 ≥ 0）；
- 两者**不污染**历史 ``details`` 的形状（那是稳定的对外契约）；
- 信息不足时**跳过**而不是猜测。
"""

from __future__ import annotations

import pytest

from dataagent.actions.tools.local_tool import dataops_quality_check as qc


# ============================================================================
# 注册表
# ============================================================================
class TestCorrectnessRegistry:
    def test_legacy_registry_is_unchanged(self):
        """历史注册表 MUST NOT 混入正确性校验 —— 它的形状是稳定契约。"""
        assert [c.name for c in qc._BUILTIN_CHECKERS] == [
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
            "row_count",
        ]

    def test_correctness_registry_holds_the_two_deterministic_rules(self):
        assert [c.name for c in qc._CORRECTNESS_CHECKERS] == [
            "pk_duplicate_rate",
            "negative_count",
        ]

    def test_select_all_when_not_configured(self):
        assert [c.name for c in qc.select_correctness_checkers(None)] == [
            "pk_duplicate_rate",
            "negative_count",
        ]

    def test_select_subset(self):
        selected = qc.select_correctness_checkers(["negative_count"])
        assert [c.name for c in selected] == ["negative_count"]

    def test_select_empty_disables_all(self):
        assert qc.select_correctness_checkers([]) == ()

    def test_correctness_violations_are_error_level(self):
        """error 级 MUST 触发自修正 —— 这是整个闭环的触发条件。"""
        for checker in qc._CORRECTNESS_CHECKERS:
            assert checker.severity == qc.SEVERITY_ERROR, checker.name
            assert checker.severity == "error"

    def test_thresholds_default_to_zero(self):
        assert qc.PK_DUPLICATE_CHECKER.default_threshold == 0.0
        assert qc.NEGATIVE_COUNT_CHECKER.default_threshold == 0.0


# ============================================================================
# 主键重复率
# ============================================================================
class TestPkDuplicateChecker:
    @staticmethod
    def _ctx(**kwargs):
        defaults = {
            "temp_table": "adhoctemp.tmp_x",
            "fields": ["udid", "dt"],
            "checkers": qc._CORRECTNESS_CHECKERS,
            "primary_key_columns": ("udid",),
            "partition_filter_expr": "pt_d = '20261010'",
        }
        defaults.update(kwargs)
        return qc.CheckContext(**defaults)

    def test_sql_is_full_table_not_sampled(self):
        """重复率 MUST 全表算 —— 采样算不出真实重复率。"""
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert sql
        # 没有 LIMIT / 派生表 —— 直接扫表。
        assert "LIMIT" not in sql.upper()
        assert "_src" not in sql

    def test_sql_counts_distinct_pk(self):
        """重复率 MUST 用 ``GROUP BY`` 口径算 —— 不能用 ``COUNT(DISTINCT)``。

        生产 adHoc **硬性拦截** ``COUNT(DISTINCT ...)``：
        ``AdHoc stage for parsing sql: ERROR: Do not display count(distinct)
        in your hive sql, because there is a performance problem. It is
        recommended that you use group by.``

        所以唯一主键数只能通过「按主键分组后数组数」拿到。
        """
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "GROUP BY `udid`" in sql
        assert "pk_duplicate_rate" in sql
        assert "pk_duplicate_rows" in sql
        assert "pk_total_rows" in sql

    def test_sql_avoids_count_distinct_the_prod_engine_rejects(self):
        """回归防护：``COUNT(DISTINCT)`` 会被生产 adHoc 直接拒。

        实测报错（生产试算日志）::

            SQL查询失败：AdHoc stage for parsing sql:ERROR:Do not display
            count(distinct) in your hive sql,because there is a performance
            problem.It is recommended that you use group by.;

        所以这条 SQL 里**不允许出现** ``COUNT(DISTINCT``（大小写不敏感）。
        """
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "COUNT(DISTINCT" not in sql.upper()

    def test_null_pk_rows_excluded_from_denominator(self):
        """主键为空的行 MUST NOT 计入分母 —— 否则重复率被稀释。"""
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "`udid` IS NOT NULL" in sql

    def test_composite_pk_uses_multi_column_group_by(self):
        """复合主键 MUST 用**多列 GROUP BY**，不做字符串拼接。

        回归防护：早先这里用 ``CONCAT_WS('\\x01', `udid`, `dt`)`` 拼成单值
        再分组 —— 那在**生产 adHoc** 上被直接拒绝，实测报错::

            org.apache.spark.sql.AnalysisException: Undefined function:
            'CONCAT_WS'. This function is neither a registered temporary
            function nor a permanent function registered in the database
            'agapdw'.

        **根因**：生产 adHoc 跑的是 **Spark SQL**，``CONCAT_WS`` 是 **Hive
        函数**，Spark 没注册它。

        多列 ``GROUP BY`` 语义完全等价（按整组主键分组），却只碰最基本的
        构造。
        """
        ctx = self._ctx(primary_key_columns=("udid", "dt"))
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(ctx)
        assert "GROUP BY `udid`, `dt`" in sql
        assert "`udid`" in sql and "`dt`" in sql

    def test_composite_pk_does_not_use_concat_ws(self):
        """复合主键 MUST NOT 用 ``CONCAT_WS`` —— 生产 adHoc（Spark SQL）不认。

        这条钉住的是**实测报错**：``Undefined function: 'CONCAT_WS'``。
        """
        ctx = self._ctx(primary_key_columns=("udid", "dt"))
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(ctx)
        assert "CONCAT_WS" not in sql.upper()
        # 内层的主键列本来就没被用到 —— 不该再拼出 `pk` 这一列。
        # （注意不能简单断言 `" AS pk" not in sql` —— `AS pk_duplicate_rate`
        # 里有子串 `AS pk`。）
        assert "SELECT COUNT(1) AS cnt" in sql
        assert "AS pk," not in sql and "AS pk\n" not in sql

    def test_pk_sql_only_uses_spark_safe_functions(self):
        """主键 SQL 只允许用 Spark SQL 一定支持的构造。

        生产 adHoc 跑 Spark SQL，Hive-only 函数（``CONCAT_WS`` / ``NVL`` /
        ``SPLIT`` / ``FROM_UNIXTIME`` ...）一律报 "Undefined function"。
        这里只放行最基本的聚合与分组构造。
        """
        for pk_columns in (("udid",), ("udid", "dt"), ("a", "b", "c")):
            ctx = self._ctx(primary_key_columns=pk_columns)
            sql = qc.PK_DUPLICATE_CHECKER.build_sql(ctx).upper()
            for banned in ("CONCAT_WS", "NVL(", "SPLIT(", "FROM_UNIXTIME", "DATE_FORMAT"):
                assert banned not in sql, f"主键 {pk_columns} 的 SQL 含 {banned}"

    def test_sql_avoids_functions_the_prod_adhoc_engine_rejects(self):
        """回归防护：这条 SQL 曾在生产 adHoc 的**提交阶段**被拒。

        早先用的是 ``CAST(... AS DOUBLE)`` + ``NULLIF(...)`` —— 同引擎、同分区
        过滤的另外三条校验项都能跑通，只有主键这条被拒。生产 adHoc 的表名
        解析与引擎路由对非标准函数比较敏感，所以这里只用
        ``COUNT`` / ``SUM`` / ``CASE WHEN`` 这几个最基本的构造。
        """
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "CAST(" not in sql.upper()
        assert "NULLIF" not in sql.upper()
        # 除零保护改成 CASE WHEN —— 不能没有保护。
        assert "CASE WHEN SUM(cnt) = 0" in sql

    def test_total_rows_uses_sum_not_count(self):
        """回归防护：外层 ``COUNT(1)`` 数的是**分组数**，不是总行数。

        分组后每组一行 ``cnt``，总行数 MUST 由 ``SUM(cnt)`` 得到；
        用 ``COUNT(1)`` 会把「10000 行、9000 个唯一主键」算成 9000 行，
        重复率直接算错。
        """
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "SUM(cnt) AS pk_total_rows" in sql
        assert "COUNT(1) AS pk_total_rows" not in sql

    def test_duplicate_rows_counts_extra_rows_in_each_group(self):
        """重复行数 = 各组 ``cnt - 1`` 之和 —— 不是组数。"""
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) AS pk_duplicate_rows" in sql

    def test_no_primary_key_produces_no_sql(self):
        """主键缺失 → 不生成 SQL（跳过），不猜。"""
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx(primary_key_columns=()))
        assert sql == ""

    def test_partition_filter_is_applied(self):
        sql = qc.PK_DUPLICATE_CHECKER.build_sql(self._ctx())
        assert "pt_d = '20261010'" in sql

    def test_violates_only_above_zero(self):
        assert qc.PK_DUPLICATE_CHECKER.violates(0.0, 0.0) is False
        assert qc.PK_DUPLICATE_CHECKER.violates(0.0001, 0.0) is True

    def test_warning_mentions_the_rate(self):
        text = qc.PK_DUPLICATE_CHECKER.format_warning("udid", 0.01, 0.0)
        assert "主键重复率" in text
        assert "0.010000" in text

    def test_scope_is_table(self):
        """表级指标 —— 不挂字段，结果进 table_metrics。"""
        assert qc.PK_DUPLICATE_CHECKER.scope == qc._SCOPE_TABLE


class TestResolvePrimaryKeyColumns:
    def test_config_fallback(self):
        cfg = qc.QualityCheckConfig(primary_key_columns=("udid",))
        assert qc.resolve_primary_key_columns(None, ["udid", "dt"], cfg) == ("udid",)

    def test_no_primary_key_returns_empty(self):
        """拿不到主键 → 空元组（调用方据此跳过），不猜。"""
        assert qc.resolve_primary_key_columns(None, ["udid"], qc.QualityCheckConfig()) == ()

    def test_primary_key_not_in_fields_is_dropped(self):
        """配置的主键列不在实际字段里 → 不匹配（避免查到不存在的列）。"""
        cfg = qc.QualityCheckConfig(primary_key_columns=("not_here",))
        assert qc.resolve_primary_key_columns(None, ["udid"], cfg) == ()


# ============================================================================
# 计数型字段非负
# ============================================================================
class TestNegativeCountChecker:
    @staticmethod
    def _ctx(fields, column_types=None, **kwargs):
        defaults = {
            "temp_table": "adhoctemp.tmp_x",
            "fields": list(fields),
            "checkers": qc._CORRECTNESS_CHECKERS,
            "column_types": column_types or {},
            "partition_filter_expr": "pt_d = '20261010'",
        }
        defaults.update(kwargs)
        return qc.CheckContext(**defaults)

    # --- 适用性判定 ---
    def test_explicit_list_wins(self):
        """显式清单为准 —— 清单里的字段即便名字不像计数型也参与。"""
        included, skipped = qc.resolve_count_fields(
            ["pay_amount"], {"pay_amount": "decimal"},
            count_fields=frozenset({"pay_amount"}),
        )
        assert included == ("pay_amount",)
        assert skipped == ()

    def test_pattern_match_with_numeric_type(self):
        included, _ = qc.resolve_count_fields(
            ["play_cnt_7d"], {"play_cnt_7d": "bigint"}
        )
        assert included == ("play_cnt_7d",)

    def test_pattern_match_without_numeric_type_is_excluded(self):
        included, _ = qc.resolve_count_fields(
            ["cnt_label"], {"cnt_label": "string"}
        )
        assert included == ()

    def test_unknown_type_is_skipped_with_reason(self):
        """类型未知 → 跳过并记原因，不猜。"""
        included, skipped = qc.resolve_count_fields(["play_cnt_7d"], {})
        assert included == ()
        assert skipped == ("play_cnt_7d",)

    def test_diff_fields_are_excluded(self):
        """差值型字段天然可正可负 —— 一律排除。"""
        included, _ = qc.resolve_count_fields(
            ["amount_diff", "balance_change", "play_cnt"],
            {"amount_diff": "bigint", "balance_change": "bigint", "play_cnt": "bigint"},
        )
        assert included == ("play_cnt",)

    def test_explicit_list_also_respects_diff_exclusion(self):
        """显式清单也不能把差值型字段拉进来 —— 那是配置错误。"""
        included, _ = qc.resolve_count_fields(
            ["amount_diff"],
            {"amount_diff": "bigint"},
            count_fields=frozenset({"amount_diff"}),
        )
        assert included == ()

    # --- SQL 生成 ---
    def test_sql_covers_all_applicable_fields(self):
        ctx = self._ctx(
            ["play_cnt", "pay_cnt"],
            {"play_cnt": "bigint", "pay_cnt": "bigint"},
        )
        sql = qc.NEGATIVE_COUNT_CHECKER.build_sql(ctx)
        assert "SUM(IF(`play_cnt` < 0, 1, 0)) / COUNT(1) AS play_cnt" in sql
        assert "SUM(IF(`pay_cnt` < 0, 1, 0)) / COUNT(1) AS pay_cnt" in sql

    def test_sql_includes_evidence_column(self):
        ctx = self._ctx(["play_cnt"], {"play_cnt": "bigint"})
        sql = qc.NEGATIVE_COUNT_CHECKER.build_sql(ctx)
        assert "neg_rows__play_cnt" in sql

    def test_no_applicable_field_produces_no_sql(self):
        ctx = self._ctx(["name"], {"name": "string"})
        assert qc.NEGATIVE_COUNT_CHECKER.build_sql(ctx) == ""

    def test_violates_only_above_zero(self):
        assert qc.NEGATIVE_COUNT_CHECKER.violates(0.0, 0.0) is False
        assert qc.NEGATIVE_COUNT_CHECKER.violates(0.01, 0.0) is True

    def test_warning_names_the_field(self):
        text = qc.NEGATIVE_COUNT_CHECKER.format_warning("play_cnt", 0.02, 0.0)
        assert "play_cnt" in text
        assert "负值" in text


# ============================================================================
# 数值型类型判定
# ============================================================================
class TestIsNumericColumnType:
    @pytest.mark.parametrize(
        "column_type",
        ["int", "BIGINT", "smallint", "tinyint", "decimal", "double", "float", "numeric"],
    )
    def test_numeric_types(self, column_type: str):
        assert qc.is_numeric_column_type(column_type) is True

    @pytest.mark.parametrize("column_type", ["string", "timestamp", "array", ""])
    def test_non_numeric_types(self, column_type: str):
        assert qc.is_numeric_column_type(column_type) is False

    def test_decimal_with_precision(self):
        assert qc.is_numeric_column_type("decimal(38,6)") is True

    def test_timestamp_is_not_numeric(self):
        """时间戳 MUST NOT 被当成数值 —— 否则负值校验完全跑偏。"""
        assert qc.is_numeric_column_type("timestamp") is False


# ============================================================================
# 源表解析
# ============================================================================
class TestExtractInsertSourceTables:
    def test_simple_from(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp_x SELECT a FROM dwd.order_detail"
        assert qc.extract_insert_source_tables(sql) == ("dwd.order_detail",)

    def test_joins_are_included(self):
        sql = (
            "INSERT OVERWRITE TABLE adhoctemp.tmp_x "
            "SELECT a FROM dwd.order_detail o JOIN dws.dim_user u ON o.uid = u.uid"
        )
        tables = qc.extract_insert_source_tables(sql)
        assert "dwd.order_detail" in tables
        assert "dws.dim_user" in tables

    def test_target_table_is_excluded(self):
        sql = "INSERT OVERWRITE TABLE dwd.target SELECT a FROM dwd.source"
        tables = qc.extract_insert_source_tables(sql)
        assert "dwd.target" not in tables

    def test_derived_table_alias_is_excluded(self):
        sql = (
            "INSERT OVERWRITE TABLE adhoctemp.tmp_x "
            "SELECT a FROM (SELECT a FROM dwd.src) AS _s"
        )
        tables = qc.extract_insert_source_tables(sql)
        assert "dwd.src" in tables
        assert not any(t.startswith("(") for t in tables)

    def test_empty_sql_returns_empty(self):
        assert qc.extract_insert_source_tables("") == ()

    def test_comments_are_ignored(self):
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp_x SELECT a FROM dwd.src -- FROM dwd.fake"
        tables = qc.extract_insert_source_tables(sql)
        assert "dwd.fake" not in tables


# ============================================================================
# DDL 注释解析
# ============================================================================
class TestParseDdlColumnsWithComments:
    def test_parses_types_and_comments(self):
        ddl = (
            "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_foo (\n"
            "  udid_sha256 STRING COMMENT 'sha256 标识',\n"
            "  retain_cnt_30d BIGINT COMMENT '近 30 天留存次数',\n"
            "  retain_ratio_30d DECIMAL(38,6) COMMENT '留存率'\n"
            ") COMMENT 'xx'\n"
            "PARTITIONED BY (pt_d STRING COMMENT '天分区')\n"
            "STORED AS ORC;"
        )
        types, comments = qc.parse_ddl_columns_with_comments(ddl)
        assert types["udid_sha256"] == "string"
        assert types["retain_cnt_30d"] == "bigint"
        assert comments["retain_cnt_30d"] == "近 30 天留存次数"
        assert comments["retain_ratio_30d"] == "留存率"

    def test_column_without_comment(self):
        ddl = "CREATE TABLE t (a BIGINT, b STRING COMMENT 'b 字段')"
        _types, comments = qc.parse_ddl_columns_with_comments(ddl)
        assert "a" not in comments
        assert comments["b"] == "b 字段"

    def test_empty_ddl(self):
        assert qc.parse_ddl_columns_with_comments("") == ({}, {})

    def test_legacy_parser_still_works(self):
        """旧函数签名与行为 MUST 不变。"""
        ddl = "CREATE TABLE t (a BIGINT COMMENT 'a 字段')"
        assert qc.parse_ddl_column_types(ddl) == {"a": "bigint"}


# ============================================================================
# 合并结果 —— 与历史 details 的隔离
# ============================================================================
class TestMergeCorrectnessResults:
    def test_does_not_pollute_details(self):
        """``details`` 的形状是稳定契约 —— 正确性指标 MUST NOT 进去。"""
        ctx = qc.CheckContext(
            temp_table="adhoctemp.tmp_x",
            fields=["udid", "play_cnt"],
            checkers=qc._BUILTIN_CHECKERS,
            primary_key_columns=("udid",),
        )
        merged = qc.merge_correctness_results([], ["udid", "play_cnt"], None, None, ctx)
        assert "details" not in merged or not merged.get("details")

    def test_pk_duplicate_goes_to_table_metrics(self):
        ctx = qc.CheckContext(
            temp_table="adhoctemp.tmp_x",
            fields=["udid"],
            checkers=qc._CORRECTNESS_CHECKERS,
            primary_key_columns=("udid",),
        )
        parsed = qc.parse_checker_result(
            {"data": [{"pk_duplicate_rate": 0.0, "pk_duplicate_rows": 0, "pk_total_rows": 100}]},
            qc.PK_DUPLICATE_CHECKER,
            ["udid"],
        )
        merged = qc.merge_correctness_results(
            [("pk_duplicate_rate", parsed)], ["udid"], None, None, ctx
        )
        assert merged["ok"] is True
        assert merged["violations"] == []

    def test_pk_duplicate_violation_is_produced(self):
        ctx = qc.CheckContext(
            temp_table="adhoctemp.tmp_x",
            fields=["udid"],
            checkers=qc._CORRECTNESS_CHECKERS,
            primary_key_columns=("udid",),
        )
        parsed = qc.parse_checker_result(
            {"data": [{"pk_duplicate_rate": 0.01, "pk_duplicate_rows": 1, "pk_total_rows": 100}]},
            qc.PK_DUPLICATE_CHECKER,
            ["udid"],
        )
        merged = qc.merge_correctness_results(
            [("pk_duplicate_rate", parsed)], ["udid"], None, None, ctx
        )
        assert merged["ok"] is False
        assert len(merged["violations"]) == 1
        violation = merged["violations"][0]
        assert violation.rule == "pk_duplicate_rate"
        assert violation.severity == qc.SEVERITY_ERROR
        assert violation.is_error is True
        # 证据里要有实测值与行数。
        assert violation.evidence


# ============================================================================
# 违规结构
# ============================================================================
class TestCorrectnessViolation:
    def test_warning_text_carries_field_in_quotes(self):
        """现有渲染层用 ``'字段名'`` 引号模式反查字段 —— 文案必须能反推。"""
        v = qc.CorrectnessViolation(
            rule="negative_count",
            field="play_cnt",
            severity=qc.SEVERITY_ERROR,
            message="存在负值",
        )
        assert "'play_cnt'" in v.to_warning_text()

    def test_table_level_violation_has_no_field(self):
        v = qc.CorrectnessViolation(
            rule="pk_duplicate_rate",
            field="",
            severity=qc.SEVERITY_ERROR,
            message="主键重复率超标",
        )
        assert v.to_warning_text().startswith("[pk_duplicate_rate]")

    def test_explicit_warning_text_is_verbatim(self):
        """历史 checker 的文案已被前端消费 —— MUST 逐字保留。"""
        v = qc.CorrectnessViolation(
            rule="null_rate",
            field="col1",
            severity=qc.SEVERITY_WARNING,
            message="非空率低",
            warning_text="字段 'col1' 非空率 0.4500 < 阈值 0.80(空值率 0.5500)",
        )
        assert v.to_warning_text() == "字段 'col1' 非空率 0.4500 < 阈值 0.80(空值率 0.5500)"


# ============================================================================
# 渲染
# ============================================================================
class TestRenderViolationsTable:
    def test_empty_returns_empty_string(self):
        assert qc.render_violations_table([]) == ""
        assert qc.render_violations_table(None) == ""

    def test_renders_header_and_rows(self):
        violations = [
            qc.CorrectnessViolation(
                rule="pk_duplicate_rate",
                field="udid",
                severity=qc.SEVERITY_ERROR,
                message="主键重复率 0.010000 > 阈值 0.000000",
                evidence={"duplicate_rate": 0.01},
                suggestion="检查 GROUP BY",
            )
        ]
        table = qc.render_violations_table(violations)
        assert "| 规则 | 字段 | 级别 | 结论 | 证据 | 修正建议 |" in table
        assert "udid" in table
        assert "检查 GROUP BY" in table

    def test_accepts_dict_form(self):
        """违规常以序列化形式跨进程传递 —— 渲染器 MUST 兼容。"""
        table = qc.render_violations_table(
            [
                {
                    "rule": "pk_duplicate_rate",
                    "field": "udid",
                    "severity": "error",
                    "message": "主键重复",
                    "evidence": {},
                    "suggestion": "检查 GROUP BY",
                }
            ]
        )
        assert "udid" in table

    def test_pipe_in_message_is_escaped(self):
        """竖线 MUST 转义 —— 否则表格列会错位。"""
        violations = [
            qc.CorrectnessViolation(
                rule="common_sense",
                field="a|b",
                severity=qc.SEVERITY_ERROR,
                message="有问题",
            )
        ]
        table = qc.render_violations_table(violations)
        assert "\\|" in table


# ============================================================================
# 配置解析
# ============================================================================
class TestCorrectnessConfigResolution:
    def test_defaults(self):
        config = qc.resolve_config(None)
        assert config.primary_key_columns == ()
        assert config.count_fields == frozenset()
        assert config.common_sense_sample_size == 20
        assert config.reconcile_sample_size == 3
        assert config.self_correct_enabled is True
        assert config.self_correct_max_rounds == 3

    def test_tool_config_overrides(self):
        tool_cfg = {
            "quality_check": {
                "primary_key_columns": ["udid"],
                "count_fields": ["play_cnt"],
                "common_sense_sample_size": 5,
                "reconcile_sample_size": 2,
                "self_correct_enabled": False,
                "self_correct_max_rounds": 1,
            }
        }
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.primary_key_columns == ("udid",)
        assert config.count_fields == frozenset({"play_cnt"})
        assert config.common_sense_sample_size == 5
        assert config.reconcile_sample_size == 2
        assert config.self_correct_enabled is False
        assert config.self_correct_max_rounds == 1

    def test_thresholds_overridden(self):
        tool_cfg = {"quality_check": {"thresholds": {"pk_duplicate_rate_threshold": 0.01}}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.threshold_for(qc.PK_DUPLICATE_CHECKER) == 0.01

    def test_correctness_checks_can_be_disabled(self):
        tool_cfg = {"quality_check": {"correctness_checks": []}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert config.correctness_checkers == ()

    def test_correctness_checks_subset(self):
        tool_cfg = {"quality_check": {"correctness_checks": ["negative_count"]}}
        config = qc.resolve_config(_StubContext(tool_cfg=tool_cfg, resource_cfg={}))
        assert [c.name for c in config.correctness_checkers] == ["negative_count"]


class _StubContext:
    """最小化的 ``ToolExecutionContext`` 替身（只提供 config 解析要用的两个字段）。"""

    def __init__(self, *, tool_cfg: dict, resource_cfg: dict):
        self.tool_config = tool_cfg
        self.runtime = None
        self._resource_cfg = resource_cfg

    def get_resource_metadata(self):  # pragma: no cover - 未使用
        return None
