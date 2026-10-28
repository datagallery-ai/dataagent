# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for :mod:`dataagent.actions.tools.local_tool.dataops_validate_tool`.

The tool is a single entry point called by the main agent after the 8-category
quality self-check has passed. It is responsible for:

1. SQL rewriting (CREATE/INSERT → temp table, ``$date`` placeholder, CTE expansion)
2. Lifecycle management (submit → poll → collect)
3. On failure, surfacing ``log_file_info`` so the caller can fetch the OBS log
4. When called via ``dataops_validate_sql_with_log_analysis``, **in-process**
   fetch + LLM analysis with structured output (no chat-history pollution)

The tests below pin down each of these contracts.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import dataops_validate_tool as vt
from dataagent.resources.catalog.models import Resource

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def collected_stream(monkeypatch):
    """Isolate response parsing from transport; live streaming is covered by the E2E tests."""
    collector = AsyncMock(return_value="")
    monkeypatch.setattr(vt, "collect_llm_stream", collector)
    return collector


def _make_resource(
    *,
    enabled: bool = True,
    exec_user: str | None = "u1",
    url: str = "http://localhost:8767/mcp",
    timeout_s: int = 60,
    poll_interval_ms: int = 30_000,
    table_suffix: str = "",
) -> Resource:
    """Build a minimal but real ``Resource`` for the dataops resource."""
    metadata = {
        "enabled": enabled,
        "timeout_s": timeout_s,
        "poll_interval_ms": poll_interval_ms,
    }
    if exec_user is not None:
        metadata["exec_user"] = exec_user
    if table_suffix:
        metadata["table_suffix"] = table_suffix
    return Resource(
        id="dataops",
        name="dataops",
        category="executable",
        metadata=metadata,
        transport={"type": "mcp", "url": url},
    )


def _catalog(resource: Resource | None) -> SimpleNamespace:
    return SimpleNamespace(get=lambda key: resource if key == "dataops" else None)


def _make_runtime(
    *,
    resource: Resource | None = None,
    coordinator: MagicMock | None = None,
    user_id: str | None = None,
) -> MagicMock:
    """Build a runtime double whose ``ensure_resource_coordinator`` returns the
    given coordinator and whose catalog lookup yields ``resource``.
    """
    runtime = MagicMock()
    runtime.ensure_resource_coordinator.return_value = coordinator
    if coordinator is not None:
        coordinator.catalog = _catalog(resource)
    if user_id is not None:
        runtime.user_id = user_id
    return runtime


def _make_context(runtime: MagicMock) -> ToolExecutionContext:
    return ToolExecutionContext(runtime=runtime)  # type: ignore[arg-type]


def _wired_coordinator(
    *,
    resource: Resource | None = None,
    submit: dict | None = None,
    poll: dict | list | None = None,
    collect: dict | list | None = None,
) -> MagicMock:
    """Build a coordinator already wired to return canned submit/poll/collect.

    poll and collect can be a single dict (return_value) or a list
    (side_effect returning one value per call, used by tests with
    multiple submit/poll/collect rounds such as the post-validate phase).
    """
    coordinator = MagicMock()
    coordinator.catalog = _catalog(resource)
    if submit is not None:
        coordinator.submit_job.return_value = submit
    if poll is not None:
        if isinstance(poll, list):
            coordinator.poll.side_effect = poll
        else:
            coordinator.poll.return_value = poll
    if collect is not None:
        if isinstance(collect, list):
            coordinator.collect.side_effect = collect
        else:
            coordinator.collect.return_value = collect
    return coordinator


class _FakeLLMResp:
    """Stand-in for whatever the runtime/manager LLM returns."""

    def __init__(self, content: str) -> None:
        self.content = content


# ===========================================================================
# Resource / user account lookup
# ===========================================================================


class TestGetDataopsResource:
    def test_returns_resource_when_catalog_has_it(self):
        resource = _make_resource()
        coordinator = _wired_coordinator(resource=resource)
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        assert vt._get_dataops_resource(runtime) is resource

    def test_returns_none_when_coordinator_raises(self):
        runtime = MagicMock()
        runtime.ensure_resource_coordinator.side_effect = RuntimeError("no coord")
        assert vt._get_dataops_resource(runtime) is None

    def test_returns_none_when_coordinator_is_none(self):
        runtime = MagicMock()
        runtime.ensure_resource_coordinator.return_value = None
        assert vt._get_dataops_resource(runtime) is None

    def test_returns_none_when_resource_not_in_catalog(self):
        coordinator = _wired_coordinator(resource=None)
        runtime = _make_runtime(resource=None, coordinator=coordinator)
        assert vt._get_dataops_resource(runtime) is None


class TestGetUserAccount:
    def test_env_var_takes_priority(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_EXEC_USER", "from_env")
        runtime = MagicMock(user_id="from_runtime")
        assert vt._get_user_account(runtime) == "from_env"

    def test_falls_back_to_runtime_user_id(self, monkeypatch):
        monkeypatch.delenv("DATAOPS_EXEC_USER", raising=False)
        runtime = MagicMock()
        runtime.user_id = "alice"
        assert vt._get_user_account(runtime) == "alice"

    def test_falls_back_to_anonymous(self, monkeypatch):
        monkeypatch.delenv("DATAOPS_EXEC_USER", raising=False)
        runtime = MagicMock(spec=[])  # no user_id attr
        assert vt._get_user_account(runtime) == "anonymous"

    def test_strips_whitespace_env(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_EXEC_USER", "   ")
        runtime = MagicMock(user_id="alice")
        assert vt._get_user_account(runtime) == "alice"


# ===========================================================================
# SQL rewriting
# ===========================================================================


class TestReplaceTargetTable:
    """_replace_target_table rewrites CREATE/INSERT and replaces $date."""

    def test_create_table_uses_temp_table(self):
        out = vt._replace_target_table(
            "CREATE TABLE biads.ads_xxx (id INT)",
            user_account="alice",
            table_suffix="",
        )
        assert "biads" not in out
        assert "adhoctemp.tmp_alice_" in out
        assert "ads_xxx" in out
        assert out.startswith("CREATE TABLE")

    def test_create_table_if_not_exists_preserved(self):
        out = vt._replace_target_table(
            "CREATE TABLE IF NOT EXISTS biads.ads_xxx (id INT)",
            user_account="alice",
            table_suffix="",
        )
        assert "IF NOT EXISTS" in out
        assert "adhoctemp.tmp_alice_" in out

    def test_insert_overwrite_uses_temp_table(self):
        out = vt._replace_target_table(
            "INSERT OVERWRITE TABLE biads.ads_yyy SELECT * FROM src",
            user_account="alice",
            table_suffix="",
        )
        assert "biads.ads_yyy" not in out
        assert "adhoctemp.tmp_alice_" in out
        assert "ads_yyy" in out

    def test_insert_external_table_strips_external_keyword(self):
        out = vt._replace_target_table(
            "INSERT OVERWRITE EXTERNAL TABLE biads.ads_eee SELECT * FROM src",
            user_account="alice",
            table_suffix="",
        )
        assert "EXTERNAL" not in out.upper() or "external" not in out.lower()
        assert "adhoctemp.tmp_alice_" in out

    def test_insert_into_uses_temp_table(self):
        out = vt._replace_target_table(
            "INSERT INTO TABLE biads.ads_zzz SELECT * FROM src",
            user_account="alice",
            table_suffix="",
        )
        assert "adhoctemp.tmp_alice_" in out

    def test_select_without_create_or_insert_unchanged(self):
        sql = "SELECT 1 FROM some_table"
        assert vt._replace_target_table(sql, user_account="alice", table_suffix="") == sql

    def test_dollar_date_placeholder_replaced(self):
        out = vt._replace_target_table(
            "SELECT * FROM t WHERE pt_d = '$date'",
            user_account="alice",
            table_suffix="",
        )
        assert "$date" not in out
        # Replaced with YYYYMMDD (8 digits)
        import re

        assert re.search(r"pt_d = '\d{8}'", out)

    def test_dollar_brace_date_placeholder_replaced(self):
        out = vt._replace_target_table(
            "SELECT * FROM t WHERE pt_d = '${date}'",
            user_account="alice",
            table_suffix="",
        )
        assert "${date}" not in out
        import re

        assert re.search(r"pt_d = '\d{8}'", out)

    def test_hyphens_in_table_name_become_underscores(self):
        out = vt._replace_target_table(
            "CREATE TABLE biads.ads-with-hyphens (id INT)",
            user_account="alice",
            table_suffix="",
        )
        assert "-" not in out

    def test_create_with_qualified_columns_preserved(self):
        # Column list inside parens must survive verbatim.
        out = vt._replace_target_table(
            "CREATE TABLE biads.ads_x (id INT, name VARCHAR(50))",
            user_account="alice",
            table_suffix="",
        )
        assert "id INT" in out
        assert "name VARCHAR(50)" in out


class TestPartitionDatePolicy:
    """两条链路的分区日期必须不同:语法校验=一年之后,质量校验=一周之前。"""

    def test_validation_date_is_one_year_ahead(self):
        expected = (date.today() + timedelta(days=365)).strftime("%Y%m%d")
        assert vt.partition_date_for_validation() == expected

    def test_quality_date_is_one_week_ago(self):
        expected = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        assert vt.partition_date_for_quality() == expected

    def test_two_chains_use_different_dates(self):
        assert vt.partition_date_for_validation() != vt.partition_date_for_quality()

    def test_replace_target_table_defaults_to_validation_date(self):
        out = vt._replace_target_table(
            "SELECT * FROM t WHERE pt_d = '$date'",
            user_account="alice",
            table_suffix="",
        )
        assert vt.partition_date_for_validation() in out

    def test_replace_target_table_honours_explicit_date(self):
        out = vt._replace_target_table(
            "SELECT * FROM t WHERE pt_d = '$date'",
            user_account="alice",
            table_suffix="",
            partition_date="20200101",
        )
        assert "pt_d = '20200101'" in out

    def test_quality_check_default_date_is_one_week_ago(self):
        from dataagent.actions.tools.local_tool import dataops_quality_check as qc

        assert qc.default_partition_date() == vt.partition_date_for_quality()


class TestHasCte:
    def test_with_before_insert(self):
        assert vt._has_cte("WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t") is True

    def test_with_after_insert_returns_false(self):
        assert vt._has_cte("INSERT INTO r WITH t AS (SELECT 1) SELECT * FROM t") is False

    def test_no_cte(self):
        assert vt._has_cte("SELECT 1") is False

    def test_empty_string(self):
        assert vt._has_cte("") is False


class TestExpandCteForDml:
    """_expand_cte_for_dml uses the runtime/main LLM, then llm_manager."""

    def test_no_cte_returns_unchanged(self):
        out = asyncio.run(vt._expand_cte_for_dml("SELECT 1 FROM t", runtime=None))
        assert out == "SELECT 1 FROM t"

    def test_runtime_llm_used_when_available(self):
        fake_llm = MagicMock(invoke=MagicMock(return_value=_FakeLLMResp("INSERT INTO r SELECT * FROM (SELECT 1) AS t")))
        fake_runtime = MagicMock()
        fake_runtime.llm = MagicMock(return_value=fake_llm)
        out = asyncio.run(
            vt._expand_cte_for_dml(
                "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t",
                runtime=fake_runtime,
            )
        )
        assert "INSERT INTO r" in out
        assert "(SELECT 1) AS t" in out
        fake_runtime.llm.assert_called_once_with("planner")

    def test_falls_back_to_llm_manager_when_runtime_llm_missing(self):
        fake_llm = MagicMock(invoke=MagicMock(return_value=_FakeLLMResp("INSERT INTO r SELECT * FROM (SELECT 1) AS t")))
        with patch.object(vt, "llm_manager") as mock_llm_manager:
            mock_llm_manager.get_default_llm.return_value = fake_llm
            out = asyncio.run(
                vt._expand_cte_for_dml(
                    "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t",
                    runtime=None,
                )
            )
        mock_llm_manager.get_default_llm.assert_called_once()
        assert "(SELECT 1) AS t" in out

    def test_strips_markdown_fenced_output(self):
        fake_llm = MagicMock(
            invoke=MagicMock(return_value=_FakeLLMResp("```sql\nINSERT INTO r SELECT * FROM (SELECT 1) AS t\n```"))
        )
        fake_runtime = MagicMock()
        fake_runtime.llm = MagicMock(return_value=fake_llm)
        out = asyncio.run(
            vt._expand_cte_for_dml(
                "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t",
                runtime=fake_runtime,
            )
        )
        assert "```" not in out

    def test_returns_original_on_llm_exception(self):
        fake_llm = MagicMock(invoke=MagicMock(side_effect=RuntimeError("LLM down")))
        fake_runtime = MagicMock()
        fake_runtime.llm = MagicMock(return_value=fake_llm)
        original = "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t"
        out = asyncio.run(vt._expand_cte_for_dml(original, runtime=fake_runtime))
        assert out == original

    def test_returns_original_when_llm_output_equals_input(self):
        # Sanity: LLM echoed the input — we don't rewrite with the same string.
        sql = "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t"
        fake_llm = MagicMock(invoke=MagicMock(return_value=_FakeLLMResp(sql)))
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        out = asyncio.run(vt._expand_cte_for_dml(sql, runtime=runtime))
        assert out == sql

    def test_default_timeout_cap_is_300s(self):
        """CTE 展开的等待上限是 300s（此前写成 60s，实际未生效）。"""
        assert vt._CTE_EXPANSION_MAX_TIMEOUT_SEC == 300

    def test_timeout_is_capped_at_300s(self):
        """传入的超时被夹到 300s，不会无限放大。"""
        captured: dict[str, float] = {}

        async def _fake_wait_for(coro, timeout):
            captured["timeout"] = timeout
            coro.close()
            raise TimeoutError

        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=_FakeLLMResp("x"))
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)

        with patch.object(vt.asyncio, "wait_for", side_effect=_fake_wait_for):
            out = asyncio.run(
                vt._expand_cte_for_dml(
                    "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t",
                    timeout_sec=9999,
                    runtime=runtime,
                )
            )

        assert captured["timeout"] == 300
        # 超时 → 回退原始 SQL（仍带 CTE）
        assert out == "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t"

    def test_timeout_returns_original_when_llm_hangs(self):
        """LLM 挂死时按 timeout_sec 放弃，回退原始 SQL 而不是无限等。"""

        def _hang(*_args, **_kwargs):
            import time

            time.sleep(5)

        fake_llm = MagicMock(invoke=MagicMock(side_effect=_hang))
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        original = "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t"

        out = asyncio.run(
            vt._expand_cte_for_dml(original, timeout_sec=1, runtime=runtime)
        )
        assert out == original


# ===========================================================================
# pt_d 范围过滤 → 单日收敛（仅质量校验链路）
# ===========================================================================


class TestCollapsePtDRange:
    """范围分区过滤收敛 —— DataOps 不替换占位符，范围写法会扫不到数据。"""

    def test_placeholder_range_uses_partition_date(self):
        sql = "SELECT a FROM t WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'"
        out = vt._collapse_pt_d_range(sql, "20261021")
        assert "pt_d = '20261021'" in out
        assert "$date" not in out
        assert "${start_time" not in out
        assert "<=" not in out
        assert ">=" not in out

    def test_literal_upper_bound_wins_over_partition_date(self):
        sql = "SELECT a FROM t WHERE pt_d <= '20260901' AND pt_d >= '20260801'"
        out = vt._collapse_pt_d_range(sql, "20261021")
        assert "pt_d = '20260901'" in out

    def test_exclusive_upper_bound_shifts_one_day_back(self):
        """``pt_d < '20260901'`` 不含上界 → 取前一天，仍落在原范围内。"""
        sql = "SELECT a FROM t WHERE pt_d < '20260901'"
        out = vt._collapse_pt_d_range(sql, "20261021")
        assert "pt_d = '20260831'" in out

    def test_single_day_equality_untouched(self):
        """已经是 ``=`` 的单日条件不动（那是目标表自己的分区声明）。"""
        sql = "INSERT OVERWRITE TABLE t PARTITION(pt_d = '20261021') SELECT a FROM s WHERE pt_d = '20261020'"
        out = vt._collapse_pt_d_range(sql, "20261021")
        assert out == sql

    def test_no_partition_condition_returns_unchanged(self):
        sql = "SELECT a FROM t WHERE x = '1'"
        assert vt._collapse_pt_d_range(sql, "20261021") == sql

    def test_dedups_repeated_collapsed_condition(self):
        """``<= X AND >= Y`` 收敛后不留两条重复条件。"""
        sql = "SELECT a FROM t WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'"
        out = vt._collapse_pt_d_range(sql, "20261021")
        assert out.count("pt_d = '20261021'") == 1

    def test_keeps_and_chain_intact(self):
        """就地改写，不删条件 → AND 链不能断。"""
        sql = (
            "SELECT a FROM t WHERE adv_oper_type_cd = '11' "
            "AND pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}' "
            "AND NOT bicoredata.IsEmpty(app_id)"
        )
        out = vt._collapse_pt_d_range(sql, "20261021")
        assert "adv_oper_type_cd = '11'" in out
        assert "NOT bicoredata.IsEmpty(app_id)" in out
        assert "AND" in out
        assert "AND AND" not in out
        assert "AND OR" not in out

    def test_idempotent(self):
        sql = "SELECT a FROM t WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'"
        once = vt._collapse_pt_d_range(sql, "20261021")
        assert vt._collapse_pt_d_range(once, "20261021") == once

    def test_keeps_insert_target_partition_clause(self):
        """INSERT 目标表自己的 ``PARTITION(pt_d = ...)`` 不能被动。"""
        sql = (
            "INSERT OVERWRITE TABLE biads.x PARTITION(pt_d = '$date') "
            "SELECT a FROM s WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'"
        )
        out = vt._collapse_pt_d_range(sql, "20261021")
        # 目标表分区保持原样（这里被 _replace_target_table 处理成字面日期，
        # 本函数只在 ``=`` 形态上不动它）
        assert "PARTITION(pt_d = '$date')" in out
        assert "pt_d <= '$date'" not in out


# ===========================================================================
# Text analysis helpers
# ===========================================================================


class TestParseAnalysisFromText:
    def test_detects_known_error_types(self):
        for et in [
            "AnalysisException",
            "ParseException",
            "TableNotFoundException",
            "ColumnNotFoundException",
            "SemanticException",
        ]:
            out = vt._parse_analysis_from_text(f"ERROR: {et}: foo bar")
            assert out["error_type"] == et, f"failed for {et}"

    def test_unknown_error_type_default(self):
        out = vt._parse_analysis_from_text("Some weird runtime error")
        assert out["error_type"] == "unknown"

    def test_extracts_suggestions_from_bullets(self):
        text = (
            "ERROR: TableNotFoundException: missing_t\n"
            "- check the table name\n"
            "- verify it's in the right database\n"
            "* make sure the cluster is correct\n"
        )
        out = vt._parse_analysis_from_text(text)
        assert len(out["suggestions"]) == 3
        assert "check the table name" in out["suggestions"]

    def test_extracts_suggestions_from_numbered_list(self):
        text = "1. fix the join\n2. reorder the select\n"
        out = vt._parse_analysis_from_text(text)
        assert len(out["suggestions"]) == 2

    def test_caps_suggestions_to_ten(self):
        text = "\n".join(f"- suggestion {i}" for i in range(20))
        out = vt._parse_analysis_from_text(text)
        assert len(out["suggestions"]) == 10

    def test_first_line_is_error_message(self):
        out = vt._parse_analysis_from_text("Some error\nsecond line\nthird line")
        assert out["error_message"] == "Some error"

    def test_caps_error_message_to_500_chars(self):
        out = vt._parse_analysis_from_text("x" * 2000)
        assert len(out["error_message"]) == 500


class TestMakeAnalysisSummary:
    def test_includes_error_type(self):
        s = vt._make_analysis_summary(
            {"error_type": "AnalysisException", "error_message": "x", "location": {}, "suggestions": []}
        )
        assert "AnalysisException" in s

    def test_skips_unknown_error_type(self):
        s = vt._make_analysis_summary(
            {"error_type": "unknown", "error_message": "x", "location": {}, "suggestions": []}
        )
        assert "unknown" not in s

    def test_includes_location_tables(self):
        s = vt._make_analysis_summary(
            {"error_type": "x", "error_message": "x", "location": {"tables": ["t1", "t2"]}, "suggestions": []}
        )
        assert "t1" in s
        assert "t2" in s

    def test_includes_location_columns(self):
        s = vt._make_analysis_summary(
            {"error_type": "x", "error_message": "x", "location": {"columns": ["c1"]}, "suggestions": []}
        )
        assert "c1" in s

    def test_includes_first_suggestion_only(self):
        s = vt._make_analysis_summary(
            {"error_type": "x", "error_message": "x", "location": {}, "suggestions": ["fix A", "fix B"]}
        )
        assert "fix A" in s
        assert "fix B" not in s

    def test_falls_back_to_raw_error(self):
        s = vt._make_analysis_summary(
            {"error_type": "unknown", "error_message": "", "location": {}, "suggestions": []},
            raw_error="Cannot find column foo",
        )
        assert "Cannot find column foo" in s

    def test_falls_back_to_unknown_when_no_info(self):
        s = vt._make_analysis_summary({"error_type": "unknown", "error_message": "", "location": {}, "suggestions": []})
        assert s == "未知错误"


# ===========================================================================
# dataops_fetch_obs_log — three schema variants
# ===========================================================================


_OBS_LOG_FILE_INFO_V3 = {
    "resultPath": "92e7dc0eb88443fc8b00757253c79fa6#schedule.log",
    "headers": {
        "hwTraceId": "a20c6ed4-2dee-48ee-9c59-8019d3408d09",
        "method": "GET",
        "url": "https://obs.cn-north-4.myhuaweicloud.com/x/schedule.log",
        "headers": {"Authorization": "AWS4-HMAC-SHA256 ..."},
        "forms": None,
        "partObjectId": None,
    },
}


def _fake_httpx_client(body: str, status: int = 200):
    """Build a fake httpx.Client whose ``get`` returns a response with the given body."""
    client = MagicMock()
    resp = MagicMock(status_code=status, text=body)
    client.get.return_value = resp
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class TestFetchObsLog:
    """Three schema variants V1 (flat) / V2 (nested) / V3 (doubly-nested)."""

    def test_v1_flat_url(self):
        # log_file_info = {"url": ..., "headers": {...}}
        client = _fake_httpx_client("log body")
        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={
                        "url": "https://obs/a.log",
                        "headers": {"Authorization": "Bearer x"},
                    },
                    _tool_context=MagicMock(),
                )
            )
        assert result["status"] == "success"
        assert result["log_content"] == "log body"
        client.get.assert_called_once()
        # The forward_headers should NOT contain the synthetic "url"/"method" keys
        called_headers = client.get.call_args.kwargs.get("headers") or {}
        assert "url" not in called_headers
        assert "method" not in called_headers
        assert called_headers.get("Authorization") == "Bearer x"

    def test_v2_nested_url_in_headers(self):
        # log_file_info = {"resultPath": ..., "headers": {"url": ..., "headers": {...}, "method": ...}}
        client = _fake_httpx_client("nested log")
        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info=_OBS_LOG_FILE_INFO_V3,
                    _tool_context=MagicMock(),
                )
            )
        assert result["status"] == "success"
        # URL resolved from nested headers.url
        assert client.get.call_args.args[0] == _OBS_LOG_FILE_INFO_V3["headers"]["url"]
        # Synthetic keys not forwarded
        called_headers = client.get.call_args.kwargs.get("headers") or {}
        assert "url" not in called_headers
        assert "method" not in called_headers
        assert "hwTraceId" not in called_headers
        # Real Authorization DID make it through
        assert called_headers.get("Authorization") == "AWS4-HMAC-SHA256 ..."

    def test_missing_url_returns_error(self):
        client = _fake_httpx_client("ignored")
        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={"headers": {"hwTraceId": "x"}},
                    _tool_context=MagicMock(),
                )
            )
        assert result["status"] == "error"
        assert "missing 'url'" in result["error"]
        client.get.assert_not_called()

    def test_missing_headers_returns_error(self):
        client = _fake_httpx_client("ignored")
        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={"url": "https://obs/x"},
                    _tool_context=MagicMock(),
                )
            )
        assert result["status"] == "error"
        assert "missing 'headers'" in result["error"]

    def test_http_4xx_returns_error(self):
        client = _fake_httpx_client("Forbidden", status=403)
        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={
                        "url": "https://obs/x",
                        "headers": {"Authorization": "x"},
                    },
                    _tool_context=MagicMock(),
                )
            )
        assert result["status"] == "error"
        assert "HTTP 403" in result["error"]

    def test_network_error_returns_error(self):
        import httpx as _httpx

        client = MagicMock()
        client.get.side_effect = _httpx.HTTPError("connection refused")
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)

        # The function does ``import httpx`` inside its body, so a real
        # ``HTTPError`` exception class must be exposed by the fake module.
        fake_httpx = MagicMock()
        fake_httpx.Client = MagicMock(return_value=client)
        fake_httpx.HTTPError = _httpx.HTTPError

        with patch.dict("sys.modules", {"httpx": fake_httpx}):
            result = asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={
                        "url": "https://obs/x",
                        "headers": {"Authorization": "x"},
                    },
                    _tool_context=MagicMock(),
                )
            )
        assert result["status"] == "error"
        assert "Failed to fetch" in result["error"]

    def test_mask_authorization_in_internal_log(self):
        """The function builds a safe_headers dict for internal logging that
        masks authorization. We can't intercept the logger directly, but we
        can verify the forward_headers DOES NOT mask (the function separately
        sends real headers to OBS)."""
        client = _fake_httpx_client("log")
        with patch.dict("sys.modules", {"httpx": MagicMock(Client=MagicMock(return_value=client))}):
            asyncio.run(
                vt.dataops_fetch_obs_log(
                    log_file_info={
                        "url": "https://obs/x",
                        "headers": {"Authorization": "Bearer real-value"},
                    },
                    _tool_context=MagicMock(),
                )
            )
        # Authorization value forwarded as-is (OBS needs the real value)
        forwarded = client.get.call_args.kwargs["headers"]
        assert forwarded["Authorization"] == "Bearer real-value"


# ===========================================================================
# _analyze_log_directly — LLM resolution order and content containment
# ===========================================================================


_SAMPLE_LOG = (
    "ERROR: AnalysisException: cannot resolve 'foo' given input columns: "
    "[id, name] in table 'db.tbl1' line 1 pos 7\n"
    "Suggestion: check the column name or use a qualified name\n"
)


class TestAnalyzeLogDirectly:
    def test_fetch_error_when_obs_returns_error(self):
        with patch.object(
            vt,
            "dataops_fetch_obs_log",
            return_value={"status": "error", "error": "OBS 403"},
        ):
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-1",
                    raw_error="boom",
                )
            )
        assert result["status"] == "fetch_error"
        assert "OBS 403" in result["error"]
        # The raw log must NEVER leak into the return — only an error string.
        assert "log_content" not in result

    def test_llm_returns_json_normalized(self, collected_stream):
        llm = object()
        collected_stream.return_value = (
            '{"error_type":"AnalysisException",'
            '"error_message":"col foo not found",'
            '"location":{"tables":["tbl1"],"columns":["foo"]},'
            '"suggestions":["rename to bar","use qualified name"]}'
        )
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(
                vt,
                "llm_manager",
            ) as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-2",
                    raw_error="col foo not found",
                )
            )
        assert result["status"] == "ok"
        analysis = result["analysis"]
        assert analysis["error_type"] == "AnalysisException"
        assert "foo" in analysis["error_message"]
        assert analysis["location"]["tables"] == ["tbl1"]
        assert len(analysis["suggestions"]) == 2
        assert all(len(s) <= 120 for s in analysis["suggestions"])
        collected_stream.assert_awaited_once_with(llm, ANY)

    def test_strips_markdown_fences_from_llm_output(self, collected_stream):
        llm = object()
        collected_stream.return_value = (
            '```json\n{"error_type":"ParseException","error_message":"bad sql",'
            '"location":{},"suggestions":["fix the syntax"]}\n```'
        )
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-3",
                    raw_error="parse fail",
                )
            )
        assert result["status"] == "ok"
        assert result["analysis"]["error_type"] == "ParseException"
        collected_stream.assert_awaited_once_with(llm, ANY)

    def test_invalid_json_falls_back_to_text_parse(self, collected_stream):
        # LLM returns garbage → _parse_analysis_from_text still produces a
        # structured dict (error_type=unknown). Better than failing entirely.
        llm = object()
        collected_stream.return_value = "not json and no markers"
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": "no markers here"},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-4",
                    raw_error="unknown",
                )
            )
        assert result["status"] == "ok"
        assert result["analysis"]["error_type"] == "unknown"
        collected_stream.assert_awaited_once_with(llm, ANY)

    def test_llm_exception_returns_llm_error(self, collected_stream):
        llm = object()
        collected_stream.side_effect = RuntimeError("LLM down")
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-5",
                    raw_error="boom",
                )
            )
        assert result["status"] == "llm_error"
        assert "LLM down" in result["error"]
        collected_stream.assert_awaited_once_with(llm, ANY)

    def test_no_llm_available_falls_back_to_regex(self):
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.side_effect = RuntimeError("no LLM")
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-6",
                    raw_error="missing foo",
                )
            )
        assert result["status"] == "ok"
        # _parse_analysis_from_text detects "AnalysisException" from the log
        assert result["analysis"]["error_type"] == "AnalysisException"

    def test_prefers_runtime_llm_over_llm_manager(self, collected_stream):
        """The main agent's LLM (runtime.llm('planner')) must be reused, not re-instantiated."""
        runtime_llm = object()
        collected_stream.return_value = (
            '{"error_type":"AnalysisException","error_message":"missing foo",'
            '"location":{"tables":["t"],"columns":["foo"]},"suggestions":["add foo"]}'
        )
        manager_llm = object()
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=runtime_llm)
        ctx = MagicMock()
        ctx.runtime = runtime

        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = manager_llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-runtime",
                    raw_error="missing foo",
                    _tool_context=ctx,
                )
            )
        runtime.llm.assert_called_once_with("planner")
        mock_llm_manager.get_default_llm.assert_not_called()
        assert result["status"] == "ok"
        collected_stream.assert_awaited_once_with(runtime_llm, ANY)

    def test_runtime_llm_failure_falls_back_to_manager(self, collected_stream):
        fallback_llm = object()
        collected_stream.return_value = (
            '{"error_type":"ParseException","error_message":"bad sql",'
            '"location":{},"suggestions":["fix syntax"]}'
        )
        runtime = MagicMock()
        runtime.llm = MagicMock(side_effect=RuntimeError("runtime not ready"))
        ctx = MagicMock()
        ctx.runtime = runtime

        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = fallback_llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-fallback",
                    raw_error="bad sql",
                    _tool_context=ctx,
                )
            )
        mock_llm_manager.get_default_llm.assert_called_once()
        assert result["status"] == "ok"
        assert result["analysis"]["error_type"] == "ParseException"
        collected_stream.assert_awaited_once_with(fallback_llm, ANY)

    def test_raw_log_never_leaked_to_caller(self, collected_stream):
        llm = object()
        collected_stream.return_value = (
            '{"error_type":"AnalysisException","error_message":"x","location":{},"suggestions":[]}'
        )
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-7",
                    raw_error="x",
                )
            )
        # The raw log content (or any substantial excerpt) must not be in the return.
        for forbidden in ("log_content", _SAMPLE_LOG[:50]):
            assert forbidden not in result, f"raw log leaked: {forbidden!r}"
        collected_stream.assert_awaited_once_with(llm, ANY)

    def test_log_truncated_at_32kb(self, collected_stream):
        """Huge logs are capped at 32KB before being sent to the LLM."""
        huge_log = "X" * 50_000
        captured_prompt = {}

        def _capture_stream(_model, messages):
            """Capture the user prompt passed to the streaming text collector."""
            captured_prompt["content"] = messages[1].get("content", "")
            return '{"error_type":"AnalysisException","error_message":"x","location":{},"suggestions":[]}'

        llm = object()
        collected_stream.side_effect = _capture_stream
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": huge_log},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = llm
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-huge",
                    raw_error="x",
                )
            )
        # The user prompt is truncated to ~32KB of log content.
        # raw_error stays at the top so the log portion is bounded.
        assert 32_000 < len(captured_prompt.get("content", "")) < 35_000
        collected_stream.assert_awaited_once_with(llm, ANY)


# ===========================================================================
# dataops_validate_sql — main entry point
# ===========================================================================


class TestDataopsValidateSql:
    """End-to-end lifecycle: submit → poll → collect."""

    def test_empty_sql_returns_skipped(self):
        runtime = _make_runtime()
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("   ", _tool_context=ctx))
        assert result == {"passed": True, "skipped": True, "reason": "empty sql"}

    def test_no_resource_returns_skipped(self):
        coordinator = _wired_coordinator(resource=None)
        runtime = _make_runtime(resource=None, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result == {"passed": True, "skipped": True, "reason": "no_dataops_resource"}

    def test_disabled_resource_returns_skipped(self):
        resource = _make_resource(enabled=False)
        coordinator = _wired_coordinator(resource=resource)
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result["skipped"] is True
        assert result["reason"] == "dataops_disabled"
        # Coordinator must not have been called.
        coordinator.submit_job.assert_not_called()

    def test_empty_url_returns_skipped(self):
        resource = _make_resource(url="")
        coordinator = _wired_coordinator(resource=resource)
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result["skipped"] is True
        assert result["reason"] == "dataops_url_empty"
        coordinator.submit_job.assert_not_called()

    def test_submit_error_returns_error(self):
        resource = _make_resource()
        submit = {"status": "ERROR", "message": "queue full"}
        coordinator = _wired_coordinator(resource=resource, submit=submit)
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result["passed"] is False
        assert "queue full" in result["error"]

    def test_submit_no_job_id_returns_error(self):
        resource = _make_resource()
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "OK"},  # no job_id
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result["passed"] is False
        assert "no job_id" in result["error"]

    def test_successful_path(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed"},
            collect={"status": "completed"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result == {"passed": True, "job_id": "j-1"}

    def test_failed_with_log_file_info(self):
        info = {"resultPath": "x", "headers": {"url": "https://obs/x"}}
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
                "log_file_info": info,
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT * FROM no_such", _tool_context=ctx))
        assert result["passed"] is False
        assert result["error"] == "Table not found"
        assert result["log_file_info"] == info

    def test_failed_with_camelcase_logfileinfo_fallback(self):
        """Legacy MCP tooling may return ``logFileInfo`` (camelCase)."""
        info = {"resultPath": "x", "headers": {"url": "https://obs/x"}}
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
                "logFileInfo": info,
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT * FROM no_such", _tool_context=ctx))
        assert result["log_file_info"] == info

    def test_failed_without_log_file_info_omits_field(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT * FROM no_such", _tool_context=ctx))
        assert result["passed"] is False
        assert "log_file_info" not in result

    def test_timeout_cancels_and_returns_timed_out(self):
        resource = _make_resource(timeout_s=1, poll_interval_ms=50)

        # poll always returns "running" — the loop never reaches a terminal
        # status before the deadline, so we hit the cancel branch.
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "running"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result["passed"] is False
        assert result.get("timed_out") is True
        assert "timed out" in result["error"]
        # Coordinator.cancel was called.
        coordinator.cancel.assert_called_once_with(job_id="j-1")

    def test_coordinator_exception_returns_error(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = MagicMock()
        coordinator.catalog = _catalog(resource)
        coordinator.submit_job.side_effect = RuntimeError("boom")
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql("SELECT 1", _tool_context=ctx))
        assert result["passed"] is False
        assert "MCP call failed" in result["error"]

    def test_user_account_priority_from_resource_metadata(self):
        """resource.metadata.exec_user overrides env and runtime."""
        resource = _make_resource(exec_user="from_meta")
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed"},
            collect={"status": "completed"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator, user_id="from_runtime")
        ctx = _make_context(runtime)
        with patch.object(vt, "_replace_target_table") as mock_replace:
            mock_replace.return_value = "SELECT 1"
            asyncio.run(
                vt.dataops_validate_sql(
                    "INSERT OVERWRITE TABLE biads.x SELECT 1",
                    _tool_context=ctx,
                )
            )
        # The user_account passed to _replace_target_table is "from_meta"
        args, kwargs = mock_replace.call_args
        assert args[0] == "INSERT OVERWRITE TABLE biads.x SELECT 1"
        assert args[1] == "from_meta"

    def test_partition_range_collapse_is_off_by_default(self):
        """语法校验链路保持原始范围语义（它只验证「SQL 能不能跑通」）。"""
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed"},
            collect={"status": "completed"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        sql = "SELECT a FROM t WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'"

        asyncio.run(vt.dataops_validate_sql(sql, _tool_context=ctx))

        submitted = coordinator.submit_job.call_args.kwargs["command"]
        assert "pt_d <= '$date'" in submitted
        assert "${start_time" in submitted

    def test_partition_range_collapse_applied_when_enabled(self):
        """质量校验链路开启后，提交给 DataOps 的 SQL 里范围条件已被收敛。"""
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed"},
            collect={"status": "completed"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        sql = "SELECT a FROM t WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'"

        asyncio.run(
            vt.dataops_validate_sql(
                sql,
                _tool_context=ctx,
                _partition_date="20261021",
                _collapse_partition_range=True,
            )
        )

        submitted = coordinator.submit_job.call_args.kwargs["command"]
        assert "pt_d = '20261021'" in submitted
        assert "pt_d <=" not in submitted
        assert "${start_time" not in submitted


# ===========================================================================
# dataops_validate_sql_with_log_analysis — wrapper + field ordering
# ===========================================================================


class TestDataopsValidateSqlWithLogAnalysis:
    """Wrapper composes validate_sql + in-process analysis."""

    def test_passed_returns_inner_as_is(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed"},
            collect={"status": "completed"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql_with_log_analysis("SELECT 1", _tool_context=ctx))
        assert result["passed"] is True
        assert result["job_id"] == "j-1"
        # No analysis fields on success.
        assert "log_analysis" not in result
        assert "log_file_info" not in result

    def test_failed_no_log_file_info_returns_inner_as_is(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = asyncio.run(vt.dataops_validate_sql_with_log_analysis("SELECT 1", _tool_context=ctx))
        assert result["passed"] is False
        assert result["error"] == "Table not found"
        # No log_file_info → no analysis was attempted.
        assert "log_analysis" not in result
        assert "log_file_info" not in result

    def test_failed_with_log_file_info_adds_analysis(self, collected_stream):
        info = {"url": "https://obs/x", "headers": {"Authorization": "x"}}
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
                "log_file_info": info,
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)

        llm = object()
        collected_stream.return_value = (
            '{"error_type":"TableNotFoundException",'
            '"error_message":"missing_t not found",'
            '"location":{"tables":["missing_t"]},'
            '"suggestions":["check the table name"]}'
        )
        # Make the runtime's LLM resolver return our stand-in so the
        # in-process analyzer reuses the main agent's LLM (its primary path).
        runtime.llm = MagicMock(return_value=llm)

        with patch.object(
            vt,
            "dataops_fetch_obs_log",
            return_value={"status": "success", "log_content": _SAMPLE_LOG},
        ):
            result = asyncio.run(
                vt.dataops_validate_sql_with_log_analysis(
                    "SELECT * FROM missing_t",
                    _tool_context=ctx,
                )
            )
        assert result["passed"] is False
        assert result["log_analysis"]["error_type"] == "TableNotFoundException"
        assert "log_analysis_summary" in result
        assert "log_file_info" in result

    def test_log_analysis_error_recorded_on_obs_failure(self):
        info = {"url": "https://obs/x", "headers": {"Authorization": "x"}}
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
                "log_file_info": info,
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)

        with patch.object(
            vt,
            "dataops_fetch_obs_log",
            return_value={"status": "error", "error": "OBS 403"},
        ):
            result = asyncio.run(
                vt.dataops_validate_sql_with_log_analysis(
                    "SELECT * FROM missing_t",
                    _tool_context=ctx,
                )
            )
        assert result["passed"] is False
        assert "log_analysis_error" in result
        assert "OBS 403" in result["log_analysis_error"]
        # log_file_info is still present for the caller.
        assert "log_file_info" in result

    def test_failed_response_orders_fields_with_analysis_before_log_file_info(self, collected_stream):
        """Field ordering matters: actionable analysis must come before the
        bulky log_file_info so the result is readable when truncated."""
        info = {"url": "https://obs/x", "headers": {"Authorization": "x"}}
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "failed"},
            collect={
                "job_id": "j-1",
                "status": "failed",
                "error": "Table not found",
                "log_file_info": info,
            },
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)

        llm = object()
        collected_stream.return_value = (
            '{"error_type":"TableNotFoundException",'
            '"error_message":"missing_t not found",'
            '"location":{"tables":["missing_t"]},'
            '"suggestions":["check the table name"]}'
        )
        runtime.llm = MagicMock(return_value=llm)
        with patch.object(
            vt,
            "dataops_fetch_obs_log",
            return_value={"status": "success", "log_content": _SAMPLE_LOG},
        ):
            result = asyncio.run(
                vt.dataops_validate_sql_with_log_analysis(
                    "SELECT * FROM missing_t",
                    _tool_context=ctx,
                )
            )
        keys = list(result.keys())
        # log_file_info must come after log_analysis / log_analysis_summary
        idx_lfi = keys.index("log_file_info")
        assert "log_analysis" in keys[:idx_lfi]
        assert "log_analysis_summary" in keys[:idx_lfi]

    # ----- prod path: inline errorLog (no OBS) --------------------------

    def test_prod_failed_with_inline_error_log_runs_analysis(self, monkeypatch, collected_stream):
        """Prod MCP returns errorLog inline; wrapper must analyze it in-process
        and attach structured log_analysis, even when log_file_info is absent.
        Regression: previously the wrapper only entered the analysis branch
        when log_file_info was present, so prod failures silently skipped the
        analyzer and returned a raw errorLog that the planner misread as PASSED.
        """
        monkeypatch.setenv("DATAOPS_ENV", "prod")
        runtime = _make_runtime(resource=None, coordinator=None)
        ctx = _make_context(runtime)

        # Stub prod flow: submit OK, poll failed, collect yields inline errorLog
        async def _fake_prod(sql=None, *, _tool_context, sql_file_path=None, partition_date=None, collapse_partition_range=False):
            return {
                "passed": False,
                "error": "AnalysisException: cannot resolve 'foo'",
                "job_id": "job-prod-1",
                "errorLog": _SAMPLE_LOG,
                "log_source": "inline",
            }

        llm = object()
        collected_stream.return_value = (
            '{"error_type":"AnalysisException",'
            '"error_message":"foo not found",'
            '"location":{"columns":["foo"]},'
            '"suggestions":["use an existing column"]}'
        )
        runtime.llm = MagicMock(return_value=llm)

        with (
            patch.object(vt, "_dataops_validate_sql_prod", side_effect=_fake_prod),
            patch.object(vt, "dataops_fetch_obs_log") as fetch_obs,
        ):
            result = asyncio.run(
                vt.dataops_validate_sql_with_log_analysis(
                    "SELECT foo FROM db.tbl1",
                    _tool_context=ctx,
                )
            )

        # Wrapper must not have fallen into the OBS path
        fetch_obs.assert_not_called()
        assert result["passed"] is False
        assert result["job_id"] == "job-prod-1"
        # Inline errorLog preserved (for downstream visibility)
        assert result["errorLog"] == _SAMPLE_LOG
        # And — critically — structured analysis is now attached
        assert result["log_analysis"]["error_type"] == "AnalysisException"
        assert result["log_analysis_summary"]
        assert "suggestions" in result["log_analysis"]

    def test_prod_failed_no_log_source_returns_inner_as_is(self, monkeypatch):
        """If prod failed but returned neither log_file_info nor errorLog,
        wrapper should still return as-is (no analyzer input to work with).
        """
        monkeypatch.setenv("DATAOPS_ENV", "prod")
        runtime = _make_runtime(resource=None, coordinator=None)
        ctx = _make_context(runtime)

        async def _fake_prod(sql=None, *, _tool_context, sql_file_path=None, partition_date=None, collapse_partition_range=False):
            return {
                "passed": False,
                "error": "submission failed",
                "job_id": None,
            }

        with patch.object(vt, "_dataops_validate_sql_prod", side_effect=_fake_prod):
            result = asyncio.run(
                vt.dataops_validate_sql_with_log_analysis(
                    "SELECT 1",
                    _tool_context=ctx,
                )
            )

        assert result["passed"] is False
        assert result["error"] == "submission failed"
        assert "log_analysis" not in result


# ===========================================================================
# _is_insert_sql / _extract_insert_target_table / _build_count_sql
# ===========================================================================


class TestIsInsertSql:
    def test_insert_overwrite(self):
        assert vt._is_insert_sql("INSERT OVERWRITE TABLE biads.x SELECT 1") is True

    def test_insert_into(self):
        assert vt._is_insert_sql("INSERT INTO TABLE biads.x SELECT 1") is True

    def test_insert_external(self):
        assert vt._is_insert_sql("INSERT OVERWRITE EXTERNAL TABLE biads.x SELECT 1") is True

    def test_select_not_insert(self):
        assert vt._is_insert_sql("SELECT 1 FROM biads.x") is False

    def test_create_not_insert(self):
        assert vt._is_insert_sql("CREATE TABLE biads.x (id INT)") is False

    def test_case_insensitive(self):
        assert vt._is_insert_sql("insert overwrite table x select 1") is True

    def test_empty(self):
        assert vt._is_insert_sql("") is False


class TestExtractInsertTargetTable:
    def test_insert_overwrite_with_db(self):
        t = vt._extract_insert_target_table(
            "INSERT OVERWRITE TABLE biads.ads_xxx SELECT 1",
            user_account="alice",
            table_suffix="",
        )
        assert t is not None
        assert "adhoctemp" in t
        assert "ads_xxx" in t

    def test_insert_into(self):
        t = vt._extract_insert_target_table(
            "INSERT INTO TABLE biads.ads_yyy SELECT * FROM src",
            user_account="bob",
            table_suffix="",
        )
        assert t is not None
        assert "adhoctemp" in t
        assert "ads_yyy" in t

    def test_insert_external(self):
        t = vt._extract_insert_target_table(
            "INSERT OVERWRITE EXTERNAL TABLE biads.ads_eee SELECT * FROM src",
            user_account="alice",
            table_suffix="",
        )
        assert t is not None
        assert "adhoctemp" in t

    def test_select_returns_none(self):
        t = vt._extract_insert_target_table(
            "SELECT 1 FROM biads.x",
            user_account="alice",
            table_suffix="",
        )
        assert t is None

    def test_with_table_suffix(self):
        t = vt._extract_insert_target_table(
            "INSERT OVERWRITE TABLE biads.ads_zzz SELECT 1",
            user_account="alice",
            table_suffix="v1",
        )
        assert t is not None
        assert "v1" in t


# ===========================================================================
# Retry / best-effort delivery (dataops_validate_sql_with_log_analysis)
# ===========================================================================


class TestExtractOriginalTargetTable:
    """_extract_original_target_table parses db.table for the report filename."""

    def test_insert_with_db_prefix(self):
        sql = "INSERT OVERWRITE TABLE biads.ads_xxx_dm SELECT 1"
        assert vt._extract_original_target_table(sql) == "biads_ads_xxx_dm"

    def test_insert_with_partition(self):
        sql = "INSERT OVERWRITE TABLE biads.ads_x PARTITION(pt_d='$date') SELECT 1"
        assert vt._extract_original_target_table(sql) == "biads_ads_x"

    def test_create_table(self):
        sql = "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_foo (id INT)"
        assert vt._extract_original_target_table(sql) == "biads_ads_foo"

    def test_no_table_returns_none(self):
        assert vt._extract_original_target_table("SELECT 1") is None

    def test_empty_returns_none(self):
        assert vt._extract_original_target_table("") is None


class TestSummarizeAttempt:
    """_summarize_attempt builds compact per-attempt records."""

    def test_compact_summary(self):
        result = {
            "passed": False,
            "job_id": "j-1",
            "error": "boom",
            "log_analysis": {
                "error_type": "ParseException",
                "error_message": "syntax error at 'INVALID'",
                "suggestions": ["replace 'INVALID' with 'SELECT'"],
            },
            "log_analysis_summary": "parse: INVALID → SELECT",
            "log_file_info": {"url": "http://x", "headers": {"k": "v"}},  # should be dropped
        }
        summary = vt._summarize_attempt(1, result, start_time=time.time() - 1.5)
        assert summary["attempt"] == 1
        assert summary["passed"] is False
        assert summary["job_id"] == "j-1"
        assert summary["error_type"] == "ParseException"
        assert summary["suggestions"] == ["replace 'INVALID' with 'SELECT'"]
        assert "log_file_info" not in summary  # bulky fields stripped
        assert summary["elapsed_sec"] >= 1.0


class TestDumpValidationArtifacts:
    """_dump_validation_artifacts writes report + warning when workspace is set."""

    def test_writes_report_and_warning_when_workspace_present(self, tmp_path):
        sql = "INSERT OVERWRITE TABLE biads.ads_x_dm SELECT 1"
        attempts = [
            {
                "attempt": 1,
                "passed": False,
                "job_id": "j-1",
                "error_type": "ParseException",
                "error_message": "bad sql",
                "suggestions": ["fix it"],
                "log_analysis_summary": "summary-1",
            },
            {
                "attempt": 2,
                "passed": False,
                "job_id": "j-2",
                "error_type": "TableNotFoundException",
                "error_message": "no tbl",
                "suggestions": ["create table first"],
                "log_analysis_summary": "summary-2",
            },
            {
                "attempt": 3,
                "passed": False,
                "job_id": "j-3",
                "error_type": "AnalysisException",
                "error_message": "still bad",
                "suggestions": ["use left join"],
                "log_analysis_summary": "summary-3",
            },
        ]
        ctx = ToolExecutionContext(runtime=None)  # type: ignore[arg-type]
        # Inject workspace_dir via a tiny runtime shim
        shim = SimpleNamespace(workspace_dir=tmp_path)
        ctx = ToolExecutionContext(runtime=shim)  # type: ignore[arg-type]
        out = vt._dump_validation_artifacts(sql=sql, attempts=attempts, _tool_context=ctx)
        assert out["validation_report_path"] is not None
        assert out["delivery_warning_path"] is not None
        # Report contents
        report = json.loads(Path(out["validation_report_path"]).read_text(encoding="utf-8"))
        assert report["validation_status"] == "failed_after_max_retries"
        # max_attempts 跟随 MAX_VALIDATE_ATTEMPTS 常量（v3.1 = 10）
        assert report["max_attempts"] == vt.MAX_VALIDATE_ATTEMPTS
        assert len(report["attempts"]) == 3
        # Warning contents: n 来自 MAX_VALIDATE_ATTEMPTS
        warning = Path(out["delivery_warning_path"]).read_text(encoding="utf-8")
        assert f"重试 {vt.MAX_VALIDATE_ATTEMPTS} 次仍失败" in warning
        assert "attempt 1" in warning
        assert "attempt 3" in warning
        assert "fix it" in warning

    def test_returns_none_paths_when_no_workspace(self, monkeypatch):
        monkeypatch.delenv("DATAOPS_VALIDATION_REPORT_DIR", raising=False)
        ctx = ToolExecutionContext(runtime=None)  # type: ignore[arg-type]
        out = vt._dump_validation_artifacts(
            sql="INSERT OVERWRITE TABLE biads.x SELECT 1",
            attempts=[{"attempt": 1, "passed": False}],
            _tool_context=ctx,
        )
        assert out["validation_report_path"] is None
        assert out["delivery_warning_path"] is None


class TestDataopsValidateSqlWithLogAnalysisRetry:
    """Retry contract: 10-call cap, failed_after_max_retries flag, report dump."""

    @pytest.mark.asyncio
    async def test_attempt_above_max_is_rejected_without_running(self, tmp_path):
        """attempt=11 → returned with failed_after_max_retries, no DataOps call."""
        runtime = MagicMock()
        runtime.workspace_dir = tmp_path
        ctx = ToolExecutionContext(runtime=runtime)  # type: ignore[arg-type]
        result = await vt.dataops_validate_sql_with_log_analysis(
            "SELECT 1",
            attempt=11,
            _tool_context=ctx,
        )
        assert result["passed"] is False
        assert result["failed_after_max_retries"] is True
        assert "exceeded MAX_VALIDATE_ATTEMPTS" in result["error"]

    @pytest.mark.asyncio
    async def test_attempt_10_triggers_dump_with_log_analysis(self, tmp_path, collected_stream):
        """attempt=10 + still failing + has log_file_info → log_analysis runs,
        failed_after_max_retries set, report + warning written."""
        coordinator = MagicMock()
        coordinator.catalog = _catalog(_make_resource())
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "j-3"}
        # poll returns "failed" directly so the wrapper goes through
        # _build_failure_response (which extracts log_file_info) instead of
        # the completed-path (which would trigger post_validate for INSERT).
        coordinator.poll.return_value = {"status": "failed"}
        coordinator.collect.return_value = {
            "status": "failed",
            "job_id": "j-10",
            "error": "syntax error",
            "logFileInfo": {"url": "http://x", "headers": {"k": "v"}},
        }
        runtime = MagicMock()
        runtime.ensure_resource_coordinator.return_value = coordinator
        runtime.workspace_dir = tmp_path

        # Fake LLM that returns a valid analysis JSON
        llm = object()
        collected_stream.return_value = (
            '{"error_type":"ParseException","error_message":"bad",'
            '"location":{"tables":[],"columns":[],"functions":[],"line":null},'
            '"suggestions":["replace INVALID with SELECT"]}'
        )
        runtime.llm = MagicMock(return_value=llm)

        ctx = _make_context(runtime)
        # Mock the OBS fetcher to avoid hitting a real endpoint
        with patch.object(vt, "dataops_fetch_obs_log") as mock_fetch:
            mock_fetch.return_value = {
                "status": "success",
                "log_content": "ParseException: INVALID not recognized",
            }
            # 9 prior failed attempts (attempt=1..9); this is attempt=10
            prior = [
                {
                    "attempt": i,
                    "passed": False,
                    "job_id": f"j-{i}",
                    "error_type": "ParseException",
                    "error_message": f"bad #{i}",
                    "suggestions": [f"hint #{i}"],
                }
                for i in range(1, 10)
            ]
            result = await vt.dataops_validate_sql_with_log_analysis(
                "INSERT OVERWRITE TABLE biads.ads_x_dm INVALID 1",
                attempt=10,
                prior_attempts=prior,
                _tool_context=ctx,
            )

        assert result["passed"] is False
        assert result["failed_after_max_retries"] is True
        assert len(result["attempts"]) == 10
        assert result["attempts"][0]["attempt"] == 1
        assert result["attempts"][-1]["attempt"] == 10
        assert result["validation_report_path"] is not None
        assert result["delivery_warning_path"] is not None
        # Report file exists and is parseable JSON
        report = json.loads(Path(result["validation_report_path"]).read_text(encoding="utf-8"))
        assert report["validation_status"] == "failed_after_max_retries"
        # Warning file contains the header (now mentions 10 retries)
        warning = Path(result["delivery_warning_path"]).read_text(encoding="utf-8")
        assert "重试 10 次仍失败" in warning

    @pytest.mark.asyncio
    async def test_attempt_2_does_not_dump(self, tmp_path, collected_stream):
        """attempt=2 with still failing → failed_after_max_retries NOT set,
        no dump, attempts list has 2 entries."""
        coordinator = MagicMock()
        coordinator.catalog = _catalog(_make_resource())
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "j-x"}
        coordinator.poll.return_value = {"status": "failed"}
        coordinator.collect.return_value = {
            "status": "failed",
            "job_id": "j-x",
            "error": "still bad",
            "logFileInfo": {"url": "http://x", "headers": {"k": "v"}},
        }
        runtime = MagicMock()
        runtime.ensure_resource_coordinator.return_value = coordinator
        runtime.workspace_dir = tmp_path
        llm = object()
        collected_stream.return_value = (
            '{"error_type":"AnalysisException","error_message":"x",'
            '"location":{"tables":[],"columns":[],"functions":[],"line":null},'
            '"suggestions":["s"]}'
        )
        runtime.llm = MagicMock(return_value=llm)

        ctx = _make_context(runtime)
        with patch.object(vt, "dataops_fetch_obs_log") as mock_fetch:
            mock_fetch.return_value = {"status": "success", "log_content": "log"}
            prior = [
                {
                    "attempt": 1,
                    "passed": False,
                    "job_id": "j-1",
                    "error_type": "AnalysisException",
                    "error_message": "x",
                    "suggestions": ["s"],
                },
            ]
            result = await vt.dataops_validate_sql_with_log_analysis(
                "INSERT OVERWRITE TABLE biads.x SELECT 1",
                attempt=2,
                prior_attempts=prior,
                _tool_context=ctx,
            )

        assert result["passed"] is False
        assert result.get("failed_after_max_retries") is not True
        assert result.get("validation_report_path") is None
        assert result.get("delivery_warning_path") is None
        assert len(result["attempts"]) == 2

    @pytest.mark.asyncio
    async def test_invalid_attempt_returns_error(self):
        runtime = MagicMock()
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql_with_log_analysis(
            "SELECT 1",
            attempt=0,
            _tool_context=ctx,
        )
        assert result["passed"] is False
        assert result["failed_after_max_retries"] is True
        assert "attempt must be a positive int" in result["error"]


# ============================================================================
# sql_file_path 入参契约（v3.1）
# ============================================================================
class TestSqlFilePathContract:
    """``sql_file_path`` 必须与 ``sql`` 等价,且优先用文件。"""

    @pytest.mark.asyncio
    async def test_sql_file_path_only_is_loaded(self, tmp_path):
        """只传 sql_file_path,工具应读取并正常返回。"""
        sql_path = tmp_path / "x.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")
        resource = _make_resource()
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed", "job_id": "j-1"},
            collect={"status": "completed", "job_id": "j-1", "data": []},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql_with_log_analysis(
            sql_file_path=sql_path,
            attempt=1,
            _tool_context=ctx,
        )
        # SELECT 1 走 completed 路径
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_sql_file_path_takes_precedence_over_sql(self, tmp_path):
        """同时传 sql 与 sql_file_path,文件优先。"""
        sql_path = tmp_path / "x.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")
        resource = _make_resource()
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed", "job_id": "j-1"},
            collect={"status": "completed", "job_id": "j-1", "data": []},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql_with_log_analysis(
            sql="INVALID SQL THAT WOULD FAIL",  # noqa: S105
            sql_file_path=sql_path,
            attempt=1,
            _tool_context=ctx,
        )
        # 走文件 (SELECT 1 通过)
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_sql_file_path_missing_returns_error(self, tmp_path):
        """sql_file_path 不存在时返回 passed=False,带 error 信息。"""
        runtime = MagicMock()
        runtime.workspace_dir = tmp_path
        ctx = ToolExecutionContext(runtime=runtime)  # type: ignore[arg-type]
        result = await vt.dataops_validate_sql_with_log_analysis(
            sql_file_path=tmp_path / "nope.sql",
            attempt=1,
            _tool_context=ctx,
        )
        assert result["passed"] is False
        assert "sql_file_path" in result["error"]


# ============================================================================
# 需求 4：数据正确性校验失败不阻塞、不重试、只在所有校验通过后执行
# ============================================================================


class TestQualityCheckNotInValidationChain:
    """质量校验已从本链路移除 —— 只在 post-hook 里跑一次。

    动机：静态校验阶段要"快"，agent 才能快速迭代修 SQL；昂贵的采样统计
    放到交付后由 ``dataops_quality_check_post`` post-hook 统一执行。
    """

    @pytest.mark.asyncio
    async def test_quality_check_not_run_on_pass(self, tmp_path):
        """DataOps 通过时不再触发质量校验，响应里也没有 reasonable_check 字段。"""
        resource = _make_resource()
        coordinator = MagicMock()
        coordinator.catalog = _catalog(resource)
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "j-1"}
        coordinator.poll.return_value = {"status": "completed", "job_id": "j-1"}
        coordinator.collect.side_effect = [
            {
                "status": "completed",
                "job_id": "j-1",
                "data": [{"pt_d": "20261010"}],
            },
            {
                "status": "completed",
                "job_id": "j-cleanup",
            },
        ]
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)

        called = {"n": 0}

        async def fake_quality(*_args, **_kwargs):
            called["n"] += 1
            return {"ok": True, "warnings": [], "details": {}}

        with patch(
            "dataagent.actions.tools.local_tool.dataops_quality_check.run_quality_check",
            side_effect=fake_quality,
        ):
            result = await vt.dataops_validate_sql_with_log_analysis(
                "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src",
                attempt=1,
                _tool_context=ctx,
            )

        assert result["passed"] is True
        # 响应里不再有 reasonable_check 字段
        assert "reasonable_check" not in result
        # 质量校验一次都没被调用（它只在 post-hook 里跑）
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_quality_check_not_run_on_failure(self, tmp_path, collected_stream):
        """DataOps 校验失败时同样不触发质量校验。"""
        resource = _make_resource()
        coordinator = MagicMock()
        coordinator.catalog = _catalog(resource)
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "j-fail"}
        coordinator.poll.return_value = {"status": "failed"}
        coordinator.collect.return_value = {
            "status": "failed",
            "job_id": "j-fail",
            "error": "syntax error",
        }
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)

        runtime.llm = MagicMock(return_value=object())
        collected_stream.return_value = (
            '{"error_type":"X","error_message":"x",'
            '"location":{"tables":[],"columns":[],"functions":[],"line":null},'
            '"suggestions":["s"]}'
        )

        called = {"n": 0}

        async def fake_quality(*_args, **_kwargs):
            called["n"] += 1
            return {"ok": True, "warnings": [], "details": {}}

        with (
            patch(
                "dataagent.actions.tools.local_tool.dataops_quality_check.run_quality_check",
                side_effect=fake_quality,
            ),
            patch.object(vt, "dataops_fetch_obs_log") as mock_fetch,
        ):
            mock_fetch.return_value = {"status": "error", "error": "no obs"}
            result = await vt.dataops_validate_sql_with_log_analysis(
                "INSERT OVERWRITE TABLE biads.x INVALID SELECT 1",
                attempt=1,
                _tool_context=ctx,
            )

        assert result["passed"] is False
        assert "reasonable_check" not in result
        assert called["n"] == 0

    def test_wrapper_entry_point_removed(self):
        """``_run_reasonable_check_wrapper`` 已从 validate_tool 移除。"""
        assert not hasattr(vt, "_run_reasonable_check_wrapper")

