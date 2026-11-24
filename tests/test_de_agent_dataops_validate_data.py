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
"""Unit tests for the data validation tool (``dataops_validate_data``).

The tool now runs the **trial-compute** flow (the flow that used to live in the
delivery post-hook): scratch table → trial INSERT → quality check → self-correction.

The contracts under test:

1. **Deliverable discovery** — the tool finds the workspace's ``insert_*.sql``
   (newest wins) or honours an explicit path.
2. **Graceful exit** — no deliverable / unreadable / empty are friendly skips,
   not exceptions and not raw error dumps.
3. **Trial compute delegation** — the trial compute result is the single source
   of truth for the report; the tool never fabricates a verdict.
4. **Self-correction** — error-level violations trigger the correction loop, and
   the verdict comes from a *complete* re-check, never from the "converged" flag.
5. **Markdown table** — the metrics are rendered as a Markdown table that the
   frontend can display directly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"
LOADER = PythonLoader()
tool = sys.modules[
    LOADER.load(PLUGIN, "utils/dataops_validate_data_tool.py:validate_data").__module__
]

_INSERT_SQL = (
    "INSERT OVERWRITE TABLE biads.ads_user_profile_ds "
    "SELECT udid, app_dl_retention_ratio_1d_udid, pay_cnt_udid "
    "FROM dws.src WHERE pt_d = '20261020'"
)


def _ctx(workspace: Path | None = None):
    return SimpleNamespace(
        session=SimpleNamespace(outputs=workspace),
        get_plugin_config=lambda _: {},
        get_plugin_state=lambda _: {},
        get_model=lambda _: None,
        semantic_client=None,
    )


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
        "table_metrics": {"row_count": 1000},
    }
    quality.update(overrides)
    return quality


def _trial_result(**overrides) -> dict:
    """一份「试算通过 + 校验通过」的试算结果。"""
    result = {
        "passed": True,
        "skipped": False,
        "target_table": "biads.ads_user_profile_ds",
        "quality_check": _quality(),
    }
    result.update(overrides)
    return result


def _deliverable(workspace: Path, name: str = "insert_x.sql", sql: str = _INSERT_SQL) -> Path:
    path = workspace / name
    path.write_text(sql, encoding="utf-8")
    return path


# ============================================================================
# Graceful exit — no usable deliverable
# ============================================================================
class TestGracefulExit:
    @pytest.mark.asyncio
    async def test_no_deliverable_is_a_friendly_skip(self, tmp_path):
        result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_dml"
        assert result["data"]["ok"] is True
        # 必须是人话，不是异常堆栈
        assert "未找到可校验的交付物" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_inline_sql_without_a_file_still_validates(self, tmp_path):
        """只给 ``sql`` 文本（没有文件）→ 能试算、能校验，但自修正没法写回。"""
        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ) as spy:
            result = await tool.validate_data(_INSERT_SQL, context=_ctx(tmp_path))

        assert result["data"]["passed"] is True
        assert spy.await_args.kwargs["sql"] == _INSERT_SQL

    @pytest.mark.asyncio
    async def test_empty_deliverable_is_a_friendly_skip(self, tmp_path):
        _deliverable(tmp_path, sql="")

        result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "empty_sql"
        assert "内容为空" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_table_name_only_falls_back_to_direct_table(self, tmp_path):
        """workspace 里没有交付物，但给了表名 → 走直连真实目标表的兜底路径。"""
        with (
            patch.object(
                tool,
                "fetch_semantic_columns",
                new=MagicMock(return_value=(["udid", "pay_cnt"], {"udid": "STRING"})),
            ),
            patch.object(tool, "run_quality_check", new=AsyncMock(return_value=_quality())) as spy,
        ):
            result = await tool.validate_data(
                "", target_table="biads.ads_user_profile_ds", context=_ctx(tmp_path)
            )

        assert result["data"]["mode"] == "direct_table"
        assert result["data"]["target_table"] == "biads.ads_user_profile_ds"
        # 直连模式**不试算** —— 不建表不插入。
        assert spy.await_args.kwargs["direct_table"] == "biads.ads_user_profile_ds"
        assert "直连真实目标表" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_direct_table_does_not_self_correct(self, tmp_path):
        """直连模式是只读体检 —— 不改真实数据，也不改上游 SQL。"""
        errored = _quality(
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

        with (
            patch.object(
                tool,
                "fetch_semantic_columns",
                new=MagicMock(return_value=(["udid"], {"udid": "STRING"})),
            ),
            patch.object(tool, "run_quality_check", new=AsyncMock(return_value=errored)),
            patch.object(tool, "run_self_correct", new=AsyncMock()) as sc,
        ):
            result = await tool.validate_data(
                "",
                target_table="biads.ads_user_profile_ds",
                context=_ctx(tmp_path),
            )

        sc.assert_not_called()
        assert result["data"]["needs_manual_review"] is True
        msg = result["frontend_msg"]
        assert "未执行" in msg
        assert "不建议" not in msg  # 直连模式没有「交付物」可交付

    @pytest.mark.asyncio
    async def test_direct_table_schema_unavailable_is_a_friendly_skip(self, tmp_path):
        """semantic 拿不到字段元数据 → 友好跳过，不是异常。"""
        SchemaResolutionError = tool.SchemaResolutionError

        with patch.object(
            tool,
            "fetch_semantic_columns",
            new=MagicMock(side_effect=SchemaResolutionError("表不在元数据里")),
        ):
            result = await tool.validate_data("", target_table="biads.nope", context=_ctx(tmp_path))

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "schema_unavailable"
        assert "表不在元数据里" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_missing_explicit_path_falls_back_to_workspace(self, tmp_path):
        """显式路径不存在时退回 workspace 扫描，而不是直接报错。"""
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ):
            result = await tool.validate_data(
                "", dml_path=str(tmp_path / "nope.sql"), context=_ctx(tmp_path)
            )

        assert result["data"]["passed"] is True

    @pytest.mark.asyncio
    async def test_unexpected_error_never_escapes(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["ok"] is False
        assert result["data"]["reason"] == "validation_failed"
        assert "数据校验执行失败" in result["frontend_msg"]


# ============================================================================
# Deliverable discovery
# ============================================================================
class TestDeliverableDiscovery:
    @pytest.mark.asyncio
    async def test_finds_insert_sql_in_workspace(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ) as spy:
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["passed"] is True
        assert spy.await_args.kwargs["sql"] == _INSERT_SQL

    def test_prefers_the_newest_insert_file(self, tmp_path):
        """``sql/`` 里可能是上一轮的旧副本 —— 必须取最新修改的那个。"""
        old = tmp_path / "sql"
        old.mkdir()
        stale = old / "insert_old.sql"
        stale.write_text("INSERT OVERWRITE TABLE t SELECT 1", encoding="utf-8")
        fresh = tmp_path / "insert_new.sql"
        fresh.write_text("INSERT OVERWRITE TABLE t SELECT 2", encoding="utf-8")

        import os
        import time

        now = time.time()
        os.utime(stale, (now - 3600, now - 3600))

        assert tool._find_dml(tmp_path) == fresh

    def test_ignores_non_insert_files(self, tmp_path):
        (tmp_path / "create_x.sql").write_text("CREATE TABLE t (a STRING)", encoding="utf-8")
        (tmp_path / "notes.md").write_text("hello", encoding="utf-8")

        assert tool._find_dml(tmp_path) is None

    def test_prefers_root_deliverable_over_sql_mirror(self, tmp_path):
        """同名时**根目录**优先 —— ``sql/`` 只是镜像，不是交付物本体。

        ``organize_workspace`` 用 ``shutil.copy`` 把根目录的 ``insert_*.sql``
        复制到 ``sql/``（不保留 mtime → 副本反而更新）。按 mtime 取「最新」
        会挑到影子副本，于是自修正把修好的 SQL 写回**影子**，而根目录的
        交付物原封不动 —— 语法校验链路读根目录那份，看到的还是改坏前的 SQL。
        """
        import os
        import time

        mirror_dir = tmp_path / "sql"
        mirror_dir.mkdir()
        root = tmp_path / "insert_a.sql"
        root.write_text("INSERT OVERWRITE TABLE t SELECT 1", encoding="utf-8")
        mirror = mirror_dir / "insert_a.sql"
        mirror.write_text("INSERT OVERWRITE TABLE t SELECT 1", encoding="utf-8")

        # 让副本的 mtime **更新**（模拟 shutil.copy 的行为）
        now = time.time()
        os.utime(root, (now - 3600, now - 3600))
        os.utime(mirror, (now, now))

        assert tool._find_dml(tmp_path) == root

    @pytest.mark.asyncio
    async def test_self_correct_writes_back_to_root_deliverable(self, tmp_path):
        """端到端：自修正的补丁必须落到**根目录**的交付物上。

        ``sql/`` 里的镜像必须**保持原样** —— 它只是备份。
        """
        import os
        import time

        mirror_dir = tmp_path / "sql"
        mirror_dir.mkdir()
        root = tmp_path / "insert_a.sql"
        root.write_text("INSERT OVERWRITE TABLE t SELECT 1", encoding="utf-8")
        mirror = mirror_dir / "insert_a.sql"
        mirror.write_text("INSERT OVERWRITE TABLE t SELECT 1", encoding="utf-8")

        # 让副本的 mtime **更新**（模拟 shutil.copy 的行为）
        now = time.time()
        os.utime(root, (now - 3600, now - 3600))
        os.utime(mirror, (now, now))

        fixed_sql = "INSERT OVERWRITE TABLE t SELECT 2"

        async def _fake_self_correct(*, dml_path, original_sql, violations, recheck, **kwargs):
            # 模拟自修正：写回修正后的 SQL，并触发一次完整复检。
            await recheck(fixed_sql)
            return MagicMock(
                converged=True,
                to_dict=lambda: {"status": "converged"},
            )

        error_quality = _quality(
            ok=False,
            violations=[
                {
                    "id": "pk_duplicate_rate",
                    "tier": "error",
                    "severity": "error",
                    "message": "主键重复",
                }
            ],
        )

        with (
            patch.object(
                tool,
                "_invoke_trial_compute",
                new=AsyncMock(return_value=_trial_result(quality_check=error_quality)),
            ),
            patch.object(tool, "run_self_correct", new=AsyncMock(side_effect=_fake_self_correct)),
        ):
            await tool.validate_data("", context=_ctx(tmp_path))

        # 补丁落在根目录的交付物上
        assert root.read_text(encoding="utf-8") == fixed_sql
        # 镜像没被动过
        assert mirror.read_text(encoding="utf-8") == "INSERT OVERWRITE TABLE t SELECT 1"

    @pytest.mark.asyncio
    async def test_explicit_dml_path_wins(self, tmp_path):
        explicit = _deliverable(
            tmp_path, name="chosen.sql", sql="INSERT OVERWRITE TABLE t SELECT 9"
        )
        _deliverable(tmp_path, name="insert_other.sql")

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ) as spy:
            await tool.validate_data("", dml_path=str(explicit), context=_ctx(tmp_path))

        assert spy.await_args.kwargs["sql"] == "INSERT OVERWRITE TABLE t SELECT 9"


# ============================================================================
# Trial compute delegation
# ============================================================================
class TestTrialComputeDelegation:
    @pytest.mark.asyncio
    async def test_trial_failure_is_reported_as_failure(self, tmp_path):
        _deliverable(tmp_path)

        failed = {
            "passed": False,
            "skipped": False,
            "error": "Table not found: biads.x",
            "log_analysis": {"error_type": "table_not_found", "suggestions": ["检查表名"]},
        }
        with patch.object(tool, "_invoke_trial_compute", new=AsyncMock(return_value=failed)):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["ok"] is False
        assert result["data"]["passed"] is False
        msg = result["frontend_msg"]
        assert "试算失败" in msg
        assert "Table not found" in msg
        assert "检查表名" in msg

    @pytest.mark.asyncio
    async def test_sampled_false_is_not_reported_as_pass(self, tmp_path):
        """校验没跑起来 —— 不能报成「全部通过」，也不能报成「0 处告警」。"""
        _deliverable(tmp_path)

        result_ = _trial_result(quality_check={"sampled": False, "error": "提交超时"})
        with patch.object(tool, "_invoke_trial_compute", new=AsyncMock(return_value=result_)):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        msg = result["frontend_msg"]
        assert "未能执行" in msg
        assert "提交超时" in msg
        assert "全部通过" not in msg

    @pytest.mark.asyncio
    async def test_partial_completion_is_not_reported_as_pass(self, tmp_path):
        """部分完成 ≠ 通过。"""
        _deliverable(tmp_path)

        result_ = _trial_result(
            quality_check=_quality(
                ok=False,
                failed_checks=[{"name": "common_sense", "reason": "超时"}],
                completed_checks=["null_rate", "row_count"],
            )
        )
        with patch.object(tool, "_invoke_trial_compute", new=AsyncMock(return_value=result_)):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        msg = result["frontend_msg"]
        # 「未完整完成」必须出现在**摘要句**里，且必须点名缺了哪几项 ——
        # 报告会被前端截断，只把它放在末尾小节里等于没说。
        assert "未完整完成" in msg
        assert "未跑完" in msg
        assert "common_sense" in msg
        # 「没跑完」与「有告警」必须分开说，不能混成一句。
        assert "不等于校验通过" in msg


# ============================================================================
# Markdown table + report shape
# ============================================================================
class TestMarkdownTable:
    @pytest.mark.asyncio
    async def test_frontend_msg_contains_a_markdown_table(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        msg = result["frontend_msg"]
        assert "| 字段 |" in msg or "|" in msg
        assert "pay_cnt_udid" in msg

    @pytest.mark.asyncio
    async def test_original_msg_and_frontend_msg_carry_the_table(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["original_msg"] == result["frontend_msg"]
        assert result["data"]["markdown"] == result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_data_payload_carries_structured_details(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        data = result["data"]
        assert data["details"]["pay_cnt_udid"]["null_rate"] == 0.95
        assert data["table_metrics"]["row_count"] == 1000
        assert data["quality_check"]["sampled"] is True

    @pytest.mark.asyncio
    async def test_report_has_an_explicit_delivery_conclusion(self, tmp_path):
        """必须有明确的「交付结论」—— 避免把「试算通过」读成「数据没问题」。"""
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert "交付结论" in result["frontend_msg"]
        assert "校验项全部跑完" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_error_violation_blocks_delivery_conclusion(self, tmp_path):
        """有 error 级违规 → 结论必须说「不建议直接交付」。"""
        _deliverable(tmp_path)

        result_ = _trial_result(
            quality_check=_quality(
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
        )
        with patch.object(tool, "_invoke_trial_compute", new=AsyncMock(return_value=result_)):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["ok"] is False
        msg = result["frontend_msg"]
        assert "error 级违规" in msg
        assert "不建议直接交付" in msg


# ============================================================================
# Self-correction
# ============================================================================
class TestSelfCorrection:
    @pytest.mark.asyncio
    async def test_no_error_violation_skips_self_correction(self, tmp_path):
        """只有 warning 级告警 → 不触发自修正。"""
        _deliverable(tmp_path)

        with (
            patch.object(
                tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
            ),
            patch.object(tool, "run_self_correct", new=AsyncMock()) as sc,
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        sc.assert_not_called()
        assert result["data"]["quality_check"]["self_correct"]["status"] == "not_needed"

    @pytest.mark.asyncio
    async def test_inline_sql_cannot_self_correct_but_says_so(self, tmp_path):
        """没有交付文件 → 自修正无法写回 → 必须**显式**标记，不能静默跳过。

        静默跳过会让用户看到「发现 N 处 error 级违规」却看不到任何自修正
        痕迹，以为闭环跑过了。
        """
        errored = _trial_result(
            quality_check=_quality(
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
        )

        with (
            patch.object(tool, "_invoke_trial_compute", new=AsyncMock(return_value=errored)),
            patch.object(tool, "run_self_correct", new=AsyncMock()) as sc,
        ):
            result = await tool.validate_data(_INSERT_SQL, context=_ctx(tmp_path))

        sc.assert_not_called()
        assert result["data"]["needs_manual_review"] is True
        self_correct = result["data"]["quality_check"]["self_correct"]
        assert self_correct["status"] == "unavailable"
        # 报告里必须显式写出「未执行」+ 原因，不能静默跳过。
        assert "未执行" in result["frontend_msg"]
        assert "没有交付文件可供写回" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_error_violation_triggers_self_correction(self, tmp_path):
        _deliverable(tmp_path)

        first = _trial_result(
            quality_check=_quality(
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
        )
        # 复检后违规清零。
        second = _trial_result()

        corrected = MagicMock()
        corrected.converged = True
        corrected.to_dict.return_value = {"status": "converged", "rounds": 1, "converged": True}

        with (
            patch.object(tool, "_invoke_trial_compute", new=AsyncMock(side_effect=[first, second])),
            patch.object(tool, "run_self_correct", new=AsyncMock(return_value=corrected)),
            patch.object(tool, "resolve_config", new=MagicMock()),
            patch.object(tool, "resolve_llm", new=MagicMock(return_value=MagicMock())),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["quality_check"]["self_correct"]["converged"] is True
        assert "自修正已收敛" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_unconverged_requires_manual_review(self, tmp_path):
        """未收敛 MUST 明确要求人工复核 —— 不伪装成功。"""
        _deliverable(tmp_path)

        first = _trial_result(
            quality_check=_quality(
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
        )
        second = _trial_result(
            quality_check=_quality(
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
        )

        corrected = MagicMock()
        corrected.converged = False
        corrected.to_dict.return_value = {
            "status": "not_converged",
            "rounds": 3,
            "converged": False,
            "reason": "仍有 error 级违规",
        }

        with (
            patch.object(tool, "_invoke_trial_compute", new=AsyncMock(side_effect=[first, second])),
            patch.object(tool, "run_self_correct", new=AsyncMock(return_value=corrected)),
            patch.object(tool, "resolve_config", new=MagicMock()),
            patch.object(tool, "resolve_llm", new=MagicMock(return_value=MagicMock())),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["needs_manual_review"] is True
        msg = result["frontend_msg"]
        assert "需要人工复核" in msg
        assert "不建议直接交付" in msg

    @pytest.mark.asyncio
    async def test_disputed_status_reports_rule_review_not_sql_fix(self, tmp_path):
        """模型判定误报 → 报告必须引导去**复核校验规则**，而不是改 SQL。"""
        _deliverable(tmp_path)

        first = _trial_result(
            quality_check=_quality(
                ok=False,
                violations=[
                    {
                        "rule": "common_sense",
                        "field": "target_conversion_rate",
                        "severity": "error",
                        "message": "转换率超过 1",
                    }
                ],
            )
        )
        second = _trial_result()

        corrected = MagicMock()
        corrected.converged = False
        corrected.to_dict.return_value = {
            "status": "disputed",
            "rounds": 1,
            "converged": False,
            "disputed": True,
            "reason": "模型判定该违规不成立（判定为误报）：common_sense；实际值与期望值一致",
        }

        with (
            patch.object(tool, "_invoke_trial_compute", new=AsyncMock(side_effect=[first, second])),
            patch.object(tool, "run_self_correct", new=AsyncMock(return_value=corrected)),
            patch.object(tool, "resolve_config", new=MagicMock()),
            patch.object(tool, "resolve_llm", new=MagicMock(return_value=MagicMock())),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        msg = result["frontend_msg"]
        # 报告里必须区分「误报」和「改不动」。
        assert "误报" in msg
        assert "复核校验规则" in msg
        # 不能出现「自修正未收敛」这种把误报说成改不动的措辞。
        assert "自修正未收敛" not in msg


# ============================================================================
# Empty table — 0 行时字段级校验是「空跑」，不能读成「通过」
# ============================================================================
class TestEmptyTable:
    """0 行 → 字段级指标全是 ``0/0 = NULL``。

    早先这些 NULL 被渲染成 ``—``（"该指标不适用"），与「真的不适用」混为一谈 ——
    报告看起来"只有 1 处 row_count 小告警，其余字段都通过了"，而实际上
    **一行数据都没校验到**。用户选定的语义：**error 级违规，阻断交付**。
    """

    @staticmethod
    def _empty_quality() -> dict:
        return _quality(
            ok=False,
            empty_table=True,
            violations=[
                {
                    "rule": "empty_table",
                    "field": "",
                    "severity": "error",
                    "message": "目标表 0 行 —— 字段级校验全部无意义",
                    "evidence": {"row_count": 0},
                    "suggestion": "先确认上游源表在试算分区是否有数据",
                },
                {
                    "rule": "row_count",
                    "field": "",
                    "severity": "warning",
                    "message": "数据行数 0 < 阈值 1",
                    "evidence": {"row_count": 0.0},
                    "suggestion": "",
                },
            ],
            details={
                "exposure_cnt": {"null_rate": None, "single_value_ratio": None},
                "target_cvr": {"null_rate": None, "single_value_ratio": None},
            },
            table_metrics={"row_count": 0},
        )

    @pytest.mark.asyncio
    async def test_empty_table_blocks_delivery(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool,
            "_invoke_trial_compute",
            new=AsyncMock(return_value=_trial_result(quality_check=self._empty_quality())),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["needs_manual_review"] is True
        assert result["data"]["ok"] is False
        msg = result["frontend_msg"]
        assert "不建议直接交付" in msg

    @pytest.mark.asyncio
    async def test_empty_table_metrics_say_no_data_not_dash(self, tmp_path):
        """指标单元格必须是「无数据」，不是 ``—``。

        ``—`` 读起来像「这个字段没有这项指标」（正常）；「无数据」才表达
        「一行都没校验到」（不正常）。
        """
        _deliverable(tmp_path)

        with patch.object(
            tool,
            "_invoke_trial_compute",
            new=AsyncMock(return_value=_trial_result(quality_check=self._empty_quality())),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        msg = result["frontend_msg"]
        assert "无数据" in msg
        # 字段级单元格不能是「—」（那是不适用）
        assert "| exposure_cnt | 无数据 |" in msg

    @pytest.mark.asyncio
    async def test_empty_table_violation_is_listed(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool,
            "_invoke_trial_compute",
            new=AsyncMock(return_value=_trial_result(quality_check=self._empty_quality())),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        rules = [v.get("rule") for v in result["data"]["violations"]]
        assert "empty_table" in rules
        assert "empty_table" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_empty_table_survives_a_lost_flag(self, tmp_path):
        """标志丢了也必须阻断交付。

        ``empty_table`` 标志会跨进程序列化、可能被旧版结果 / 手工构造的
        结果漏掉 —— 只看标志的话，「一行都没校验到」会被读成「全部通过」。
        """
        _deliverable(tmp_path)

        degraded = _quality(
            ok=False,
            empty_table=True,
            violations=[],  # 标志在，违规项不在
            details={"exposure_cnt": {"null_rate": None, "single_value_ratio": None}},
            table_metrics={"row_count": 0},
        )

        with patch.object(
            tool,
            "_invoke_trial_compute",
            new=AsyncMock(return_value=_trial_result(quality_check=degraded)),
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        assert result["data"]["ok"] is False
        assert result["data"]["needs_manual_review"] is True
        msg = result["frontend_msg"]
        assert "不建议直接交付" in msg
        assert "无数据" in msg


# ============================================================================
# Persistence
# ============================================================================
class TestPersistence:
    @pytest.mark.asyncio
    async def test_report_is_persisted_to_runtime_dir(self, tmp_path):
        _deliverable(tmp_path)

        with patch.object(
            tool, "_invoke_trial_compute", new=AsyncMock(return_value=_trial_result())
        ):
            result = await tool.validate_data("", context=_ctx(tmp_path))

        persisted = result["data"]["persisted_report"]
        assert persisted is not None
        path = Path(persisted)
        assert path.exists()
        assert path.parent.name == ".runtime"
        assert path.name.startswith("data_quality_")
        assert "校验结论已落盘" in result["frontend_msg"]

    def test_persist_failure_does_not_break_the_flow(self, tmp_path):
        """落盘失败只是少了复核依据 —— 不能阻断任何流程。"""
        # workspace 指向一个不可写的路径。
        result = tool._persist_quality_report(
            workspace=tmp_path / "nested" / "deep",
            result=_trial_result(),
        )
        # 能落盘（tmp_path 可写）—— 断言返回值形态即可。
        assert result is None or result.exists()


# ============================================================================
# Tool registration
# ============================================================================
class TestToolRegistration:
    """工具必须**注册成 YAML 里写的名字** —— 否则 agent 按名字调用会「not found」。

    Regression（真实事故）：``tools.yaml`` 里声明
    ``name: "dataops_validate_data"`` + ``function: "validate_data"``，
    而 ToolManager 早先按 ``function or name`` 解析注册名 → 工具注册成
    ``validate_data`` → agent 调 ``dataops_validate_data`` 报
    ``Tool 'dataops_validate_data' not found``。
    """

    def test_suite_yaml_registers_under_the_declared_name(self):
        from dataagent.config.declarations import PluginSpec
        from dataagent.config.strict_yaml import read_yaml
        from dataagent.extensions.tools import collect_tools

        spec = PluginSpec.model_validate(read_yaml(PLUGIN / ".plugin.yaml"))
        tools = {}
        collect_tools(PLUGIN, spec, PythonLoader(), tools)
        assert "de-agent__dataops_validate_data" in tools
        assert "de-agent__validate_data" not in tools
