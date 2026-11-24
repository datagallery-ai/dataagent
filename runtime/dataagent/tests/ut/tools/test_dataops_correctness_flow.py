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
"""Unit tests for the flow-type correctness checkers.

Covers:

- :mod:`dataops_correctness_flow` —— 共享基座（只读白名单 / 数值容差 / 抽样）；
- :mod:`dataops_common_sense` —— 常识违规检测（需求 1.3）；
- :mod:`dataops_source_reconciliation` —— 抽样回源比对（需求 1.4）。
"""

from __future__ import annotations

import pytest

from dataagent.actions.tools.local_tool import dataops_common_sense as cs
from dataagent.actions.tools.local_tool import dataops_correctness_flow as flow
from dataagent.actions.tools.local_tool import dataops_source_reconciliation as sr
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CorrectnessViolation,
)


# ============================================================================
# 只读 SQL 白名单
# ============================================================================
class TestIsReadOnlySql:
    def test_plain_select_allowed(self):
        assert flow.is_read_only_sql("SELECT a, b FROM t WHERE pt_d = '20261010'") is True

    def test_with_cte_allowed(self):
        sql = "WITH x AS (SELECT 1 AS a) SELECT a FROM x"
        assert flow.is_read_only_sql(sql) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t SELECT 1",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "TRUNCATE TABLE t",
            "CREATE TABLE t (a int)",
            "ALTER TABLE t ADD COLUMN b int",
            "REPLACE INTO t SELECT 1",
            "MERGE INTO t USING s ON 1=1",
        ],
    )
    def test_write_statements_rejected(self, sql: str):
        assert flow.is_read_only_sql(sql) is False

    def test_multi_statement_rejected(self):
        assert flow.is_read_only_sql("SELECT 1; SELECT 2") is False

    def test_non_select_rejected(self):
        assert flow.is_read_only_sql("SHOW TABLES") is False

    def test_empty_rejected(self):
        assert flow.is_read_only_sql("") is False
        assert flow.is_read_only_sql("   ") is False

    def test_comment_is_stripped(self):
        """注释里出现 insert 不该被拒。"""
        sql = "SELECT a FROM t -- insert into other\n"
        assert flow.is_read_only_sql(sql) is True

    def test_word_boundary_respected(self):
        """``created_at`` 这类标识符不该被当成 ``create`` 关键字。"""
        sql = "SELECT created_at FROM t WHERE pt_d = '20261010'"
        assert flow.is_read_only_sql(sql) is True

    def test_trailing_semicolon_ok(self):
        assert flow.is_read_only_sql("SELECT a FROM t;") is True


# ============================================================================
# 数值容差比对
# ============================================================================
class TestValuesMatch:
    def test_both_null_match(self):
        assert flow.values_match(None, None) is True

    def test_one_null_mismatch(self):
        assert flow.values_match(None, 1) is False
        assert flow.values_match(1, None) is False

    def test_exact_numbers_match(self):
        assert flow.values_match(1.0, 1.0) is True

    def test_string_numbers_match(self):
        assert flow.values_match("1.0", 1) is True

    def test_within_relative_tolerance(self):
        assert flow.values_match(100.0, 100.0000001, relative_tolerance=1e-6) is True

    def test_outside_tolerance(self):
        assert flow.values_match(100.0, 100.1, relative_tolerance=1e-6) is False

    def test_strings_compared_exactly(self):
        assert flow.values_match("abc", "abc") is True
        assert flow.values_match("abc", "abd") is False

    def test_whitespace_ignored(self):
        assert flow.values_match("abc ", " abc") is True

    def test_bool_not_treated_as_number(self):
        assert flow.values_match(True, 1) is False


# ============================================================================
# 抽样 SQL
# ============================================================================
class TestBuildSampleSql:
    def test_explicit_columns_and_limit(self):
        sql = flow.build_sample_sql("adhoctemp.tmp_x", ["a", "b"], limit=3)
        assert sql == "SELECT `a`, `b` FROM `adhoctemp`.`tmp_x` LIMIT 3"

    def test_partition_filter_included(self):
        sql = flow.build_sample_sql(
            "adhoctemp.tmp_x", ["a"], partition_filter="pt_d = '20261010'", limit=3
        )
        assert "WHERE pt_d = '20261010'" in sql

    def test_no_select_star(self):
        sql = flow.build_sample_sql("adhoctemp.tmp_x", ["a", "b"], limit=3)
        assert "*" not in sql

    def test_empty_columns_returns_empty(self):
        assert flow.build_sample_sql("adhoctemp.tmp_x", [], limit=3) == ""

    def test_unsafe_table_name_returns_empty(self):
        """表名不合法 → 不拼 SQL。"""
        assert flow.build_sample_sql("db.tbl; DROP TABLE x", ["a"], limit=3) == ""
        assert flow.build_sample_sql("db.", ["a"], limit=3) == ""

    def test_unsafe_column_name_returns_empty(self):
        """列名不合法 → 不拼 SQL。"""
        assert flow.build_sample_sql("db.tbl", ["a`; DROP TABLE x"], limit=3) == ""
        assert flow.build_sample_sql("db.tbl", ["*"], limit=3) == ""

    def test_unsafe_partition_filter_returns_empty(self):
        """分区条件不合法 → 不拼 SQL。"""
        assert (
            flow.build_sample_sql(
                "db.tbl",
                ["a"],
                partition_filter="pt_d = '20261010'; DROP TABLE x",
                limit=3,
            )
            == ""
        )


# ============================================================================
# FlowContext
# ============================================================================
class TestFlowContext:
    def test_column_quotes_once(self):
        ctx = flow.FlowContext(sql="", target_table="t")
        assert ctx.column("a") == "`a`"
        assert ctx.column("`a`") == "`a`"

    def test_qualified_table(self):
        ctx = flow.FlowContext(sql="", target_table="db.tbl")
        assert ctx.qualified_table() == "`db`.`tbl`"


# ============================================================================
# 常识违规检测
# ============================================================================
class TestCommonSense:
    @staticmethod
    def _ctx(executor=None, llm=object(), **kwargs):
        defaults = {
            "sql": "INSERT OVERWRITE TABLE t SELECT a, b FROM src",
            "target_table": "adhoctemp.tmp_x",
            "fields": ["age", "is_vip"],
            "column_types": {"age": "bigint", "is_vip": "string"},
            "column_comments": {"age": "用户年龄", "is_vip": "是否 VIP"},
            "partition_filter": "pt_d = '20261010'",
            "common_sense_sample_size": 20,
        }
        defaults.update(kwargs)
        return flow.FlowContext(execute_sql=executor, llm=llm, **defaults)

    @pytest.mark.asyncio
    async def test_reports_violation_with_evidence(self, monkeypatch):
        async def _executor(sql: str):
            return "completed", [{"age": -5, "is_vip": "1"}]

        payload = {
            "violations": [
                {
                    "field": "age",
                    "row_index": 1,
                    "observed_value": -5,
                    "reason": "年龄为负，不可能成立",
                    "severity": "error",
                    "expected": ">= 0",
                }
            ]
        }

        async def _fake(llm, *, system, user, llm_kwargs=None):
            assert "用户年龄" in user
            return payload

        monkeypatch.setattr(cs, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await cs.run_common_sense(ctx)

        assert len(violations) == 1
        v = violations[0]
        assert v.rule == "common_sense"
        assert v.field == "age"
        assert v.severity == SEVERITY_ERROR
        assert v.is_error is True
        assert v.evidence["observed_value"] == -5
        assert ctx.notes["common_sense__status"] == "completed"

    @pytest.mark.asyncio
    async def test_no_violation_returns_empty(self, monkeypatch):
        async def _executor(sql: str):
            return "completed", [{"age": 20, "is_vip": "0"}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"violations": []}

        monkeypatch.setattr(cs, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await cs.run_common_sense(ctx)
        assert violations == []
        assert ctx.notes["common_sense__status"] == "completed"

    @pytest.mark.asyncio
    async def test_llm_failure_degrades_to_incomplete(self, monkeypatch):
        async def _executor(sql: str):
            return "completed", [{"age": 20}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return None

        monkeypatch.setattr(cs, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await cs.run_common_sense(ctx)
        assert violations == []
        assert ctx.notes["common_sense__status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_llm_is_unavailable(self):
        ctx = self._ctx(llm=None)
        violations = await cs.run_common_sense(ctx)
        assert violations == []
        assert ctx.notes["common_sense__status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_empty_sample_is_skipped(self):
        async def _executor(sql: str):
            return "completed", []

        ctx = self._ctx(executor=_executor)
        violations = await cs.run_common_sense(ctx)
        assert violations == []
        assert ctx.notes["common_sense__status"] == "skipped"

    def test_parse_drops_items_without_reason(self):
        """模型说不出依据 → 丢弃（避免「不确定」被当成「违规」）。"""
        parsed = cs._parse_common_sense_violations(
            [
                {"field": "age", "reason": ""},
                {"field": "", "reason": "有依据但没字段"},
                {"field": "age", "reason": "年龄为负"},
            ],
            row_count=1,
        )
        assert len(parsed) == 1
        assert parsed[0].field == "age"

    def test_parse_rejects_out_of_range_row_index(self):
        parsed = cs._parse_common_sense_violations(
            [{"field": "age", "reason": "x", "row_index": 99}], row_count=3
        )
        assert "row_index" not in parsed[0].evidence

    def test_parse_defaults_to_warning_severity(self):
        parsed = cs._parse_common_sense_violations(
            [{"field": "age", "reason": "x"}], row_count=1
        )
        assert parsed[0].severity == SEVERITY_WARNING
        assert parsed[0].is_error is False


# ============================================================================
# 常识判定：数值精度差异不算违规
# ============================================================================
class TestCommonSenseRoundingTolerance:
    """四舍五入差异 MUST NOT 被判成违规。

    真实案例（``target_conversion_rate``）：模型算出 ``939 / 3327 * 1.2 ≈
    0.3387``，实测值 ``0.338683``，相对差 ``5e-5`` —— 这是**显示精度**
    差异，不是口径错误。但模型把它判成 ``error`` 级 → 触发自修正去改一份
    本身正确的 SQL。误报比漏报更糟。
    """

    #: 用户报的真实案例（模型输出原样）。
    _REAL_CASE = {
        "field": "target_conversion_rate",
        "row_index": 1,
        "observed_value": 0.338683,
        "reason": (
            "根据字段语义，target_conversion_rate = total_conversion_cnt / "
            "total_target_mask_cnt * 1.2，计算值为 939 / 3327 * 1.2 ≈ 0.3387，"
            "但实际值为 0.338683，与计算值不一致，违反了字段定义的公式规则"
        ),
        "severity": "error",
        "expected": (
            "0.338683 与公式计算结果 0.3387 基本一致，但精度差异可能为四舍五入，"
            "不视为违规"
        ),
    }

    def test_real_rounding_case_is_not_a_violation(self):
        """用户的真实案例 —— 必须判为「无违规」。"""
        parsed = cs._parse_common_sense_violations([dict(self._REAL_CASE)], row_count=20)
        assert parsed == [], f"四舍五入差异被误判成违规：{parsed}"

    def test_real_rounding_case_without_expected_still_dropped(self):
        """即使模型没给 ``expected``，靠数值比对也要能识别精度差异。"""
        item = dict(self._REAL_CASE)
        item.pop("expected")
        parsed = cs._parse_common_sense_violations([item], row_count=20)
        assert parsed == [], f"四舍五入差异被误判成违规：{parsed}"

    def test_real_violation_is_still_caught(self):
        """**真违规不能被放过** —— 口径明显不对（0.30 vs 0.3387）仍要报。"""
        item = {
            "field": "target_conversion_rate",
            "row_index": 1,
            "observed_value": 0.30,
            "reason": "计算值为 939 / 3327 * 1.2 ≈ 0.3387，但实际值为 0.30",
            "severity": "error",
            "expected": "0.3387",
        }
        parsed = cs._parse_common_sense_violations([item], row_count=20)
        assert len(parsed) == 1, "真违规被漏掉了"
        assert parsed[0].severity == SEVERITY_ERROR

    def test_large_discrepancy_is_still_caught(self):
        """差 3% —— 明显超出四舍五入精度，必须报。"""
        item = {
            "field": "target_conversion_rate",
            "row_index": 1,
            "observed_value": 0.3377,
            "reason": "计算值 0.3387 与实际值 0.3377 不一致",
            "severity": "error",
            "expected": "0.3387",
        }
        parsed = cs._parse_common_sense_violations([item], row_count=20)
        assert len(parsed) == 1, "差 0.3% 被误放过"

    def test_model_self_declares_not_a_violation(self):
        """模型自己说「不视为违规」→ 以它为准（它 reason 里也写「违反」）。"""
        item = {
            "field": "target_conversion_rate",
            "row_index": 1,
            "observed_value": 0.338683,
            "reason": "与计算值不一致，违反了字段定义的公式规则",
            "severity": "error",
            "expected": "0.338683 与 0.3387 基本一致，不视为违规",
        }
        parsed = cs._parse_common_sense_violations([item], row_count=20)
        assert parsed == []

    def test_non_numeric_violation_unaffected(self):
        """非数值类违规（年龄为负等）完全不受影响。"""
        item = {
            "field": "age",
            "row_index": 1,
            "observed_value": -5,
            "reason": "年龄为负，不符合常识",
            "severity": "error",
            "expected": "非负数",
        }
        parsed = cs._parse_common_sense_violations([item], row_count=20)
        assert len(parsed) == 1
        assert parsed[0].field == "age"

    def test_ratio_observed_value_not_treated_as_rounding(self):
        """实测值本身是**比值/区间**（多个数字）→ 不按精度差异处理。"""
        item = {
            "field": "rate",
            "row_index": 1,
            "observed_value": "0.1-0.2",
            "reason": "实际值 0.1-0.2 超出预期",
            "severity": "error",
            "expected": "0.3387",
        }
        parsed = cs._parse_common_sense_violations([item], row_count=20)
        assert len(parsed) == 1


class TestRoundingToleranceHelpers:
    """``matches_with_rounding`` / ``extract_numeric_candidates`` 的单元行为。"""

    def test_matches_with_rounding_accepts_rounded_value(self):
        assert flow.matches_with_rounding(0.3387, 0.338683) is True

    def test_matches_with_rounding_rejects_real_difference(self):
        assert flow.matches_with_rounding(0.3387, 0.30) is False

    def test_matches_with_rounding_rejects_non_numeric(self):
        assert flow.matches_with_rounding("abc", 0.3) is False

    def test_extract_numeric_candidates_from_prose(self):
        values = flow.extract_numeric_candidates(
            "计算值为 939 / 3327 * 1.2 ≈ 0.3387，但实际值为 0.338683"
        )
        assert 0.3387 in values
        assert 0.338683 in values
        assert 939 in values

    def test_extract_numeric_candidates_from_number(self):
        assert flow.extract_numeric_candidates(0.5) == (0.5,)

    def test_extract_numeric_candidates_empty(self):
        assert flow.extract_numeric_candidates("") == ()
        assert flow.extract_numeric_candidates(None) == ()


# ============================================================================
# 抽样回源比对
# ============================================================================
class TestSourceReconciliation:
    @staticmethod
    def _is_sample(sql: str) -> bool:
        """抽样那条 SELECT 打的是临时表；回源那条打的是源表。"""
        return "tmp_x" in sql

    @staticmethod
    def _ctx(executor=None, llm=object(), **kwargs):
        defaults = {
            "sql": (
                "INSERT OVERWRITE TABLE adhoctemp.tmp_x "
                "SELECT udid, SUM(amt) AS total_amt FROM dwd.order GROUP BY udid"
            ),
            "target_table": "adhoctemp.tmp_x",
            "fields": ["total_amt"],
            "primary_key_columns": ("udid",),
            "source_tables": ("dwd.order",),
            "partition_filter": "pt_d = '20261010'",
            "reconcile_sample_size": 3,
        }
        defaults.update(kwargs)
        return flow.FlowContext(execute_sql=executor, llm=llm, **defaults)

    @pytest.mark.asyncio
    async def test_matching_values_produce_no_violation(self, monkeypatch):
        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0}]
            return "completed", [{"total_amt": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        assert ctx.notes["source_reconciliation__status"] == "completed"
        assert ctx.notes["source_reconciliation__matched_rows"] == 1

    @pytest.mark.asyncio
    async def test_mismatch_produces_error_violation(self, monkeypatch):
        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0}]
            return "completed", [{"total_amt": 999.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        assert len(violations) == 1
        v = violations[0]
        assert v.rule == "source_reconciliation"
        assert v.field == "total_amt"
        assert v.severity == SEVERITY_ERROR
        assert v.evidence["feature_value"] == 100.0
        assert v.evidence["source_value"] == 999.0
        assert v.evidence["primary_key"] == {"udid": "u1"}

    @pytest.mark.asyncio
    async def test_write_sql_is_rejected(self, monkeypatch):
        """生成的 SQL 不是只读 → 丢弃，绝不执行。"""
        executed: list[str] = []

        async def _executor(sql: str):
            executed.append(sql)
            return "completed", [{"udid": "u1", "total_amt": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "DELETE FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        # 只有抽样那条 SELECT 被执行过。
        assert all("DELETE" not in s for s in executed)
        assert ctx.notes["source_reconciliation__status"] == "skipped"

    @pytest.mark.asyncio
    async def test_sql_without_partition_is_rejected(self, monkeypatch):
        async def _executor(sql: str):
            return "completed", [{"udid": "u1", "total_amt": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        unverifiable = ctx.notes["source_reconciliation__unverifiable"]
        assert any("分区" in item["reason"] for item in unverifiable)

    @pytest.mark.asyncio
    async def test_zero_source_rows_is_unverifiable_not_mismatch(self, monkeypatch):
        """源表 0 行 ≠ 不匹配（源表口径更严是常态）。"""

        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0}]
            return "completed", []

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        unverifiable = ctx.notes["source_reconciliation__unverifiable"]
        assert any("0 行" in item["reason"] for item in unverifiable)

    @pytest.mark.asyncio
    async def test_no_source_tables_is_skipped(self):
        ctx = self._ctx(source_tables=())
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        assert ctx.notes["source_reconciliation__status"] == "skipped"

    @pytest.mark.asyncio
    async def test_no_primary_key_is_skipped(self):
        ctx = self._ctx(primary_key_columns=())
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        assert ctx.notes["source_reconciliation__status"] == "skipped"

    @pytest.mark.asyncio
    async def test_row_index_survives_discarded_sql(self, monkeypatch):
        """某行的 SQL 被丢弃时，其余行的比对不能错位到别的行。"""
        rows = [
            {"udid": "u1", "total_amt": 1.0},
            {"udid": "u2", "total_amt": 2.0},
            {"udid": "u3", "total_amt": 3.0},
        ]
        calls: list[str] = []

        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", rows
            calls.append(sql)
            # 源表取值与特征表不一致 —— 每行都应各自命中自己那行。
            return "completed", [{"total_amt": -1.0}]

        generated = [
            {"sql": "DROP TABLE t WHERE pt_d = '20261010'"},  # 被丢弃
            {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"},
            {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"},
        ]
        seq = list(generated)

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return seq.pop(0)

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        # 被丢弃的是第 1 行 → 剩下 2 条 SQL 被执行。
        assert len(calls) == 2
        # 两条违规各自指向第 2、第 3 行（不能都指向第 2 行）。
        assert [v.evidence["row_index"] for v in violations] == [2, 3]

    # ------------------------------------------------------------------
    # 假阳性回归防护
    #
    # 历史上 ``matched += 1`` 发生在**字段比对之前**，且列名对不上时
    # ``_compare_row`` 会静默跳过所有字段 —— 结果是「一个字段都没比」却
    # 被报成「成功比对 N 行 / 全部通过」。这比不校验更危险：口径完全错误
    # 的 SQL 会顺利交付。
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_column_mismatch_is_unverifiable_not_passed(self, monkeypatch):
        """源表返回的列名跟特征字段名对不上 → **无法验证**，不能算「通过」。"""

        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0}]
            # 列名对不上（模型没按特征字段名起别名）。
            return "completed", [{"amt_sum": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS amt_sum FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        # **关键**：不能算「成功比对」。
        assert ctx.notes.get("source_reconciliation__matched_rows", 0) == 0
        assert ctx.notes.get("source_reconciliation__compared_fields", 0) == 0
        unverifiable = ctx.notes["source_reconciliation__unverifiable"]
        assert unverifiable, "列名对不上必须记为「无法验证」"
        assert any("未返回任何可对齐的字段列" in item["reason"] for item in unverifiable)
        # 「无法验证」的行也要能看到期望列与实际列。
        item = unverifiable[0]
        assert item["expected_columns"] == ["total_amt"]
        assert item["actual_columns"] == ["amt_sum"]

    @pytest.mark.asyncio
    async def test_partial_column_coverage_reports_missing_fields(self, monkeypatch):
        """部分列对不上 → 已比上的照常比，但**缺失的字段必须报出来**。"""

        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0, "cnt": 5}]
            # 只返回了 total_amt，缺 cnt。
            return "completed", [{"total_amt": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor, fields=["total_amt", "cnt"])
        violations = await sr.run_source_reconciliation(ctx)
        assert violations == []
        assert ctx.notes["source_reconciliation__matched_rows"] == 1
        assert ctx.notes["source_reconciliation__compared_fields"] == 1
        assert ctx.notes["source_reconciliation__missing_fields"] == ["cnt"]

    @pytest.mark.asyncio
    async def test_executed_sql_is_recorded_for_audit(self, monkeypatch):
        """执行过的回源 SQL MUST 记进 notes —— 用户要能看到「到底查了什么」。"""

        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0}]
            return "completed", [{"total_amt": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {
                "sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'",
                "reason": "按同一口径聚合",
            }

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        await sr.run_source_reconciliation(ctx)
        executed = ctx.notes["source_reconciliation__executed_sql"]
        assert len(executed) == 1
        assert "dwd.order" in executed[0]["sql"]
        assert executed[0]["row_index"] == 1
        assert executed[0]["reason"] == "按同一口径聚合"

    @pytest.mark.asyncio
    async def test_comparisons_record_source_values(self, monkeypatch):
        """比对明细 MUST 记录源表实际取到的值 —— 「查出来的数据」要能展示。"""

        async def _executor(sql: str):
            if self._is_sample(sql):
                return "completed", [{"udid": "u1", "total_amt": 100.0}]
            return "completed", [{"total_amt": 100.0}]

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {"sql": "SELECT SUM(amt) AS total_amt FROM dwd.order WHERE pt_d = '20261010'"}

        monkeypatch.setattr(sr, "invoke_llm_json", _fake)

        ctx = self._ctx(executor=_executor)
        await sr.run_source_reconciliation(ctx)
        comparisons = ctx.notes["source_reconciliation__comparisons"]
        assert len(comparisons) == 1
        item = comparisons[0]
        assert item["row_index"] == 1
        assert item["primary_key"] == {"udid": "u1"}
        assert item["compared_fields"] == 1
        assert item["source_row"] == {"total_amt": 100.0}
        assert item["feature_row"] == {"total_amt": 100.0}


# ============================================================================
# Violation 序列化
# ============================================================================
class TestViolationSerialization:
    def test_round_trip(self):
        v = CorrectnessViolation(
            rule="pk_duplicate_rate",
            field="udid",
            severity=SEVERITY_ERROR,
            message="主键重复",
            evidence={"duplicate_rate": 0.01},
            suggestion="检查 GROUP BY",
        )
        restored = CorrectnessViolation.from_dict(v.to_dict())
        assert restored.rule == v.rule
        assert restored.field == v.field
        assert restored.severity == v.severity
        assert restored.message == v.message
        assert restored.evidence == v.evidence
        assert restored.suggestion == v.suggestion
        assert restored.is_error is True

    def test_from_dict_defaults_to_warning(self):
        v = CorrectnessViolation.from_dict({"rule": "x", "message": "y"})
        assert v.severity == SEVERITY_WARNING
        assert v.is_error is False


# ============================================================================
# Flow checker 聚合器
# ============================================================================
class TestFlowCheckers:
    def test_builds_both_checkers(self):
        from dataagent.actions.tools.local_tool.dataops_flow_checkers import (
            build_flow_checkers,
        )

        names = [c.name for c in build_flow_checkers()]
        assert "common_sense" in names
        assert "source_reconciliation" in names

    def test_no_circular_import(self):
        """模块必须能被独立导入（不与 dataops_quality_check 形成环）。"""
        import importlib

        importlib.reload(
            importlib.import_module(
                "dataagent.actions.tools.local_tool.dataops_flow_checkers"
            )
        )
