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
"""Unit tests for the standalone data validation tool (``dataops_validate_data``).

The contracts under test:

1. **Read-only** — the tool never creates/inserts/drops and never rewrites the
   target into a scratch table; it queries the *real* target table.
2. **Graceful exit** — no SQL / non-INSERT SQL / unresolvable target table are
   friendly skips, not exceptions and not raw error dumps.
3. **Markdown table** — the metrics are rendered as a Markdown table that the
   frontend can display directly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dataagent.core.suite.builtin_suites.de_agent.tools import dataops_validate_data_tool as tool

_INSERT_SQL = (
    "INSERT OVERWRITE TABLE biads.ads_user_profile_ds "
    "SELECT udid, app_dl_retention_ratio_1d_udid, pay_cnt_udid "
    "FROM dws.src WHERE pt_d = '20261020'"
)


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.tool_config = {}
    return ctx


def _ok_result(**overrides):
    result = {
        "ok": True,
        "warnings": [],
        "details": {
            "pay_cnt_udid": {
                "null_rate": 0.95,
                "single_value_ratio": 0.12,
                "outlier_ratio": None,
            },
            "app_dl_retention_ratio_1d_udid": {
                "null_rate": 0.45,
                "single_value_ratio": 0.98,
                "outlier_ratio": 0.05,
            },
        },
        "sampled": True,
        "sample_size": 10000,
        "checks": ["null_rate", "single_value_ratio", "outlier_ratio"],
    }
    result.update(overrides)
    return result


# ============================================================================
# Graceful exit — no SQL
# ============================================================================
class TestGracefulExit:
    @pytest.mark.asyncio
    async def test_no_sql_at_all_is_a_friendly_skip(self):
        result = await tool.validate_data("", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_sql"
        assert result["data"]["ok"] is True
        # 必须是人话，不是异常堆栈
        assert "未检测到可用的 INSERT 语句" in result["frontend_msg"]
        assert "Traceback" not in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_whitespace_only_sql_is_a_friendly_skip(self):
        result = await tool.validate_data("   \n\t  ", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_sql"

    @pytest.mark.asyncio
    async def test_non_insert_sql_is_a_friendly_skip(self):
        result = await tool.validate_data("SELECT a, b FROM t", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "not_insert_sql"
        assert "不是 INSERT 语句" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_create_sql_is_a_friendly_skip(self):
        result = await tool.validate_data(
            "CREATE TABLE biads.x (a int)", _tool_context=_ctx()
        )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "not_insert_sql"

    @pytest.mark.asyncio
    async def test_insert_without_target_table_is_a_friendly_skip(self):
        result = await tool.validate_data("INSERT INTO x SELECT a FROM t", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_target_table"
        assert "无法从 INSERT 语句中解析出目标表" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_missing_sql_file_is_a_friendly_skip(self, tmp_path):
        result = await tool.validate_data(
            "", sql_file_path=str(tmp_path / "nope.sql"), _tool_context=_ctx()
        )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "sql_read_failed"
        assert "Traceback" not in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_empty_sql_file_is_a_friendly_skip(self, tmp_path):
        path = tmp_path / "insert_x.sql"
        path.write_text("   \n", encoding="utf-8")

        result = await tool.validate_data(
            "", sql_file_path=str(path), _tool_context=_ctx()
        )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_sql"


# ============================================================================
# Read-only direct-target validation
# ============================================================================
class TestDirectTargetValidation:
    @pytest.mark.asyncio
    async def test_queries_the_real_target_table(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["data"]["skipped"] is False
        assert result["data"]["target_table"] == "biads.ads_user_profile_ds"
        # 直连真实目标表 —— 没有临时表参数
        assert captured["direct_table"] == "biads.ads_user_profile_ds"
        assert "temp_table" not in captured

    @pytest.mark.asyncio
    async def test_partition_filter_is_taken_from_the_insert(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert captured["partition_filter"] == "pt_d = '20261020'"

    @pytest.mark.asyncio
    async def test_explicit_partition_filter_wins(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            await tool.validate_data(
                _INSERT_SQL, partition_filter="pt_d = '20260101'", _tool_context=_ctx()
            )

        assert captured["partition_filter"] == "pt_d = '20260101'"

    @pytest.mark.asyncio
    async def test_explicit_target_table_wins(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            await tool.validate_data(
                _INSERT_SQL, target_table="biads.other_table", _tool_context=_ctx()
            )

        assert captured["direct_table"] == "biads.other_table"

    @pytest.mark.asyncio
    async def test_reads_sql_from_file(self, tmp_path):
        path = tmp_path / "insert_x.sql"
        path.write_text(_INSERT_SQL, encoding="utf-8")
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(
                "", sql_file_path=str(path), _tool_context=_ctx()
            )

        assert result["data"]["target_table"] == "biads.ads_user_profile_ds"
        assert captured["direct_table"] == "biads.ads_user_profile_ds"

    @pytest.mark.asyncio
    async def test_no_checkable_fields_is_a_friendly_skip(self):
        with patch.object(tool, "run_quality_check", AsyncMock()):
            result = await tool.validate_data(
                "INSERT OVERWRITE TABLE biads.t SELECT 1 FROM src", _tool_context=_ctx()
            )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_checkable_fields"
        assert "无法从 INSERT 的 SELECT 投影中解析出可校验字段" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_check_not_run_is_not_reported_as_pass(self):
        """校验没跑起来时不能报成「全部通过」。"""
        with patch.object(tool, "run_quality_check", AsyncMock(return_value=None)):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["data"]["ok"] is False
        assert result["data"]["reason"] == "check_not_run"
        assert "未能执行" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_sampled_false_is_not_reported_as_pass(self):
        with patch.object(
            tool,
            "run_quality_check",
            AsyncMock(
                return_value={
                    "ok": False,
                    "warnings": [],
                    "details": {},
                    "sampled": False,
                    "error": "partition condition not found",
                    "sample_size": 10000,
                }
            ),
        ):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["data"]["ok"] is False
        assert "partition condition not found" in result["frontend_msg"]


# ============================================================================
# Markdown table rendering
# ============================================================================
class TestMarkdownTable:
    @pytest.mark.asyncio
    async def test_frontend_msg_contains_a_markdown_table(self):
        with patch.object(tool, "run_quality_check", AsyncMock(return_value=_ok_result())):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        msg = result["frontend_msg"]
        assert "| 字段名 | 非空率(覆盖率) | 单一值比率 | 异常值占比 | 状态 |" in msg
        assert "|---|---|---|---|---|" in msg
        assert "| pay_cnt_udid | 0.9500 | 0.1200 | — | ✅ 通过 |" in msg

    @pytest.mark.asyncio
    async def test_not_applicable_metric_renders_as_dash(self):
        """非比率类字段的异常值占比是 None —— 必须渲染成 ``—`` 而不是 0。"""
        with patch.object(tool, "run_quality_check", AsyncMock(return_value=_ok_result())):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert "| pay_cnt_udid | 0.9500 | 0.1200 | — | ✅ 通过 |" in result["frontend_msg"]
        assert "| 0.0000 |" not in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_warned_field_is_flagged(self):
        result_payload = _ok_result(
            ok=False,
            warnings=["字段 'app_dl_retention_ratio_1d_udid' 非空率 0.4500 < 阈值 0.80"],
        )
        with patch.object(tool, "run_quality_check", AsyncMock(return_value=result_payload)):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert "⚠️ 告警" in result["frontend_msg"]
        assert "1 处告警" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_original_msg_and_frontend_msg_carry_the_table(self):
        with patch.object(tool, "run_quality_check", AsyncMock(return_value=_ok_result())):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["original_msg"] == result["frontend_msg"]
        assert "| 字段名 |" in result["original_msg"]
        assert result["data"]["markdown"] == result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_data_payload_carries_structured_details(self):
        with patch.object(tool, "run_quality_check", AsyncMock(return_value=_ok_result())):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["data"]["details"]["pay_cnt_udid"]["null_rate"] == 0.95
        assert result["data"]["sample_size"] == 10000
        assert result["data"]["checks"] == [
            "null_rate",
            "single_value_ratio",
            "outlier_ratio",
        ]
