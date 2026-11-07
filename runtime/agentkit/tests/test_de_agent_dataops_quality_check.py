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
"""Row-count tests migrated unchanged from main !561 (native plugin imports)."""

import sys
from pathlib import Path

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
LOADER = PythonLoader()
qc = sys.modules[LOADER.load(PLUGIN, "utils/dataops_quality_check.py:resolve_config").__module__]

class TestRowCountChecker:
    def test_sql_is_full_table_count(self):
        """行数必须全量 COUNT —— 不能有派生表、不能有 LIMIT。

        采样算比率，算不出真实行数：前 10000 行永远是 10000。
        """
        statements = qc.build_quality_check_sqls(
            "adhoctemp.tmp_x", ["col1"], "pt_d", "20261010", 10000
        )
        sql = dict(statements)["row_count"]
        assert "COUNT(1) AS row_count" in sql
        assert "FROM adhoctemp.tmp_x" in sql
        assert "WHERE pt_d = '20261010'" in sql
        assert "LIMIT" not in sql
        assert "_src" not in sql

    def test_uses_explicit_partition_filter(self):
        """直连模式传入的分区条件优先（跟其它校验项同一口径）。"""
        statements = qc.build_quality_check_sqls(
            "biads.ads_t",
            ["col1"],
            "pt_d",
            "20261010",
            10000,
            partition_filter="pt_d = '20261020'",
        )
        assert "WHERE pt_d = '20261020'" in dict(statements)["row_count"]

    def test_parsing_writes_to_table_metrics_not_details(self):
        """行数是表级指标 —— 写进 table_metrics，不挂在字段上。"""
        parsed = qc.parse_checker_result(
            {"data": [{"row_count": 1234}]},
            qc.ROW_COUNT_CHECKER,
            ["col1", "col2"],
        )
        assert parsed["table_metrics"] == {"row_count": 1234.0}
        assert parsed["details"] == {}
        assert parsed["ok"] is True
        assert parsed["warnings"] == []

    def test_zero_rows_raises_warning(self):
        """0 行 → 低于默认阈值 1 → 告警。"""
        parsed = qc.parse_checker_result(
            {"data": [{"row_count": 0}]},
            qc.ROW_COUNT_CHECKER,
            ["col1"],
        )
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1
        assert "行数" in parsed["warnings"][0]

    def test_threshold_is_a_lower_bound(self):
        """阈值语义是「下限」—— 行数**少于**阈值才违规。"""
        config = qc.QualityCheckConfig(
            thresholds={"row_count_threshold": 100.0},
            checker_names=("row_count",),
        )
        # 50 < 100 → 违规
        low = qc.parse_checker_result(
            {"data": [{"row_count": 50}]}, qc.ROW_COUNT_CHECKER, ["col1"], config
        )
        assert low["ok"] is False
        # 500 > 100 → 不违规
        high = qc.parse_checker_result(
            {"data": [{"row_count": 500}]}, qc.ROW_COUNT_CHECKER, ["col1"], config
        )
        assert high["ok"] is True

    def test_merge_keeps_table_metrics_separate(self):
        merged = qc.merge_checker_results(
            [
                (
                    "null_rate",
                    qc.parse_checker_result(
                        {"data": [{"col1": 0.9}]}, qc.NULL_RATE_CHECKER, ["col1"]
                    ),
                ),
                (
                    "row_count",
                    qc.parse_checker_result(
                        {"data": [{"row_count": 500}]}, qc.ROW_COUNT_CHECKER, ["col1"]
                    ),
                ),
            ],
            ["col1"],
        )
        assert merged["table_metrics"] == {"row_count": 500.0}
        # 字段级 details 不该被表级指标污染。
        assert "row_count" not in merged["details"]["col1"]

    def test_failed_table_check_does_not_mark_fields_incomplete(self):
        """表级 checker 失败不该把逐字段指标标成「未完成」。"""
        merged = qc.merge_checker_results(
            [("null_rate", qc.parse_checker_result(
                {"data": [{"col1": 0.9}]}, qc.NULL_RATE_CHECKER, ["col1"]
            ))],
            ["col1"],
        )
        assert merged["table_metrics"] == {"row_count": None}


class TestRenderTableMetrics:
    def test_empty_returns_blank(self):
        assert qc.render_table_metrics({}) == ""
        assert qc.render_table_metrics(None) == ""

    def test_row_count_rendered_as_integer(self):
        table = qc.render_table_metrics({"row_count": 1234.0})
        assert "| 数据行数 | 1234 |" in table
        assert table.splitlines()[0] == "| 表级指标 | 值 |"

    def test_none_renders_as_not_completed(self):
        table = qc.render_table_metrics({"row_count": None})
        assert "| 数据行数 | 未完成 |" in table

    def test_truncated_marker_notes_lower_bound(self):
        """试算写入被截断时，行数只是下界 —— 表格里要注明。"""
        table = qc.render_table_metrics({"row_count": 10000.0}, truncated=True)
        assert "试算写入被截断" in table
        assert "| 10000 |" in table

