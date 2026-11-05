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
2. **Graceful exit** — no input / unresolvable target table / unavailable schema
   are friendly skips, not exceptions and not raw error dumps.
3. **Three field sources** — DML projection, DDL column list, semantic metadata.
4. **Markdown table** — the metrics are rendered as a Markdown table that the
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

_DDL_SQL = (
    "CREATE TABLE biads.ads_user_profile_ds ("
    "device_id STRING, app_dl_retention_ratio_1d_udid DOUBLE, pay_cnt_udid BIGINT"
    ")"
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
# Graceful exit — no usable input
# ============================================================================
class TestGracefulExit:
    @pytest.mark.asyncio
    async def test_no_sql_and_no_table_is_a_friendly_skip(self):
        result = await tool.validate_data("", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_sql"
        assert result["data"]["ok"] is True
        # 必须是人话，不是异常堆栈
        assert "未检测到可用的校验输入" in result["frontend_msg"]
        assert "Traceback" not in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_whitespace_only_sql_is_a_friendly_skip(self):
        result = await tool.validate_data("   \n\t  ", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_sql"

    @pytest.mark.asyncio
    async def test_select_sql_without_target_table_is_a_friendly_skip(self):
        """纯 SELECT 既没有目标表也没有字段 —— 友好跳过。"""
        result = await tool.validate_data("SELECT a, b FROM t", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_target_table"
        assert "无法确定要校验的目标表" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_insert_without_target_table_is_a_friendly_skip(self):
        result = await tool.validate_data("INSERT INTO x SELECT a FROM t", _tool_context=_ctx())

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_target_table"
        assert "无法确定要校验的目标表" in result["frontend_msg"]

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
        """没有任何可校验项时（字段级 + 表级都关掉）友好跳过。"""
        ctx = _ctx()
        ctx.tool_config = {"checks": ["null_rate"]}
        with patch.object(tool, "run_quality_check", AsyncMock()):
            result = await tool.validate_data(
                "INSERT OVERWRITE TABLE biads.t SELECT 1 FROM src", _tool_context=ctx
            )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "no_checkable_fields"
        assert "无法解析出可校验的字段" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_table_level_checker_runs_without_parsed_fields(self):
        """表级指标（数据行数）不依赖字段 —— 字段解析不出来时它仍然可以跑。"""
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(
                "INSERT OVERWRITE TABLE biads.t SELECT 1 FROM src", _tool_context=_ctx()
            )

        assert result["data"].get("skipped") is not True
        assert captured["direct_table"] == "biads.t"

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
# Field sources — DML / DDL / semantic
# ============================================================================
class TestFieldSources:
    @pytest.mark.asyncio
    async def test_dml_projection_provides_fields(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["data"]["field_source"] == "dml"
        assert captured["fields"] == [
            "app_dl_retention_ratio_1d_udid",
            "pay_cnt_udid",
        ]

    @pytest.mark.asyncio
    async def test_ddl_provides_fields_and_types(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(_DDL_SQL, _tool_context=_ctx())

        assert result["data"]["skipped"] is False
        assert result["data"]["target_table"] == "biads.ads_user_profile_ds"
        assert result["data"]["field_source"] == "ddl"
        assert captured["fields"] == [
            "device_id",
            "app_dl_retention_ratio_1d_udid",
            "pay_cnt_udid",
        ]
        assert captured["column_types"]["device_id"] == "string"
        assert captured["column_types"]["pay_cnt_udid"] == "bigint"

    @pytest.mark.asyncio
    async def test_table_name_alone_uses_semantic_metadata(self):
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        semantic_fields = ["device_id", "pay_cnt_udid"]
        semantic_types = {"device_id": "string", "pay_cnt_udid": "bigint"}

        with (
            patch.object(tool, "run_quality_check", _fake_run),
            patch.object(
                tool,
                "fetch_semantic_columns",
                return_value=(semantic_fields, semantic_types),
            ),
        ):
            result = await tool.validate_data(
                target_table="biads.ads_user_profile_ds", _tool_context=_ctx()
            )

        assert result["data"]["skipped"] is False
        assert result["data"]["target_table"] == "biads.ads_user_profile_ds"
        assert result["data"]["field_source"] == "semantic"
        assert captured["fields"] == semantic_fields
        assert captured["column_types"] == semantic_types
        assert captured["direct_table"] == "biads.ads_user_profile_ds"

    @pytest.mark.asyncio
    async def test_semantic_unavailable_is_a_friendly_skip(self):
        from dataagent.actions.tools.local_tool.dataops_table_schema import (
            SchemaResolutionError,
        )

        with patch.object(
            tool,
            "fetch_semantic_columns",
            side_effect=SchemaResolutionError("表 `x` 在元数据中不存在或没有字段。"),
        ):
            result = await tool.validate_data(
                target_table="biads.missing_table", _tool_context=_ctx()
            )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "schema_unavailable"
        assert "元数据中不存在" in result["frontend_msg"]
        assert "Traceback" not in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_unexpected_semantic_exception_does_not_escape(self):
        """semantic 抛非 SchemaResolutionError 的异常时，不能冒泡给调用方。

        回归点：``except SchemaResolutionError`` 之外没有兜底，形态异常的
        元数据会打穿工具的「永不抛异常」契约。
        """
        with patch.object(
            tool,
            "fetch_semantic_columns",
            side_effect=RuntimeError("metavisor exploded"),
        ):
            result = await tool.validate_data(
                target_table="biads.some_table", _tool_context=_ctx()
            )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "schema_unavailable"
        assert "Traceback" not in result["frontend_msg"]
        assert "metavisor exploded" in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_resolution_failure_does_not_escape(self):
        """解析阶段整体挂掉时，也要变成友好跳过而不是抛异常。"""
        with patch.object(
            tool,
            "_resolve_target_and_fields",
            side_effect=ValueError("bad input"),
        ):
            result = await tool.validate_data(
                target_table="biads.some_table", _tool_context=_ctx()
            )

        assert result["data"]["skipped"] is True
        assert result["data"]["reason"] == "resolution_failed"
        assert "Traceback" not in result["frontend_msg"]

    @pytest.mark.asyncio
    async def test_excluded_fields_are_dropped(self):
        """分区列 / 时间戳 / 主键 ID 不参与校验。"""
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        ddl = (
            "CREATE TABLE biads.t ("
            "device_id STRING, pt_d STRING, create_time TIMESTAMP, pay_cnt_udid BIGINT)"
        )
        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(ddl, _tool_context=_ctx())

        assert result["data"]["skipped"] is False
        assert captured["fields"] == ["device_id", "pay_cnt_udid"]

    @pytest.mark.asyncio
    async def test_empty_field_list_still_runs_table_level_checks(self):
        """字段一个都没有时，表级校验项（数据行数）仍要能跑。

        回归点：``fields=[]`` 曾被当成「没传字段」，导致整轮校验被跳过。
        """
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result(table_metrics={"row_count": 42})

        ctx = _ctx()
        ctx.tool_config = {"checks": ["row_count"]}
        with (
            patch.object(tool, "run_quality_check", _fake_run),
            patch.object(tool, "fetch_semantic_columns", return_value=([], {})),
        ):
            result = await tool.validate_data(
                target_table="biads.ads_user_profile_ds", _tool_context=ctx
            )

        assert result["data"]["skipped"] is False
        # 传的是**列表**（可能是空的），不是 None —— None 表示「字段由调用方自己解析」。
        assert captured["fields"] == []

    @pytest.mark.asyncio
    async def test_ddl_with_bare_table_name_is_recognised_as_ddl(self):
        """裸表名的 DDL 也要被识别为 DDL（不能误走 semantic 路径）。

        回归点：``looks_like_ddl`` 曾要求表名带库名，导致裸表名 DDL 被丢掉。
        """
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(
                "CREATE TABLE ads_user_profile_ds (device_id STRING)",
                target_table="biads.ads_user_profile_ds",
                _tool_context=_ctx(),
            )

        assert result["data"]["skipped"] is False
        assert result["data"]["field_source"] == "ddl"
        assert captured["fields"] == ["device_id"]
        assert captured["direct_table"] == "biads.ads_user_profile_ds"

    @pytest.mark.asyncio
    async def test_backtick_quoted_insert_target_is_normalised(self):
        """反引号包裹的目标表要解析成 ``db.table``。

        回归点：只 ``strip("`")`` 剥首尾的话，``\\`biads\\`.\\`ads_x\\``` 会留下
        中间的反引号变成 ``biads`.`ads_x`` —— 拿这个名字去查表必然查不到。
        """
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        sql = "INSERT OVERWRITE TABLE `biads`.`ads_user_profile_ds` SELECT device_id FROM src"
        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(sql, _tool_context=_ctx())

        assert result["data"]["target_table"] == "biads.ads_user_profile_ds"
        assert captured["direct_table"] == "biads.ads_user_profile_ds"

    @pytest.mark.asyncio
    async def test_ddl_comment_mentioning_create_table_is_not_misdetected(self):
        """注释里写 ``create table`` 不应把 INSERT 误判成 DDL。"""
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        sql = (
            "-- create table biads.something\n"
            "INSERT OVERWRITE TABLE biads.t SELECT device_id FROM src"
        )
        with patch.object(tool, "run_quality_check", _fake_run):
            result = await tool.validate_data(sql, _tool_context=_ctx())

        assert result["data"]["field_source"] == "dml"
        assert captured["fields"] == ["device_id"]

    @pytest.mark.asyncio
    async def test_dml_path_enriches_types_from_semantic(self):
        """DML 路径也去 semantic 补一次列类型 —— 判空写法的口径靠它。"""
        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with (
            patch.object(tool, "run_quality_check", _fake_run),
            patch.object(
                tool,
                "fetch_semantic_columns",
                return_value=(["device_id"], {"device_id": "string"}),
            ),
        ):
            await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert captured["column_types"] == {"device_id": "string"}

    @pytest.mark.asyncio
    async def test_dml_path_degrades_gracefully_when_semantic_unavailable(self):
        """semantic 查不到类型时不能抛异常 —— 降级成 None（退回 DDL / IS NOT NULL）。"""
        from dataagent.actions.tools.local_tool.dataops_table_schema import (
            SchemaResolutionError,
        )

        captured = {}

        async def _fake_run(sql, **kwargs):
            captured.update(kwargs)
            return _ok_result()

        with (
            patch.object(tool, "run_quality_check", _fake_run),
            patch.object(
                tool,
                "fetch_semantic_columns",
                side_effect=SchemaResolutionError("查不到"),
            ),
        ):
            result = await tool.validate_data(_INSERT_SQL, _tool_context=_ctx())

        assert result["data"]["skipped"] is False
        assert captured["column_types"] is None


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
