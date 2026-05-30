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
"""策略 D：litellm retry_policy + DataAgent 5xx 薄层 + LLMCallError 映射。"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from dataagent.core.managers.llm_manager.llm_client import (
    LLMCallError,
    LLMClient,
    LLMErrorCategory,
    _normalize_litellm_retry_kwargs,
    map_litellm_exception,
)
from dataagent.utils.constants import DEFAULT_LLM_MAX_RETRIES


class TestNormalizeLitellmRetryKwargs:
    def test_defaults_num_retries_zero_and_policy(self):
        call_kw, max_attempts = _normalize_litellm_retry_kwargs({})
        assert max_attempts == DEFAULT_LLM_MAX_RETRIES
        assert call_kw["num_retries"] == 0
        assert call_kw["retry_policy"]["AuthenticationErrorRetries"] == 0
        assert call_kw["retry_policy"]["RateLimitErrorRetries"] == DEFAULT_LLM_MAX_RETRIES

    def test_yaml_num_retries_overrides_attempts(self):
        call_kw, max_attempts = _normalize_litellm_retry_kwargs({"num_retries": 5})
        assert max_attempts == 5
        assert call_kw["retry_policy"]["RateLimitErrorRetries"] == 5

    def test_strips_max_retries_and_yaml_retry_policy(self):
        call_kw, _ = _normalize_litellm_retry_kwargs(
            {
                "max_retries": 9,
                "retry_policy": {"AuthenticationErrorRetries": 99},
            }
        )
        assert "max_retries" not in call_kw
        assert call_kw["retry_policy"]["AuthenticationErrorRetries"] == 0


class TestMapLitellmException:
    def test_rate_limit_category(self):
        from litellm.exceptions import RateLimitError

        exc = RateLimitError("quota", "openai", "gpt-4")
        mapped = map_litellm_exception(exc, model="deepseek-chat")
        assert mapped.category == LLMErrorCategory.RATE_LIMIT
        rendered = str(mapped)
        assert rendered.startswith("[429 rate_limit]")
        assert "deepseek-chat" in rendered

    def test_already_llm_call_error_unchanged(self):
        original = LLMCallError(LLMErrorCategory.AUTH, "bad key")
        assert map_litellm_exception(original) is original

    def test_request_url_in_error_string(self):
        from litellm.exceptions import NotFoundError

        base = "https://dashscope.aliyuncs.com/compatible-mode/"
        exc = NotFoundError("Error code: 404", "openai", "qwen-plus", response=None)
        mapped = map_litellm_exception(exc, model="qwen-plus", api_base=base)
        assert mapped.request_url == "https://dashscope.aliyuncs.com/compatible-mode/chat/completions"
        rendered = str(mapped)
        assert rendered.startswith("[404 not_found]")
        assert "url=https://dashscope.aliyuncs.com/compatible-mode/chat/completions" in rendered
        assert mapped.category == LLMErrorCategory.NOT_FOUND


class TestAinvokeRetryBehavior:
    @pytest.mark.asyncio
    @patch("litellm.acompletion", new_callable=AsyncMock)
    async def test_auth_error_single_call(self, mock_acompletion):
        from litellm.exceptions import AuthenticationError

        mock_acompletion.side_effect = AuthenticationError("invalid key", "openai", "m", response=None)
        client = LLMClient(
            model="test-model",
            api_base="http://test",
            api_key="k",
        )
        with pytest.raises(LLMCallError) as ei:
            await client.ainvoke([{"role": "user", "content": "hi"}])
        assert ei.value.category == LLMErrorCategory.AUTH
        assert mock_acompletion.await_count == 1

    @pytest.mark.asyncio
    @patch("litellm.acompletion", new_callable=AsyncMock)
    async def test_not_found_single_call(self, mock_acompletion):
        from litellm.exceptions import NotFoundError

        mock_acompletion.side_effect = NotFoundError("model not found", "openai", "missing", response=None)
        client = LLMClient(
            model="missing",
            api_base="http://test",
            api_key="k",
        )
        with pytest.raises(LLMCallError) as ei:
            await client.ainvoke([{"role": "user", "content": "hi"}])
        assert ei.value.category == LLMErrorCategory.NOT_FOUND
        assert mock_acompletion.await_count == 1

    @pytest.mark.asyncio
    @patch("litellm.acompletion", new_callable=AsyncMock)
    async def test_internal_server_error_dataagent_retries(self, mock_acompletion):
        from litellm.exceptions import InternalServerError

        ok_resp = Mock(
            choices=[Mock(message=Mock(content="ok", tool_calls=[], thinking_blocks=None))],
            usage=Mock(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
        mock_acompletion.side_effect = [
            InternalServerError("503", "openai", "m", response=None),
            ok_resp,
        ]
        client = LLMClient(
            model="test-model",
            api_base="http://test",
            api_key="k",
            num_retries=2,
        )
        out = await client.ainvoke([{"role": "user", "content": "hi"}])
        assert out.content == "ok"
        assert mock_acompletion.await_count == 2

    @pytest.mark.asyncio
    @patch("litellm.acompletion", new_callable=AsyncMock)
    async def test_passes_num_retries_zero_to_litellm(self, mock_acompletion):
        mock_acompletion.return_value = Mock(
            choices=[Mock(message=Mock(content="x", tool_calls=[], thinking_blocks=None))],
            usage=Mock(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )
        client = LLMClient(model="m", api_base="http://t", api_key="k")
        await client.ainvoke([{"role": "user", "content": "hi"}])
        _, kwargs = mock_acompletion.await_args
        assert kwargs["num_retries"] == 0
        assert kwargs["retry_policy"]["AuthenticationErrorRetries"] == 0
