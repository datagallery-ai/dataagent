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
"""Unit tests for DataOps error-log bounding.

DataOps runs Spark/Hive jobs. When a job fails, the backend returns an inline
``errorLog`` that is routinely hundreds of KB. These tests pin down the
guarantee that such a log never reaches the agent's context unbounded — while
still reaching the *in-process* analyzer (which needs the full text to
classify the failure).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dataagent.actions.tools.local_tool import dataops_log_utils as lu
from dataagent.actions.tools.local_tool import dataops_probe_tool as pt
from dataagent.actions.tools.local_tool import dataops_prod_adapter as pa
from dataagent.actions.tools.local_tool import dataops_validate_tool as vt

# A stand-in for a real Spark driver log: exception at the head, executor
# noise filling the middle, "job aborted" chain at the tail.
_BIG_LOG = (
    "ERROR: AnalysisException: cannot resolve 'foo' given input columns: [id, name]\n"
    + "at org.apache.spark.sql.catalyst.expressions.ResolveReferences$$anonfun$1.apply\n" * 5000
    + "Job aborted due to stage failure: task 37 failed 3 times, most recent failure: AnalysisException\n"
)


# ===========================================================================
# dataops_log_utils — the shared helper
# ===========================================================================


class TestBoundText:
    def test_short_text_untouched(self):
        assert lu.bound_text("short", max_chars=100) == "short"

    def test_non_string_passthrough(self):
        assert lu.bound_text(123, max_chars=10) == 123
        assert lu.bound_text(None, max_chars=10) is None

    def test_long_text_is_capped(self):
        out = lu.bound_text("A" * 10_000, max_chars=500)
        # head + marker + tail, so the result is slightly longer than max_chars
        assert len(out) < 700
        assert "truncated" in out
        assert "omitted middle" in out

    def test_keeps_head_and_tail(self):
        """The root cause (head) and the abort chain (tail) both survive."""
        out = lu.bound_text(_BIG_LOG, max_chars=1000)
        assert "AnalysisException: cannot resolve 'foo'" in out
        assert "Job aborted due to stage failure" in out

    def test_respects_max_chars(self):
        out = lu.bound_text("B" * 50_000, max_chars=200)
        assert len(out) < 400


class TestBoundErrorFields:
    def test_bounds_error_and_error_log(self):
        payload = {"error": "E" * 50_000, "errorLog": "L" * 50_000}
        out = lu.bound_error_fields(payload)
        assert len(out["error"]) < 3000
        assert len(out["errorLog"]) < 3000

    def test_leaves_other_fields_alone(self):
        payload = {"rows": [1, 2, 3], "row_count": 3}
        out = lu.bound_error_fields(payload)
        assert out["rows"] == [1, 2, 3]
        assert out["row_count"] == 3

    def test_short_values_unchanged(self):
        payload = {"error": "boom", "errorLog": "log"}
        out = lu.bound_error_fields(payload)
        assert out["error"] == "boom"
        assert out["errorLog"] == "log"

    def test_bounds_summary_and_reason(self):
        payload = {"summary": "S" * 50_000, "reason": "R" * 50_000}
        out = lu.bound_error_fields(payload)
        assert len(out["summary"]) < 3000
        assert len(out["reason"]) < 3000


# ===========================================================================
# dataops_probe_tool — previously had ZERO truncation on errorLog
# ===========================================================================


class TestProbeErrorBounding:
    def test_err_bounds_error_text(self):
        out = pt._err({"table": "db.t"}, "execute_failed", start=0.0, error="X" * 100_000)
        assert len(out["error"]) < 3000

    def test_err_bounds_error_log_in_extra(self):
        """Regression: the prod path passed the raw Spark log via ``extra``
        and it reached the agent with no bound at all."""
        out = pt._err(
            {"table": "db.t"},
            "execute_failed",
            start=0.0,
            error="boom",
            extra={"errorLog": _BIG_LOG},
        )
        assert len(out["errorLog"]) < 3000
        # The root cause is still visible to the agent.
        assert "AnalysisException" in out["errorLog"]

    def test_err_keeps_job_id_and_reason(self):
        out = pt._err({"table": "db.t"}, "timed_out", start=0.0, job_id="j-1")
        assert out["job_id"] == "j-1"
        assert out["reason"] == "timed_out"
        assert out["ok"] is False


# ===========================================================================
# dataops_prod_adapter — the log used to be duplicated into ``error``
# ===========================================================================


class TestProdAdapterErrorBounding:
    @pytest.mark.asyncio
    async def test_failed_error_field_is_a_summary_not_the_whole_log(self):
        """``error`` and ``errorLog`` used to hold the same full log, so the
        log was sent to the LLM twice."""
        polled = {
            "status": "failed",
            "raw": {"errorLog": _BIG_LOG},
            "error": None,
        }
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-big")

        assert result["status"] == "failed"
        # errorLog keeps the full log for the in-process analyzer.
        assert result["errorLog"] == _BIG_LOG
        # ...but ``error`` is a short summary.
        assert len(result["error"]) < 1000
        assert "AnalysisException" in result["error"]

    @pytest.mark.asyncio
    async def test_failed_short_log_preserved_in_both(self):
        polled = {
            "status": "failed",
            "raw": {"errorLog": "Table 'xxx' does not exist at line 1"},
            "error": None,
        }
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-3")

        assert result["errorLog"] == "Table 'xxx' does not exist at line 1"
        assert result["error"] == "Table 'xxx' does not exist at line 1"


# ===========================================================================
# dataops_validate_tool — the agent-facing boundary
# ===========================================================================


class TestValidateErrorBounding:
    def test_failure_response_bounds_error(self):
        out = vt._build_failure_response("failed", "j-1", {"error": "E" * 100_000}, "SELECT 1")
        assert len(out["error"]) < 3000
        assert out["passed"] is False

    def test_prod_failure_response_bounds_error(self):
        out = vt._build_failure_response_prod("failed", "j-1", {"error": "E" * 100_000}, "SELECT 1")
        assert len(out["error"]) < 3000

    def test_prod_failure_response_keeps_full_error_log_for_analyzer(self):
        """The analyzer needs the raw log, so it must NOT be truncated here —
        the bound happens at the agent boundary instead."""
        out = vt._build_failure_response_prod("failed", "j-1", {"error": "boom", "errorLog": _BIG_LOG}, "SELECT 1")
        assert out["errorLog"] == _BIG_LOG
        assert out["log_source"] == "inline"

    def test_summarize_attempt_bounds_error_message(self):
        """``attempts`` is echoed back as ``prior_attempts`` on every retry,
        so an unbounded error would be replayed up to 10 times."""
        summary = vt._summarize_attempt(
            1,
            {"passed": False, "error": "E" * 100_000},
            start_time=0.0,
        )
        assert len(summary["error_message"]) < 3000

    def test_summarize_attempt_bounds_log_analysis_summary(self):
        summary = vt._summarize_attempt(
            1,
            {"passed": False, "log_analysis_summary": "S" * 100_000},
            start_time=0.0,
        )
        assert len(summary["log_analysis_summary"]) < 3000

    def test_summarize_attempt_prefers_analysis_error_message(self):
        summary = vt._summarize_attempt(
            1,
            {
                "passed": False,
                "error": "raw",
                "log_analysis": {"error_message": "cannot resolve 'foo'"},
            },
            start_time=0.0,
        )
        assert summary["error_message"] == "cannot resolve 'foo'"

    @pytest.mark.asyncio
    async def test_no_log_source_early_return_is_bounded(self):
        """Regression: the "no log source" branch returned before the
        truncation at the end of the function, so a huge ``error`` escaped."""
        huge = "Z" * 100_000
        with patch.object(
            vt,
            "dataops_validate_sql",
            AsyncMock(return_value={"passed": False, "error": huge, "job_id": "j-1"}),
        ):
            result = await vt.dataops_validate_sql_with_log_analysis(
                "SELECT 1",
                _tool_context=MagicMock(),
            )

        assert result["passed"] is False
        assert len(result["error"]) < 3000

    @pytest.mark.asyncio
    async def test_wrapper_result_error_log_is_bounded(self):
        """The wrapper is the agent-facing boundary: its returned errorLog
        must be bounded even though the analyzer saw the full log."""
        with patch.object(
            vt,
            "dataops_validate_sql",
            AsyncMock(
                return_value={
                    "passed": False,
                    "error": "boom",
                    "job_id": "j-1",
                    "errorLog": _BIG_LOG,
                }
            ),
        ), patch.object(vt, "_analyze_log_directly", AsyncMock(return_value={"status": "ok", "analysis": {}})):
            result = await vt.dataops_validate_sql_with_log_analysis(
                "SELECT 1",
                _tool_context=MagicMock(),
            )

        assert len(result["errorLog"]) < 3000
        assert "AnalysisException" in result["errorLog"]


# ===========================================================================
# dataops_fetch_obs_log — defensive cap on the fetched log
# ===========================================================================


class TestFetchObsLogBounding:
    def test_huge_log_is_capped(self):
        client = MagicMock()
        resp = MagicMock(status_code=200, text="L" * 500_000)
        client.get.return_value = resp
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={"url": "https://obs/a.log", "headers": {"Authorization": "x"}},
                    _tool_context=MagicMock(),
                )
            )

        assert result["status"] == "success"
        assert len(result["log_content"]) <= vt._MAX_OBS_LOG_CHARS + 500
