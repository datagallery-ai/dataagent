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
"""「每一项校验的实际查出来的值」的采集与提炼单测。

核心契约：**每一项**校验都要能给出它算在**哪些实际取值**上 ——
字段级校验给逐字段取值、主键重复率给重复的主键明细、行数给实测行数。
"""

from __future__ import annotations

import asyncio

from dataagent.actions.tools.local_tool import dataops_quality_examples as ex

#: 一份「抽样到 3 行」的样本 —— 覆盖空值、重复值、越界值、负值。
_SAMPLE = [
    {"row_index": 1, "cells": {"udid": "u1", "cnt": 3, "rate": 0.5, "delta": -2}},
    {"row_index": 2, "cells": {"udid": "u2", "cnt": 3, "rate": 1.5, "delta": 1}},
    {"row_index": 3, "cells": {"udid": "u3", "cnt": None, "rate": 0.2, "delta": -5}},
]

_FIELDS = ["udid", "cnt", "rate", "delta"]


# ============================================================================
# 逐字段取值统计
# ============================================================================
class TestFieldValueStats:
    def test_counts_null_and_present(self):
        stats = ex.field_value_stats(_SAMPLE, ["cnt"])

        assert stats["cnt"]["total"] == 3
        assert stats["cnt"]["null_count"] == 1

    def test_finds_the_most_frequent_value(self):
        """单一值比率看的就是「最高频取值」—— 统计必须找得到它。"""
        stats = ex.field_value_stats(_SAMPLE, ["cnt"])

        assert stats["cnt"]["top"] == ("3", 2)

    def test_finds_out_of_range_values(self):
        """异常值占比看的是越界取值（<0 或 >1）。"""
        stats = ex.field_value_stats(_SAMPLE, ["rate"])

        assert stats["rate"]["outliers"] == [1.5]

    def test_finds_negative_values(self):
        stats = ex.field_value_stats(_SAMPLE, ["delta"])

        assert stats["delta"]["negatives"] == [-2, -5]

    def test_boolean_is_not_treated_as_a_number(self):
        """``True`` 不是 1 —— 否则布尔字段会被算成「越界取值」。"""
        stats = ex.field_value_stats(
            [{"row_index": 1, "cells": {"flag": True}}],
            ["flag"],
        )

        assert stats["flag"]["outliers"] == []
        assert stats["flag"]["negatives"] == []

    def test_missing_field_is_reported_as_not_sampled(self):
        stats = ex.field_value_stats(_SAMPLE, ["not_there"])

        assert stats["not_there"]["total"] == 0


# ============================================================================
# 样本形状兼容
# ============================================================================
class TestNormalizeSampleRows:
    def test_accepts_wrapped_shape(self):
        rows = ex.normalize_sample_rows([{"row_index": 7, "cells": {"a": 1}}])

        assert rows == [{"row_index": 7, "cells": {"a": 1}}]

    def test_accepts_raw_row_shape(self):
        """流程型校验记进 notes 的是**原始行**（没有 ``cells`` 包装）。"""
        rows = ex.normalize_sample_rows([{"a": 1, "b": 2}])

        assert rows == [{"row_index": 1, "cells": {"a": 1, "b": 2}}]

    def test_non_list_input_yields_empty(self):
        assert ex.normalize_sample_rows(None) == []
        assert ex.normalize_sample_rows("nope") == []


# ============================================================================
# 逐项示例组装
# ============================================================================
class TestBuildCheckExamples:
    def _build(self, **overrides):
        kwargs = {
            "check_sqls": {
                "null_rate": "SELECT null_rate",
                "single_value_ratio": "SELECT svr",
                "outlier_ratio": "SELECT out",
                "negative_count": "SELECT neg",
                "row_count": "SELECT COUNT(1)",
                "pk_duplicate_rate": "SELECT pk",
            },
            "fields": _FIELDS,
            "check_fields": {
                "null_rate": _FIELDS,
                "single_value_ratio": _FIELDS,
                "outlier_ratio": ["rate"],
                "negative_count": ["cnt", "delta"],
            },
            "sample_rows": _SAMPLE,
            "table_metrics": {"row_count": 1000},
            "duplicate_rows": [{"udid": "u1", "cnt": 3}],
            "duplicate_ok": True,
            "primary_key_columns": ["udid"],
            "flow_notes": {"common_sense": {"sample_rows": 20}},
            "check_names": [
                "null_rate",
                "single_value_ratio",
                "outlier_ratio",
                "negative_count",
                "row_count",
                "pk_duplicate_rate",
                "common_sense",
            ],
        }
        kwargs.update(overrides)
        return ex.build_check_examples(**kwargs)

    def test_every_check_gets_an_entry(self):
        """**每一项**校验都要有示例 —— 缺一项用户就少一份证据。"""
        result = self._build()

        assert set(result) == {
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
            "negative_count",
            "row_count",
            "pk_duplicate_rate",
            "common_sense",
        }

    def test_null_rate_shows_the_actual_values(self):
        """非空率这一项必须给出**实际取值**，而不只是 0.9。"""
        result = self._build()

        text = result["null_rate"]["fields"]["cnt"]
        assert "3" in text
        assert "空 1/3 行" in text

    def test_single_value_ratio_shows_the_top_value(self):
        result = self._build()

        assert "最高频取值 3（2/3 行）" in result["single_value_ratio"]["fields"]["cnt"]

    def test_outlier_ratio_only_covers_its_applicable_fields(self):
        """异常值占比只对比率类字段 —— 计数字段 3 不该被当成越界值。"""
        result = self._build()

        assert set(result["outlier_ratio"]["fields"]) == {"rate"}
        assert "1.5" in result["outlier_ratio"]["fields"]["rate"]

    def test_negative_count_only_covers_count_fields(self):
        result = self._build()

        assert set(result["negative_count"]["fields"]) == {"cnt", "delta"}
        assert "-2" in result["negative_count"]["fields"]["delta"]

    def test_row_count_shows_the_measured_value(self):
        result = self._build()

        assert result["row_count"]["summary"] == "实测 1000 行"

    def test_row_count_unknown_is_stated_explicitly(self):
        result = self._build(table_metrics={})

        assert "未完成" in result["row_count"]["summary"]

    def test_pk_duplicate_rate_lists_the_duplicate_keys(self):
        """主键重复率必须给出**具体哪几组主键重复**。"""
        result = self._build()

        assert "u1" in result["pk_duplicate_rate"]["summary"]
        assert "u1（3 行）" in result["pk_duplicate_rate"]["groups"]

    def test_pk_duplicate_rate_distinguishes_no_duplicate_from_no_result(self):
        """「查过了没重复」与「没查成」必须能区分。"""
        checked = self._build(duplicate_rows=[], duplicate_ok=True)
        failed = self._build(duplicate_rows=[], duplicate_ok=False)

        assert "未发现重复" in checked["pk_duplicate_rate"]["summary"]
        assert "未取到" in failed["pk_duplicate_rate"]["summary"]

    def test_flow_check_shows_the_sample_size(self):
        result = self._build()

        assert "抽样 20 行" in result["common_sense"]["summary"]

    def test_the_sql_of_each_check_is_carried(self):
        """每项校验的 SQL 要能追溯 —— 用户要验证「这个结论怎么算出来的」。"""
        result = self._build()

        assert result["null_rate"]["sql"] == "SELECT null_rate"
        assert result["pk_duplicate_rate"]["sql"] == "SELECT pk"

    def test_check_order_is_fixed(self):
        """顺序固定 —— 用户不用每次重新扫。"""
        result = self._build()

        expected = [name for name in ex.CHECK_ORDER if name in result]
        assert list(result) == expected

    def test_no_sample_rows_yields_an_explicit_placeholder(self):
        """抽不到样本要说清原因 —— 不能留一个空摘要。"""
        result = self._build(sample_rows=[])

        assert "无取值示例" in result["null_rate"]["summary"]


# ============================================================================
# 流程型校验的示例（常识 / 回源比对）
# ============================================================================
class TestFlowCheckExamples:
    def _build(self, **overrides):
        flow_notes = {
            "common_sense": {
                "status": "completed",
                "sample_rows": 2,
                "sample_data": [
                    {"udid": "u1", "cnt": 3, "rate": 0.5},
                    {"udid": "u2", "cnt": 0, "rate": 1.5},
                ],
            },
            "source_reconciliation": {
                "status": "completed",
                "sample_rows": 2,
                "sample_data": [
                    {"udid": "u1", "cnt": 3},
                    {"udid": "u2", "cnt": 0},
                ],
                "comparisons": [
                    {
                        "row_index": 1,
                        "feature_row": {"udid": "u1", "cnt": 3},
                        "source_row": {"udid": "u1", "cnt": 3},
                    },
                    {
                        "row_index": 2,
                        "feature_row": {"udid": "u2", "cnt": 0},
                        "source_row": {"udid": "u2", "cnt": 7},
                    },
                ],
            },
        }
        kwargs = {
            "check_sqls": {},
            "fields": ["udid", "cnt"],
            "check_fields": {},
            "sample_rows": [],
            "flow_notes": flow_notes,
            "check_names": ["common_sense", "source_reconciliation"],
        }
        kwargs.update(overrides)
        return ex.build_check_examples(**kwargs)

    def test_common_sense_carries_its_sampled_rows(self):
        """常识检测的**实际抽样行**必须在这一项里 —— 不是只说一句「抽了 20 行」。"""
        result = self._build()

        assert len(result["common_sense"]["rows"]) == 2
        assert result["common_sense"]["rows"][0]["cells"]["udid"] == "u1"
        assert "u1" in result["common_sense"]["sample_text"]

    def test_common_sense_states_the_sample_size(self):
        result = self._build()

        assert "抽样 2 行" in result["common_sense"]["summary"]

    def test_reconciliation_carries_its_sampled_rows(self):
        result = self._build()

        assert len(result["source_reconciliation"]["rows"]) == 2

    def test_reconciliation_shows_both_sides_of_the_comparison(self):
        """回源比对的核心证据是**两侧的取值** —— 特征表的值 vs 回源的值。"""
        result = self._build()

        groups = result["source_reconciliation"]["groups"]
        assert "特征表=3 / 回源=3" in groups
        assert "特征表=0 / 回源=7" in groups

    def test_reconciliation_marks_the_mismatched_values(self):
        """不一致的取值必须**标出来** —— 否则用户看不出哪里对不上。"""
        result = self._build()

        assert "⚠️不一致" in result["source_reconciliation"]["groups"]

    def test_no_sample_data_yields_an_explicit_placeholder(self):
        """没抽到数据要说清 —— 不能留一句「抽样 0 行」糊弄过去。"""
        result = self._build(
            flow_notes={
                "common_sense": {"status": "completed", "sample_rows": 0},
                "source_reconciliation": {"status": "completed"},
            }
        )

        assert "未抽到样本数据" in result["common_sense"]["summary"]
        assert result["common_sense"]["rows"] == []


# ============================================================================
# 重复主键查询
# ============================================================================
class TestDuplicateKeyQuery:
    def test_sql_groups_by_the_primary_key(self):
        sql = ex.build_duplicate_key_sql("db.tbl", ["udid"], partition_filter="pt_d = '20261123'")

        assert "GROUP BY `udid`" in sql
        assert "HAVING COUNT(1) > 1" in sql
        assert "pt_d = '20261123'" in sql
        assert "`db`.`tbl`" in sql

    def test_composite_primary_key_groups_by_all_columns(self):
        sql = ex.build_duplicate_key_sql("db.tbl", ["udid", "app_type"])

        assert "GROUP BY `udid`, `app_type`" in sql

    def test_empty_input_yields_no_sql(self):
        assert ex.build_duplicate_key_sql("db.tbl", []) == ""
        assert ex.build_duplicate_key_sql("", ["udid"]) == ""

    def test_unsafe_table_name_yields_no_sql(self):
        """表名不合法 → 不拼 SQL（拼出来就是一条注入语句）。"""
        assert ex.build_duplicate_key_sql("db.tbl; DROP TABLE x", ["udid"]) == ""
        assert ex.build_duplicate_key_sql("db.", ["udid"]) == ""

    def test_unsafe_column_name_yields_no_sql(self):
        """主键列名不合法 → 不拼 SQL。"""
        assert ex.build_duplicate_key_sql("db.tbl", ["udid`; DROP TABLE x"]) == ""
        assert ex.build_duplicate_key_sql("db.tbl", ["*"]) == ""

    def test_unsafe_partition_filter_yields_no_sql(self):
        """分区条件不合法 → 不拼 SQL。"""
        assert (
            ex.build_duplicate_key_sql(
                "db.tbl", ["udid"], partition_filter="pt_d = '20261123'; DROP TABLE x"
            )
            == ""
        )
        assert (
            ex.build_duplicate_key_sql(
                "db.tbl", ["udid"], partition_filter="1 = 1"
            )
            == ""
        )

    def test_safe_input_still_builds_sql(self):
        """白名单没有误伤正常输入。"""
        sql = ex.build_duplicate_key_sql(
            "db.tbl", ["udid"], partition_filter="pt_d = '20261123'"
        )

        assert "FROM `db`.`tbl`" in sql
        assert "WHERE pt_d = '20261123' AND `udid` IS NOT NULL" in sql

    def test_capture_reports_success_without_duplicates(self):
        async def _execute(sql):
            return "completed", []

        ok, rows = asyncio.run(
            ex.capture_duplicate_keys(
                execute=_execute,
                table="db.tbl",
                primary_key_columns=["udid"],
            )
        )

        assert ok is True
        assert rows == []

    def test_capture_reports_failure_distinctly(self):
        """执行失败 → 「没查成」，不能读成「查过了没重复」。"""
        async def _execute(sql):
            return "error", None

        ok, rows = asyncio.run(
            ex.capture_duplicate_keys(
                execute=_execute,
                table="db.tbl",
                primary_key_columns=["udid"],
            )
        )

        assert ok is False
        assert rows == []

    def test_capture_swallows_exceptions(self):
        """查询挂了不能把整轮校验炸掉 —— 缺了示例只影响报告完整度。"""
        async def _execute(sql):
            raise RuntimeError("boom")

        ok, rows = asyncio.run(
            ex.capture_duplicate_keys(
                execute=_execute,
                table="db.tbl",
                primary_key_columns=["udid"],
            )
        )

        assert ok is False
        assert rows == []
