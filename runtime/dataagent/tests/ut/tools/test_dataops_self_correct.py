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
"""Unit tests for :mod:`dataagent.actions.tools.local_tool.dataops_self_correct`.

核心契约（用户明确要求：「自修正时还是在原基础上修改 不要完全重写」）：

1. 增量补丁 —— 只动违规相关的片段，其余逐字保留；
2. 锚点唯一 —— ``old_text`` 不唯一则整条补丁作废；
3. 有界收敛 —— 轮次用尽 MUST ``needs_manual_review=True``，不许静默；
4. 必须写回 workspace + 先备份。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.actions.tools.local_tool import dataops_self_correct as sc
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CorrectnessViolation,
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


def _violation(severity: str = SEVERITY_ERROR, rule: str = "negative_count") -> CorrectnessViolation:
    return CorrectnessViolation(
        rule=rule,
        field="pay_cnt",
        severity=severity,
        message="计数型字段 'pay_cnt' 存在负值",
        evidence={"negative_rows": 3},
        suggestion="检查 SUM(IF(...)) 的口径，负值通常来自明细表里的负金额行",
    )


# ============================================================================
# apply_edits — 增量补丁
# ============================================================================
class TestApplyEdits:
    def test_single_replacement_keeps_rest_verbatim(self):
        updated = sc.apply_edits(
            _SQL,
            [
                {
                    "old_text": "SUM(IF(is_pay = 1, 1, 0)) AS pay_cnt",
                    "new_text": "SUM(IF(is_pay = 1 AND amt > 0, 1, 0)) AS pay_cnt",
                }
            ],
        )
        # 只有目标片段变了。
        assert "SUM(IF(is_pay = 1 AND amt > 0, 1, 0)) AS pay_cnt" in updated
        assert "FROM dwd.dwd_order_detail" in updated
        assert "GROUP BY udid, dt" in updated
        # 长度只增加了插入的片段。
        assert len(updated) > len(_SQL)

    def test_multiple_edits_applied(self):
        updated = sc.apply_edits(
            _SQL,
            [
                {"old_text": "SELECT udid,", "new_text": "SELECT udid, uid,"},
                {"old_text": "dt\n", "new_text": "dt, pt_d\n"},
            ],
        )
        assert "uid," in updated
        assert "dt, pt_d" in updated

    def test_empty_edits_rejected(self):
        with pytest.raises(sc.PatchError):
            sc.apply_edits(_SQL, [])

    def test_missing_old_text_rejected(self):
        with pytest.raises(sc.PatchError):
            sc.apply_edits(_SQL, [{"old_text": "not_in_sql", "new_text": "x"}])

    def test_non_unique_anchor_rejected(self):
        """锚点出现多次 → 无法确定改哪一处 → 整条补丁作废。"""
        sql = "SELECT a FROM t WHERE a = 1 AND a = 1"
        with pytest.raises(sc.PatchError):
            sc.apply_edits(sql, [{"old_text": "a = 1", "new_text": "a = 2"}])

    def test_identical_old_and_new_rejected(self):
        with pytest.raises(sc.PatchError):
            sc.apply_edits(_SQL, [{"old_text": "udid,", "new_text": "udid,"}])

    def test_overlapping_anchors_rejected(self):
        sql = "SELECT abc FROM t"
        with pytest.raises(sc.PatchError):
            sc.apply_edits(
                sql,
                [
                    {"old_text": "SELECT abc", "new_text": "SELECT xyz"},
                    {"old_text": "abc FROM", "new_text": "q FROM"},
                ],
            )

    def test_missing_new_text_rejected(self):
        with pytest.raises(sc.PatchError):
            sc.apply_edits(_SQL, [{"old_text": "udid,"}])


# ============================================================================
# summarize_diff
# ============================================================================
class TestSummarizeDiff:
    def test_shows_change(self):
        updated = _SQL.replace("GROUP BY udid, dt", "GROUP BY udid")
        diff = sc.summarize_diff(_SQL, updated)
        assert "GROUP BY" in diff

    def test_no_change(self):
        assert sc.summarize_diff(_SQL, _SQL) == "（无变化）"


# ============================================================================
# build_correction_prompt
# ============================================================================
class TestBuildCorrectionPrompt:
    def test_contains_sql_and_violation(self):
        prompt = sc.build_correction_prompt(_SQL, _violation())
        assert "dwd.dwd_order_detail" in prompt
        assert "negative_count" in prompt
        assert "pay_cnt" in prompt


# ============================================================================
# run_self_correct — 有界闭环
# ============================================================================
class TestRunSelfCorrect:
    @staticmethod
    def _llm(payloads: list[dict]):
        """返回一个按顺序吐 payload 的假模型。"""
        calls: list[dict] = []

        class _FakeLLM:
            async def ainvoke(self, *args, **kwargs):  # pragma: no cover - 不该被调到
                raise AssertionError("不应直接调用 ainvoke")

        def _factory():
            return _FakeLLM()

        return _factory, calls

    @staticmethod
    def _patch_invoke(payload: dict):
        """把 ``invoke_llm_json`` 替换成固定返回值。"""

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return payload

        return _fake

    @pytest.mark.asyncio
    async def test_converges_in_one_round(self, tmp_path: Path, monkeypatch):
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        payload = {
            "edits": [
                {
                    "old_text": "SUM(IF(is_pay = 1, 1, 0)) AS pay_cnt",
                    "new_text": "SUM(IF(is_pay = 1 AND amt > 0, 1, 0)) AS pay_cnt",
                }
            ],
            "reason": "过滤掉负金额行",
        }
        monkeypatch.setattr(sc, "invoke_llm_json", self._patch_invoke(payload))

        seen: list[str] = []

        async def _recheck(sql: str):
            seen.append(sql)
            return []

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=_recheck,
            llm=object(),
            max_rounds=3,
        )

        assert result.converged is True
        assert result.needs_manual_review is False
        assert result.rounds == 1
        # SQL 已写回 workspace。
        assert "amt > 0" in dml.read_text(encoding="utf-8")
        # 原文件已备份。
        assert result.backups and Path(result.backups[0]).exists()
        # 复检拿到的是**修正后**的 SQL。
        assert seen and "amt > 0" in seen[0]

    @pytest.mark.asyncio
    async def test_not_converged_requires_manual_review(self, tmp_path: Path, monkeypatch):
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        payload = {
            "edits": [{"old_text": "GROUP BY udid, dt", "new_text": "GROUP BY udid"}],
            "reason": "改了分组",
        }
        monkeypatch.setattr(sc, "invoke_llm_json", self._patch_invoke(payload))

        async def _recheck(sql: str):
            return [_violation()]

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
        assert result.rounds == 2
        assert result.remaining_violations

    @pytest.mark.asyncio
    async def test_warning_only_violations_skip_correction(self, tmp_path: Path):
        """只有 warning 级 → 不调模型、不写回。"""
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation(severity=SEVERITY_WARNING)],
            recheck=None,
            llm=None,
            max_rounds=3,
        )
        assert result.converged is True
        assert result.rounds == 0
        assert result.needs_manual_review is False
        assert dml.read_text(encoding="utf-8") == _SQL

    @pytest.mark.asyncio
    async def test_disabled_marks_manual_review(self, tmp_path: Path):
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=None,
            llm=object(),
            max_rounds=3,
            enabled=False,
        )
        assert result.converged is False
        assert result.needs_manual_review is True
        assert result.remaining_violations

    @pytest.mark.asyncio
    async def test_llm_unavailable_marks_manual_review(self, tmp_path: Path):
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=None,
            llm=None,
            max_rounds=3,
        )
        assert result.needs_manual_review is True
        assert "模型不可用" in result.reason

    @pytest.mark.asyncio
    async def test_non_unique_anchor_aborts_round(self, tmp_path: Path, monkeypatch):
        """锚点不唯一 → 本轮失败 → 需要人工复核，SQL 不被改坏。"""
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        payload = {"edits": [{"old_text": "udid", "new_text": "uid"}], "reason": "x"}
        monkeypatch.setattr(sc, "invoke_llm_json", self._patch_invoke(payload))

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=None,
            llm=object(),
            max_rounds=3,
        )
        assert result.converged is False
        assert result.needs_manual_review is True
        # SQL 未被写坏。
        assert dml.read_text(encoding="utf-8") == _SQL

    @pytest.mark.asyncio
    async def test_second_round_uses_previous_sql_as_base(self, tmp_path: Path, monkeypatch):
        """第 2 轮的底稿是**第 1 轮修正后**的 SQL（增量，不是从原始重来）。"""
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        payloads = [
            {
                "edits": [{"old_text": "dt\n", "new_text": "dt, pt_d\n"}],
                "reason": "第一轮",
            },
            {
                "edits": [{"old_text": "pt_d\n", "new_text": "pt_d, 1\n"}],
                "reason": "第二轮",
            },
        ]
        seq = list(payloads)

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return seq.pop(0)

        monkeypatch.setattr(sc, "invoke_llm_json", _fake)

        prompts: list[str] = []

        async def _recheck(sql: str):
            prompts.append(sql)
            return [_violation()] if len(prompts) == 1 else []

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=_recheck,
            llm=object(),
            max_rounds=3,
        )
        assert result.converged is True
        assert result.rounds == 2
        assert "pt_d" in prompts[1]


# ============================================================================
# 误报（disputed）—— 模型判定违规不成立 ≠ 「SQL 改不动」
# ============================================================================
class TestDisputedViolations:
    """模型判定违规不成立时，必须与「模型改不动」区分开。

    常识违规检测偶发误报，模型识别出「实际值与期望值一致，无违规」→
    返回空 ``edits``。早先这被报成 ``not_converged``「本轮没有产生任何
    补丁」—— 读起来像「SQL 有问题但改不动」。
    """

    @staticmethod
    def _patch_invoke(payload: dict):
        """把 ``invoke_llm_json`` 替换成固定返回值。"""

        async def _fake(llm, *, system, user, llm_kwargs=None):
            return payload

        return _fake

    @pytest.mark.asyncio
    async def test_false_positive_marked_disputed_not_not_converged(
        self, tmp_path: Path, monkeypatch
    ):
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        payload = {
            "edits": [],
            "reason": "实际值与期望值一致，无违规，采样口径宽松",
        }
        monkeypatch.setattr(sc, "invoke_llm_json", self._patch_invoke(payload))

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=None,
            llm=object(),
            max_rounds=3,
        )

        assert result.disputed is True
        payload_out = result.to_dict()
        assert payload_out["status"] == "disputed"
        assert payload_out["disputed"] is True
        # SQL 未被修改。
        assert dml.read_text(encoding="utf-8") == _SQL
        # 仍需人工复核（但方向是「复核规则」不是「改 SQL」）。
        assert result.needs_manual_review is True

    @pytest.mark.asyncio
    async def test_cannot_fix_is_not_disputed(self, tmp_path: Path, monkeypatch):
        """模型说「改不动」但没判定误报 → 仍是 not_converged。"""
        dml = tmp_path / "insert_tmp_x.sql"
        dml.write_text(_SQL, encoding="utf-8")

        payload = {"edits": [], "reason": "源表缺少必要字段，无法计算该指标"}
        monkeypatch.setattr(sc, "invoke_llm_json", self._patch_invoke(payload))

        result = await sc.run_self_correct(
            dml_path=dml,
            original_sql=_SQL,
            violations=[_violation()],
            recheck=None,
            llm=object(),
            max_rounds=3,
        )

        assert result.disputed is False
        assert result.to_dict()["status"] == "not_converged"

    def test_is_dispute_markers(self):
        assert sc._is_dispute("实际值与期望值一致，无违规") is True
        assert sc._is_dispute("该违规不成立") is True
        assert sc._is_dispute("误报") is True
        assert sc._is_dispute("源表缺少必要字段，无法计算该指标") is False
        assert sc._is_dispute("") is False

    def test_status_mapping(self):
        assert sc.SelfCorrectResult(converged=True).to_dict()["status"] == "converged"
        assert (
            sc.SelfCorrectResult(disputed=True, rounds=1).to_dict()["status"]
            == "disputed"
        )
        assert (
            sc.SelfCorrectResult(needs_manual_review=True).to_dict()["status"]
            == "not_needed"
        )
        assert (
            sc.SelfCorrectResult(needs_manual_review=True, rounds=1).to_dict()["status"]
            == "not_converged"
        )
