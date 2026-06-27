import json
import logging
import time
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass

import requests
import urllib3

try:
    from .. import config
except ImportError:
    import config

# 全局禁用 InsecureRequestWarning（避免每次请求刷屏）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


@dataclass
class ChatCompletionMessage:
    """模拟 openai.types.chat.ChatCompletionMessage"""
    content: str
    role: str = "assistant"


class LlmClient:
    """
    LLM 客户端（标准模式，OpenAI 兼容 API）

    - 流式响应处理
    - 指数退避重试
    - <think> 标签清理（兼容 DeepSeek-R1 等推理模型）
    - 代码块标记清理
    """

    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str = "",
        max_retries: int = None,
        retry_delay: int = None,
        backoff_multiplier: int = None,
        timeout: Union[Tuple[int, int], int, None] = None,
        temperature: float = None,
        max_tokens: int = None,
        verify_ssl: bool = True,
        extra_body: Optional[Dict] = None,
    ):
        """
        初始化 LLM 客户端

        Args:
            api_base: LLM API 基地址（OpenAI 兼容端点，如 https://api.deepseek.com/v1/chat/completions）
            model: 模型名称（如 deepseek-chat）
            api_key: API Key
            temperature: 模型温度
            max_retries: 最大重试次数
            retry_delay: 初始重试延迟（秒）
            backoff_multiplier: 退避乘数
            timeout: 请求超时，支持 (connect, read) 元组或单个值
            verify_ssl: 是否验证 SSL 证书（内部网络环境可设为 False）
            extra_body: 额外请求体字段（如 cache_control），会 merge 到 JSON body 中
        """
        self._url = api_base.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._max_retries = max_retries if max_retries is not None else config.LLM_MAX_RETRIES
        self._retry_delay = retry_delay if retry_delay is not None else config.LLM_RETRY_DELAY
        self._backoff_multiplier = backoff_multiplier if backoff_multiplier is not None else config.LLM_BACKOFF_MULTIPLIER
        normalized_timeout = timeout if timeout is not None else config.LLM_TIMEOUT
        if isinstance(normalized_timeout, list):
            normalized_timeout = tuple(normalized_timeout)
        self._timeout = normalized_timeout
        self._temperature = float(temperature if temperature is not None else config.LLM_TEMPERATURE)
        self._max_tokens = max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS
        self._verify_ssl = verify_ssl
        self._extra_body = extra_body or {}
        self._last_finish_reason = None  # 最近一次 LLM 调用的 finish_reason
        self._last_reasoning_content = None  # 最近一次 LLM 调用的 reasoning_content（DeepSeek thinking mode）
        self._last_usage_info = None  # 最近一次 LLM 调用的原始 usage 信息（含 reasoning_tokens）
        if not self._verify_ssl:
            logger.warning("SSL certificate verification is DISABLED. Use only in trusted networks.")
        logger.info(f"LlmClient initialized: model={self._model}, url={self._url}")

    @property
    def last_reasoning_content(self) -> Optional[str]:
        return self._last_reasoning_content

    @classmethod
    def _clean_response(cls, content: str) -> str:
        """
        清理 LLM 响应内容

        - 去除 <think>...</think> 推理过程标签
        - 去除 Markdown 代码块标记（```、```json、```python 等）

        Args:
            content: 原始响应内容

        Returns:
            清理后的内容
        """
        if not content:
            logger.debug("Received empty response from LLM")
            return ""

        # 去除 <think> 标签（DeepSeek-R1 等推理模型会返回思考过程）
        if "</think>" in content:
            logger.debug("Removing <think> tags and reasoning content")
            content = content.split("</think>", 1)[1]

        # 去除 Markdown 代码块标记（支持 ```、```json、```python 等任意格式）
        content = content.strip()
        if content.startswith("```"):
            # 移除开头的 ```xxx（例如 ```json、```python 或单独的 ```）
            lines = content.split("\n", 1)
            if len(lines) > 1:
                content = lines[1]  # 跳过第一行的 ```xxx
            else:
                content = ""  # 只有 ``` 没有内容
        
        if content.endswith("```"):
            # 移除结尾的 ```
            content = content.rsplit("```", 1)[0]

        return content.strip()

    def chat(self, prompt: str, stop: Optional[List[str]] = None,
             temperature: float = None) -> str:
        """
        发送请求并获取响应（带指数退避重试）
        
        支持对以下情况重试：
        - 网络错误、超时等异常

        Args:
            prompt: 输入的提示文本
            stop: 可选的停止 token 列表
            temperature: 可选温度参数，None 时使用初始化默认值

        Returns:
            LLM 返回的响应文本（已清理）

        Raises:
            LLMMaxRetriesExceeded: 超过最大重试次数后抛出
        """
        last_exception = None
        delay = self._retry_delay

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._call_llm(prompt, stop=stop, temperature=temperature)
                logger.debug(f"LLM response ({len(response)} chars):\n{response}")
                return response
                
            except Exception as e:
                last_exception = e
                logger.warning(
                    f"LLM call failed (attempt {attempt}/{self._max_retries}): {e}. "
                    f"{'Retrying...' if attempt < self._max_retries else 'Max retries reached'}"
                )
                if attempt < self._max_retries:
                    time.sleep(delay)
                    delay *= self._backoff_multiplier

        logger.error(f"LLM call failed after {self._max_retries} retries")
        from . import LLMMaxRetriesExceeded
        raise LLMMaxRetriesExceeded(f"LLM max retries ({self._max_retries}) exceeded: {last_exception}")

    def _call_llm(self, prompt: str, stop: Optional[List[str]] = None,
                  temperature: float = None) -> str:
        """
        标准模式调用（OpenAI 兼容 API，流式响应）

        Args:
            prompt: 提示文本
            stop: 可选的停止 token 列表
            temperature: 可选温度参数，None 时使用初始化默认值

        Returns:
            清理后的完整响应内容
        """
        eff_temperature = temperature if temperature is not None else self._temperature

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        data = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": eff_temperature,
            "stream": True,
            "max_tokens": self._max_tokens,
        }
        if stop:
            data["stop"] = stop
        if self._extra_body:
            data.update(self._extra_body)

        logger.debug("Sending streaming request to %s, model is %s, temperature is %s, max_tokens is %s, extra_body is %s",
                     self._url, self._model, eff_temperature, self._max_tokens, self._extra_body)
        response = requests.post(
            self._url,
            headers=headers,
            data=json.dumps(data),
            stream=True,
            timeout=self._timeout,
            verify=self._verify_ssl,
        )
        response.raise_for_status()

        # 使用 try/finally 确保流式响应连接一定被释放回连接池
        # stream=True 时若不调用 response.close()，遇到 break 退出循环后
        # TCP 连接会滞留 CLOSE_WAIT/TIME_WAIT，大规模测试时导致连接耗尽
        content = ""
        reasoning_content = ""
        usage_info = None
        finish_reason = None  # 记录流式响应的终止原因（stop/length/...）
        try:
            for line in response.iter_lines():
                if not line:
                    continue

                # 安全解码：处理非 UTF-8 编码
                try:
                    decoded_line = line.decode("utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"Failed to decode line: {e}")
                    continue

                if not decoded_line.startswith("data:"):
                    continue

                data_str = decoded_line[5:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    json_data = json.loads(data_str)
                    if "choices" in json_data and len(json_data["choices"]) > 0:
                        choice = json_data["choices"][0]
                        delta = choice.get("delta", {})
                        if "reasoning_content" in delta and delta["reasoning_content"] is not None:
                            reasoning_content += delta["reasoning_content"]
                        # 采集 finish_reason（通常在最后一个 choice chunk 中）
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        if "content" in delta and delta["content"] is not None:
                            content += delta["content"]
                    if "reasoning_content" in json_data and json_data["reasoning_content"] is not None:
                        reasoning_content += json_data["reasoning_content"]
                    # 捕获 usage 信息（通常在流式响应的最后一个 chunk 中）
                    if "usage" in json_data:
                        usage_info = json_data["usage"]
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed JSON line: %s", data_str[:100])
                    continue
        finally:
            # 无论正常结束还是 break/异常，都显式关闭连接，归还连接池
            response.close()

        # 记录 token usage 和缓存命中信息（DEBUG 级别）
        if usage_info:
            prompt_tokens = usage_info.get("prompt_tokens", 0)
            completion_tokens = usage_info.get("completion_tokens", 0)
            details = usage_info.get("prompt_tokens_details", {})
            cached = details.get("cached_tokens", 0)
            cache_write = details.get("cache_write_tokens", 0)
            # 记录 reasoning tokens（OpenRouter 在 completion_tokens_details 中返回）
            comp_details = usage_info.get("completion_tokens_details", {})
            reasoning_tokens = comp_details.get("reasoning_tokens", 0)
            if reasoning_tokens:
                logger.debug(
                    "Token usage: prompt=%d, completion=%d (reasoning=%d, content=%d), cached=%d",
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    completion_tokens - reasoning_tokens, cached,
                )
            else:
                logger.debug(
                    "Token usage: prompt=%d, completion=%d, cached=%d, cache_write=%d",
                    prompt_tokens, completion_tokens, cached, cache_write,
                )
            if cached > 0:
                logger.debug("Cache HIT: %d/%d prompt tokens from cache (%.0f%%)",
                             cached, prompt_tokens, cached / prompt_tokens * 100 if prompt_tokens else 0)
            elif cache_write > 0:
                logger.debug("Cache WRITE: %d tokens written to cache (will be available for next request)",
                             cache_write)

        # 记录 finish_reason（stop=正常截断, length=token 耗尽, 其他=异常）
        # 上层 generator/validator/selector 已有带 question_id 上下文的 WARNING，
        # 此处保留 DEBUG 级别供底层诊断
        self._last_finish_reason = finish_reason
        self._last_reasoning_content = reasoning_content or None
        self._last_usage_info = usage_info  # 暴露给 LLMAdapter 使用真实 token 统计
        if finish_reason == "length":
            logger.debug("Finish reason: length (token limit reached, possible thinking death loop)")
        elif finish_reason:
            logger.debug("Finish reason: %s", finish_reason)
        else:
            logger.debug("Finish reason not received (stream may have been interrupted)")

        logger.debug("Received response length: %s chars", len(content))
        return self._clean_response(content)


class LLMAdapter:
    """
    封装 LlmClient
    当 n > 1 时，循环调用 n 次独立请求
    """

    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str = "",
        max_retries: int = None,
        retry_delay: int = None,
        backoff_multiplier: int = None,
        timeout: Union[Tuple[int, int], int, None] = None,
        temperature: float = None,
        max_tokens: int = None,
        verify_ssl: bool = True,
        extra_body: Optional[Dict] = None,
    ):
        llm_api_base, llm_model, llm_api_key, llm_extra = config.get_llm_config()
        self._client = LlmClient(
            api_base=api_base, model=model, api_key=api_key,
            max_retries=max_retries, retry_delay=retry_delay, backoff_multiplier=backoff_multiplier,
            timeout=timeout, temperature=temperature, max_tokens=max_tokens,
            verify_ssl=verify_ssl, extra_body=extra_body or llm_extra,
        )

    def ask(
        self,
        prompt: str,
        *,
        num_samples: int = 1,
        stop: Optional[List[str]] = None,
    ) -> Tuple[List[str], Dict[str, int]]:
        """
        LLM chat封装接口

        Args:
            prompt: 完整 prompt 文本
            num_samples: 采样次数（等价旧参数 n）
            stop: 可选 stop tokens

        Returns:
            (List[str], Dict[str, int])
        """
        n = max(1, int(num_samples or 1))

        results: List[str] = []
        for _ in range(n):
            response_text = self._client.chat(prompt, stop=stop)
            results.append(response_text)

        # 简化 token 统计（流式 API 无法获取精确 token 数）
        # 估算 token 数量：大约 1 token = 4 characters for English, 1 token = 1.5 characters for Chinese
        prompt_length = len(prompt)
        completion_length = sum(len(result or "") for result in results)
        estimated_prompt_tokens = int(prompt_length / 3)
        estimated_completion_tokens = int(completion_length / 3)
        
        token_usage = {
            "prompt_tokens": estimated_prompt_tokens,
            "completion_tokens": estimated_completion_tokens,
            "total_tokens": estimated_prompt_tokens + estimated_completion_tokens
        }
        return results, token_usage
