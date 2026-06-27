"""LLM 适配层 — 在 LlmClient 之上封装 sql_gen 兼容的 ask() 接口

并行由上层 sql_generator 控制，本层不做并行化。
"""
from typing import List, Dict, Tuple, Optional
import logging

from .llm_client import LlmClient

logger = logging.getLogger(__name__)


class LLMResponse:
    """LLM响应包装类"""

    def __init__(self, content: str, finish_reason: str = None):
        self.content = content
        self.finish_reason = finish_reason  # 来自 LlmClient._last_finish_reason

    def __str__(self):
        return self.content

    def __repr__(self):
        return f"LLMResponse(content={self.content[:50]}...)"


class LLMAdapter:
    """LLM 适配器 — 在 LlmClient 之上封装 ask() 接口（不负责并行化）"""

    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str = "",
        max_retries: int = None,
        retry_delay: int = None,
        backoff_multiplier: int = None,
        timeout=None,
        temperature: float = None,
        max_tokens: int = None,
        verify_ssl: bool = True,
        extra_body: Optional[Dict] = None,
    ):
        self._client = LlmClient(
            api_base=api_base,
            model=model,
            api_key=api_key,
            max_retries=max_retries,
            retry_delay=retry_delay,
            backoff_multiplier=backoff_multiplier,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            verify_ssl=verify_ssl,
            extra_body=extra_body,
        )

    def ask(
        self,
        messages: List[Dict[str, str]],
        n: int = 1,
        stop: Optional[List[str]] = None,
        temperature: float = None,
    ) -> Tuple[List[LLMResponse], Dict[str, int], List[str]]:
        """
        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            n: 生成数量（串行循环 n 次）
            stop: 停止 token 列表
            temperature: 温度参数（透传给 LlmClient.chat()）

        Returns:
            (List[LLMResponse], token_usage, reasoning_contents) 三元组

            reasoning_contents 与 results 一一对应等长；
            若底层 LlmClient 未提供 _last_reasoning_content（非思考模型或无 reasoning），
            对应位置填入空字符串 ""。
        """
        prompt = messages[-1]["content"] if messages else ""

        results: List[LLMResponse] = []
        reasoning_contents: List[str] = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_reasoning_tokens = 0
        has_real_usage = False  # 是否从 API 获取到了真实 usage

        for _ in range(max(1, n)):
            response_text = self._client.chat(prompt, stop=stop, temperature=temperature)
            results.append(LLMResponse(
                content=response_text,
                finish_reason=self._client._last_finish_reason,
            ))

            # 即时取出本次 chat 的 reasoning（每次 chat 后会被覆盖，必须在循环内取）
            reasoning_text = getattr(self._client, "_last_reasoning_content", "") or ""
            reasoning_contents.append(reasoning_text)

            # 优先使用 API 返回的真实 usage 数据
            usage_info = self._client._last_usage_info
            if usage_info:
                has_real_usage = True
                total_prompt_tokens += usage_info.get("prompt_tokens", 0)
                total_completion_tokens += usage_info.get("completion_tokens", 0)
                # 提取 reasoning_tokens（OpenRouter 在 completion_tokens_details 中返回）
                comp_details = usage_info.get("completion_tokens_details") or {}
                total_reasoning_tokens += comp_details.get("reasoning_tokens", 0)
            else:
                # fallback: 字符估算（API 未返回 usage 时）
                total_prompt_tokens += int(len(prompt) / 3)
                total_completion_tokens += int(len(response_text or "") / 3)

        # 构建 token_usage（向后兼容：保留 input_tokens/output_tokens，新增 reasoning 分类）
        content_tokens = total_completion_tokens - total_reasoning_tokens
        token_usage = {
            "input_tokens": total_prompt_tokens,
            "output_tokens": total_completion_tokens,
            "reasoning_tokens": total_reasoning_tokens,
            "content_tokens": content_tokens,
        }

        return results, token_usage, reasoning_contents
