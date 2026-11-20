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
"""Unit tests for CTE handling in the data quality check pipeline.

覆盖三个根因：

1. **CTE 别名被当成物理源表** —— ``WITH _src AS (...)`` 里的 ``_src`` 是
   中间结果集。质量校验拿它去回源比对 → 必然 0 行 → 全部「无法验证」。
2. **质量校验链路完全没做 CTE 改写** —— 语法校验链路靠
   ``_expand_cte_for_dml``（LLM 内联）解决，但那只作用在「提交给 DataOps
   的 DML」上；质量校验自己解析的是**原始 SQL**。
3. **回源比对生成的 SQL 可能带 CTE** —— 生产 adHoc 不支持 ``WITH AS``，
   提交会被拒 → 也是「无法验证」。

修法：质量校验链路用 **sqlglot 静态展开**（确定性、零延迟、不改口径），
源表解析排除 CTE 别名，回源 SQL 强制摊平后再执行。
"""

from __future__ import annotations

from typing import Any

import pytest

from dataagent.actions.tools.local_tool.dataops_quality_check import (
    extract_insert_source_tables,
)
from dataagent.actions.tools.local_tool.dataops_validate_tool import (
    _extract_fields_from_insert_sql,
    cte_names,
    expand_ctes_static,
    has_cte,
)

# ============================================================================
# Fixtures
# ============================================================================

_CTE_INSERT_SQL = """WITH _src AS (
  SELECT udid, cnt FROM biads.src_table WHERE pt_d = '20260801'
),
_agg AS (
  SELECT udid, SUM(cnt) AS s FROM _src GROUP BY udid
)
INSERT OVERWRITE TABLE biads.tgt PARTITION (pt_d = '20260801')
SELECT udid, s AS total_cnt FROM _agg
"""


class TestExpandCtes:
    """静态 CTE 展开器本身的行为。"""

    def test_detects_cte(self) -> None:
        assert has_cte(_CTE_INSERT_SQL) is True
        assert has_cte("SELECT a FROM t") is False
        assert has_cte("") is False

    def test_cte_names(self) -> None:
        assert cte_names(_CTE_INSERT_SQL) == ("_src", "_agg")
        assert cte_names("SELECT a FROM t") == ()

    def test_expands_to_derived_tables(self) -> None:
        """展开后不能再有 WITH —— 派生表是 adHoc 唯一支持的复用形式。"""
        expanded = expand_ctes_static(_CTE_INSERT_SQL)
        assert has_cte(expanded) is False
        assert "WITH" not in expanded.upper()

    def test_expansion_preserves_semantics(self) -> None:
        """展开必须保留原始 SQL 的口径 —— 聚合、分组、投影一个都不能丢。"""
        expanded = expand_ctes_static(_CTE_INSERT_SQL)
        assert "biads.src_table" in expanded
        assert "SUM(cnt)" in expanded
        assert "GROUP BY" in expanded.upper()
        assert "total_cnt" in expanded
        assert "biads.tgt" in expanded

    def test_no_cte_returns_unchanged(self) -> None:
        sql = "SELECT a FROM t"
        assert expand_ctes_static(sql) == sql

    def test_empty_sql(self) -> None:
        assert expand_ctes_static("") == ""

    def test_nested_cte_dependency(self) -> None:
        """CTE 互相引用时必须按依赖顺序展开 —— 否则内层引用悬空。"""
        expanded = expand_ctes_static(
            "WITH a AS (SELECT * FROM s), b AS (SELECT * FROM a) SELECT * FROM b"
        )
        assert has_cte(expanded) is False
        # a 必须被内联进 b 的定义体里。
        assert "biads" not in expanded
        assert expanded.upper().count("SELECT") >= 3

    def test_self_join_preserves_aliases(self) -> None:
        """``FROM t t1 JOIN t t2`` —— 两个引用带各自的别名，展开后必须保留。

        否则 ``t1.a`` / ``t2.a`` 的列引用会悬空，SQL 语义就变了。
        """
        expanded = expand_ctes_static(
            "WITH t AS (SELECT a FROM x) SELECT t1.a FROM t t1 JOIN t t2 ON t1.a = t2.a"
        )
        assert has_cte(expanded) is False
        assert "AS t1" in expanded
        assert "AS t2" in expanded
        assert "t1.a" in expanded
        assert "t2.a" in expanded

    def test_unparseable_sql_returns_original(self) -> None:
        """解析失败必须原样返回 —— 绝不产出半成品 SQL。"""
        broken = "WITH ((("
        assert expand_ctes_static(broken) == broken

    def test_backtick_cte_name_expands(self) -> None:
        """反引号包裹的 CTE 名（``WITH `t` AS (...)``）必须能展开。

        生产 SQL 常见反引号写法。**默认方言解析不了它**（报 "Expecting ("），
        必须用 hive 方言 —— 这条用例把这个坑钉住。
        """
        sql = "WITH `t` AS (SELECT `a` FROM `biads`.`src`) SELECT `a` FROM `t`"
        expanded = expand_ctes_static(sql)
        assert has_cte(expanded) is False
        assert "biads" in expanded
        assert "src" in expanded

    def test_insert_with_cte_is_expanded(self) -> None:
        """``WITH ... INSERT ...`` 这种写法（WITH 在 INSERT 之前）也要展开。"""
        expanded = expand_ctes_static(_CTE_INSERT_SQL)
        assert has_cte(expanded) is False
        assert expanded.upper().lstrip().startswith("INSERT")


class TestSourceTableExtraction:
    """源表清单解析 —— CTE 别名绝不能被当成物理表。"""

    def test_cte_aliases_excluded(self) -> None:
        """核心回归：``_src`` / ``_agg`` 是中间结果集，不是源表。"""
        tables = extract_insert_source_tables(_CTE_INSERT_SQL)
        assert "_src" not in tables
        assert "_agg" not in tables
        assert tables == ("biads.src_table",)

    def test_expanded_sql_yields_physical_source(self) -> None:
        """展开后的 SQL 解析出的源表应该是真实物理表。"""
        expanded = expand_ctes_static(_CTE_INSERT_SQL)
        assert extract_insert_source_tables(expanded) == ("biads.src_table",)

    def test_plain_insert_unaffected(self) -> None:
        sql = "INSERT OVERWRITE TABLE biads.tgt SELECT a, b FROM biads.src JOIN biads.dim ON a = id"
        assert extract_insert_source_tables(sql) == ("biads.src", "biads.dim")

    def test_target_table_excluded(self) -> None:
        sql = "INSERT OVERWRITE TABLE biads.tgt SELECT a FROM biads.src"
        assert "biads.tgt" not in extract_insert_source_tables(sql)

    def test_derived_table_alias_excluded(self) -> None:
        sql = "INSERT OVERWRITE TABLE biads.tgt SELECT a FROM (SELECT a FROM biads.src) AS s"
        assert extract_insert_source_tables(sql) == ("biads.src",)


class TestFieldExtractionWithCte:
    """字段解析 —— 带 CTE 的 INSERT 也要能取到目标表真实落地的列。"""

    def test_fields_from_expanded_sql(self) -> None:
        expanded = expand_ctes_static(_CTE_INSERT_SQL)
        fields = _extract_fields_from_insert_sql(expanded, frozenset())
        assert fields == ["udid", "total_cnt"]

    def test_fields_from_unexpanded_sql(self) -> None:
        """即使没展开（兜底路径），字段解析也要正确 —— 取 INSERT 自己的投影。"""
        fields = _extract_fields_from_insert_sql(_CTE_INSERT_SQL, frozenset())
        assert fields == ["udid", "total_cnt"]


class TestReconciliationSqlCteGuard:
    """回源比对生成的 SQL 必须在执行前摊平 CTE。"""

    def test_reconcile_prompt_forbids_cte(self) -> None:
        """prompt 里必须明确禁止 CTE —— 引擎不支持。"""
        from dataagent.actions.tools.local_tool import dataops_source_reconciliation as sr

        assert "CTE" in sr._SYSTEM_PROMPT
        assert "WITH" in sr._SYSTEM_PROMPT

    def test_read_only_whitelist_allows_with(self) -> None:
        """只读白名单**允许** WITH 开头 —— 它只管「不改数据」。

        所以「引擎不支持 CTE」必须由**另一道**闸门拦，不能指望白名单。
        这条测试把这个事实钉住 —— 万一哪天有人给白名单加了 WITH 的拦截，
        下面的 ``test_reconcile_sql_with_cte_is_expanded`` 就不再是唯一防线了。
        """
        from dataagent.actions.tools.local_tool.dataops_correctness_flow import (
            is_read_only_sql,
        )

        assert is_read_only_sql("WITH t AS (SELECT a FROM x) SELECT a FROM t") is True


class TestQualityCheckPipelineKeepsOriginalSql:
    """质量校验**解析层**必须看到交付物原貌 —— 展开只发生在提交层。"""

    @pytest.mark.asyncio
    async def test_parsing_sees_original_cte_sql(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``source_sql`` / 解析层拿到的必须是**带 CTE 的原文**。

        展开后的 SQL 排版被重排（``PARTITION(pt_d=...)`` 少空格、多行折成
        一行）。``source_sql`` 是**自修正的增量修改底稿**、也是回源比对
        prompt 里模型理解口径的依据 —— 让它看到展开版等于让自修正在错误的
        基础上改。
        """
        from dataagent.actions.tools.local_tool import dataops_quality_check as qc

        seen: dict[str, str] = {}

        original = qc.extract_insert_source_tables

        def _spy(sql: str) -> tuple[str, ...]:
            seen["sql"] = sql
            return original(sql)

        monkeypatch.setattr(qc, "extract_insert_source_tables", _spy)
        # prod 分支不依赖 runtime / coordinator —— 用它绕开「没有 runtime 就
        # 提前返回」的问题，让流水线真正跑到解析阶段。
        monkeypatch.setattr(qc, "_get_dataops_env", lambda: "prod")
        # 校验执行链路全部截断 —— 本测试只关心「解析层的 SQL 形态」。
        monkeypatch.setattr(qc, "_execute_check_sql_prod", _never_called_async)
        monkeypatch.setattr(qc, "_run_flow_checks", _never_called_async)

        ctx = _FakeToolContext()
        config = qc.QualityCheckConfig(enabled=True)

        await qc._run_quality_check_impl(
            _CTE_INSERT_SQL,
            _tool_context=ctx,  # type: ignore[arg-type]
            config=config,
            temp_table="adhoctemp.tmp_x",
            partition_date="20260801",
        )

        # 观测点必须**真的被调用**了 —— 否则这条测试就是假阳性。
        assert "sql" in seen, "extract_insert_source_tables 未被调用，测试无效"
        # **必须仍是带 CTE 的原文** —— 解析层不做展开。
        assert has_cte(seen["sql"]) is True, "解析层不应展开 CTE（会污染自修正底稿）"
        assert "_src" in seen["sql"]
        assert "_agg" in seen["sql"]


class TestSubmissionExpandsCte:
    """**提交给 DataOps 的 SQL** 必须做 CTE 展开。"""

    @pytest.mark.asyncio
    async def test_prod_submission_expands_cte(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产路径：提交给 DataOps 的 SQL 必须是**展开后**的。"""
        from dataagent.actions.tools.local_tool import dataops_quality_check as qc

        submitted: list[str] = []

        async def _fake_prod_execute_sql(sql: str) -> dict[str, object]:
            submitted.append(sql)
            return {"success": False, "error": "stop here"}

        import dataagent.actions.tools.local_tool.dataops_prod_adapter as adapter

        monkeypatch.setattr(adapter, "prod_execute_sql", _fake_prod_execute_sql)

        await qc._execute_check_sql_prod(
            "WITH t AS (SELECT a FROM src) SELECT a FROM t",
            timeout_sec=1,
        )

        assert submitted, "没有提交任何 SQL"
        assert has_cte(submitted[0]) is False, "提交给 DataOps 的 SQL 仍带 CTE"
        assert "src" in submitted[0]

    @pytest.mark.asyncio
    async def test_integration_submission_expands_cte(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """联调路径：提交给 DataOps 的 SQL 必须是**展开后**的。"""
        from dataagent.actions.tools.local_tool import dataops_quality_check as qc

        submitted: list[str] = []

        class _Coordinator:
            def submit_job(self, **kwargs: object) -> dict[str, object]:
                submitted.append(str(kwargs.get("command")))
                return {"status": "ERROR", "message": "stop here"}

        await qc._execute_check_sql_integration(
            "WITH t AS (SELECT a FROM src) SELECT a FROM t",
            _Coordinator(),
            timeout_sec=1,
            poll_interval=1.0,
        )

        assert submitted, "没有提交任何 SQL"
        assert has_cte(submitted[0]) is False, "提交给 DataOps 的 SQL 仍带 CTE"
        assert "src" in submitted[0]


async def _never_called_async(*args: Any, **kwargs: Any) -> Any:
    """占位桩 —— 校验执行链路不该在这条测试里真的跑。"""
    raise AssertionError("校验执行链路不该被调用")


class _FakeToolContext:
    """最小 ToolExecutionContext 替身 —— 只提供 runtime=None 即可。"""

    runtime = None
    tool_config = None
