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
"""Unit tests for the fixes derived from the 20261116 quality report.

覆盖两个根因：

1. **主键推断缺陷** —— 复合主键只匹配上一列时**静默降级成单列主键**，
   导致 ``pk_duplicate_rate`` 误报 0.866412（真实重复率 0）。现在主键
   **只认 TASKIR 的声明**（``identity.components`` / ``output_features
   .aggregation.grain``），且声明必须「全有或全无」地匹配上目标表的列。
2. **复检不完整被判成收敛** —— 复检时某个 checker 超时，违规列表恰好为空，
   被判成「修好了」。现在复检不完整 → 本轮不算收敛。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.actions.tools.local_tool import dataops_quality_check as qc
from dataagent.actions.tools.local_tool import dataops_self_correct as sc
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    SEVERITY_ERROR,
    CorrectnessViolation,
    QualityCheckConfig,
    resolve_primary_key_columns,
)

# ============================================================================
# 生产实测的 SQL（ads_apptype_convtarget_stat_1d_dm）
# ============================================================================

_APPTYPE_SQL = """INSERT OVERWRITE TABLE biads.ads_apptype_convtarget_stat_1d_dm
PARTITION (pt_d = '$date')
SELECT
  s.app_second_class_cn_name,
  s.conversionbehaviorid,
  SUM(s.down_cnt_double) AS total_conversion_cnt,
  COUNT(*) AS total_delivery_target_cnt,
  CAST(IF(total_delivery_target_cnt <> 0,
          total_conversion_cnt / total_delivery_target_cnt * 1.2,
          0) AS DECIMAL(38,6)) AS target_conversion_rate
FROM biads.ads_agrec_drilling_down_analysis_dm_v3 s
WHERE s.pt_d = '$date'
GROUP BY s.app_second_class_cn_name, s.conversionbehaviorid
"""

_APPTTYPE_FIELDS = [
    "app_second_class_cn_name",
    "conversionbehaviorid",
    "total_conversion_cnt",
    "total_delivery_target_cnt",
    "target_conversion_rate",
]


class _Runtime:
    """最小 runtime 替身 —— 只提供本模块用到的东西。"""

    def __init__(self, user_query: str = "") -> None:
        self.user_query = user_query
        self.parent_user_query = ""
        self._cache: dict[str, object] = {}

    def get_cache(self, key: str, default: object = None) -> object:
        return self._cache.get(key, default)


# ============================================================================
# 1) 主键只认 TASKIR 的声明（identity.components / output_features.grain）
# ============================================================================
class TestTaskIrPrimaryKey:
    """主键**只**来自 TASKIR 的口径声明。

    曾经的错误做法：从 INSERT 的 ``GROUP BY`` 反推主键。那是 SQL 的实现
    细节，不是业务主键的权威声明 —— GROUP BY 可能多带分区列、可能按月
    累加导致本就不唯一、也可能与业务主键粒度不同。
    """

    def test_identity_components_gives_composite_key(self):
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "identity",
                "value": {
                    "components": [
                        {"field_ref": "app_second_class_cn_name"},
                        {"field_ref": "conversionbehaviorid"},
                    ],
                    "uniqueness_scope": "final_dataset",
                },
            }
        ]
        pk = resolve_primary_key_columns(runtime, _APPTTYPE_FIELDS)
        assert pk == ("app_second_class_cn_name", "conversionbehaviorid")

    def test_identity_single_component_gives_single_key(self):
        """声明几列就是几列 —— 单列声明得到单列主键。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}
        ]
        assert resolve_primary_key_columns(runtime, ["udid", "cnt"]) == ("udid",)

    def test_aggregation_grain_gives_composite_key(self):
        """聚合表(DM 汇总表)的主键记在 output_features 的 aggregation.grain。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "output_features",
                "value": {
                    "layout": "flat",
                    "axes": [],
                    "measures": [],
                    "overrides": [],
                    "extras": [],
                    "features": [
                        {
                            "field_name": "total_conversion_cnt",
                            "role": "metric",
                            "aggregation": {
                                "grain": [
                                    "app_second_class_cn_name",
                                    "conversionbehaviorid",
                                ]
                            },
                        }
                    ],
                },
            }
        ]
        pk = resolve_primary_key_columns(runtime, _APPTTYPE_FIELDS)
        assert pk == ("app_second_class_cn_name", "conversionbehaviorid")

    def test_grain_from_measures_and_extras_is_unioned(self):
        """measures / extras / features 三种容器都可能有 grain —— 取并集。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "output_features",
                "value": {
                    "measures": [
                        {"measure_id": "m1", "aggregation": {"grain": ["a"]}},
                    ],
                    "extras": [
                        {"field_name": "e1", "aggregation": {"grain": ["b"]}},
                    ],
                    "features": [
                        {"field_name": "f1", "aggregation": {"grain": ["c"]}},
                    ],
                },
            }
        ]
        pk = resolve_primary_key_columns(runtime, ["a", "b", "c", "v"])
        assert pk == ("a", "b", "c")

    def test_group_by_is_never_used_as_primary_key_source(self):
        """回归防护：即使 SQL 里有两列 GROUP BY，没声明就是没主键。"""
        runtime = _Runtime()
        assert resolve_primary_key_columns(runtime, _APPTTYPE_FIELDS) == ()

    def test_partial_match_rejects_whole_key(self):
        """核心回归：复合主键只对上一列时**整组作废**，绝不降级成单列。

        这正是 0.866412 误报的根因：声明了
        ``(app_second_class_cn_name, conversionbehaviorid)`` 两列，
        丢掉一列后按单列算 → 262 行里 227 行被判成「重复」。
        """
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "identity",
                "value": {
                    "components": [
                        {"field_ref": "app_second_class_cn_name"},
                        {"field_ref": "conversionbehaviorid"},
                    ]
                },
            }
        ]
        # 目标表里只有第一列 —— 整组作废。
        assert resolve_primary_key_columns(runtime, ["app_second_class_cn_name"]) == ()

    def test_identity_and_grain_are_unioned(self):
        """identity 与 grain 是同一套口径的两个视角 —— 并起来才是完整主键。

        IR 模板写明「若某列既是最终记录主键又是聚合分组键，只在 identity
        声明；``aggregation.grain`` 可以**引用**同一 field_ref」—— 两处
        描述的是同一张表的粒度。

        修掉的真实场景：``identity`` 只填了 1 列（漏填），真实粒度是 2 列
        且记在 ``grain`` 里 → 只按 identity 算就会把复合主键降级成单列 →
        262 行里 227 行被判成「重复」（0.866412 误报）。
        """
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "identity",
                "value": {"components": [{"field_ref": "a"}]},
            },
            {
                "field_id": "output_features",
                "value": {"features": [{"aggregation": {"grain": ["a", "b"]}}]},
            },
        ]
        assert resolve_primary_key_columns(runtime, ["a", "b"]) == ("a", "b")

    def test_union_falls_back_to_single_sets(self):
        """并集对不上（多了一列表里没有的）→ 退到能完整匹配上的单集。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "identity",
                "value": {"components": [{"field_ref": "a"}]},
            },
            {
                "field_id": "output_features",
                "value": {"features": [{"aggregation": {"grain": ["a", "b"]}}]},
            },
        ]
        # 表里只有 a → 并集 (a, b) 作废，退到 identity 单集 (a)。
        assert resolve_primary_key_columns(runtime, ["a"]) == ("a",)

    def test_config_used_when_no_ir(self):
        """没有 IR 声明时用显式配置。"""
        cfg = QualityCheckConfig(primary_key_columns=("udid",))
        assert resolve_primary_key_columns(_Runtime(), ["udid", "cnt"], cfg) == ("udid",)

    def test_partial_config_match_is_rejected(self):
        """配置也是「全有或全无」—— 复合配置缺一列就整组作废。"""
        cfg = QualityCheckConfig(primary_key_columns=("a", "b"))
        assert resolve_primary_key_columns(_Runtime(), ["a"], cfg) == ()

    def test_no_declaration_yields_empty(self):
        """什么都没声明 → 空元组（调用方据此跳过），不猜。"""
        assert resolve_primary_key_columns(_Runtime(), ["a"]) == ()

    def test_field_ref_with_table_prefix_is_stripped(self):
        """``field_ref`` 可能是 ``db.table.column`` —— 取最后一段。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "identity",
                "value": {"components": [{"field_ref": "biads.tbl.udid"}]},
            }
        ]
        assert resolve_primary_key_columns(runtime, ["udid", "cnt"]) == ("udid",)


# ============================================================================
# 1b) 跨轮触发质量校验：主键从**持久化快照**回落
# ============================================================================
class TestPrimaryKeyFromPersistedSnapshot:
    """第二轮对话触发质量校验时，``ir_field_values`` 缓存是空的。

    ``Runtime`` 每次 ``chat()`` 都新建，``ir_field_values`` 只由 IR 的
    post-hook 写入 —— 独立工具触发的第二轮不触发那两个 hook → 缓存为空。
    TASKIR 的权威口径持久化在会话快照的 ``fields`` 里，主键解析必须能
    回落到快照。
    """

    def _runtime_with_snapshot(self, fields, status="committed"):
        runtime = _Runtime()
        runtime._cache["ir_session"] = {
            "snapshot": {"status": status, "fields": fields},
        }
        return runtime

    def test_identity_from_committed_snapshot(self):
        runtime = self._runtime_with_snapshot(
            [{"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}]
        )
        assert resolve_primary_key_columns(runtime, ["udid", "cnt"]) == ("udid",)

    def test_grain_from_committed_snapshot(self):
        runtime = self._runtime_with_snapshot(
            [
                {
                    "field_id": "output_features",
                    "value": {"features": [{"aggregation": {"grain": ["a", "b"]}}]},
                }
            ]
        )
        assert resolve_primary_key_columns(runtime, ["a", "b", "cnt"]) == ("a", "b")

    def test_uncommitted_snapshot_is_not_authoritative(self):
        """草稿快照不是权威口径 —— 不能拿它当主键声明。"""
        runtime = self._runtime_with_snapshot(
            [{"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}],
            status="draft",
        )
        assert resolve_primary_key_columns(runtime, ["udid", "cnt"]) == ()

    def test_awaiting_relation_snapshot_is_not_authoritative(self):
        """关系待澄清的快照同样不能用。"""
        runtime = self._runtime_with_snapshot(
            [{"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}],
            status="awaiting_relation",
        )
        assert resolve_primary_key_columns(runtime, ["udid", "cnt"]) == ()

    def test_runtime_cache_wins_over_snapshot(self):
        """缓存优先 —— 本轮刚填好的口径比旧快照新。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {"field_id": "identity", "value": {"components": [{"field_ref": "new_col"}]}}
        ]
        runtime._cache["ir_session"] = {
            "snapshot": {
                "status": "committed",
                "fields": [
                    {"field_id": "identity", "value": {"components": [{"field_ref": "old_col"}]}}
                ],
            }
        }
        assert resolve_primary_key_columns(runtime, ["new_col", "old_col"]) == ("new_col",)

    def test_snapshot_pk_must_still_match_table_columns(self):
        """快照里的主键同样要「全有或全无」匹配上目标表的列。"""
        runtime = self._runtime_with_snapshot(
            [
                {
                    "field_id": "identity",
                    "value": {"components": [{"field_ref": "a"}, {"field_ref": "b"}]},
                }
            ]
        )
        # 表里只有 a → 复合主键整组作废，不降级成单列。
        assert resolve_primary_key_columns(runtime, ["a"]) == ()

    def test_second_turn_runtime_shape_really_loses_the_cache(self):
        """回归防护：第二轮的 runtime **确实**拿不到 ``ir_field_values``。

        旧实现只读缓存，所以第二轮主键解析必然返回空 —— 主键重复率整项被
        跳过，报告却写成「跳过」，读起来像「查过了，没问题」。
        """
        # 第一轮：IR post-hook 写过缓存。
        first = _Runtime()
        first._cache["ir_field_values"] = [
            {"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}
        ]
        assert resolve_primary_key_columns(first, ["udid", "cnt"]) == ("udid",)

        # 第二轮：**全新 Runtime** —— 缓存是空的。
        second = _Runtime()
        assert second.get_cache("ir_field_values") is None
        # 旧实现在这里返回 () —— 主键拿不到。
        assert resolve_primary_key_columns(second, ["udid", "cnt"]) == ()

        # 但 prepare_turn 会把持久化快照恢复到 ir_session 里。
        second._cache["ir_session"] = {
            "snapshot": {
                "status": "committed",
                "fields": [
                    {"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}
                ],
            }
        }
        # 修复后：从快照回落，主键拿得到。
        assert resolve_primary_key_columns(second, ["udid", "cnt"]) == ("udid",)


# ============================================================================
# 2) 自修正：复检不完整不算收敛
# ============================================================================
def _violation() -> CorrectnessViolation:
    return CorrectnessViolation(
        rule="common_sense",
        field="target_conversion_rate",
        severity=SEVERITY_ERROR,
        message="转化率 22.89 远超合理上限 1.2",
        evidence={"observed": 22.89},
        suggestion="核对分子分母口径（分母不应是源表行数）",
    )


_SQL = (
    "INSERT OVERWRITE TABLE adhoctemp.tmp_x PARTITION (pt_d = '20261010')\n"
    "SELECT udid,\n"
    "       SUM(IF(is_pay = 1, 1, 0)) AS pay_cnt,\n"
    "       dt\n"
    "FROM dwd.dwd_order_detail\n"
    "WHERE dt = '20261010'\n"
    "GROUP BY udid, dt"
)


class TestSelfCorrectRequiresCompleteRecheck:
    @pytest.mark.asyncio
    async def test_incomplete_recheck_is_not_convergence(self, tmp_path: Path, monkeypatch):
        """复检本身没跑完 → 「没查到违规」不等于「校验通过」。"""
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {
                "edits": [
                    {
                        "old_text": "SUM(IF(is_pay = 1, 1, 0)) AS pay_cnt",
                        "new_text": "SUM(IF(is_pay = 1 AND amt > 0, 1, 0)) AS pay_cnt",
                    }
                ],
                "reason": "过滤负金额行",
            }

        monkeypatch.setattr(sc, "invoke_llm_json", _fake)

        async def _recheck(sql: str):
            # 违规列表是空的，但 common_sense 判定降级了 —— 复检不完整。
            return {
                "violations": [],
                "failed_checks": [{"name": "common_sense", "reason": "模型不可用"}],
                "partial": True,
            }

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=_recheck,
            llm=object(),
            max_rounds=2,
        )

        assert result.converged is False
        assert result.needs_manual_review is True
        assert result.round_summaries
        assert result.round_summaries[0]["status"] == "incomplete"

    @pytest.mark.asyncio
    async def test_complete_recheck_with_no_violations_converges(self, tmp_path: Path, monkeypatch):
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return {
                "edits": [
                    {
                        "old_text": "SUM(IF(is_pay = 1, 1, 0)) AS pay_cnt",
                        "new_text": "SUM(IF(is_pay = 1 AND amt > 0, 1, 0)) AS pay_cnt",
                    }
                ],
                "reason": "过滤负金额行",
            }

        monkeypatch.setattr(sc, "invoke_llm_json", _fake)

        async def _recheck(sql: str):
            return {
                "violations": [],
                "failed_checks": [],
                "partial": False,
                "ok": True,
            }

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=_recheck,
            llm=object(),
            max_rounds=2,
        )

        assert result.converged is True
        assert result.needs_manual_review is False


# ============================================================================
# 3) 空表 → error 级违规（阻断交付）
# ============================================================================
class TestEmptyTableIsAnError:
    """0 行时字段级指标全是 ``0/0 = NULL`` —— 校验实际是**空跑**。

    早先这些 NULL 渲染成 ``—``（"该指标不适用"），与「真的不适用」混为一谈。
    报告看起来"只有 1 处 row_count 小告警，其余字段都通过了" —— 而实际上
    一行数据都没校验到。判定口径：``row_count`` **成功算出且等于 0**。
    """

    def test_zero_rows_produces_an_error_violation(self):
        violation = qc._detect_empty_table(
            merged={
                "table_metrics": {"row_count": 0},
                "details": {
                    "exposure_cnt": {"null_rate": None, "single_value_ratio": None},
                    "target_cvr": {"null_rate": None, "single_value_ratio": None},
                },
            },
            correctness_merged={},
        )
        assert violation is not None
        assert violation.severity == SEVERITY_ERROR
        assert violation.is_error is True
        assert violation.rule == "empty_table"
        assert violation.evidence["row_count"] == 0
        assert "exposure_cnt" in violation.evidence["unverifiable_fields"]

    def test_non_zero_rows_is_not_an_empty_table(self):
        assert (
            qc._detect_empty_table(
                merged={"table_metrics": {"row_count": 1000}, "details": {}},
                correctness_merged={},
            )
            is None
        )

    def test_unknown_row_count_is_not_an_empty_table(self):
        """**没算出来**不等于 0 行 —— 那是「未知」，归 failed_checks 管。

        猜成空表会误报「阻断交付」，把一个执行失败伪装成数据问题。
        """
        assert (
            qc._detect_empty_table(
                merged={"table_metrics": {}, "details": {}},
                correctness_merged={},
            )
            is None
        )
        # row_count 算出来了但是 NULL → 同样不算空表
        assert (
            qc._detect_empty_table(
                merged={"table_metrics": {"row_count": None}, "details": {}},
                correctness_merged={},
            )
            is None
        )

    def test_empty_table_metrics_render_as_no_data_not_dash(self):
        """指标单元格渲染成「无数据」，不是 ``—``。

        ``—`` 读起来像「这个字段没有这项指标」（正常情况）；
        「无数据」才表达「一行都没校验到」（不正常）。
        """
        table = qc.render_metrics_table(
            {"exposure_cnt": {"null_rate": None, "single_value_ratio": None}},
            warnings=[],
            failed_metrics={},
            empty_table=True,
        )
        assert "| exposure_cnt | 无数据 |" in table
        assert "| exposure_cnt | — |" not in table
        assert "无数据" in table

    def test_non_empty_table_keeps_dash_for_not_applicable(self):
        """非空表时，「该指标不适用」仍然渲染成 ``—`` —— 语义没变。"""
        table = qc.render_metrics_table(
            {"cnt": {"null_rate": 1.0, "single_value_ratio": None}},
            warnings=[],
            failed_metrics={},
            empty_table=False,
        )
        assert "| cnt | 1.0000 | — |" in table


# ============================================================================
# 4) 主键匹配用「未排除」的投影列
# ============================================================================
class TestPrimaryKeySurvivesFieldExclusion:
    """排除名单里有 ``udid`` / ``id`` / ``uuid`` / ``guid`` ——
    而这些恰恰**经常就是主键本身**。

    早先先排除再匹配主键 → 主键列被滤掉 → 匹配不上 → 主键重复率整项
    被跳过，需求里「主键重复率必须为 0」这条最硬的规则**根本没跑**，
    报告却写成「跳过」，读起来像「查过了，没问题」。
    """

    def test_pk_matches_against_unexcluded_projection(self):
        """主键在排除名单里也要能匹配上。"""
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {"field_id": "identity", "value": {"components": [{"field_ref": "udid"}]}}
        ]
        # 未排除的投影列里有 udid → 匹配成功
        assert resolve_primary_key_columns(runtime, ["udid", "cnt"]) == ("udid",)

    def test_pk_still_requires_all_members(self):
        """排除名单的修复**不能**放松「全有或全无」的要求。

        复合主键 ``(a, b)`` 里 ``b`` 不在投影里 → 整组作废（不能降级成单列
        ``a`` —— 那会造成 0.866412 那种误报）。
        """
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = [
            {
                "field_id": "identity",
                "value": {
                    "components": [
                        {"field_ref": "app_second_class_cn_name"},
                        {"field_ref": "conversionbehaviorid"},
                    ]
                },
            }
        ]
        assert resolve_primary_key_columns(runtime, ["app_second_class_cn_name"]) == ()
