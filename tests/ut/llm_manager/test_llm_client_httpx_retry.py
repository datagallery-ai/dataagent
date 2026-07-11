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
"""httpx 定制 chat client 的重试 / 异常映射 / 重复检测单元测试。"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from dataagent.core.managers.llm_manager.llm_client import (
    LLMCallError,
    LLMClient,
    LLMErrorCategory,
    LLMRepetitionError,
    map_httpx_exception,
)
from dataagent.utils.constants import DEFAULT_LLM_MAX_RETRIES


class TestMapHttpException:
    """map_httpx_exception: httpx 异常 → LLMCallError 映射。"""

    def test_already_llm_call_error_unchanged(self):
        original = LLMCallError(LLMErrorCategory.AUTH, "bad key")
        assert map_httpx_exception(original) is original

    def test_rate_limit_429(self):
        resp = MagicMock(status_code=429)
        resp.json.return_value = {"error": {"message": "quota exceeded"}}
        resp.text = '{"error": {"message": "quota exceeded"}}'
        exc = httpx.HTTPStatusError("429", request=MagicMock(), response=resp)
        mapped = map_httpx_exception(exc, model="deepseek-chat", api_base="https://api.example/v1")
        assert mapped.category == LLMErrorCategory.RATE_LIMIT
        assert mapped.status_code == 429
        rendered = str(mapped)
        assert "[429 rate_limit]" in rendered
        assert "deepseek-chat" in rendered

    def test_not_found_404(self):
        resp = MagicMock(status_code=404)
        resp.json.return_value = {"error": {"message": "model not found"}}
        resp.text = '{"error": {"message": "model not found"}}'
        exc = httpx.HTTPStatusError("404", request=MagicMock(), response=resp)
        mapped = map_httpx_exception(exc, model="qwen-plus", api_base="https://dashscope/v1")
        assert mapped.category == LLMErrorCategory.NOT_FOUND
        assert mapped.request_url == "https://dashscope/v1/chat/completions"

    def test_server_error_500(self):
        resp = MagicMock(status_code=503)
        resp.json.return_value = {}
        resp.text = "Service Unavailable"
        exc = httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
        mapped = map_httpx_exception(exc, model="m")
        assert mapped.category == LLMErrorCategory.SERVER_ERROR

    def test_timeout(self):
        exc = httpx.TimeoutException("read timeout")
        mapped = map_httpx_exception(exc, model="m")
        assert mapped.category == LLMErrorCategory.TIMEOUT

    def test_connection_error(self):
        exc = httpx.ConnectError("connection refused")
        mapped = map_httpx_exception(exc, model="m")
        assert mapped.category == LLMErrorCategory.CONNECTION

    def test_auth_401(self):
        resp = MagicMock(status_code=401)
        resp.json.return_value = {"error": {"message": "invalid api key"}}
        resp.text = '{"error": {"message": "invalid api key"}}'
        exc = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)
        mapped = map_httpx_exception(exc, model="m")
        assert mapped.category == LLMErrorCategory.AUTH


class TestResolveMaxAttempts:
    """_resolve_max_attempts: num_retries → max_attempts 解析（通过 LLMClient 实例）。"""

    def test_defaults(self):
        client = LLMClient(model="m", api_base="http://t", api_key="k")
        assert client._resolve_max_attempts({}) == DEFAULT_LLM_MAX_RETRIES

    def test_explicit_num_retries(self):
        client = LLMClient(model="m", api_base="http://t", api_key="k", num_retries=5)
        assert client._resolve_max_attempts({}) == 5

    def test_per_call_override(self):
        client = LLMClient(model="m", api_base="http://t", api_key="k")
        assert client._resolve_max_attempts({"num_retries": 7}) == 7


def _ok_response_json(content="ok", tool_calls=None, usage=None):
    """构造一个 OpenAI 兼容的成功响应 JSON dict。"""
    msg = {"content": content, "reasoning_content": "", "tool_calls": tool_calls or []}
    return {
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class TestAinvokeRetryBehavior:
    """LLMClient.ainvoke 重试行为（mock httpx.AsyncClient）。"""

    @pytest.mark.asyncio
    async def test_auth_error_single_call(self):
        """401 不可重试，单次调用后抛出 LLMCallError(AUTH)。"""
        resp = MagicMock(status_code=401)
        resp.json.return_value = {"error": {"message": "invalid key"}}
        resp.text = '{"error": {"message": "invalid key"}}'
        resp.is_error = True
        exc = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=exc)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)

        with patch("dataagent.core.managers.llm_manager.llm_client.httpx.AsyncClient", return_value=mock_client):
            client = LLMClient(model="test-model", api_base="http://test", api_key="k")
            with pytest.raises(LLMCallError) as ei:
                await client.ainvoke([{"role": "user", "content": "hi"}])
            assert ei.value.category == LLMErrorCategory.AUTH
        assert mock_client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_server_error_retries_then_succeeds(self):
        """503 可重试，重试后成功返回内容。"""
        ok_json = _ok_response_json("recovered")

        mock_resp_ok = MagicMock()
        mock_resp_ok.is_error = False
        mock_resp_ok.json.return_value = ok_json
        mock_resp_ok.raise_for_status = MagicMock()

        resp_503 = MagicMock(status_code=503)
        resp_503.json.return_value = {}
        resp_503.text = "Service Unavailable"
        resp_503.is_error = True
        exc_503 = httpx.HTTPStatusError("503", request=MagicMock(), response=resp_503)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[exc_503, mock_resp_ok])
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)

        with (
            patch("dataagent.core.managers.llm_manager.llm_client.httpx.AsyncClient", return_value=mock_client),
            patch("dataagent.core.managers.llm_manager.llm_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            client = LLMClient(model="test-model", api_base="http://test", api_key="k", num_retries=2)
            out = await client.ainvoke([{"role": "user", "content": "hi"}])
        assert out.content == "recovered"
        assert mock_client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_connection_error_retries(self):
        """httpx.ConnectError 可重试。"""
        ok_json = _ok_response_json("ok-after-retry")

        mock_resp_ok = MagicMock()
        mock_resp_ok.is_error = False
        mock_resp_ok.json.return_value = ok_json
        mock_resp_ok.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[httpx.ConnectError("refused"), mock_resp_ok])
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)

        with (
            patch("dataagent.core.managers.llm_manager.llm_client.httpx.AsyncClient", return_value=mock_client),
            patch("dataagent.core.managers.llm_manager.llm_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            client = LLMClient(model="m", api_base="http://t", api_key="k", num_retries=2)
            out = await client.ainvoke([{"role": "user", "content": "hi"}])
        assert out.content == "ok-after-retry"
        assert mock_client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_metrics_extracted_from_usage(self):
        """ainvoke 返回的 usage_metadata 应含缓存子字段（input_cache_read_tokens 等）。"""
        ok_json = _ok_response_json(
            "ok",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 30},
            },
        )

        mock_resp = MagicMock()
        mock_resp.is_error = False
        mock_resp.json.return_value = ok_json
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)

        with patch("dataagent.core.managers.llm_manager.llm_client.httpx.AsyncClient", return_value=mock_client):
            client = LLMClient(model="m", api_base="http://t", api_key="k")
            out = await client.ainvoke([{"role": "user", "content": "hi"}])

        assert out.usage_metadata["input_tokens"] == 100
        assert out.usage_metadata["input_cache_read_tokens"] == 80
        assert out.usage_metadata["input_cache_creation_tokens"] == 20
        assert out.usage_metadata["output_reasoning_tokens"] == 30

    @pytest.mark.asyncio
    async def test_deepseek_prompt_cache_hit_tokens_fallback(self):
        """DeepSeek 格式：usage.prompt_cache_hit_tokens 顶层字段 fallback。"""
        ok_json = _ok_response_json(
            "ok",
            usage={
                "prompt_tokens": 200,
                "completion_tokens": 10,
                "total_tokens": 210,
                "prompt_cache_hit_tokens": 150,
            },
        )

        mock_resp = MagicMock()
        mock_resp.is_error = False
        mock_resp.json.return_value = ok_json
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)

        with patch("dataagent.core.managers.llm_manager.llm_client.httpx.AsyncClient", return_value=mock_client):
            client = LLMClient(model="deepseek-chat", api_base="http://t", api_key="k")
            out = await client.ainvoke([{"role": "user", "content": "hi"}])

        assert out.usage_metadata["input_cache_read_tokens"] == 150
