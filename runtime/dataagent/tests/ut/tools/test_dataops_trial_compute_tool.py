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
"""Unit tests for ``de_agent.tools.dataops_trial_compute_tool``.

The trial compute runs the delivered INSERT against a scratch table and then
reuses the shared data quality check. The contracts under test:

1. Skip paths (``dml_path`` missing / unreadable / empty SQL) never raise.
2. A quality-check warning does **not** flip ``passed`` — "SQL runs" and
   "data is sane" are two different things.
3. The scratch table is dropped for DML, but kept for DDL (the matching
   INSERT has to target it).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.de_agent.tools import dataops_trial_compute_tool as tct

# The tool imports the dataops backend lazily; patch it at the module that
# owns the names, i.e. ``dataops_validate_tool`` / ``dataops_quality_check``.
_VALIDATE = "dataagent.actions.tools.local_tool.dataops_validate_tool"
_QUALITY = "dataagent.actions.tools.local_tool.dataops_quality_check"


def _ctx(workspace: Path | None = None) -> ToolExecutionContext:
    runtime = MagicMock()
    # ``_find_ddl`` 只认真实的 str/Path,测试替身的 MagicMock 路径必须置空。
    runtime.workspace_dir = workspace
    return ToolExecutionContext(runtime=runtime)  # type: ignore[arg-type]


def _write_sql(tmp_path: Path, sql: str, name: str = "insert_x.sql") -> Path:
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    return path


# ============================================================================
# Skip paths
# ============================================================================
class TestSkipPaths:
    @pytest.mark.asyncio
    async def test_missing_dml_path_is_skipped(self):
        result = await tct.trial_compute_table(dml_path="", _tool_context=_ctx())
        assert result["skipped"] is True
        assert result["passed"] is True
        assert result["reason"] == "no_dml_path"

    @pytest.mark.asyncio
    async def test_unreadable_file_is_reported_not_raised(self, tmp_path):
        missing = tmp_path / "does_not_exist.sql"
        result = await tct.trial_compute_table(dml_path=str(missing), _tool_context=_ctx())
        assert result["skipped"] is False
        assert result["passed"] is False
        assert "failed to read" in result["summary"]

    @pytest.mark.asyncio
    async def test_empty_sql_is_skipped(self, tmp_path):
        path = _write_sql(tmp_path, "   \n  ")
        result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())
        assert result["skipped"] is True
        assert result["reason"] == "empty_sql"


# ============================================================================
# Execution outcome
# ============================================================================
class TestExecution:
    @pytest.mark.asyncio
    async def test_dml_enables_partition_range_collapse(self, tmp_path):
        """质量校验链路必须开启 pt_d 范围收敛。

        交付物常用范围分区表达时间窗口（``pt_d <= '$date' AND pt_d >=
        '${start_time,-30,yyyyMMdd}'``），而 DataOps 后端不替换这些占位符，
        源表会被过滤成空集 → 试算扫不到任何行。
        """
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        seen: list[bool] = []

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            seen.append(_collapse_partition_range)
            return {"passed": True, "job_id": "j-1"}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value="j-cleanup"),
        ):
            await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        assert seen == [True]

    @pytest.mark.asyncio
    async def test_pass_runs_quality_check_and_drops_scratch(self, tmp_path):
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": True, "job_id": "j-1"}

        async def fake_quality(sql, *, _tool_context, _partition_date=None):
            return {"ok": True, "warnings": [], "details": {}}

        cleanup = MagicMock()

        async def fake_cleanup(sql, *, _tool_context, partition_date=None):
            cleanup(sql)
            return "j-cleanup"

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", side_effect=fake_cleanup),
            patch(f"{_QUALITY}.run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(
                dml_path=str(path), _tool_context=_ctx()
            )

        assert result["passed"] is True
        assert result["job_id"] == "j-1"
        assert result["quality_check"]["ok"] is True
        assert result["cleanup_job_id"] == "j-cleanup"
        assert cleanup.called

    @pytest.mark.asyncio
    async def test_failure_skips_quality_check(self, tmp_path):
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": False, "error": "syntax error", "job_id": "j-2"}

        quality = MagicMock()

        async def fake_quality(sql, *, _tool_context, _partition_date=None):
            quality(sql)
            return {"ok": True}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value=None),
            patch(f"{_QUALITY}.run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        assert result["passed"] is False
        assert "quality_check" not in result
        assert not quality.called

    @pytest.mark.asyncio
    async def test_failure_surfaces_log_analysis(self, tmp_path):
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {
                "passed": False,
                "error": "boom",
                "log_analysis": {
                    "error_type": "AnalysisException",
                    "suggestions": ["add a join"],
                },
            }

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value=None),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        assert result["log_analysis"]["error_type"] == "AnalysisException"


# ============================================================================
# Quality check is non-blocking
# ============================================================================
class TestQualityCheckNonBlocking:
    @pytest.mark.asyncio
    async def test_warnings_do_not_flip_passed(self, tmp_path):
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": True, "job_id": "j-3"}

        async def fake_quality(sql, *, _tool_context, _partition_date=None):
            return {
                "ok": False,
                "warnings": ["字段 'col1' 非空率 0.4500 < 阈值 0.80"],
                "details": {"col1": {"null_rate": 0.45}},
            }

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value="j-cleanup"),
            patch(f"{_QUALITY}.run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        # The trial run succeeded — the data merely has something to report.
        assert result["passed"] is True
        assert result["quality_check"]["ok"] is False
        assert len(result["quality_check"]["warnings"]) == 1
        assert "1 warning(s)" in result["summary"]


# ============================================================================
# Scratch table lifecycle
# ============================================================================
class TestScratchTableLifecycle:
    @pytest.mark.asyncio
    async def test_scratch_table_is_dropped_even_on_failure(self, tmp_path):
        """质量校验链路:不管成功与否,临时表都必须删掉。"""
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": False, "error": "syntax error", "job_id": "j-4"}

        cleanup = MagicMock()

        async def fake_cleanup(sql, *, _tool_context, partition_date=None):
            cleanup(sql)
            return "j-cleanup"

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", side_effect=fake_cleanup),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        assert result["passed"] is False
        # 失败也必须清理。
        assert cleanup.called
        assert result["cleanup_job_id"] == "j-cleanup"

    @pytest.mark.asyncio
    async def test_ddl_scratch_table_is_dropped_too(self, tmp_path):
        """DDL 本身没有数据可查,但它建的临时表同样要清理。"""
        path = _write_sql(tmp_path, "CREATE TABLE adhoctemp.tmp_x (col1 string)")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": True, "job_id": "j-4"}

        cleanup = MagicMock()

        async def fake_cleanup(sql, *, _tool_context, partition_date=None):
            cleanup(sql)
            return "j-cleanup"

        quality = MagicMock()

        async def fake_quality(sql, *, _tool_context, _partition_date=None):
            quality(sql)
            return {"ok": True}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", side_effect=fake_cleanup),
            patch(f"{_QUALITY}.run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        # DDL 没有数据可查。
        assert not quality.called
        # 但临时表照样要删。
        assert cleanup.called
        assert result["cleanup_job_id"] == "j-cleanup"

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_break_result(self, tmp_path):
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": True, "job_id": "j-5"}

        async def fake_cleanup(sql, *, _tool_context, partition_date=None):
            raise RuntimeError("drop failed")

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", side_effect=fake_cleanup),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        # 清理失败不能影响试算结论。
        assert result["passed"] is True
        assert "cleanup_job_id" not in result


# ============================================================================
# Scratch table re-creation
# ============================================================================
class TestScratchTableRecreation:
    """质量校验前要先按 DDL 再建一次临时表。"""

    @pytest.mark.asyncio
    async def test_ddl_is_executed_before_insert(self, tmp_path):
        (tmp_path / "create_x.sql").write_text(
            "CREATE TABLE biads.x (col1 string)", encoding="utf-8"
        )
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        calls: list[str] = []

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            calls.append(sql)
            return {"passed": True, "job_id": f"j-{len(calls)}"}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value="j-cleanup"),
        ):
            result = await tct.trial_compute_table(
                dml_path=str(path), _tool_context=_ctx(tmp_path)
            )

        assert len(calls) == 2
        assert calls[0].startswith("CREATE TABLE")
        assert "INSERT" in calls[1]
        assert result["create_passed"] is True
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_no_ddl_means_single_execution(self, tmp_path):
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        calls: list[str] = []

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            calls.append(sql)
            return {"passed": True, "job_id": "j-1"}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value="j-cleanup"),
        ):
            result = await tct.trial_compute_table(
                dml_path=str(path), _tool_context=_ctx(tmp_path)
            )

        assert len(calls) == 1
        assert "create_passed" not in result

    @pytest.mark.asyncio
    async def test_create_failure_aborts_before_insert(self, tmp_path):
        (tmp_path / "create_x.sql").write_text(
            "CREATE TABLE biads.x (col1 string)", encoding="utf-8"
        )
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        calls: list[str] = []

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            calls.append(sql)
            return {"passed": False, "error": "table exists", "job_id": "j-c"}

        cleanup = MagicMock()

        async def fake_cleanup(sql, *, _tool_context, partition_date=None):
            cleanup(sql)
            return "j-cleanup"

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", side_effect=fake_cleanup),
        ):
            result = await tct.trial_compute_table(
                dml_path=str(path), _tool_context=_ctx(tmp_path)
            )

        # 建表失败就不该继续试算。
        assert len(calls) == 1
        assert result["passed"] is False
        assert result["create_passed"] is False
        assert "table exists" in result["error"]
        assert "table exists" in result["summary"]
        # 建表失败也可能留下半成品,照样要清理。
        assert cleanup.called


# ============================================================================
# Partition date policy
# ============================================================================
class TestPartitionDate:
    """试算链路必须用「一周之前」的日期,而不是语法校验的「一年之后」。"""

    @pytest.mark.asyncio
    async def test_uses_one_week_ago_date(self, tmp_path):
        from datetime import date, timedelta

        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")
        expected = (date.today() - timedelta(days=7)).strftime("%Y%m%d")

        seen: dict[str, str | None] = {}

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            seen["validate"] = _partition_date
            return {"passed": True, "job_id": "j-pd"}

        async def fake_quality(sql, *, _tool_context, _partition_date=None):
            seen["quality"] = _partition_date
            return {"ok": True}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value="j-cleanup"),
            patch(f"{_QUALITY}.run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        assert seen["validate"] == expected
        assert seen["quality"] == expected
        assert result["partition_date"] == expected

    @pytest.mark.asyncio
    async def test_date_differs_from_validation_chain(self, tmp_path):
        from dataagent.actions.tools.local_tool import dataops_validate_tool as vt

        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        async def fake_validate(sql, *, _tool_context, _partition_date=None, _collapse_partition_range=False):
            return {"passed": True, "job_id": "j-pd2"}

        with (
            patch(f"{_VALIDATE}.dataops_validate_sql", side_effect=fake_validate),
            patch(f"{_VALIDATE}._cleanup_temp_table_wrapper", return_value="j-cleanup"),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), _tool_context=_ctx())

        assert result["partition_date"] != vt.partition_date_for_validation()


# ============================================================================
# Summary rendering
# ============================================================================
class TestSummary:
    def test_summary_reports_passed(self):
        summary = tct._build_summary({"passed": True, "job_id": "j-1"})
        assert "PASSED" in summary
        assert "j-1" in summary

    def test_summary_reports_failed(self):
        summary = tct._build_summary({"passed": False})
        assert "FAILED" in summary

    def test_summary_reports_skipped(self):
        summary = tct._build_summary({"passed": True, "skipped": True})
        assert "SKIPPED" in summary

    def test_summary_reports_quality_ok(self):
        summary = tct._build_summary({"passed": True, "quality_check": {"ok": True}})
        assert "quality_check=ok" in summary

    def test_summary_reports_quality_not_run(self):
        """校验没跑起来时不能报成「0 warning(s)」—— 那看起来像校验通过了。"""
        summary = tct._build_summary(
            {
                "passed": True,
                "quality_check": {
                    "ok": False,
                    "sampled": False,
                    "error": "partition condition not found",
                    "warnings": [],
                },
            }
        )
        assert "quality_check=not-run" in summary
        assert "partition condition not found" in summary
        assert "0 warning" not in summary
