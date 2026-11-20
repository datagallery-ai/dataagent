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
"""回归测试：数据校验流程的 6 个 bug（20261118 流程审查）。

每个 bug 都对应一次「报告会撒谎」或「校验根本没跑」的事故：

1. **假空表** —— 试算写入的分区 ≠ 质量校验查询的分区 → COUNT(1) 恒为 0
   → 一次**成功的**试算被误判成「一行数据都没校验过」。
2. **复检失败被判成收敛** —— 复检产出空字典 → 判成「✅ 自修正已收敛」。
3. **直连模式主键匹配用了已排除的字段** → 主键重复率整项被跳过。
4. **DDL 与 DML 配对错** —— 多表交付时拿 A 表的建表语句建 B 表的临时表。
5. **partition_filter 参数丢失** —— 直连真实表只能查固定那一天。
6. **分区列硬编码 pt_d** —— 按 ``ds`` 分区的表查不到行。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dataagent.actions.tools.local_tool import dataops_quality_check as qc
from dataagent.actions.tools.local_tool import dataops_self_correct as sc
from dataagent.core.suite.builtin_suites.de_agent.tools import (
    dataops_trial_compute_tool as tct,
)
from dataagent.core.suite.builtin_suites.de_agent.tools import dataops_validate_data_tool as tool

# ============================================================================
# BUG-1 临时表模式不加分区过滤（假空表的根因）
# ============================================================================


class TestTempTableNoPartitionFilter:
    """临时表里只有本次试算的数据 —— 额外加分区过滤只会查不到行。"""

    def _ctx(self, partition_filter):
        return qc.build_check_context(
            temp_table="adhoctemp.tmp_user_20261111_x",
            fields=["udid", "cnt"],
            partition_col="pt_d",
            partition_date="20261111",
            checkers=qc._BUILTIN_CHECKERS,
            partition_filter=partition_filter,
        )

    def test_temp_table_row_count_has_no_where(self):
        sql = qc._build_row_count_sql(self._ctx(""))
        assert "WHERE" not in sql
        assert "COUNT(1) AS row_count" in sql

    def test_temp_table_null_rate_has_no_where(self):
        ctx = self._ctx("")
        statements = qc.build_statements_from_context(ctx)
        for _name, sql in statements:
            assert "WHERE" not in sql, sql

    def test_temp_table_pk_duplicate_has_no_where(self):
        base = self._ctx("")
        ctx = qc.CheckContext(
            **{
                f.name: getattr(base, f.name)
                for f in base.__dataclass_fields__.values()
                if f.name != "primary_key_columns"
            },
            primary_key_columns=("udid",),
        )
        sql = qc._build_pk_duplicate_sql(ctx)
        # 主键非空条件是「有效行」口径的一部分，必须保留；
        # 但**不能**有分区过滤（那会查不到行 → 假空表）。
        assert "WHERE `udid` IS NOT NULL" in sql
        assert "pt_d" not in sql
        # 分区过滤不存在 → WHERE 里只有主键非空那一条
        where_parts = sql.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
        assert "IS NOT NULL" in where_parts
        assert "'" not in where_parts, "WHERE 里不该出现分区日期字面量"

    def test_direct_table_still_filters_by_partition(self):
        sql = qc._build_row_count_sql(self._ctx("pt_d = '20261020'"))
        assert "WHERE pt_d = '20261020'" in sql

    def test_none_means_default_filter(self):
        """``None`` = 未指定 → 用默认条件（向后兼容）。"""
        sql = qc._build_row_count_sql(self._ctx(None))
        assert "WHERE pt_d = '20261111'" in sql

    def test_empty_string_means_explicitly_no_filter(self):
        """``""`` = 明确不过滤 —— 与 ``None`` 语义不同。"""
        assert qc._build_row_count_sql(self._ctx("")) != qc._build_row_count_sql(self._ctx(None))


# ============================================================================
# BUG-2 复检失败被当成「收敛」
# ============================================================================


class TestRecheckFailureIsNotConvergence:
    def test_empty_dict_is_not_a_complete_recheck(self):
        violations, reason = sc._extract_recheck({})
        assert violations == []
        assert reason, "空字典必须被认成「复检未完成」"

    def test_unsampled_result_is_not_a_complete_recheck(self):
        _violations, reason = sc._extract_recheck({"violations": [], "sampled": False, "error": "提交失败"})
        assert reason

    def test_partial_result_is_not_a_complete_recheck(self):
        _violations, reason = sc._extract_recheck({"violations": [], "failed_checks": [], "partial": True})
        assert reason

    @pytest.mark.asyncio
    async def test_trial_failure_after_correction_is_not_converged(self, tmp_path):
        """模型把 SQL 改坏 → 复检试算失败 → **不能**判成收敛。"""
        dml = tmp_path / "insert_x.sql"
        dml.write_text("INSERT OVERWRITE TABLE biads.t SELECT udid, cnt FROM src", encoding="utf-8")

        calls = {"n": 0}

        async def fake_trial(sql, target_table, _tool_context):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "passed": True,
                    "skipped": False,
                    "target_table": "biads.t",
                    "quality_check": {
                        "ok": False,
                        "warnings": ["x"],
                        "sampled": True,
                        "sample_size": 10000,
                        "checks": ["null_rate"],
                        "completed_checks": ["null_rate"],
                        "failed_checks": [],
                        "details": {"udid": {"null_rate": 0.1}},
                        "table_metrics": {"row_count": 5},
                        "violations": [
                            {
                                "rule": "pk_duplicate_rate",
                                "field": "udid",
                                "severity": "error",
                                "message": "重复率超标",
                                "evidence": {},
                                "suggestion": "去重",
                            }
                        ],
                    },
                }
            # 复检：试算失败 → 没有 quality_check
            return {
                "passed": False,
                "skipped": False,
                "target_table": "biads.t",
                "error": "SQL 执行失败：字段 cnt 未找到",
            }

        async def fake_llm_json(llm, *, system, user, llm_kwargs=None):
            return {
                "edits": [{"old_text": "cnt", "new_text": "cnt2"}],
                "reason": "改个名",
            }

        ctx = MagicMock()
        ctx.tool_config = {}
        runtime = MagicMock()
        runtime.workspace_dir = tmp_path
        ctx.runtime = runtime

        with (
            patch.object(tool, "_invoke_trial_compute", new=fake_trial),
            patch(
                "dataagent.actions.tools.local_tool.dataops_self_correct.invoke_llm_json",
                new=fake_llm_json,
            ),
            patch.object(tool, "resolve_llm", return_value=MagicMock()),
        ):
            result = await tool.validate_data("", _tool_context=ctx)

        payload = result["data"].get("quality_check") or {}
        self_correct = payload.get("self_correct") or {}
        assert self_correct.get("converged") is False
        assert result["data"]["needs_manual_review"] is True
        assert "收敛" not in result["frontend_msg"] or "未收敛" in result["frontend_msg"]


# ============================================================================
# BUG-3 直连模式主键匹配用了已排除的字段
# ============================================================================


class TestDirectModePrimaryKeyUsesUnexcludedFields:
    @pytest.mark.asyncio
    async def test_direct_mode_passes_unexcluded_fields(self, tmp_path):
        """直连模式：覆盖率统计用「已排除」的字段，主键匹配用「未排除」的。"""
        captured: dict = {}

        def fake_run(sql, **kwargs):
            captured.update(kwargs)
            return {
                "ok": True,
                "warnings": [],
                "violations": [],
                "details": {},
                "table_metrics": {},
                "sampled": True,
                "sample_size": 10000,
                "checks": [],
                "completed_checks": [],
                "failed_checks": [],
            }

        ctx = MagicMock()
        ctx.tool_config = {}
        runtime = MagicMock()
        runtime.workspace_dir = tmp_path
        ctx.runtime = runtime

        with (
            patch.object(tool, "run_quality_check", side_effect=fake_run),
            patch.object(
                tool,
                "fetch_semantic_columns",
                return_value=(["udid", "app_type", "cnt"], {"udid": "string"}),
            ),
        ):
            await tool.validate_data("", target_table="biads.t", _tool_context=ctx)

        # 覆盖率统计：udid 被排除
        assert "udid" not in (captured.get("fields") or [])
        # 主键匹配：udid 必须还在
        assert "udid" in (captured.get("unexcluded_fields") or [])


# ============================================================================
# BUG-4 DDL 与 DML 按目标表配对
# ============================================================================


class TestDdlMatchesTargetTable:
    def _workspace(self, tmp_path: Path) -> Path:
        (tmp_path / "create_aaa.sql").write_text("CREATE TABLE biads.t1 (a INT)", encoding="utf-8")
        (tmp_path / "create_zzz.sql").write_text("CREATE TABLE biads.t2 (b INT)", encoding="utf-8")
        (tmp_path / "insert_x.sql").write_text("INSERT OVERWRITE TABLE biads.t2 SELECT b FROM s", encoding="utf-8")
        return tmp_path

    def _ctx(self, workspace: Path) -> MagicMock:
        ctx = MagicMock()
        ctx.tool_config = {}
        runtime = MagicMock()
        runtime.workspace_dir = workspace
        ctx.runtime = runtime
        return ctx

    def test_ddl_is_matched_by_target_table(self, tmp_path):
        workspace = self._workspace(tmp_path)
        found = tct._find_ddl(self._ctx(workspace), "biads.t2")
        assert found is not None
        assert found.name == "create_zzz.sql"

    def test_bare_table_name_still_matches(self, tmp_path):
        """DDL 不带库名时按表名匹配。"""
        (tmp_path / "create_bare.sql").write_text("CREATE TABLE t3 (c INT)", encoding="utf-8")
        (tmp_path / "create_other.sql").write_text("CREATE TABLE biads.t9 (d INT)", encoding="utf-8")
        found = tct._find_ddl(self._ctx(tmp_path), "biads.t3")
        assert found is not None
        assert found.name == "create_bare.sql"

    def test_quality_check_ddl_is_matched_by_target_table(self, tmp_path):
        workspace = self._workspace(tmp_path)
        runtime = MagicMock()
        runtime.workspace_dir = workspace
        ddl = qc._find_ddl_in_workspace(runtime, "biads.t2")
        assert "t2" in ddl
        assert "t1" not in ddl


# ============================================================================
# BUG-5 partition_filter 参数丢失
# ============================================================================


class TestPartitionFilterParameter:
    @pytest.mark.asyncio
    async def test_parameter_exists_and_is_forwarded(self, tmp_path):
        captured: dict = {}

        def fake_run(sql, **kwargs):
            captured.update(kwargs)
            return {
                "ok": True,
                "warnings": [],
                "violations": [],
                "details": {},
                "table_metrics": {},
                "sampled": True,
                "sample_size": 10000,
                "checks": [],
                "completed_checks": [],
                "failed_checks": [],
            }

        ctx = MagicMock()
        ctx.tool_config = {}
        runtime = MagicMock()
        runtime.workspace_dir = tmp_path
        ctx.runtime = runtime

        with (
            patch.object(tool, "run_quality_check", side_effect=fake_run),
            patch.object(tool, "fetch_semantic_columns", return_value=(["a"], {"a": "string"})),
        ):
            await tool.validate_data(
                "",
                target_table="biads.t",
                partition_filter="pt_d = '20261020'",
                _tool_context=ctx,
            )

        assert captured.get("partition_filter") == "pt_d = '20261020'"


# ============================================================================
# BUG-6 分区列硬编码 pt_d
# ============================================================================


class TestPartitionColumnFromDdl:
    def test_parses_ds(self):
        ddl = "CREATE TABLE biads.t (udid STRING, cnt BIGINT) PARTITIONED BY (ds STRING)"
        assert qc.parse_ddl_partition_columns(ddl) == ("ds",)

    def test_parses_pt_d(self):
        ddl = "CREATE TABLE biads.t (udid STRING) PARTITIONED BY (pt_d STRING)"
        assert qc.parse_ddl_partition_columns(ddl) == ("pt_d",)

    def test_multiple_partition_columns_keep_order(self):
        ddl = "CREATE TABLE biads.t (a INT) PARTITIONED BY (ds STRING, hr INT)"
        assert qc.parse_ddl_partition_columns(ddl) == ("ds", "hr")

    def test_no_partition_clause(self):
        assert qc.parse_ddl_partition_columns("CREATE TABLE biads.t (a INT)") == ()

    def test_empty_ddl(self):
        assert qc.parse_ddl_partition_columns("") == ()

    def test_ddl_with_comments(self):
        ddl = """
        -- 这是注释 PARTITIONED BY (fake)
        CREATE TABLE biads.t (a INT)
        PARTITIONED BY (dt STRING)
        """
        assert qc.parse_ddl_partition_columns(ddl) == ("dt",)


# ============================================================================
# BUG-10 临时表模式用「临时表名」找 DDL —— 永远匹配不上
# ============================================================================


class TestTempTableDdlLookupUsesRealTarget:
    """临时表模式下 DDL 必须按**真实目标表**去找。

    临时表名是 ``adhoctemp.tmp_{user}_{date}_{table}``，而 workspace 里的 DDL
    声明的是真实表（``biads.b``）—— 两者永远匹配不上 → 退回「按 mtime 取最新」
    → **可能拿到另一张表的 DDL** → 把 A 表的列类型套到 B 表上
    （STRING 被当成数值 → 判空口径直接错），分区列也认错。
    """

    def _runtime(self, workspace: Path) -> MagicMock:
        runtime = MagicMock()
        runtime.workspace_dir = str(workspace)
        return runtime

    def test_lookup_by_real_target_finds_the_right_ddl(self, tmp_path):
        (tmp_path / "create_b.sql").write_text(
            "CREATE EXTERNAL TABLE biads.b (amount DECIMAL(10,2), name STRING) "
            "PARTITIONED BY (ds STRING)",
            encoding="utf-8",
        )
        (tmp_path / "create_a.sql").write_text(
            "CREATE EXTERNAL TABLE biads.a (amount STRING, name STRING) "
            "PARTITIONED BY (pt_d STRING)",
            encoding="utf-8",
        )

        runtime = self._runtime(tmp_path)
        ddl = qc._find_ddl_in_workspace(runtime, "biads.b")

        assert "biads.b" in ddl
        assert "biads.a" not in ddl

    def test_bare_table_name_ddl_still_matches(self, tmp_path):
        """DDL 不带库名（``CREATE TABLE ads_x``）时按裸表名匹配。"""
        (tmp_path / "create_bare.sql").write_text(
            "CREATE TABLE ads_x (amount STRING) PARTITIONED BY (pt_d STRING)",
            encoding="utf-8",
        )
        runtime = self._runtime(tmp_path)
        ddl = qc._find_ddl_in_workspace(runtime, "biads.ads_x")
        assert "ads_x" in ddl

    def test_temp_mode_uses_real_target_for_column_types(self, tmp_path):
        """试算校验（临时表模式）必须用**真实目标表**的 DDL 取列类型。

        临时表名 ``adhoctemp.tmp_u1_20261111_b`` 匹配不上任何交付 DDL ——
        早先这里拿「最新修改的 DDL」（可能是 A 表的）→ 把 decimal 认成 string
        → 判空写法从 ``IS NOT NULL`` 变成 ``bicoredata.IsEmpty`` → 口径错。
        """
        (tmp_path / "create_b.sql").write_text(
            "CREATE EXTERNAL TABLE biads.b (amount DECIMAL(10,2)) PARTITIONED BY (pt_d STRING)",
            encoding="utf-8",
        )
        # A 表的 DDL 较新 —— 拿错它会把 decimal 认成 string
        (tmp_path / "create_a.sql").write_text(
            "CREATE EXTERNAL TABLE biads.a (amount STRING) PARTITIONED BY (pt_d STRING)",
            encoding="utf-8",
        )

        runtime = self._runtime(tmp_path)
        sql = (
            "INSERT OVERWRITE TABLE biads.b PARTITION (pt_d = '20261020') "
            "SELECT amount FROM biads.src"
        )

        # 真实目标表 → 正确命中 biads.b 的 DDL
        assert qc.extract_insert_target_table(sql) == "biads.b"
        ddl = qc._find_ddl_in_workspace(runtime, qc.extract_insert_target_table(sql))
        assert qc.parse_ddl_column_types(ddl).get("amount") != "string"


# ============================================================================
# BUG-11 主键三个来源全落空 → 主键重复率 + 回源比对整项跳过
# ============================================================================


class TestSemanticPrimaryKeyFallback:
    """主键拿不到时，semantic 元数据是最后的兜底。

    ``DataTaskIR`` 的口径声明只在走 IR 链路的会话里有。独立的数据校验
    （用户输入「执行数据校验」）**根本没建 IR** → ``identity.components`` /
    ``output_features.grain`` 全空；``tools.yaml`` 里的
    ``primary_key_columns`` 又被注释掉了 → **三个来源全落空** →
    「主键重复率」和「抽样回源比对」这两项**整项跳过** —— 而它们恰恰是
    正确性校验里最硬的两条。
    """

    def test_resolve_uses_semantic_primary_keys(self):
        assert qc.resolve_primary_key_columns(
            None,
            ["udid", "cnt"],
            semantic_primary_keys=("udid",),
        ) == ("udid",)

    def test_semantic_fallback_is_all_or_nothing(self):
        """semantic 只标了复合主键的一部分 → **整组作废**，不降级成子集。

        降级成子集就是「重复率 0.87」的误报（生产实测过的坑）。
        """
        assert (
            qc.resolve_primary_key_columns(
                None,
                ["udid"],
                semantic_primary_keys=("udid", "app_type"),
            )
            == ()
        )

    def test_config_takes_priority_over_semantic(self):
        """配置是人为写的口径 —— 优先级高于平台侧的物理表标记。"""
        cfg = qc.QualityCheckConfig(primary_key_columns=("udid",))
        assert (
            qc.resolve_primary_key_columns(
                None,
                ["udid", "cnt"],
                cfg,
                semantic_primary_keys=("cnt",),
            )
            == ("udid",)
        )

    def test_semantic_meta_boolean_only(self):
        """``"false"`` 字符串是**真值** —— 不能被当成主键。"""
        from dataagent.actions.tools.local_tool import dataops_table_schema as ts

        assert ts._is_primary_key_meta({"is_primary_key": True}) is True
        assert ts._is_primary_key_meta({"is_primary_key": False}) is False
        assert ts._is_primary_key_meta({"is_primary_key": "false"}) is False
        assert ts._is_primary_key_meta({"is_primary_key": "true"}) is True
        assert ts._is_primary_key_meta({"is_primary_key": ""}) is False
        assert ts._is_primary_key_meta({}) is False

    def test_semantic_meta_variants(self):
        """不同版本的 key 拼法都认。"""
        from dataagent.actions.tools.local_tool import dataops_table_schema as ts

        assert ts._is_primary_key_meta({"isPrimaryKey": True}) is True
        assert ts._is_primary_key_meta({"primary_key": True}) is True
        assert ts._is_primary_key_meta({"is_pk": True}) is True

    def test_lookup_skips_temp_table(self):
        """临时表没有 semantic 元数据 —— 不能拿它去查。"""
        assert (
            qc._fetch_semantic_primary_keys(
                target_table="adhoctemp.tmp_u1_20261111_b",
                real_target_table="adhoctemp.tmp_u1_20261111_b",
                direct_table=None,
                tool_context=MagicMock(),
            )
            == ()
        )

    def test_lookup_uses_real_target_in_temp_mode(self):
        """临时表模式 → 查**真实目标表**（主键是真实表的属性）。"""
        with patch(
            "dataagent.actions.tools.local_tool.dataops_table_schema."
            "fetch_semantic_primary_key_columns",
            return_value=("udid",),
        ) as spy:
            keys = qc._fetch_semantic_primary_keys(
                target_table="adhoctemp.tmp_u1_20261111_b",
                real_target_table="biads.b",
                direct_table=None,
                tool_context=MagicMock(),
            )
        assert keys == ("udid",)
        assert spy.call_args.args[0] == "biads.b"

    def test_lookup_uses_direct_table_in_direct_mode(self):
        """直连模式 → 查真实目标表。"""
        with patch(
            "dataagent.actions.tools.local_tool.dataops_table_schema."
            "fetch_semantic_primary_key_columns",
            return_value=("udid",),
        ) as spy:
            keys = qc._fetch_semantic_primary_keys(
                target_table="biads.t",
                real_target_table="biads.t",
                direct_table="biads.t",
                tool_context=MagicMock(),
            )
        assert keys == ("udid",)
        assert spy.call_args.args[0] == "biads.t"

    def test_lookup_failure_degrades_to_empty(self):
        """semantic 查不到 → 降级成空元组，绝不抛异常。"""
        with patch(
            "dataagent.actions.tools.local_tool.dataops_table_schema."
            "fetch_semantic_primary_key_columns",
            side_effect=RuntimeError("semantic down"),
        ):
            assert (
                qc._fetch_semantic_primary_keys(
                    target_table="biads.t",
                    real_target_table="biads.t",
                    direct_table="biads.t",
                    tool_context=MagicMock(),
                )
                == ()
            )


# ============================================================================
# BUG-12 报告里没有抽样数据 —— 「0 处违规」无法自证
# ============================================================================


class TestSampleDataInReport:
    """常识检测抽了 20 行，但报告里一个字都没显示。

    没有违规明细时，抽样数据是**唯一**能证明「校验真的跑过」的证据。
    """

    def test_render_sample_data(self):
        notes = {"sample_data": [{"udid": "u1", "cnt": 3}, {"udid": "u2", "cnt": 0}]}
        md = qc.render_sample_data(notes, title="常识检测抽样数据")
        assert "常识检测抽样数据" in md
        assert "udid=u1" in md
        assert "cnt=3" in md
        assert "第 2 行" in md

    def test_render_sample_data_handles_null(self):
        notes = {"sample_data": [{"udid": None}]}
        md = qc.render_sample_data(notes)
        assert "NULL" in md

    def test_render_sample_data_empty_returns_blank(self):
        assert qc.render_sample_data({}) == ""
        assert qc.render_sample_data(None) == ""
        assert qc.render_sample_data({"sample_data": []}) == ""

    def test_render_sample_data_truncates(self):
        rows = [{"a": i} for i in range(50)]
        md = qc.render_sample_data({"sample_data": rows})
        assert "共 50 行" in md
        assert "另有 30 行未展示" in md

    def test_common_sense_records_sampled_rows(self):
        """common_sense 必须把抽到的行记进 notes。"""
        from dataagent.actions.tools.local_tool import dataops_common_sense as cs
        from dataagent.actions.tools.local_tool import dataops_correctness_flow as flow

        async def _exec(_sql):
            return "completed", []

        ctx = flow.FlowContext(
            sql="INSERT OVERWRITE TABLE t SELECT a FROM s",
            target_table="t",
            fields=["a"],
            common_sense_sample_size=5,
            llm=object(),
            execute_sql=_exec,
        )
        rows = [{"a": 1}, {"a": 2}]

        async def _fake_sample(_ctx, _cols, *, limit):
            return rows, "completed"

        with (
            patch.object(cs, "sample_rows", side_effect=_fake_sample),
            patch.object(cs, "invoke_llm_json", return_value={"violations": []}),
        ):
            import asyncio

            asyncio.run(cs.run_common_sense(ctx))

        assert ctx.notes["common_sense__sample_rows"] == 2
        assert ctx.notes["common_sense__sample_data"] == rows


# ============================================================================
# BUG-13 被跳过的校验项在报告里不显眼
# ============================================================================


class TestSkippedChecksAreProminent:
    """跳过 ≠ 通过 —— 必须在摘要句里点名。"""

    def _quality(self, **overrides):
        base = {
            "ok": True,
            "details": {"udid": {"null_rate": 1.0}},
            "warnings": [],
            "violations": [],
            "table_metrics": {},
            "checks": ["null_rate", "row_count"],
            "completed_checks": ["null_rate", "row_count"],
            "failed_checks": [],
            "skipped_checks": [
                {"name": "pk_duplicate_rate", "reason": "未提供主键列配置"},
            ],
            "flow_notes": {},
            "sample_size": 10000,
        }
        base.update(overrides)
        return base

    def test_summary_names_skipped_checks(self):
        report = tool._render_report(result={"passed": True, "quality_check": self._quality()})
        assert "pk_duplicate_rate" in report
        assert "被跳过" in report
        # 摘要句附近就要出现，不能只藏在末尾小节里
        head = report.split("\n\n")[0]
        assert "被跳过" in head

    def test_conclusion_names_skipped_checks(self):
        report = tool._render_report(result={"passed": True, "quality_check": self._quality()})
        assert "因信息不足被跳过" in report
        assert "没有产出任何结论" in report

    def test_no_skipped_no_extra_warning(self):
        report = tool._render_report(
            result={
                "passed": True,
                "quality_check": self._quality(skipped_checks=[]),
            }
        )
        assert "被跳过" not in report

    def test_direct_report_also_names_skipped(self):
        result = self._quality()
        report = tool._render_direct_report(result=result, target_table="biads.t")
        assert "pk_duplicate_rate" in report
        assert "被跳过" in report


# ============================================================================
# BUG-14 明细被终端截断吃掉 —— 结论句没有数据支撑
# ============================================================================


class TestFlowDetailsSurviveTruncation:
    """「抽样 20 行；发现 0 处违规」必须紧跟**实际数据行**。

    终端渲染层把工具结果截断到 500 字符
    （``DEFAULT_RICH_RESULT_TRUNCATION``）—— 排在指标表之后的明细
    **永远看不到**。用户只看到一句没有内容支撑的结论。
    """

    #: 终端渲染层的截断长度（与 ``dataagent.utils.constants`` 一致）。
    _CLI_TRUNCATION = 500

    def _flow_notes(self):
        return {
            "common_sense": {
                "status": "completed",
                "sample_rows": 20,
                "violation_count": 0,
                "sample_data": [
                    {"app_id": f"com.app{i}", "pay_cnt_30d": i} for i in range(20)
                ],
            },
            "source_reconciliation": {
                "status": "completed",
                "sample_rows": 3,
                "matched_rows": 1,
                "compared_fields": 2,
                "sample_data": [
                    {"udid": "u1", "app_id": "com.app0"},
                    {"udid": "u2", "app_id": "com.app1"},
                    {"udid": "u3", "app_id": "com.app2"},
                ],
                "comparisons": [
                    {
                        "row_index": 1,
                        "primary_key": {"udid": "u1"},
                        "compared_fields": 2,
                        "missing_fields": [],
                        "source_row": {"app_id": "com.app0", "pay_cnt_30d": 0},
                        "feature_row": {"app_id": "com.app0", "pay_cnt_30d": 0},
                    }
                ],
                "unverifiable": [
                    {"row_index": 2, "reason": "回源查询未完成（failed）"},
                    {"row_index": 3, "reason": "源表查询返回 0 行"},
                ],
            },
        }

    def _quality(self, flow_notes=None):
        return {
            "ok": True,
            "details": {f"f{i}": {"null_rate": 1.0} for i in range(10)},
            "warnings": [],
            "violations": [],
            "table_metrics": {},
            "checks": ["null_rate", "row_count"],
            "completed_checks": ["null_rate", "row_count"],
            "failed_checks": [],
            "skipped_checks": [],
            "flow_notes": self._flow_notes() if flow_notes is None else flow_notes,
            "sample_size": 10000,
        }

    def test_render_flow_details_lists_sample_rows(self):
        md = qc.render_flow_details(self._flow_notes())
        assert "常识违规检测抽样明细" in md
        assert "抽样回源比对抽样明细" in md
        # 实际取值必须出现
        assert "app_id=com.app0" in md

    def test_render_flow_details_marks_matched_rows(self):
        md = qc.render_flow_details(self._flow_notes())
        # 比上了的行：标明比了几个字段
        assert "比对 2 个字段" in md
        assert "一致 2 个" in md

    def test_render_flow_details_marks_unverifiable_rows(self):
        md = qc.render_flow_details(self._flow_notes())
        # 没比上的行必须说明原因 —— 否则用户以为「抽样 3 行」全都比过了
        assert "未比对：回源查询未完成（failed）" in md
        assert "源表查询返回 0 行" in md

    def test_render_flow_details_empty_returns_blank(self):
        assert qc.render_flow_details({}) == ""
        assert qc.render_flow_details(None) == ""
        assert qc.render_flow_details({"common_sense": {}}) == ""

    def test_details_are_visible_within_cli_truncation(self):
        """核心回归：明细必须在 500 字符窗口内可见。"""
        report = tool._render_report(
            result={"passed": True, "quality_check": self._quality()}
        )
        head = report[: self._CLI_TRUNCATION]
        # 结论句在窗口内
        assert "流程型校验明细" in head
        # 实际数据行也在窗口内
        assert "app_id=com.app0" in head

    def test_direct_report_details_visible_within_cli_truncation(self):
        report = tool._render_direct_report(
            result=self._quality(), target_table="biads.t"
        )
        head = report[: self._CLI_TRUNCATION]
        assert "流程型校验明细" in head
        assert "app_id=com.app0" in head

    def test_reconcile_sql_is_rendered_on_one_line(self):
        """回源 SQL 必须压成一行 —— 逐行渲染会挤掉后面的比对明细。"""
        notes = {
            "executed_sql": [
                {"row_index": 1, "sql": "SELECT a\nFROM db.src\nWHERE dt='1'", "reason": "口径"}
            ]
        }
        md = qc.render_reconcile_evidence(notes)
        sql_line = [ln for ln in md.splitlines() if "SELECT a" in ln]
        assert sql_line, "SQL 应渲染在一行里"
        assert "FROM db.src" in sql_line[0]

    def test_source_reconciliation_records_sampled_rows(self):
        """回源比对必须把抽样行记进 notes —— 否则「没比上的行」无从追溯。"""
        from dataagent.actions.tools.local_tool import dataops_correctness_flow as flow
        from dataagent.actions.tools.local_tool import dataops_source_reconciliation as sr

        async def _exec(_sql):
            return "completed", [{"udid": "u1", "app_id": "com.app0"}]

        ctx = flow.FlowContext(
            sql="INSERT OVERWRITE TABLE t SELECT udid, app_id FROM s",
            target_table="t",
            fields=["udid", "app_id"],
            primary_key_columns=("udid",),
            source_tables=("s",),
            partition_filter="dt='20260101'",
            reconcile_sample_size=1,
            llm=object(),
            execute_sql=_exec,
        )
        rows = [{"udid": "u1", "app_id": "com.app0"}]

        async def _fake_sample(_ctx, _cols, *, limit):
            return rows, "completed"

        async def _fake_llm(_llm, *, system, user, llm_kwargs=None):
            return {
                "sql": "SELECT udid, app_id FROM s WHERE dt='20260101'",
                "reason": "按主键回源",
            }

        with (
            patch.object(sr, "sample_rows", side_effect=_fake_sample),
            patch.object(sr, "invoke_llm_json", side_effect=_fake_llm),
        ):
            import asyncio

            asyncio.run(sr.run_source_reconciliation(ctx))

        assert ctx.notes["source_reconciliation__sample_data"] == rows


