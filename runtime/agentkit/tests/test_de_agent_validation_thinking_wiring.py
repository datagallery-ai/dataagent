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
"""Thinking wiring cases ported from main !561 to native call boundaries."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessageChunk

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
LOADER = PythonLoader()
vt = sys.modules[LOADER.load(PLUGIN, "utils/dataops_validate_tool.py:extract_sql_target_table").__module__]
sql_checkers = sys.modules[LOADER.load(PLUGIN, "utils/sql_checkers.py:SQLCheckerPipeline").__module__]
_THINKING = "enable_thinking"


def _bailian_llm():
    return SimpleNamespace(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", extra_body={_THINKING: True})


def _plain_llm():
    return SimpleNamespace(base_url="https://api.openai.com/v1", extra_body={})


def _context(model):
    return SimpleNamespace(get_model=lambda _: model, get_plugin_config=lambda _: {})

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
            patch.object(vt, "invoke_model_text", fake_collect),
            patch.object(vt, "dataops_fetch_obs_log", return_value={"status": "success", "log_content": "log"}),
        ):
            result = asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {}},
                    job_id="job-1",
                    raw_error="boom",
                    context=_context(llm),
                )
            )

        assert result["status"] == "ok"
        assert captured["extra_body"][_THINKING] is False

    def test_unknown_gateway_gets_no_flag(self):
        llm = _plain_llm()
        captured: dict = {}

        async def fake_collect(_llm, _prompts, **kwargs):
            captured.update(kwargs)
            return "{}"

        with (
            patch.object(vt, "invoke_model_text", fake_collect),
            patch.object(vt, "dataops_fetch_obs_log", return_value={"status": "success", "log_content": "log"}),
        ):
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {}},
                    job_id="job-1",
                    raw_error="boom",
                    context=_context(llm),
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
            patch.object(vt, "invoke_model_text", fake_collect),
            patch.object(vt, "dataops_fetch_obs_log", return_value={"status": "success", "log_content": "log"}),
        ):
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {}},
                    job_id="job-1",
                    raw_error="boom",
                    context=_context(llm),
                    enable_thinking=True,
                )
            )

        assert captured["extra_body"][_THINKING] is True


class TestExpandCteThinking:
    """CTE inlining is a mechanical SQL rewrite — no reasoning needed."""

    def test_flag_reaches_the_invoke_call(self):
        llm = _bailian_llm()
        captured: dict = {}

        async def fake_invoke(_messages, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content="INSERT INTO r SELECT * FROM (SELECT 1) AS t")

        llm.ainvoke = fake_invoke  # type: ignore[method-assign]
        runtime = _context(llm)

        out = asyncio.run(
            vt._expand_cte_for_dml("WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t", context=runtime)
        )

        assert "(SELECT 1) AS t" in out
        assert captured["extra_body"][_THINKING] is False

    def test_tool_config_can_turn_thinking_on(self):
        llm = _bailian_llm()
        captured: dict = {}

        async def fake_invoke(_messages, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content="INSERT INTO r SELECT * FROM (SELECT 1) AS t")

        llm.ainvoke = fake_invoke  # type: ignore[method-assign]
        runtime = _context(llm)

        asyncio.run(
            vt._expand_cte_for_dml(
                "WITH t AS (SELECT 1) INSERT INTO r SELECT * FROM t",
                context=runtime,
                enable_thinking=True,
            )
        )

        assert captured["extra_body"][_THINKING] is True


class TestSqlCheckerRepairThinking:
    """Rule repair is a bounded patch task."""

    @pytest.mark.asyncio
    async def test_flag_reaches_the_repair_stream(self):

        llm = _bailian_llm()
        captured: dict = {}


        async def fake_astream(_messages, **kwargs):
            captured.update(kwargs)
            yield AIMessageChunk(content="{}", usage_metadata={"input_tokens": 0, "output_tokens": 1, "total_tokens": 1})

        llm.astream = fake_astream  # type: ignore[method-assign]

        pipeline = sql_checkers.SQLCheckerPipeline(MagicMock(), llm, MagicMock(), {})
        _content, _usage = await pipeline._invoke("prompt")

        assert captured["extra_body"][_THINKING] is False

    @pytest.mark.asyncio
    async def test_tool_config_can_turn_thinking_on(self):

        llm = _bailian_llm()
        captured: dict = {}


        async def fake_astream(_messages, **kwargs):
            captured.update(kwargs)
            yield AIMessageChunk(content="{}", usage_metadata={"input_tokens": 0, "output_tokens": 1, "total_tokens": 1})

        llm.astream = fake_astream  # type: ignore[method-assign]

        pipeline = sql_checkers.SQLCheckerPipeline(MagicMock(), llm, MagicMock(), {"enable_thinking": True})
        await pipeline._invoke("prompt")

        assert captured["extra_body"][_THINKING] is True

