# ============================================================================
# Unit tests: the validation chain's thinking-mode switch reaches the model call.
#
# These are wiring tests — they assert the ``enable_thinking`` flag actually
# reaches the model call at each validation entry point, not just that a helper
# returns it.
#
# The switch is **per tool configuration** (``config.enable_thinking``), not a
# global env var. Each call site must therefore thread its own tool config
# through to the model call.
#
# Covered call sites:
#   1. ``dataops_validate_tool._analyze_log_directly`` — failure-log analysis
#   2. ``dataops_validate_tool._expand_cte_for_dml``   — CTE inlining
#   3. ``de_agent.tools.verify``                       — deliverable quality gate
#   4. ``de_agent.tools.sql_checkers``                 — SQL rule repair
#   5. ``de_agent.tools.nl2sql``                       — metadata grounding
# ============================================================================

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.local_tool import dataops_validate_tool as vt
from dataagent.core.managers.llm_manager.llm_client import LLMClient

_THINKING = "enable_thinking"


def _bailian_llm() -> LLMClient:
    """A client pointed at bailian — the endpoint that understands the flag."""
    return LLMClient(
        model="DeepSeek_V4_Flash_Client",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-test",
        provider="bailian",
        extra_body={_THINKING: True},  # slot-level thinking ON — must be overridden
    )


def _plain_llm() -> LLMClient:
    """A client pointed at a strict OpenAI-compatible endpoint."""
    return LLMClient(
        model="gpt-4o",
        api_base="https://api.openai.com/v1",
        api_key="sk-test",
        provider="openai",
    )


class TestAnalyzeLogThinking:
    """Failure-log analysis is a structured-output task — thinking only adds latency."""

    def test_flag_reaches_the_stream_call(self):
        llm = _bailian_llm()
        captured: dict = {}

        async def fake_collect(_llm, _prompts, **kwargs):
            captured.update(kwargs)
            return json.dumps(
                {
                    "error_type": "AnalysisException",
                    "error_message": "boom",
                    "location": {"tables": [], "columns": [], "functions": [], "line": None},
                    "suggestions": [],
                }
            )

        with (
            patch.object(vt, "collect_llm_stream", fake_collect),
            patch.object(vt, "dataops_fetch_obs_log", return_value={"status": "success", "log_content": "log"}),
            patch.object(vt, "llm_manager") as mock_manager,
        ):
            mock_manager.get_default_llm.return_value = llm
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {}},
                    job_id="job-1",
                    raw_error="boom",
                )
            )

        assert result["status"] == "ok"
        assert captured[_THINKING] is False

    def test_unknown_gateway_gets_no_flag(self):
        llm = _plain_llm()
        captured: dict = {}

        async def fake_collect(_llm, _prompts, **kwargs):
            captured.update(kwargs)
            return "{}"

        with (
            patch.object(vt, "collect_llm_stream", fake_collect),
            patch.object(vt, "dataops_fetch_obs_log", return_value={"status": "success", "log_content": "log"}),
            patch.object(vt, "llm_manager") as mock_manager,
        ):
            mock_manager.get_default_llm.return_value = llm
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {}},
                    job_id="job-1",
                    raw_error="boom",
                )
            )

        assert _THINKING not in captured

    def test_tool_config_can_turn_thinking_on(self):
        """``config.enable_thinking: true`` must reach the model call."""
        llm = _bailian_llm()
        captured: dict = {}

        async def fake_collect(_llm, _prompts, **kwargs):
            captured.update(kwargs)
            return json.dumps(
                {
                    "error_type": "AnalysisException",
                    "error_message": "boom",
                    "location": {"tables": [], "columns": [], "functions": [], "line": None},
                    "suggestions": [],
                }
            )

        with (
            patch.object(vt, "collect_llm_stream", fake_collect),
            patch.object(vt, "dataops_fetch_obs_log", return_value={"status": "success", "log_content": "log"}),
            patch.object(vt, "llm_manager") as mock_manager,
        ):
            mock_manager.get_default_llm.return_value = llm
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {}},
                    job_id="job-1",
                    raw_error="boom",
                    enable_thinking=True,
                )
            )

        assert captured[_THINKING] is True


class TestExpandCteThinking:
    """CTE inlining is a mechanical SQL rewrite — no reasoning needed."""

    def test_flag_reaches_the_invoke_call(self):
        llm = _bailian_llm()
        captured: dict = {}

        def fake_invoke(_messages, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content="INSERT INTO r SELECT * FROM (SELECT 1) AS t")

        llm.invoke = fake_invoke  # type: ignore[method-assign]
        runtime = SimpleNamespace(llm=lambda _name: llm)

        out = asyncio.run(
            vt._expand_cte_for_dml("WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t", runtime=runtime)
        )

        assert "(SELECT 1) AS t" in out
        assert captured[_THINKING] is False

    def test_tool_config_can_turn_thinking_on(self):
        llm = _bailian_llm()
        captured: dict = {}

        def fake_invoke(_messages, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content="INSERT INTO r SELECT * FROM (SELECT 1) AS t")

        llm.invoke = fake_invoke  # type: ignore[method-assign]
        runtime = SimpleNamespace(llm=lambda _name: llm)

        asyncio.run(
            vt._expand_cte_for_dml(
                "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t",
                runtime=runtime,
                enable_thinking=True,
            )
        )

        assert captured[_THINKING] is True


class TestQualityGateThinking:
    """The deliverable quality gate parses JSON — thinking fragments would break it."""

    def test_flag_reaches_the_quality_gate_stream(self, tmp_path):
        from dataagent.actions.tools.local_tool.sandbox import (
            create_sandbox,
            reset_current_sandbox,
            set_current_sandbox,
        )
        from dataagent.core.suite.builtin_suites.de_agent.tools import verify

        llm = _bailian_llm()
        captured: dict = {}

        def fake_stream(_llm, _prompts, **kwargs):
            captured.update(kwargs)
            return json.dumps({"gate_passed": True, "gate_failures": [], "checks": [], "summary": "ok"})

        sandbox = create_sandbox(enabled=False, workspace_root=tmp_path)
        token = set_current_sandbox(sandbox)
        try:
            with (
                patch.object(verify, "_run_quality_gate_stream", fake_stream),
                patch.object(verify, "llm_manager") as mock_manager,
            ):
                mock_manager.get_default_llm.return_value = llm
                warnings: list[str] = []
                verify._validate_deliverables_quality_gate(
                    None,
                    None,
                    warnings,
                    "query",
                    False,
                    _tool_context=SimpleNamespace(runtime=SimpleNamespace(get_cache={}.get)),
                )
        finally:
            reset_current_sandbox(token)

        assert captured[_THINKING] is False

    def test_tool_config_can_turn_thinking_on(self, tmp_path):
        from dataagent.actions.tools.local_tool.sandbox import (
            create_sandbox,
            reset_current_sandbox,
            set_current_sandbox,
        )
        from dataagent.core.suite.builtin_suites.de_agent.tools import verify

        llm = _bailian_llm()
        captured: dict = {}

        def fake_stream(_llm, _prompts, **kwargs):
            captured.update(kwargs)
            return json.dumps({"gate_passed": True, "gate_failures": [], "checks": [], "summary": "ok"})

        sandbox = create_sandbox(enabled=False, workspace_root=tmp_path)
        token = set_current_sandbox(sandbox)
        try:
            with (
                patch.object(verify, "_run_quality_gate_stream", fake_stream),
                patch.object(verify, "llm_manager") as mock_manager,
            ):
                mock_manager.get_default_llm.return_value = llm
                warnings: list[str] = []
                verify._validate_deliverables_quality_gate(
                    None,
                    None,
                    warnings,
                    "query",
                    False,
                    _tool_context=SimpleNamespace(runtime=SimpleNamespace(get_cache={}.get)),
                    enable_thinking=True,
                )
        finally:
            reset_current_sandbox(token)

        assert captured[_THINKING] is True


class TestSqlCheckerRepairThinking:
    """Rule repair is a bounded patch task."""

    @pytest.mark.asyncio
    async def test_flag_reaches_the_repair_stream(self):
        from dataagent.core.suite.builtin_suites.de_agent.tools import sql_checkers

        llm = _bailian_llm()
        captured: dict = {}

        class _Chunk:
            final_response = SimpleNamespace(content="{}", usage={"total_tokens": 1})

        async def fake_astream(_messages, **kwargs):
            captured.update(kwargs)
            yield _Chunk()

        llm.astream = fake_astream  # type: ignore[method-assign]

        pipeline = sql_checkers.SQLCheckerPipeline(MagicMock(), llm, MagicMock(), {})
        _content, _usage = await pipeline._invoke("prompt")

        assert captured[_THINKING] is False

    @pytest.mark.asyncio
    async def test_tool_config_can_turn_thinking_on(self):
        from dataagent.core.suite.builtin_suites.de_agent.tools import sql_checkers

        llm = _bailian_llm()
        captured: dict = {}

        class _Chunk:
            final_response = SimpleNamespace(content="{}", usage={"total_tokens": 1})

        async def fake_astream(_messages, **kwargs):
            captured.update(kwargs)
            yield _Chunk()

        llm.astream = fake_astream  # type: ignore[method-assign]

        pipeline = sql_checkers.SQLCheckerPipeline(MagicMock(), llm, MagicMock(), {"enable_thinking": True})
        await pipeline._invoke("prompt")

        assert captured[_THINKING] is True


class TestMetadataGroundingThinking:
    """Metadata grounding validation returns strict JSON."""

    def test_flag_reaches_the_invoke_call(self, tmp_path):
        from dataagent.actions.tools.local_tool.sandbox import (
            create_sandbox,
            reset_current_sandbox,
            set_current_sandbox,
        )
        from dataagent.core.suite.builtin_suites.de_agent.tools import nl2sql

        llm = _bailian_llm()
        captured: dict = {}

        def fake_invoke(_messages, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=json.dumps({"valid": True, "summary": "ok", "details": []}))

        llm.invoke = fake_invoke  # type: ignore[method-assign]

        # The validator requires both metadata files to exist.
        (tmp_path / "schema_schemair.md").write_text("Hive: t(a STRING)", encoding="utf-8")
        (tmp_path / "schema_udf_basic.md").write_text("UDF: f", encoding="utf-8")

        sandbox = create_sandbox(enabled=False, workspace_root=tmp_path)
        token = set_current_sandbox(sandbox)
        try:
            with patch.object(nl2sql, "llm_manager") as mock_manager:
                mock_manager.get_default_llm.return_value = llm
                nl2sql._validate_nl2sql_metadata_grounding("request")
        finally:
            reset_current_sandbox(token)

        assert captured.get(_THINKING) is False
