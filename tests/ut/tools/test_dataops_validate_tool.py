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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import dataops_validate_tool as vt
from dataagent.resources.catalog.models import Resource

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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

    def test_llm_returns_json_normalized(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"error_type":"AnalysisException",'
                    '"error_message":"col foo not found",'
                    '"location":{"tables":["tbl1"],"columns":["foo"]},'
                    '"suggestions":["rename to bar","use qualified name"]}'
                )
            )
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
            mock_llm_manager.get_default_llm.return_value = fake_llm
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

    def test_strips_markdown_fences_from_llm_output(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '```json\n{"error_type":"ParseException","error_message":"bad sql",'
                    '"location":{},"suggestions":["fix the syntax"]}\n```'
                )
            )
        )
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = fake_llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-3",
                    raw_error="parse fail",
                )
            )
        assert result["status"] == "ok"
        assert result["analysis"]["error_type"] == "ParseException"

    def test_invalid_json_falls_back_to_text_parse(self):
        # LLM returns garbage → _parse_analysis_from_text still produces a
        # structured dict (error_type=unknown). Better than failing entirely.
        fake_llm = MagicMock(invoke=MagicMock(return_value=_FakeLLMResp("not json and no markers")))
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": "no markers here"},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = fake_llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-4",
                    raw_error="unknown",
                )
            )
        assert result["status"] == "ok"
        assert result["analysis"]["error_type"] == "unknown"

    def test_llm_exception_returns_llm_error(self):
        fake_llm = MagicMock(invoke=MagicMock(side_effect=RuntimeError("LLM down")))
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = fake_llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-5",
                    raw_error="boom",
                )
            )
        assert result["status"] == "llm_error"
        assert "LLM down" in result["error"]

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

    def test_prefers_runtime_llm_over_llm_manager(self):
        """The main agent's LLM (runtime.llm('planner')) must be reused, not re-instantiated."""
        runtime_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"error_type":"AnalysisException","error_message":"missing foo",'
                    '"location":{"tables":["t"],"columns":["foo"]},"suggestions":["add foo"]}'
                )
            )
        )
        manager_llm = MagicMock(
            invoke=MagicMock(
                side_effect=AssertionError(
                    "llm_manager.get_default_llm() should NOT be called when runtime.llm succeeds"
                )
            )
        )
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

    def test_runtime_llm_failure_falls_back_to_manager(self):
        fallback_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"error_type":"ParseException","error_message":"bad sql",'
                    '"location":{},"suggestions":["fix syntax"]}'
                )
            )
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

    def test_raw_log_never_leaked_to_caller(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"error_type":"AnalysisException","error_message":"x","location":{},"suggestions":[]}'
                )
            )
        )
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": _SAMPLE_LOG},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = fake_llm
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

    def test_log_truncated_at_32kb(self):
        """Huge logs are capped at 32KB before being sent to the LLM."""
        huge_log = "X" * 50_000
        captured_prompt = {}

        def capture_invoke(messages):
            # Capture the user prompt for inspection.
            captured_prompt["content"] = messages[1]["content"]
            return _FakeLLMResp('{"error_type":"AnalysisException","error_message":"x","location":{},"suggestions":[]}')

        fake_llm = MagicMock(invoke=MagicMock(side_effect=capture_invoke))
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": huge_log},
            ),
            patch.object(vt, "llm_manager") as mock_llm_manager,
        ):
            mock_llm_manager.get_default_llm.return_value = fake_llm
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-huge",
                    raw_error="x",
                )
            )
        # The user prompt is truncated to ~32KB of log content.
        # raw_error stays at the top so the log portion is bounded.
        assert len(captured_prompt["content"]) < 35_000


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

    def test_failed_with_log_file_info_adds_analysis(self):
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

        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"error_type":"TableNotFoundException",'
                    '"error_message":"missing_t not found",'
                    '"location":{"tables":["missing_t"]},'
                    '"suggestions":["check the table name"]}'
                )
            )
        )
        # Make the runtime's LLM resolver return our fake_llm so the
        # in-process analyzer reuses the main agent's LLM (its primary path).
        runtime.llm = MagicMock(return_value=fake_llm)

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

    def test_failed_response_orders_fields_with_analysis_before_log_file_info(self):
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

        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"error_type":"TableNotFoundException",'
                    '"error_message":"missing_t not found",'
                    '"location":{"tables":["missing_t"]},'
                    '"suggestions":["check the table name"]}'
                )
            )
        )
        runtime.llm = MagicMock(return_value=fake_llm)
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


class TestBuildCountSql:
    def test_standard_insert(self):
        # Build from the rewritten form (after _replace_target_table)
        rewritten = (
            "INSERT OVERWRITE TABLE adhoctemp.tmp_alice_20260817_ads_xxx "
            "SELECT * FROM biads.src WHERE pt_d = '20260817'"
        )
        sql = vt._build_count_sql(rewritten)
        assert sql is not None
        assert "SELECT count(*)" in sql
        assert "adhoctemp" in sql
        assert "ads_xxx" in sql
        assert "pt_d" in sql
        assert "20260817" in sql

    def test_insert_without_db(self):
        rewritten = "INSERT OVERWRITE TABLE adhoctemp.tmp_bob_20260817_src SELECT 1"
        sql = vt._build_count_sql(rewritten)
        assert sql is not None
        assert "count(*)" in sql

    def test_select_returns_none(self):
        sql = vt._build_count_sql("SELECT 1 FROM biads.x")
        assert sql is None


class TestParseCountFromCollect:
    def test_standard_data(self):
        result = {"status": "completed", "data": [{"_c0": "42"}]}
        assert vt._parse_count_from_collect(result) == 42

    def test_named_count_column(self):
        result = {"status": "completed", "data": [{"count(*)": "0"}]}
        assert vt._parse_count_from_collect(result) == 0

    def test_float_that_is_integer(self):
        result = {"status": "completed", "data": [{"_c0": "7.0"}]}
        assert vt._parse_count_from_collect(result) == 7

    def test_empty_data_returns_none(self):
        result = {"status": "completed", "data": []}
        assert vt._parse_count_from_collect(result) is None

    def test_data_not_list_returns_none(self):
        result = {"status": "completed", "data": "not a list"}
        assert vt._parse_count_from_collect(result) is None

    def test_no_data_field_returns_none(self):
        result = {"status": "completed"}
        assert vt._parse_count_from_collect(result) is None


# ===========================================================================
# _run_post_validate
# ===========================================================================


class TestRunPostValidate:
    """Mocked coordinator — submit / poll / collect all via MagicMock side_effect."""

    @pytest.mark.asyncio
    async def test_count_success_greater_than_zero(self):
        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "pv-1"}
        # First poll: running → Second poll: completed
        coordinator.poll.side_effect = [
            {"status": "running", "job_id": "pv-1"},
            {"status": "completed", "job_id": "pv-1"},
        ]
        coordinator.collect.return_value = {
            "status": "completed",
            "job_id": "pv-1",
            "data": [{"_c0": "10"}],
            "data_meta": {"format": "csv", "row_count": 1},
        }

        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM t",
            timeout_sec=60,
            poll_interval=0.01,
        )
        assert result["ok"] is True
        assert result["count"] == 10
        assert result["job_id"] == "pv-1"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_count_zero_parsed(self):
        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "pv-2"}
        coordinator.poll.side_effect = [{"status": "completed", "job_id": "pv-2"}]
        coordinator.collect.return_value = {
            "status": "completed",
            "job_id": "pv-2",
            "data": [{"_c0": "0"}],
        }

        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM t",
            timeout_sec=60,
            poll_interval=0.01,
        )
        assert result["ok"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_submit_error(self):
        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "ERROR", "message": "queue full"}

        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM t",
            timeout_sec=60,
            poll_interval=0.01,
        )
        assert result["ok"] is False
        assert "queue full" in result["error"]

    @pytest.mark.asyncio
    async def test_collect_failed(self):
        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "pv-3"}
        coordinator.poll.side_effect = [{"status": "completed", "job_id": "pv-3"}]
        coordinator.collect.return_value = {
            "status": "failed",
            "job_id": "pv-3",
            "error": "table not found",
        }

        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM nonexistent",
            timeout_sec=60,
            poll_interval=0.01,
        )
        assert result["ok"] is False
        assert "table not found" in result["error"]

    @pytest.mark.asyncio
    async def test_count_parse_failure(self):
        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "pv-4"}
        coordinator.poll.side_effect = [{"status": "completed", "job_id": "pv-4"}]
        coordinator.collect.return_value = {
            "status": "completed",
            "job_id": "pv-4",
            "data": "not a list",
        }

        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM t",
            timeout_sec=60,
            poll_interval=0.01,
        )
        assert result["ok"] is False
        assert "parse failed" in result["error"]


# ===========================================================================
# dataops_validate_sql — INSERT post-validate integration
# ===========================================================================


class TestDataopsValidateSqlPostValidate:
    """INSERT only: when first-phase passes, second-phase SELECT count(*) runs."""

    @pytest.mark.asyncio
    async def test_insert_passed_count_gt_zero_final_passed(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll=[{"status": "completed", "job_id": "j-1"}],
            collect={"status": "completed", "job_id": "j-1"},
        )
        # Second-phase submit + poll + collect for count query
        coordinator.submit_job.side_effect = [
            {"status": "queued", "job_id": "j-1"},  # first INSERT
            {"status": "queued", "job_id": "pv-1"},  # count
        ]
        coordinator.poll.side_effect = [
            {"status": "completed", "job_id": "j-1"},
            {"status": "completed", "job_id": "pv-1"},
        ]
        coordinator.collect.side_effect = [
            {"status": "completed", "job_id": "j-1"},
            {
                "status": "completed",
                "job_id": "pv-1",
                "data": [{"_c0": "5"}],
                "data_meta": {"format": "csv", "row_count": 1},
            },
        ]
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql(
            "INSERT OVERWRITE TABLE biads.x SELECT 1",
            _tool_context=ctx,
        )
        assert result["passed"] is True
        assert result["job_id"] == "j-1"

    @pytest.mark.asyncio
    async def test_insert_passed_count_zero_final_failed(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-2"},
            poll=[{"status": "completed", "job_id": "j-2"}],
            collect={"status": "completed", "job_id": "j-2"},
        )
        coordinator.submit_job.side_effect = [
            {"status": "queued", "job_id": "j-2"},
            {"status": "queued", "job_id": "pv-2"},
        ]
        coordinator.poll.side_effect = [
            {"status": "completed", "job_id": "j-2"},
            {"status": "completed", "job_id": "pv-2"},
        ]
        coordinator.collect.side_effect = [
            {"status": "completed", "job_id": "j-2"},
            {"status": "completed", "job_id": "pv-2", "data": [{"_c0": "0"}]},
        ]
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql(
            "INSERT INTO TABLE biads.x SELECT * FROM src",
            _tool_context=ctx,
        )
        assert result["passed"] is False
        assert "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_insert_count_job_failed_final_failed(self):
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-3"},
            poll=[{"status": "completed", "job_id": "j-3"}],
            collect={"status": "completed", "job_id": "j-3"},
        )
        coordinator.submit_job.side_effect = [
            {"status": "queued", "job_id": "j-3"},
            {"status": "queued", "job_id": "pv-3"},
        ]
        coordinator.poll.side_effect = [
            {"status": "completed", "job_id": "j-3"},
            {"status": "failed", "job_id": "pv-3"},
        ]
        coordinator.collect.side_effect = [
            {"status": "completed", "job_id": "j-3"},
            {"status": "failed", "job_id": "pv-3", "error": "table not found"},
        ]
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql(
            "INSERT OVERWRITE TABLE biads.x SELECT 1",
            _tool_context=ctx,
        )
        assert result["passed"] is False
        assert "post_validate" in result["error"]

    @pytest.mark.asyncio
    async def test_select_no_post_validate(self):
        """SELECT does not trigger second-phase count query."""
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-sel"},
            poll=[{"status": "completed", "job_id": "j-sel"}],
            collect={"status": "completed", "job_id": "j-sel"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql(
            "SELECT 1 FROM biads.x",
            _tool_context=ctx,
        )
        assert result["passed"] is True
        assert result["job_id"] == "j-sel"
        # submit_job should be called only once
        assert coordinator.submit_job.call_count == 1

    @pytest.mark.asyncio
    async def test_create_no_post_validate(self):
        """CREATE TABLE does not trigger second-phase count query."""
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-create"},
            poll=[{"status": "completed", "job_id": "j-create"}],
            collect={"status": "completed", "job_id": "j-create"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql(
            "CREATE TABLE biads.x (id INT)",
            _tool_context=ctx,
        )
        assert result["passed"] is True
        assert coordinator.submit_job.call_count == 1

    @pytest.mark.asyncio
    async def test_first_phase_failed_no_post_validate(self):
        """First phase failed — no second phase at all."""
        resource = _make_resource(timeout_s=5, poll_interval_ms=50)
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-fail"},
            poll=[{"status": "failed", "job_id": "j-fail"}],
            collect={"status": "failed", "job_id": "j-fail", "error": "bad sql"},
        )
        runtime = _make_runtime(resource=resource, coordinator=coordinator)
        ctx = _make_context(runtime)
        result = await vt.dataops_validate_sql(
            "INSERT OVERWRITE TABLE biads.x SELECT 1",
            _tool_context=ctx,
        )
        assert result["passed"] is False
        assert "bad sql" in result["error"]
        # Only the first submit_job was called (count not submitted)
        assert coordinator.submit_job.call_count == 1


# ===========================================================================
# _analyze_count_zero_with_llm + count_zero_analysis integration
# ===========================================================================


class TestAnalyzeCountZeroWithLlm:
    """_analyze_count_zero_with_llm resolves LLM and parses the response."""

    @pytest.mark.asyncio
    async def test_returns_has_mismatch_true(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"has_mismatch":true,'
                    '"mismatch_reason":"WHERE condition filters out all rows",'
                    '"fix_suggestion":"Change pt_d to 20260817"}'
                )
            )
        )
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        result = await vt._analyze_count_zero_with_llm(
            original_query="insert today data",
            generated_sql="INSERT INTO t SELECT * FROM src WHERE pt_d='20260801'",
            runtime=runtime,
        )
        assert result["has_mismatch"] is True
        assert "WHERE condition" in result["mismatch_reason"]
        assert "pt_d to 20260817" in result["fix_suggestion"]

    @pytest.mark.asyncio
    async def test_returns_has_mismatch_false_when_llm_says_so(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp('{"has_mismatch":false,"mismatch_reason":"","fix_suggestion":""}')
            )
        )
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        result = await vt._analyze_count_zero_with_llm(
            original_query="insert today data",
            generated_sql="INSERT INTO t SELECT * FROM src WHERE pt_d='20260817'",
            runtime=runtime,
        )
        assert result["has_mismatch"] is False
        assert result["mismatch_reason"] == ""
        assert result["fix_suggestion"] == ""

    @pytest.mark.asyncio
    async def test_no_runtime_llm_falls_back_to_llm_manager(self):
        fallback_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"has_mismatch":true,"mismatch_reason":"wrong table","fix_suggestion":"use src instead of dst"}'
                )
            )
        )
        runtime = MagicMock()
        runtime.llm = MagicMock(side_effect=RuntimeError("not ready"))
        with patch.object(vt, "llm_manager") as mock_llm_manager:
            mock_llm_manager.get_default_llm.return_value = fallback_llm
            result = await vt._analyze_count_zero_with_llm(
                original_query="copy from src",
                generated_sql="INSERT INTO dst SELECT * FROM src",
                runtime=runtime,
            )
        mock_llm_manager.get_default_llm.assert_called_once()
        assert result["has_mismatch"] is True
        assert "wrong table" in result["mismatch_reason"]

    @pytest.mark.asyncio
    async def test_no_llm_returns_no_mismatch(self):
        runtime = MagicMock()
        runtime.llm = MagicMock(side_effect=RuntimeError("no LLM"))
        with patch.object(vt, "llm_manager") as mock_llm_manager:
            mock_llm_manager.get_default_llm.side_effect = RuntimeError("no LLM at all")
            result = await vt._analyze_count_zero_with_llm(
                original_query="insert today data",
                generated_sql="INSERT INTO t SELECT * FROM src",
                runtime=runtime,
            )
        assert result["has_mismatch"] is False

    @pytest.mark.asyncio
    async def test_invalid_json_returns_no_mismatch(self):
        fake_llm = MagicMock(invoke=MagicMock(return_value=_FakeLLMResp("not json output")))
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        result = await vt._analyze_count_zero_with_llm(
            original_query="insert today data",
            generated_sql="INSERT INTO t SELECT * FROM src",
            runtime=runtime,
        )
        assert result["has_mismatch"] is False

    @pytest.mark.asyncio
    async def test_llm_exception_returns_no_mismatch(self):
        fake_llm = MagicMock(invoke=MagicMock(side_effect=RuntimeError("LLM down")))
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        result = await vt._analyze_count_zero_with_llm(
            original_query="insert today data",
            generated_sql="INSERT INTO t SELECT * FROM src",
            runtime=runtime,
        )
        assert result["has_mismatch"] is False

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '```json\n{"has_mismatch":true,"mismatch_reason":"bad","fix_suggestion":"fix"}\n```'
                )
            )
        )
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)
        result = await vt._analyze_count_zero_with_llm(
            original_query="q",
            generated_sql="INSERT INTO t SELECT * FROM src",
            runtime=runtime,
        )
        assert result["has_mismatch"] is True


class TestRunPostValidateCountZeroWithLlm:
    """_run_post_validate returns LLM analysis fields when count=0 and original_query given."""

    @pytest.mark.asyncio
    async def test_count_zero_no_original_query_no_llm_call(self):
        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "pv-5"}
        coordinator.poll.side_effect = [{"status": "completed", "job_id": "pv-5"}]
        coordinator.collect.return_value = {
            "status": "completed",
            "job_id": "pv-5",
            "data": [{"_c0": "0"}],
        }
        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM t",
            timeout_sec=60,
            poll_interval=0.01,
            runtime=None,
            original_query="",  # empty → no LLM call
        )
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["has_mismatch"] is False

    @pytest.mark.asyncio
    async def test_count_zero_with_original_query_calls_llm(self):
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"has_mismatch":true,'
                    '"mismatch_reason":"date range too narrow",'
                    '"fix_suggestion":"extend to last 7 days"}'
                )
            )
        )
        runtime = MagicMock()
        runtime.llm = MagicMock(return_value=fake_llm)

        coordinator = MagicMock()
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "pv-6"}
        coordinator.poll.side_effect = [{"status": "completed", "job_id": "pv-6"}]
        coordinator.collect.return_value = {
            "status": "completed",
            "job_id": "pv-6",
            "data": [{"_c0": "0"}],
        }
        result = await vt._run_post_validate(
            coordinator,
            "SELECT count(*) FROM t",
            "INSERT INTO t SELECT * FROM src WHERE pt_d='20260817'",
            timeout_sec=60,
            poll_interval=0.01,
            runtime=runtime,
            original_query="insert today's data",
        )
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["has_mismatch"] is True
        assert "date range" in result["mismatch_reason"]
        assert "last 7 days" in result["fix_suggestion"]


class TestDataopsValidateSqlCountZeroAnalysis:
    """INSERT + count=0: count_zero_analysis appears when LLM detects mismatch."""

    @pytest.mark.asyncio
    async def test_count_zero_llm_mismatch_includes_count_zero_analysis(self):
        """INSERT + count=0 + LLM mismatch: count_zero_analysis in result."""
        fake_llm = MagicMock(
            invoke=MagicMock(
                return_value=_FakeLLMResp(
                    '{"has_mismatch":true,'
                    '"mismatch_reason":"WHERE condition filters out all rows",'
                    '"fix_suggestion":"Remove pt_d filter"}'
                )
            )
        )
        resource = _make_resource(timeout_s=300, poll_interval_ms=50)
        coordinator = MagicMock()
        coordinator.catalog = _catalog(resource)
        # INSERT submit+collect
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "j-cz-1"}
        coordinator.poll.return_value = {"status": "completed"}
        coordinator.collect.return_value = {"status": "completed", "job_id": "j-cz-1"}
        runtime = MagicMock()
        runtime.ensure_resource_coordinator.return_value = coordinator
        runtime.llm = MagicMock(return_value=fake_llm)
        ctx = _make_context(runtime)
        with patch.object(vt, "_run_post_validate") as mock_pv:
            mock_pv.return_value = {
                "ok": True,
                "count": 0,
                "job_id": "pv-1",
                "has_mismatch": True,
                "mismatch_reason": "WHERE condition filters out all rows",
                "fix_suggestion": "Remove pt_d filter",
            }
            result = await vt.dataops_validate_sql(
                "INSERT OVERWRITE TABLE biads.x SELECT * FROM biads.src WHERE pt_d = '$date'",
                _tool_context=ctx,
            )
        assert result["passed"] is False
        assert "empty" in result["error"].lower()
        assert "count_zero_analysis" in result
        assert result["count_zero_analysis"]["has_mismatch"] is True
        assert "WHERE condition" in result["count_zero_analysis"]["mismatch_reason"]
        assert "Remove pt_d filter" in result["count_zero_analysis"]["fix_suggestion"]

    @pytest.mark.asyncio
    async def test_count_zero_no_llm_no_count_zero_analysis(self):
        """No original_query → no LLM call → no count_zero_analysis field."""
        resource = _make_resource(timeout_s=300, poll_interval_ms=50)
        coordinator = MagicMock()
        coordinator.catalog = _catalog(resource)
        coordinator.submit_job.return_value = {"status": "queued", "job_id": "j-cz-2"}
        coordinator.poll.return_value = {"status": "completed"}
        coordinator.collect.return_value = {"status": "completed", "job_id": "j-cz-2"}
        runtime = MagicMock()
        runtime.ensure_resource_coordinator.return_value = coordinator
        ctx = _make_context(runtime)
        with patch.object(vt, "_run_post_validate") as mock_pv:
            mock_pv.return_value = {
                "ok": True,
                "count": 0,
                "job_id": "pv-2",
                "has_mismatch": False,
                "mismatch_reason": "",
                "fix_suggestion": "",
            }
            result = await vt.dataops_validate_sql(
                "INSERT OVERWRITE TABLE biads.x SELECT * FROM src",
                _tool_context=ctx,
            )
        assert result["passed"] is False
        assert "empty" in result["error"].lower()
        assert "count_zero_analysis" not in result
