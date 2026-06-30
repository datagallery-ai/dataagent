# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""验证 intent_understanding hook 的槽位填充与短路行为。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.core.flex.hooks.intent_understanding import (
    _format_history,
    _parse_llm_response,
    intent_understanding,
)


class TestParseLlmResponse:
    """_parse_llm_response 的解析与容错。"""

    def test_parses_plain_json_complete(self):
        raw = '{"complete": true, "filled": {"dataset": "A"}, "missing": [], "message": "ok"}'
        result = _parse_llm_response(raw)
        assert result["complete"] is True
        assert result["filled"] == {"dataset": "A"}
        assert result["missing"] == []
        assert result["message"] == "ok"

    def test_parses_json_code_block(self):
        raw = '```json\n{"complete": false, "filled": {}, "missing": ["dataset"], "message": "missing"}\n```'
        result = _parse_llm_response(raw)
        assert result["complete"] is False
        assert result["missing"] == ["dataset"]

    def test_parses_json_code_block_without_json_suffix(self):
        raw = '```\n{"complete": true, "filled": {"metric": "sales"}, "missing": [], "message": ""}\n```'
        result = _parse_llm_response(raw)
        assert result["complete"] is True
        assert result["filled"] == {"metric": "sales"}

    def test_fallback_on_invalid_json(self):
        raw = "I don't know what to fill."
        result = _parse_llm_response(raw)
        assert result["complete"] is False
        assert result["message"] == raw.strip()

    def test_fallback_on_empty_response(self):
        result = _parse_llm_response("")
        assert result["complete"] is False
        assert result["message"] == "意图理解失败，请重试。"

    def test_missing_fields_default_to_falsy(self):
        raw = '{"complete": true}'
        result = _parse_llm_response(raw)
        assert result["filled"] == {}
        assert result["missing"] == []
        assert result["message"] == ""


class TestFormatHistory:
    """_format_history 的格式化逻辑。"""

    def test_empty_returns_placeholder(self):
        assert _format_history([]) == "（无历史消息）"

    def test_formats_role_and_content(self):
        messages = [
            HumanMessage(content="分析数据"),
            AIMessage(content="好的，我来帮你。"),
        ]
        result = _format_history(messages)
        assert "**human**: 分析数据" in result
        assert "**ai**: 好的，我来帮你。" in result

    def test_limits_to_last_10_messages(self):
        messages = [HumanMessage(content=f"msg{i}") for i in range(15)]
        result = _format_history(messages)
        assert "msg0" not in result
        assert "msg5" in result


class TestIntentUnderstanding:
    """intent_understanding 主函数的分支行为。"""

    def _make_runtime(self, llm_response: str):
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=MagicMock(content=llm_response))
        runtime = SimpleNamespace(
            env=SimpleNamespace(llm_configs={"intent_understanding": {"model": "test"}}),
            llm=MagicMock(return_value=fake_llm),
            get_config=MagicMock(
                return_value={
                    "template": "分析 {dataset} 的 {metric}",
                    "fields": ["dataset", "metric"],
                }
            ),
        )
        return runtime

    def test_skips_when_hook_not_in_llm_configs(self):
        runtime = SimpleNamespace(
            env=SimpleNamespace(llm_configs={}),
            get_config=MagicMock(return_value={"template": "x", "fields": ["a"]}),
        )
        state = {"messages": [], "user_query": "分析销售"}
        result = intent_understanding(state, runtime)
        assert result is state  # 未修改

    def test_skips_when_intent_template_not_configured(self):
        runtime = SimpleNamespace(
            env=SimpleNamespace(llm_configs={"intent_understanding": {"model": "test"}}),
            get_config=MagicMock(return_value=None),
        )
        state = {"messages": [], "user_query": "分析销售"}
        result = intent_understanding(state, runtime)
        assert result is state

    def test_skips_when_template_or_fields_empty(self):
        runtime = SimpleNamespace(
            env=SimpleNamespace(llm_configs={"intent_understanding": {"model": "test"}}),
            get_config=MagicMock(return_value={"template": "", "fields": []}),
        )
        state = {"messages": [], "user_query": "分析销售"}
        result = intent_understanding(state, runtime)
        assert result is state

    @patch("dataagent.core.flex.hooks.intent_understanding.PromptTemplate")
    def test_complete_true_fills_slots_and_renders_template(self, mock_prompt_cls):
        runtime = self._make_runtime(
            '{"complete": true, "filled": {"dataset": "A", "metric": "sales"}, "missing": [], "message": ""}'
        )
        mock_template = MagicMock()
        mock_template.apply_prompt_template = MagicMock(return_value="分析 A 的 sales")
        mock_prompt_cls.from_package_relative = MagicMock(return_value=mock_template)
        mock_prompt_cls.from_string = MagicMock(return_value=mock_template)

        state = {"messages": [], "user_query": "分析销售"}
        result = intent_understanding(state, runtime)

        assert result["intent_complete"] is True
        assert result["intent_slots"] == {"dataset": "A", "metric": "sales"}
        assert result["missing_slots"] == []
        assert result["user_query"] == "分析 A 的 sales"

    def test_complete_false_only_records_intent_state_without_message_mutation(self):
        runtime = self._make_runtime(
            '{"complete": false, "filled": {}, "missing": ["dataset", "metric"], "message": "缺少必要信息：dataset 和 metric"}'
        )

        with patch("dataagent.core.flex.hooks.intent_understanding._emit_missing_info_message"):
            state = {"messages": [HumanMessage(content="hi")], "user_query": "分析销售"}
            result = intent_understanding(state, runtime)

        assert result["intent_complete"] is False
        assert result["intent_slots"] == {}
        assert result["missing_slots"] == ["dataset", "metric"]
        assert result["intent_missing_message"] == "缺少必要信息：dataset 和 metric"
        assert result.get("complete", False) is False
        assert result["messages"] == [HumanMessage(content="hi")]

    def test_llm_error_returns_state_unchanged(self):
        runtime = self._make_runtime("")
        runtime.llm = MagicMock(side_effect=Exception("LLM 调用失败"))

        state = {"messages": [], "user_query": "分析销售"}
        result = intent_understanding(state, runtime)
        assert result is state
