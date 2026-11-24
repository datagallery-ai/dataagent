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
"""数据质量校验报告的**固定形状**单测。

两条核心契约：

1. **形状固定** —— 十个小节永远按同一顺序出现，内容为空时写明「（无内容）」，
   而不是让小节凭空消失。用户不需要每次重新扫结构。
2. **每一项都有数据示例** —— 报告里能看到**实际查到的原始行**，而不只是
   聚合值。
"""

from __future__ import annotations

from dataagent.actions.tools.local_tool import dataops_quality_report_format as fmt


def _quality(**overrides) -> dict:
    """一份「校验跑完了」的 quality_check 结果。"""
    quality = {
        "ok": True,
        "warnings": [],
        "violations": [],
        "details": {
            "pay_cnt_udid": {
                "null_rate": 0.95,
                "single_value_ratio": 0.12,
                "outlier_ratio": None,
            },
        },
        "sampled": True,
        "sample_size": 10000,
        "checks": ["null_rate", "single_value_ratio", "outlier_ratio"],
        "completed_checks": ["null_rate", "single_value_ratio", "outlier_ratio"],
        "table_metrics": {"row_count": 1000},
        "primary_key_columns": ["udid"],
        "sample_rows": [
            {"row_index": 1, "cells": {"udid": "u1", "pay_cnt_udid": 3}},
            {"row_index": 2, "cells": {"udid": "u2", "pay_cnt_udid": 0}},
        ],
    }
    quality.update(overrides)
    return quality


# ============================================================================
# 形状固定
# ============================================================================
class TestFixedShape:
    def test_every_section_is_always_present(self):
        """**空的小节也必须出现** —— 消失会让用户分不清「没查」与「没问题」。"""
        report = fmt.render_fixed_report(_quality(), scope="目标表 `t`")

        for title in fmt.FIXED_SECTIONS:
            assert f"### {title}" in report, f"缺少小节：{title}"

    def test_sections_appear_in_a_fixed_order(self):
        report = fmt.render_fixed_report(_quality(), scope="目标表 `t`")

        positions = [report.index(f"### {title}") for title in fmt.FIXED_SECTIONS]
        assert positions == sorted(positions), "小节顺序变了"

    def test_shape_does_not_depend_on_the_result(self):
        """**空结果与满结果的形状必须一样** —— 否则用户又要重新学怎么看。"""
        empty = fmt.render_fixed_report({}, scope="目标表 `t`")
        full = fmt.render_fixed_report(_quality(), scope="目标表 `t`")

        assert [line for line in empty.splitlines() if line.startswith("### ")] == [
            line for line in full.splitlines() if line.startswith("### ")
        ]

    def test_empty_section_says_it_is_empty(self):
        """空内容要写明「（无内容）」—— 不能留一个光秃秃的标题。"""
        report = fmt.render_fixed_report(_quality(violations=[]), scope="t")

        assert "### 正确性违规明细\n（无违规）" in report


# ============================================================================
# 数据示例
# ============================================================================
class TestSampleData:
    def test_sample_rows_are_rendered_as_a_table(self):
        report = fmt.render_fixed_report(_quality(), scope="t")

        assert "### 数据示例" in report
        assert "udid" in report
        assert "u1" in report
        assert "pay_cnt_udid" in report

    def test_sample_rows_show_the_actual_values(self):
        body = fmt.render_sample_rows(
            [{"row_index": 1, "cells": {"app_id": "com.a", "cnt": 3}}]
        )

        assert "com.a" in body
        assert "cnt" in body
        # 行号必须标出来 —— 用户要说清「哪一行」。
        assert "| 1 |" in body

    def test_missing_sample_data_is_explained_not_silently_empty(self):
        """没有数据也要说清为什么 —— 空着不写会被当成「忘了」。"""
        report = fmt.render_fixed_report(_quality(sample_rows=[]), scope="t")

        assert "### 数据示例" in report
        assert "未抽到数据示例" in report

    def test_sample_data_escapes_pipes(self):
        """值里有 ``|`` 会把表格列冲散 —— 必须转义。"""
        body = fmt.render_sample_rows(
            [{"row_index": 1, "cells": {"tags": "a|b"}}]
        )

        assert "a\\|b" in body

    def test_null_cells_render_as_dash(self):
        body = fmt.render_sample_rows(
            [{"row_index": 1, "cells": {"a": None}}]
        )

        assert "| — |" in body

    def test_flow_check_sample_data_is_rendered(self):
        """**每一项流程型校验**都要有自己的抽样数据。"""
        quality = _quality(
            flow_notes={
                "common_sense": {
                    "status": "completed",
                    "sample_rows": 20,
                    "sample_data": [
                        {"row_index": 1, "cells": {"app_id": "com.a"}},
                    ],
                },
                "source_reconciliation": {
                    "status": "completed",
                    "sample_data": [
                        {"row_index": 1, "cells": {"udid": "u1"}},
                    ],
                },
            }
        )

        report = fmt.render_fixed_report(quality, scope="t")

        assert "常识违规检测抽样数据" in report
        assert "抽样回源比对抽样数据" in report
        assert "com.a" in report
        assert "u1" in report


# ============================================================================
# 校验项执行情况
# ============================================================================
class TestCheckStatus:
    def test_every_check_gets_a_row(self):
        """**每一个**校验项一行状态 —— 完成 / 未完成 / 已跳过，各带原因。"""
        quality = _quality(
            checks=["null_rate", "pk_duplicate_rate", "common_sense"],
            completed_checks=["null_rate"],
            failed_checks=[{"name": "common_sense", "reason": "超时"}],
            skipped_checks=[{"name": "pk_duplicate_rate", "reason": "未提供主键列"}],
        )

        report = fmt.render_fixed_report(quality, scope="t")

        assert "null_rate" in report
        assert "✅ 完成" in report
        assert "❌ 未完成" in report
        assert "超时" in report
        assert "⚪ 已跳过" in report
        assert "未提供主键列" in report

    def test_skipped_check_is_not_reported_as_passed(self):
        """被跳过的项**不能**读成「查过了没问题」。"""
        quality = _quality(
            skipped_checks=[{"name": "pk_duplicate_rate", "reason": "未提供主键列"}],
            violations=[],
        )

        report = fmt.render_fixed_report(quality, scope="t")

        assert "因信息不足被跳过" in report
        assert "pk_duplicate_rate" in report


# ============================================================================
# 各校验项数据示例
# ============================================================================
class TestCheckExamples:
    def _quality(self, **overrides):
        examples = {
            "null_rate": {
                "label": "空值覆盖率",
                "summary": "`cnt`=3（空 1/3 行）",
                "sql": "SELECT null_rate FROM t",
                "fields": {"cnt": "3（空 1/3 行）"},
                "groups": "",
            },
            "pk_duplicate_rate": {
                "label": "主键重复率",
                "summary": "重复的udid：u1（3 行）",
                "sql": "SELECT pk FROM t",
                "fields": {},
                "groups": "u1（3 行）",
            },
        }
        quality = _quality(check_examples=examples)
        quality.update(overrides)
        return quality

    def test_every_check_gets_its_actual_values(self):
        """**每一项**校验都要展示实际查出来的值 —— 缺一项就少一份证据。"""
        report = fmt.render_fixed_report(self._quality(), scope="t")

        assert "### 各校验项数据示例" in report
        assert "3（空 1/3 行）" in report
        assert "u1（3 行）" in report

    def test_the_section_is_always_present(self):
        """形状固定 —— 没有取值示例时也要有这一小节。"""
        report = fmt.render_fixed_report(_quality(check_examples={}), scope="t")

        assert "### 各校验项数据示例" in report
        assert "未产出各校验项的取值示例" in report

    def test_check_status_table_carries_the_actual_values(self):
        """状态表里也要能看出「查出来的是什么」 —— 不用翻到下一小节。"""
        report = fmt.render_fixed_report(self._quality(), scope="t")

        assert "实际查出来的值" in report
        assert "重复的udid：u1（3 行）" in report

    def test_the_sql_of_each_check_is_shown(self):
        """每项校验的 SQL 要能追溯 —— 用户要验证结论怎么算出来的。"""
        report = fmt.render_fixed_report(self._quality(), scope="t")

        assert "该校验项实际提交的 SQL" in report
        assert "SELECT null_rate FROM t" in report

    def test_field_metrics_table_has_an_examples_column(self):
        """字段级指标表要给一行取值示例 —— 指标与证据并排。"""
        report = fmt.render_fixed_report(self._quality(), scope="t")

        assert "实际取值示例" in report

    def test_example_values_escape_pipes(self):
        """值里有 ``|`` 会把表格列冲散 —— 必须转义。"""
        quality = self._quality()
        quality["check_examples"]["null_rate"]["fields"] = {"cnt": "a|b"}
        report = fmt.render_fixed_report(quality, scope="t")

        assert "a\\|b" in report

    def test_flow_check_sampled_rows_are_rendered(self):
        """**流程型校验**抽到的行也要在这一项里摊开 —— 结论后面必须跟数据。"""
        quality = self._quality()
        quality["check_examples"]["common_sense"] = {
            "label": "常识违规检测",
            "summary": "抽样 2 行",
            "sql": "",
            "fields": {},
            "groups": "",
            "rows": [
                {"row_index": 1, "cells": {"udid": "u1", "cnt": 3}},
                {"row_index": 2, "cells": {"udid": "u2", "cnt": 0}},
            ],
            "sample_text": "udid=u1，cnt=3",
        }
        report = fmt.render_fixed_report(quality, scope="t")

        assert "抽样到的实际数据" in report
        assert "第 1 行：udid=u1，cnt=3" in report

    def test_reconciliation_shows_both_sides(self):
        """回源比对的逐行明细要展示**两侧取值** —— 差在哪一眼可见。"""
        quality = self._quality()
        quality["check_examples"]["source_reconciliation"] = {
            "label": "抽样回源比对",
            "summary": "抽样 1 行；比对 1 行",
            "sql": "",
            "fields": {},
            "groups": "第 1 行：cnt：特征表=3 / 回源=7 ⚠️不一致",
            "rows": [],
            "sample_text": "",
        }
        report = fmt.render_fixed_report(quality, scope="t")

        assert "逐行比对明细" in report
        assert "特征表=3 / 回源=7" in report


# ============================================================================
# 结论
# ============================================================================
class TestConclusion:
    def test_error_violation_blocks_delivery(self):
        quality = _quality(
            ok=False,
            violations=[
                {
                    "rule": "pk_duplicate_rate",
                    "field": "主键",
                    "severity": "error",
                    "message": "重复率 0.5 超过阈值 0.0",
                }
            ],
        )

        report = fmt.render_fixed_report(quality, scope="t")

        assert "error 级违规" in report
        assert "不建议直接交付" in report

    def test_partial_completion_is_not_reported_as_passed(self):
        quality = _quality(
            partial=True,
            failed_checks=[{"name": "common_sense", "reason": "超时"}],
        )

        report = fmt.render_fixed_report(quality, scope="t")

        assert "未完整完成" in report
        assert "不等于校验通过" in report

    def test_direct_mode_never_claims_self_correction_ran(self):
        """直连模式**只读** —— 不能让用户以为自修正跑过。"""
        report = fmt.render_fixed_report(_quality(), scope="目标表 `t`", mode="direct")

        assert "未执行" in report
        assert "只读校验" in report

    def test_direct_mode_conclusion_uses_data_wording(self):
        """直连模式没有「交付物」可交付 —— 措辞必须跟着变。"""
        report = fmt.render_fixed_report(_quality(), scope="目标表 `t`", mode="direct")

        assert "数据可用" in report or "可按当前口径使用" in report
        assert "本表不建议直接交付" not in report

    def test_disputed_violation_points_at_the_rule_not_the_sql(self):
        """误报 ≠ SQL 有错 —— 结论要引导去复核规则。"""
        quality = _quality(
            ok=False,
            violations=[
                {
                    "rule": "common_sense",
                    "field": "target_conversion_rate",
                    "severity": "error",
                    "message": "违反常识",
                }
            ],
            self_correct={"status": "disputed", "reason": "实际值与期望值一致"},
        )

        report = fmt.render_fixed_report(quality, scope="t")

        assert "误报" in report
        assert "复核校验规则" in report


# ============================================================================
# 概况
# ============================================================================
class TestOverview:
    def test_overview_states_the_primary_key(self):
        """主键要写出来 —— 它是「哪些校验项能跑」的关键前提。"""
        report = fmt.render_fixed_report(_quality(primary_key_columns=["udid"]), scope="t")

        assert "udid" in report
        assert "主键列" in report

    def test_overview_states_when_the_primary_key_is_missing(self):
        report = fmt.render_fixed_report(_quality(primary_key_columns=[]), scope="t")

        assert "未提供" in report
        assert "已跳过" in report

    def test_overview_states_the_field_source(self):
        """字段来源必须写出来。

        字段由**用户显式指定**时，结论的口径取决于用户给的字段（可能写错、
        可能表结构后来变了）—— 用户必须看到「这一轮的字段是我给的」。
        """
        report = fmt.render_fixed_report(
            _quality(field_source="user_fields"), scope="t"
        )

        assert "字段来源" in report
        assert "用户显式指定的字段" in report

    def test_overview_omits_the_field_source_when_unknown(self):
        report = fmt.render_fixed_report(_quality(), scope="t")

        assert "字段来源" not in report

    def test_overview_names_a_ddl_field_source(self):
        report = fmt.render_fixed_report(_quality(field_source="ddl"), scope="t")

        assert "用户提供的建表 DDL" in report
