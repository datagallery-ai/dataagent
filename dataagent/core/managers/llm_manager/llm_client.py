# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
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
"""仅依赖 httpx 的定制 chat client —— 专为 dashscope / deepseek 适配（无 litellm 依赖）。

- 平台差异全部由 ``extra_body`` 原样透传承担（遵守各平台自身参数规则）。
- **``params``**：``model`` / ``base_url`` / ``api_key`` / ``timeout`` / ``num_retries`` 单独解析，
  其余自动透传进 ``extra_body``。
- **cache_control**：支持显式缓存的模型（Qwen/Claude/百炼 deepseek-v3.2 等）由
  ``_apply_cache_control_with_anchors`` 注入 bp0-bp4 断点；不支持的模型由 ``_strip_cache_control``
  剥离预置 cc（替代 litellm 的 monkey-patch，见设计文档 §1.7）。
- **缓存命中指标**：``_usage_to_metadata`` 调用 ``_extract_detail_tokens_from_dict`` 提取
  OpenAI/Anthropic/DeepSeek 三种格式的 cache_read/cache_creation/reasoning 子 token 字段。

错误统一映射为 :class:`LLMCallError`；薄层对 5xx / 连接 / 429 / timeout 重试。
``astream`` 建流与读流共用同一重试计数；读流超时/断连及无 ``finish_reason`` 的静默中断可重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import random
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from loguru import logger

from dataagent.common_utils.outbound_tls import httpx_verify
from dataagent.core.managers.llm_manager.llm_config import LLMConfig
from dataagent.utils.constants import (
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_COMPRESS_TOKEN_LIMIT,
    DEFAULT_LLM_MAX_RETRIES,
)

# ── 重试退避常量（定制模块自包含，不外置到 constants）────────────────────────────
DEFAULT_LLM_RETRY_BACKOFF_BASE: float = 3.0
"""重试退避基数（秒）：第 n 次重试前等待约 base * 2**(n-1)，再叠加满抖动 jitter。"""

DEFAULT_LLM_RETRY_BACKOFF_MAX: float = 300.0
"""重试退避上限（秒）：单次等待时间封顶，避免指数退避无限增长。"""

# ── 重复检测常量 ────────────────────────────────────────────────────────────────
DEFAULT_REPETITION_DETECTION_ENABLED: bool = True
"""是否启用 LLM 输出重复检测。"""

DEFAULT_REPETITION_NGRAM_WINDOW: int = 20
"""n-gram 重复检测的窗口大小（token 数）。"""

DEFAULT_REPETITION_NGRAM_MAX_REPEAT: int = 40
"""同一 n-gram 出现频次的最大容忍值，超过则判定为重复。"""

DEFAULT_REPETITION_CHAR_CYCLE_MIN: int = 50
"""字符级循环检测的最小连续重复次数阈值。"""

DEFAULT_REPETITION_CHAR_DIVERSITY_MIN: float = 0.02
"""字符多样性的最低阈值，len(set(text))/len(text) 低于此值判定为重复。"""

DEFAULT_REPETITION_TOOL_CALL_MAX_REPEAT: int = 2
"""同一 tool call 在单次模型输出中重复出现的最大容忍值，超过则判定为重复。"""

DEFAULT_REPETITION_TOOL_CALL_MIN_TEXT_LEN: int = 100
"""tool call 参数文本达到该长度后才启用复杂重复检测，降低短参数误报。"""


# ── cache_control 断点策略常量 ──────────────────────────────────────────────────
_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}
_MAX_BREAKPOINTS = 4
_MIN_SPACING_CHARS = 3072
_MIN_TOOL_CONTENT_CHARS = 512
_CACHE_COMPRESS_APPROACH_RATIO = 0.8

# 百炼平台支持显式缓存的具体模型版本（来源：DashScope 官方文档，需定期同步）
# https://help.aliyun.com/zh/model-studio/context-cache
_BAILIAN_EXPLICIT_CACHE_MODELS: frozenset[str] = frozenset(
    {
        "deepseek-v3.2",
        "kimi-k2.6",
        "kimi-k2.5",
        "glm-5.1",
    }
)


def _supports_explicit_cache_control(model: str, provider: str | None = None) -> bool:
    """Whether the model/provider accepts explicit cache_control markers.

    判断依据（来源：各厂商官方文档，见设计文档 §1.3.2）：
    1. Qwen/QwQ — 按模型名匹配（任何 provider 下只要是 Qwen 模型就支持）
    2. Anthropic Claude — 按 provider 或模型名匹配
    3. 百炼平台上的非 Qwen 模型 — 按模型名精确匹配 _BAILIAN_EXPLICIT_CACHE_MODELS
    """
    m = model.lower()
    p = (provider or "").lower()
    if "qwen" in m or "qwq" in m:
        return True
    if "claude" in m or p == "anthropic":
        return True
    return m in _BAILIAN_EXPLICIT_CACHE_MODELS


def _apply_cache_defaults(params: dict[str, Any]) -> None:
    """兼容 flex_runtime_from_config 的导入；自实现客户端不再需要 litellm 的 custom_llm_provider。"""
    params.pop("custom_llm_provider", None)


def _strip_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剥离消息中预置的 cache_control（用于不支持显式缓存的模型，避免 API 报错）。

    自实现客户端 httpx 原样发送 body，不像 litellm 会自动剥离 cache_control。
    对于不支持显式缓存的模型，session restore 等路径带入的 cache_control 需在此清除。
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for part in content:
                if isinstance(part, dict):
                    part = {k: v for k, v in part.items() if k != "cache_control"}
                    if part:
                        new_content.append(part)
                else:
                    new_content.append(part)
            msg["content"] = new_content
    return messages


def _safe_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _safe_assign(obj: Any, attr: str, target: dict[str, Any], target_key: str) -> None:
    """Safely read *attr* from *obj* and store the int in *target[target_key]*."""
    target[target_key] = _safe_int(getattr(obj, attr, None))


def _extract_detail_tokens(usage: Any, target: dict[str, Any]) -> None:
    """从 OpenAI/Anthropic/DeepSeek 的 usage 对象提取缓存与推理子 token 字段。

    覆盖三种格式：
    1. OpenAI/DeepSeek: ``prompt_tokens_details.cached_tokens`` / ``cache_creation_tokens``
    2. Anthropic: ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` (flat)
    3. DeepSeek fallback: ``prompt_cache_hit_tokens`` (顶层)
    """
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details:
        target["input_cache_read_tokens"] = _safe_int(getattr(prompt_details, "cached_tokens", None))
        target["input_cache_creation_tokens"] = _safe_int(
            getattr(prompt_details, "cache_creation_tokens", None)
            or getattr(prompt_details, "cache_creation_input_tokens", None)
        )
    else:
        _safe_assign(usage, "cache_read_input_tokens", target, "input_cache_read_tokens")
        _safe_assign(usage, "cache_creation_input_tokens", target, "input_cache_creation_tokens")

    if not target.get("input_cache_read_tokens"):
        target["input_cache_read_tokens"] = _safe_int(getattr(usage, "prompt_cache_hit_tokens", None))

    completion_details = getattr(usage, "completion_tokens_details", None)
    if completion_details:
        target["output_reasoning_tokens"] = _safe_int(getattr(completion_details, "reasoning_tokens", None))
    else:
        _safe_assign(usage, "reasoning_tokens", target, "output_reasoning_tokens")


def _extract_detail_tokens_from_dict(usage: dict, target: dict[str, Any]) -> None:
    """从 dict 形式的 usage 提取缓存与推理子 token 字段（流式 / httpx JSON 路径）。

    覆盖三种格式（同 :func:`_extract_detail_tokens`），但操作对象是 dict 而非 Pydantic 模型。
    """
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        target["input_cache_read_tokens"] = _safe_int(prompt_details.get("cached_tokens"))
        target["input_cache_creation_tokens"] = _safe_int(
            prompt_details.get("cache_creation_tokens") or prompt_details.get("cache_creation_input_tokens")
        )
    else:
        target["input_cache_read_tokens"] = _safe_int(usage.get("cache_read_input_tokens"))
        target["input_cache_creation_tokens"] = _safe_int(usage.get("cache_creation_input_tokens"))

    if not target.get("input_cache_read_tokens"):
        target["input_cache_read_tokens"] = _safe_int(usage.get("prompt_cache_hit_tokens"))

    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        target["output_reasoning_tokens"] = _safe_int(completion_details.get("reasoning_tokens"))
    else:
        target["output_reasoning_tokens"] = _safe_int(usage.get("reasoning_tokens"))


class LLMErrorCategory(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    CONTEXT_WINDOW = "context_window"
    CONTENT_POLICY = "content_policy"
    BAD_REQUEST = "bad_request"
    SERVER_ERROR = "server_error"
    RESPONSE_INVALID = "response_invalid"
    STREAM_INTERRUPTED = "stream_interrupted"
    REPETITION_DETECTED = "repetition_detected"
    UNKNOWN = "unknown"


class LLMCallError(Exception):
    """单一 LLM 失败出口；可直接 raise/except，无需再剥底层异常类型。"""

    def __init__(
        self,
        category: LLMErrorCategory,
        message: str,
        *,
        app_recoverable: bool = False,
        model: str | None = None,
        request_url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.app_recoverable = app_recoverable
        self.model = model
        self.request_url = request_url
        self.status_code = status_code

    def __str__(self) -> str:
        label = f"[{self.status_code} {self.category}]" if self.status_code is not None else f"[{self.category}]"
        meta: list[str] = []
        if self.model:
            meta.append(f"model={self.model}")
        if self.request_url:
            meta.append(f"url={self.request_url}")
        suffix = f" ({' '.join(meta)})" if meta else ""
        return f"{label} {self.message}{suffix}"


# 4xx 中按类别区分；其余 4xx 统一归 BAD_REQUEST
_STATUS_CATEGORY: dict[int, LLMErrorCategory] = {
    400: LLMErrorCategory.BAD_REQUEST,
    401: LLMErrorCategory.AUTH,
    403: LLMErrorCategory.AUTH,
    404: LLMErrorCategory.NOT_FOUND,
    429: LLMErrorCategory.RATE_LIMIT,
}


class LLMRepetitionError(Exception):
    """LLM 输出重复检测异常 — 可触发带退避的重试，独立于传输层错误。"""

    def __init__(
        self,
        detection_type: str,
        detail: str,
        *,
        content_snippet: str = "",
        model: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.category = LLMErrorCategory.REPETITION_DETECTED
        self.detection_type = detection_type
        self.detail = detail
        self.content_snippet = content_snippet[:500]
        self.model = model
        self.app_recoverable = True

    def __str__(self) -> str:
        return f"[{self.category} {self.detection_type}] {self.detail}"


# 可重试类别：5xx / 连接 / 429 / timeout / repetition
_RETRYABLE_CATEGORIES = frozenset(
    {
        LLMErrorCategory.SERVER_ERROR,
        LLMErrorCategory.CONNECTION,
        LLMErrorCategory.RATE_LIMIT,
        LLMErrorCategory.TIMEOUT,
        LLMErrorCategory.REPETITION_DETECTED,
    }
)

# 流式读流阶段额外可重试：无 finish_reason 的静默中断
_STREAM_RETRYABLE_CATEGORIES = _RETRYABLE_CATEGORIES | frozenset({LLMErrorCategory.STREAM_INTERRUPTED})


def _should_retry_stream_error(mapped: LLMCallError, *, attempt: int, max_attempts: int) -> bool:
    """流式建流/读流是否在抛出 ``LLMCallError`` 前继续重试（对齐 litellm 版 ``astream``）。"""
    if attempt >= max_attempts:
        return False
    return mapped.category in _STREAM_RETRYABLE_CATEGORIES


def _retry_backoff_seconds(attempt: int) -> float:
    """第 ``attempt`` 次失败后、下一次重试前的等待时长（秒）。

    指数退避 + 满抖动（full jitter）：在 ``[0, min(cap, base * 2**attempt)]`` 内随机取值，
    既能在限流/瞬时故障时拉开重试间隔，又能打散并发客户端的重试时刻避免惊群。

    Args:
        attempt: 已失败的次数（0 表示第一次调用失败后、首个重试前）。

    Returns:
        本次重试前应 sleep 的秒数。
    """
    ceiling = min(DEFAULT_LLM_RETRY_BACKOFF_MAX, DEFAULT_LLM_RETRY_BACKOFF_BASE * (2**attempt))
    return random.uniform(0, ceiling)


def _category_for_status(status_code: int) -> LLMErrorCategory:
    if status_code in _STATUS_CATEGORY:
        return _STATUS_CATEGORY[status_code]
    if 500 <= status_code < 600:
        return LLMErrorCategory.SERVER_ERROR
    if 400 <= status_code < 500:
        return LLMErrorCategory.BAD_REQUEST
    return LLMErrorCategory.UNKNOWN


def _is_httpcore_read_error(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "ReadError" and exc.__class__.__module__.split(".", 1)[0] == "httpcore"


def _extract_http_error_message(exc: httpx.HTTPStatusError) -> str:
    """尽量从响应体里取出平台返回的 error.message，便于排障。

    流式响应（``stream=True``）在 ``aread()`` 之前访问 ``content``/``text``/``json``
    会抛 ``httpx.ResponseNotRead``；此处吞掉该情况并回退到状态码，避免错误信息提取
    反而盖掉真实的 HTTP 错误（如 401）。
    """
    resp = exc.response
    try:
        data = resp.json()
    except httpx.ResponseNotRead:
        return f"HTTP {resp.status_code}"
    except Exception:
        try:
            text = (resp.text or "").strip()
        except httpx.ResponseNotRead:
            return f"HTTP {resp.status_code}"
        return text or f"HTTP {resp.status_code}"
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
        if data.get("message"):
            return str(data["message"])
    return f"HTTP {resp.status_code}"


def map_httpx_exception(
    exc: BaseException,
    *,
    model: str | None = None,
    api_base: str | None = None,
) -> LLMCallError:
    """将 httpx（或其它）异常映射为 ``LLMCallError``；已是 ``LLMCallError`` 则原样返回。"""
    if isinstance(exc, LLMCallError):
        return exc

    request_url = f"{api_base.rstrip('/')}/chat/completions" if api_base else None

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        category = _category_for_status(status_code)
        return LLMCallError(
            category,
            _extract_http_error_message(exc),
            model=model,
            request_url=request_url,
            status_code=status_code,
        )

    if isinstance(exc, httpx.TimeoutException):
        category = LLMErrorCategory.TIMEOUT
    elif isinstance(exc, (httpx.ConnectError, httpx.TransportError)) or _is_httpcore_read_error(exc):
        category = LLMErrorCategory.CONNECTION
    else:
        category = LLMErrorCategory.UNKNOWN

    return LLMCallError(
        category,
        str(exc) or type(exc).__name__,
        model=model,
        request_url=request_url,
    )


def _llm_config_for_adapter(cfg: dict[str, Any], logical_name: str) -> LLMConfig:
    """供 ``LangChainChatModelAdapter`` 使用；适配器读取 logical name 等元数据。"""
    return LLMConfig(
        name=logical_name,
        provider="openai",
        model_type="chat",
        section=logical_name,
        params={},
    )


@dataclass
class LLMClientMessage:
    """单次模型输出，字段对齐 adapter :meth:`LangChainChatModelAdapter._wrap_output`。"""

    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    invalid_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_blocks: list[dict[str, Any]] | None = None
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


def _resolve_core_endpoint(
    *,
    name: str,
    provider_env: str,
    param_base_url: Any,
    param_api_key: Any,
) -> tuple[str, str]:
    """解析 api_base / api_key：优先显式参数，回退环境变量 ``{PROVIDER}_BASE_URL`` / ``{PROVIDER}_API_KEY``。"""
    if param_base_url and str(param_base_url).strip():
        api_base = str(param_base_url).strip()
    else:
        api_base = os.getenv(f"{provider_env}_BASE_URL")
    if not api_base:
        raise ValueError(
            f"Missing URL for '{name}'. Set MODEL.{name}.params.base_url or {provider_env}_BASE_URL in .env."
        )

    if param_api_key and str(param_api_key).strip():
        api_key = str(param_api_key).strip()
    else:
        api_key = os.getenv(f"{provider_env}_API_KEY")
    if not api_key:
        raise ValueError(
            f"Missing API key for '{name}'. Set MODEL.{name}.params.api_key or {provider_env}_API_KEY in .env."
        )
    return str(api_base), str(api_key)


# ── 重复检测辅助函数 ────────────────────────────────────────────────────────────


def _is_cjk(ch: str) -> bool:
    """判断单个字符是否属于 CJK 统一表意文字或日韩音节（每个字符独立分词）。"""
    cp = ord(ch)
    return (
        (0x4E00 <= cp <= 0x9FFF)  # CJK Unified Ideographs
        or (0x3400 <= cp <= 0x4DBF)  # CJK Unified Ideographs Extension A
        or (0xF900 <= cp <= 0xFAFF)  # CJK Compatibility Ideographs
        or (0x3040 <= cp <= 0x309F)  # Hiragana
        or (0x30A0 <= cp <= 0x30FF)  # Katakana
        or (0xAC00 <= cp <= 0xD7AF)  # Hangul Syllables
    )


def _tokenize_simple(text: str) -> list[str]:
    """按空白/标点边界做简易分词，CJK 字符逐字切分。"""
    tokens: list[str] = []
    current: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            if current:
                tokens.append("".join(current))
                current = []
            tokens.append(ch)
        elif ch.isalnum():
            current.append(ch)
        else:
            if current:
                tokens.append("".join(current))
                current = []
            if ch.strip():
                tokens.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def _detect_ngram_repetition(
    text: str,
    *,
    window: int = DEFAULT_REPETITION_NGRAM_WINDOW,
    max_repeat: int = DEFAULT_REPETITION_NGRAM_MAX_REPEAT,
) -> tuple[bool, str | None]:
    """检测 token n-gram 级别的重复：统计所有 n-gram 频次，任一超过阈值即触发。"""
    tokens = _tokenize_simple(text)
    if len(tokens) < window:
        return False, None

    freq: dict[tuple[str, ...], int] = {}

    for i in range(len(tokens) - window + 1):
        ngram = tuple(tokens[i : i + window])
        count = freq.get(ngram, 0) + 1
        freq[ngram] = count
        if count > max_repeat:
            rep = "".join(ngram)
            return True, (f"token_ngram_loop: ngram={rep} window={window} freq={count} text_len={len(text)}")

    return False, None


def _detect_char_cycle(
    text: str,
    *,
    cycle_min: int = DEFAULT_REPETITION_CHAR_CYCLE_MIN,
    diversity_min: float = DEFAULT_REPETITION_CHAR_DIVERSITY_MIN,
) -> tuple[bool, str | None]:
    """检测字符级别循环重复：多样性预过滤后在 2~20 字符窗内找最小重复单元。"""
    if len(text) < cycle_min:
        return False, None

    unique_chars = len(set(text))
    if unique_chars / max(len(text), 1) >= diversity_min:
        return False, None

    best_cycle = ""
    best_count = 0
    max_period = min(20, len(text) // 2)

    for period in range(2, max_period + 1):
        pattern = text[:period]
        count = 1
        pos = period
        while pos + period <= len(text) and text[pos : pos + period] == pattern:
            count += 1
            pos += period
        if count > best_count:
            best_count = count
            best_cycle = pattern

    if best_count < cycle_min // max(len(best_cycle), 1):
        return False, None

    return True, (
        f"char_cycle: pattern_len={len(best_cycle)} repeat={best_count} "
        f"snippet='{best_cycle[:100]}' text_len={len(text)}"
    )


def _detect_periodicity(
    text: str,
    *,
    min_repeats: int = 4,
    min_coverage: float = 0.7,
    max_period_cap: int = 2000,
) -> tuple[bool, str | None]:
    """检测文本是否由某个片段的多次重复构成。

    search_limit = min(len // min_repeats, max_period_cap)，每个候选周期用 ``startswith`` 验证。
    ``startswith`` 在首字符不匹配时立即返回，多数周期 O(1) 跳过。
    """
    n = len(text)
    search_limit = min(n // min_repeats, max_period_cap)
    for period in range(1, search_limit + 1):
        pattern = text[:period]
        covered = period
        pos = period
        while pos + period <= n and text.startswith(pattern, pos):
            covered += period
            pos += period
        repeats = covered // period
        if covered >= min_coverage * n and repeats >= min_repeats:
            return True, (
                f"periodicity: period={period} repeats={repeats} "
                f"coverage={covered / n:.2f} snippet='{pattern[:100]}' text_len={n}"
            )
    return False, None


def _detect_duplicate_message(
    current_content: str,
    previous_content: str,
    *,
    min_len: int = 100,
    similarity_threshold: float = 0.99,
) -> tuple[bool, str | None]:
    """检测输出与上一条 assistant 消息完全相同或高度相似。"""
    if not current_content or not previous_content:
        return False, None
    if len(current_content) < min_len or len(previous_content) < min_len:
        return False, None
    if current_content == previous_content:
        return True, "duplicate_message: content identical to previous assistant message"
    similarity = difflib.SequenceMatcher(None, current_content, previous_content).ratio()
    if similarity >= similarity_threshold:
        return True, f"duplicate_message: content similarity={similarity:.3f} to previous assistant message"
    return False, None


def _detect_repetition(
    content: str,
    *,
    previous_content: str = "",
    enabled: bool = DEFAULT_REPETITION_DETECTION_ENABLED,
) -> tuple[bool, str | None]:
    """组合检测：n-gram 重复 + 周期性 + 字符循环 + 消息重复。"""
    if not enabled or not content:
        return False, None
    if len(content) < 20:
        return False, None

    is_rep, detail = _detect_ngram_repetition(content)
    if is_rep:
        return True, detail

    is_rep, detail = _detect_periodicity(content)
    if is_rep:
        return True, detail

    is_rep, detail = _detect_char_cycle(content)
    if is_rep:
        return True, detail

    if previous_content:
        is_dup, detail = _detect_duplicate_message(content, previous_content)
        if is_dup:
            return True, detail

    return False, None


class _AStreamState:
    __slots__ = (
        "by_index",
        "finish_reason",
        "received_meaningful",
        "content_parts",
        "reasoning_parts",
        "chunk_count",
        "has_interrupted",
    )

    def __init__(self) -> None:
        self.by_index: dict[int, dict[str, str]] = {}
        self.finish_reason: str = ""
        self.received_meaningful: bool = False
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.chunk_count: int = 0
        self.has_interrupted: bool = False


class LLMClient:
    """httpx 直连 OpenAI 兼容 ``/chat/completions``；工具调用走 OpenAI native tools API。"""

    def __init__(
        self,
        *,
        model: str,
        api_base: str,
        api_key: str,
        tools: list[Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        num_retries: int | None = None,
        provider: str | None = None,
        compress_token_limit: int | None = None,
        compress_message_cnt: int | None = None,
    ) -> None:
        """初始化 LLMClient 实例。

        Args:
            model: 模型名（如 ``qwen-plus`` / ``deepseek-chat``）。
            api_base: OpenAI 兼容根地址（拼接 ``/chat/completions``）。
            api_key: Bearer token。
            tools: 绑定的工具列表（OpenAI tools 形态或可转换对象）。
            extra_body: 原样透传进请求体的扩展参数（``temperature`` / ``enable_thinking`` 等）。
            timeout: httpx 请求超时（秒）；``None`` 用 httpx 默认。
            num_retries: 5xx/连接/429/timeout 的重试次数；``None`` 用默认值。
            provider: 模型供应商标识，用于判断是否支持显式 cache_control。
            compress_token_limit: 实际压缩 token 阈值（由 runtime.env 注入），影响 approaching_compress 判断。
            compress_message_cnt: 实际压缩消息数阈值（由 runtime.env 注入），影响 approaching_compress 判断。
        """
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._tools: list[Any] = list(tools) if tools else []
        self._extra_body: dict[str, Any] = dict(extra_body) if extra_body else {}
        self._timeout = timeout
        self._num_retries = int(num_retries) if num_retries is not None else DEFAULT_LLM_MAX_RETRIES
        self._provider = provider
        self._compress_token_limit = compress_token_limit
        self._compress_message_cnt = compress_message_cnt

    @staticmethod
    def _extract_previous_assistant_content(messages: list[Any]) -> str:
        """从 messages 列表中提取最后一条 assistant 消息的 content。"""
        for msg in reversed(messages):
            if isinstance(msg, dict):
                if msg.get("role") == "assistant":
                    return str(msg.get("content", "") or "")
            else:
                role = getattr(msg, "type", "") or getattr(getattr(msg, "__class__", None), "__name__", "") or ""
                if role in ("ai", "assistant") or "AI" in str(role) or "Assistant" in str(role):
                    return str(getattr(msg, "content", "") or "")
        return ""

    @staticmethod
    def _extract_previous_assistant_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
        """从 messages 列表中提取最后一条 assistant 消息的 tool_calls。"""
        for msg in reversed(messages):
            if isinstance(msg, dict):
                if msg.get("role") == "assistant":
                    tool_calls = msg.get("tool_calls") or []
                    return tool_calls if isinstance(tool_calls, list) else []
            else:
                role = getattr(msg, "type", "") or getattr(getattr(msg, "__class__", None), "__name__", "") or ""
                if role in ("ai", "assistant") or "AI" in str(role) or "Assistant" in str(role):
                    tool_calls = getattr(msg, "tool_calls", None) or []
                    return tool_calls if isinstance(tool_calls, list) else []
        return []

    @staticmethod
    def _json_dumps_stable(value: Any) -> str:
        """将任意值稳定序列化，供重复检测比较使用。"""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    @staticmethod
    def _normalize_tool_call_for_repetition(tool_call: Any) -> str | None:
        """将不同形态的 tool call 归一为稳定字符串。"""
        if not isinstance(tool_call, dict):
            return None
        fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else None
        name = (fn or {}).get("name") if fn else tool_call.get("name")
        args = (fn or {}).get("arguments") if fn else tool_call.get("args", tool_call.get("arguments"))
        if not name and args is None:
            return None
        normalized = {
            "name": str(name or ""),
            "args": LLMClient._json_dumps_stable(args if args is not None else ""),
        }
        return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _normalize_tool_calls_for_repetition(tool_calls: list[dict[str, Any]]) -> str:
        """将 tool_calls 列表归一为可用于文本重复检测的字符串。"""
        parts = []
        for tool_call in tool_calls:
            normalized = LLMClient._normalize_tool_call_for_repetition(tool_call)
            if normalized:
                parts.append(normalized)
        return "\n".join(parts)

    @staticmethod
    def _detect_repeated_tool_call_items(tool_calls: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """检测同一 tool call 是否在单次响应中重复过多。"""
        freq: dict[str, int] = {}
        for tool_call in tool_calls:
            normalized = LLMClient._normalize_tool_call_for_repetition(tool_call)
            if not normalized:
                continue
            count = freq.get(normalized, 0) + 1
            freq[normalized] = count
            if count > DEFAULT_REPETITION_TOOL_CALL_MAX_REPEAT:
                return True, f"duplicate_tool_call: repeat={count} snippet={normalized[:200]}"
        return False, None

    @staticmethod
    def _detect_tool_call_repetition(tool_calls: list[dict[str, Any]]) -> tuple[bool, str | None, str]:
        """检测单次响应内部的 tool call repetition，并返回归一化片段。"""
        snippet = LLMClient._normalize_tool_calls_for_repetition(tool_calls)
        is_rep, detail = LLMClient._detect_repeated_tool_call_items(tool_calls)
        if is_rep:
            return True, detail, snippet
        if len(snippet) < DEFAULT_REPETITION_TOOL_CALL_MIN_TEXT_LEN:
            return False, None, snippet
        is_rep, detail = _detect_repetition(snippet, previous_content="")
        return is_rep, detail, snippet

    @staticmethod
    def _stream_tool_calls_for_repetition(by_index: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
        """将流式累计中的半成品 tool calls 转成可做重复检测的快照。"""
        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(by_index.keys()):
            part = by_index.get(idx, {})
            name = str(part.get("name") or "")
            arguments = str(part.get("arguments") or "")
            if not name and not arguments:
                continue
            tool_calls.append(
                {
                    "id": str(part.get("id") or "") or f"call_stream_{idx}",
                    "name": name,
                    "args": arguments,
                    "type": "tool_call",
                }
            )
        return tool_calls

    @staticmethod
    def _is_likely_incomplete_json(raw_args: Any, error: json.JSONDecodeError) -> bool:
        """判断流式参数解析失败是否更像截断/半包，而非完整但非法 JSON。"""
        if not isinstance(raw_args, str):
            return False
        stripped = raw_args.strip()
        if not stripped:
            return False
        if stripped in {"{", "["}:
            return True
        opens = stripped.count("{") + stripped.count("[")
        closes = stripped.count("}") + stripped.count("]")
        if opens > closes:
            return True
        if stripped.endswith((":", ",", '"', "\\")):
            return True
        return error.pos >= max(len(stripped) - 2, 0)

    @staticmethod
    def _finalize_stream_tool_calls_for_lc(
        by_index: dict[int, dict[str, str]],
        finish_reason: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将累积的 OpenAI 流式分片转为 LangChain AIMessage 可用的 tool_calls / invalid_tool_calls。"""
        out: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        finish_reason = finish_reason or ""
        for idx in sorted(by_index.keys()):
            part = by_index[idx]
            name = (part.get("name") or "").strip()
            if not name:
                logger.warning(
                    "stream.tool_call.missing_name index={} has_id={} args_len={}",
                    idx,
                    bool(part.get("id")),
                    len(str(part.get("arguments") or "")),
                )
                invalid.append(
                    {
                        "id": str(part.get("id") or "").strip() or f"call_stream_{idx}",
                        "name": "unknown",
                        "args": part.get("arguments") or "",
                        "error": "Streamed tool call is missing a function name",
                    }
                )
                continue
            raw_args = part.get("arguments") or "{}"
            tid = str(part.get("id") or "").strip() or f"call_stream_{idx}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError as e:
                truncated = finish_reason == "length"
                filtered = finish_reason == "content_filter"
                incomplete = truncated or LLMClient._is_likely_incomplete_json(raw_args, e)
                log_fn = logger.debug if incomplete else logger.warning
                log_fn(
                    "stream.tool_call.args_json_decode_failed index={} error_type={}"
                    " args_len={} has_name={} has_id={} suspected_incomplete={} finish_reason={}",
                    idx,
                    type(e).__name__,
                    len(raw_args) if isinstance(raw_args, str) else 0,
                    bool(name),
                    bool(part.get("id")),
                    incomplete,
                    finish_reason,
                )
                invalid.append(
                    {
                        "id": tid,
                        "name": name,
                        "args": raw_args,
                        "error": (
                            "Streamed tool arguments were truncated by the model output limit"
                            if truncated
                            else (
                                "Streamed tool arguments were blocked or truncated by the content filter"
                                if filtered
                                else (
                                    "Incomplete streamed tool arguments JSON"
                                    if incomplete
                                    else "Invalid streamed tool arguments JSON"
                                )
                            )
                        ),
                    }
                )
                continue
            except Exception as e:
                logger.exception(
                    "stream.tool_call.args_parse_failed index={} error_type={} raw_args_type={} has_name={} has_id={}",
                    idx,
                    type(e).__name__,
                    type(raw_args).__name__,
                    bool(name),
                    bool(part.get("id")),
                )
                invalid.append(
                    {
                        "id": tid,
                        "name": name,
                        "args": raw_args,
                        "error": f"Failed to parse streamed tool arguments: {type(e).__name__}",
                    }
                )
                continue
            if not isinstance(args, dict):
                logger.warning(
                    "stream.tool_call.args_not_object index={} args_type={} has_name={} has_id={}",
                    idx,
                    type(args).__name__,
                    bool(name),
                    bool(part.get("id")),
                )
                invalid.append(
                    {
                        "id": tid,
                        "name": name,
                        "args": args,
                        "error": "Streamed tool arguments must decode to object",
                    }
                )
                continue
            out.append({"id": tid, "name": name, "args": args, "type": "tool_call"})
        return out, invalid

    @staticmethod
    def _tool_calls_to_dicts(tool_calls: list[Any]) -> list[dict[str, Any]]:
        """将 tool_calls（dict 形态）规整为 OpenAI function 字典格式。"""
        result: list[dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
            name = (fn or {}).get("name") if fn else tc.get("name")
            args = (fn or {}).get("arguments") if fn else tc.get("arguments")
            tid = tc.get("id") or ""
            if name:
                result.append(
                    {
                        "id": str(tid),
                        "type": "function",
                        "function": {"name": str(name), "arguments": str(args or "")},
                    }
                )
        return result

    @staticmethod
    def _tools_to_openai(tools: list[Any]) -> list[dict[str, Any]]:
        """将工具列表转换为 OpenAI tools 描述字典格式。"""
        out: list[dict[str, Any]] = []
        for t in tools:
            if isinstance(t, dict) and (t.get("type") == "function" or "function" in t):
                out.append(t)
                continue
            name = getattr(t, "name", None) or getattr(t, "__name__", None)
            if not name:
                continue
            desc = str(getattr(t, "description", None) or "")
            params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            args_schema = getattr(t, "args_schema", None)
            if args_schema is not None:
                try:
                    params = args_schema.model_json_schema()  # type: ignore[attr-defined]
                except Exception:
                    with contextlib.suppress(Exception):
                        params = args_schema.schema()  # type: ignore[attr-defined]
            out.append(
                {
                    "type": "function",
                    "function": {"name": str(name), "description": desc, "parameters": params},
                }
            )
        return out

    @staticmethod
    def _lc_messages_to_dicts(messages: list[Any]) -> list[dict[str, Any]]:
        """将 LangChain Message 对象列表转换为 OpenAI dict 消息（委托 adapter，避免重复实现）。"""
        from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter

        converted = LangChainChatModelAdapter.messages_to_openai_dicts(messages)
        return converted if isinstance(converted, list) else messages

    @staticmethod
    def _apply_cache_control_with_anchors(
        messages: list[dict[str, Any]],
        *,
        compress_token_limit: int | None = None,
        compress_message_cnt: int | None = None,
    ) -> list[dict[str, Any]]:
        """Apply cache_control breakpoints (dynamic tail strategy).

        Breakpoint layout (max 4, priority bp0>bp1>bp3>bp2>bp4):
          bp0 — System (always set; ensures [0..0] cache entry survives compression)
          bp1 — history_summary (if present, largest stable prefix that survives compression)
          bp2 — first tool with spacing≥3072c AND content≥512c, falling back to
                tail2 position (only when no history_summary; bp1/bp2 mutually exclusive)
          bp3 — Todo前一条 (unconditional tail anchor, NOT gated by approaching_compress)
          bp4 — tail2 = Todo前第3条 (optional, skipped when approaching_compress)
        """
        use_tail_cc = os.getenv("DATAAGENT_CACHE_ANCHOR", "1") != "0"

        system_idx = None
        history_summary_idx = None
        tool_indices: list[int] = []
        todo_idx = None

        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "system" and system_idx is None:
                system_idx = i
            elif role == "user":
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                if text.startswith("# Work Plan Status"):
                    todo_idx = i
                if history_summary_idx is None:
                    additional_kwargs = msg.get("additional_kwargs") or {}
                    if additional_kwargs.get("_folded") is True or text.startswith("<history_summary>"):
                        history_summary_idx = i
            elif role == "tool":
                tool_indices.append(i)

        def _content_chars(content: Any) -> int:
            if isinstance(content, str):
                return len(content)
            if isinstance(content, list):
                return sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
            return 0

        def _has_explicit_cc(content: Any) -> bool:
            if not isinstance(content, list):
                return False
            return any(isinstance(p, dict) and "cache_control" in p for p in content)

        def _add_cc(msg: dict[str, Any]) -> None:
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                msg["content"] = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL_EPHEMERAL}]
            elif isinstance(content, list) and content:
                new_content = list(content)
                last = new_content[-1]
                if isinstance(last, dict) and "cache_control" not in last:
                    new_content[-1] = {**last, "cache_control": _CACHE_CONTROL_EPHEMERAL}
                msg["content"] = new_content

        system_chars = 0
        if system_idx is not None:
            system_chars += _content_chars(messages[system_idx].get("content"))

        eff_token_limit = compress_token_limit or DEFAULT_COMPRESS_TOKEN_LIMIT
        eff_message_cnt = compress_message_cnt or DEFAULT_COMPRESS_MESSAGE_CNT

        total_chars = sum(_content_chars(msg.get("content")) for msg in messages)
        estimated_tokens = total_chars // 4
        approaching_compress = (
            len(messages) >= _CACHE_COMPRESS_APPROACH_RATIO * eff_message_cnt
            or estimated_tokens >= _CACHE_COMPRESS_APPROACH_RATIO * eff_token_limit
        )

        bp0_idx: int | None = system_idx
        bp1_idx: int | None = None
        bp2_idx: int | None = None
        bp3_idx: int | None = None
        bp4_idx: int | None = None

        if history_summary_idx is not None:
            bp1_idx = history_summary_idx

        if use_tail_cc and todo_idx is not None and todo_idx > (system_idx or 0):
            bp3_idx = todo_idx - 1

            if not approaching_compress:
                if bp1_idx is None:
                    cumulative = 0
                    for i, msg in enumerate(messages):
                        cumulative += _content_chars(msg.get("content"))
                        if i in tool_indices:
                            spacing = cumulative - system_chars
                            tool_chars = _content_chars(msg.get("content"))
                            if spacing >= _MIN_SPACING_CHARS and tool_chars >= _MIN_TOOL_CONTENT_CHARS:
                                bp2_idx = i
                                break

                tail2_idx = todo_idx - 3
                if (
                    bp2_idx is not None
                    and bp2_idx != tail2_idx
                    and tail2_idx > (system_idx or 0)
                    and tail2_idx != bp3_idx
                    and bp2_idx != history_summary_idx
                ):
                    bp4_idx = tail2_idx

                if bp1_idx is None and bp2_idx is None and tail2_idx is not None and tail2_idx > (system_idx or 0):
                    bp2_idx = tail2_idx

        priority = [bp0_idx, bp1_idx, bp3_idx, bp2_idx, bp4_idx]
        selected = list(dict.fromkeys(bp for bp in priority if bp is not None))[:_MAX_BREAKPOINTS]

        result = [dict(msg) for msg in messages]
        for idx in selected:
            if idx < len(result):
                msg = result[idx]
                if not _has_explicit_cc(msg.get("content")):
                    _add_cc(msg)

        return result

    @staticmethod
    def _is_meaningful_stream_chunk(chunk: dict[str, Any]) -> bool:
        """是否含正文/工具分片或 finish_reason（排除仅 usage 的尾包）。"""
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return False
        c0 = choices[0]
        if c0.get("finish_reason"):
            return True
        delta = c0.get("delta") or {}
        if not isinstance(delta, dict):
            return False
        return bool(delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"))

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | None:
        """解析单行 SSE：取 ``data:`` 负载，``[DONE]`` 与非 data 行返回 None。"""
        if not line:
            return None
        stripped = line.strip()
        if not stripped or not stripped.startswith("data:"):
            return None
        payload = stripped[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("stream.sse.json_decode_failed payload_len={}", len(payload))
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _usage_to_metadata(usage: Any) -> dict[str, Any]:
        """将 OpenAI 兼容 usage dict 转为 6 字段 usage_metadata（含缓存/推理子字段）。

        覆盖 OpenAI/Anthropic/DeepSeek 三种 cache 字段格式（见 _extract_detail_tokens_from_dict）。
        """
        usage = usage if isinstance(usage, dict) else {}
        target: dict[str, Any] = {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        _extract_detail_tokens_from_dict(usage, target)
        return target

    @staticmethod
    def _astream_has_emitted_output(state: _AStreamState) -> bool:
        return bool(state.content_parts or state.reasoning_parts or state.by_index)

    @staticmethod
    def _feed_stream_tool_call_deltas(
        chunk: dict[str, Any],
        by_index: dict[int, dict[str, str]],
    ) -> str:
        """合并流式 delta.tool_calls（含 index 分片）及末包 message.tool_calls。"""
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        c0 = choices[0]
        if not isinstance(c0, dict):
            return ""
        finish_reason = c0.get("finish_reason")
        choice_idx = int(c0.get("index", 0) or 0)
        delta = c0.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}
        raw_delta_tool_calls = delta.get("tool_calls") or []
        if not isinstance(raw_delta_tool_calls, list):
            raw_delta_tool_calls = []
        if finish_reason:
            logger.debug("stream.finish_reason index={} reason={}", choice_idx, finish_reason)
        for tc in raw_delta_tool_calls:
            if not isinstance(tc, dict):
                logger.warning("stream.tool_call.delta_non_dict raw={}", tc)
                continue
            idx = int(tc.get("index", 0))
            if idx not in by_index:
                by_index[idx] = {"id": "", "name": "", "arguments": ""}
            tid = tc.get("id")
            if tid:
                by_index[idx]["id"] = str(tid)
            fn = tc.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    by_index[idx]["name"] = str(fn["name"])
                if fn.get("arguments"):
                    by_index[idx]["arguments"] += str(fn["arguments"])
        msg = c0.get("message")
        if isinstance(msg, dict):
            mtc = msg.get("tool_calls")
            if isinstance(mtc, list):
                for i, tc in enumerate(mtc):
                    if not isinstance(tc, dict):
                        logger.warning("stream.tool_call.message_non_dict raw={}", tc)
                        continue
                    idx = int(tc.get("index", i))
                    if idx not in by_index:
                        by_index[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.get("id"):
                        by_index[idx]["id"] = str(tc["id"])
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            by_index[idx]["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            by_index[idx]["arguments"] = str(fn["arguments"])
        return str(finish_reason or "")

    @classmethod
    def from_llm_config(
        cls,
        config: LLMConfig,
        *,
        compress_token_limit: int | None = None,
        compress_message_cnt: int | None = None,
    ) -> LLMClient:
        """由 :class:`LLMConfig` 构造（YAML ``MODEL.<name>.params``）。"""
        params = dict(config.client_params() or {})
        model = params.pop("model", None)
        if not model:
            raise ValueError(f"Missing model for '{config.name}'.")

        provider_env = (config.provider or "").upper()
        if not provider_env:
            raise ValueError(f"Missing provider for '{config.name}'.")

        api_base, api_key = _resolve_core_endpoint(
            name=config.name,
            provider_env=provider_env,
            param_base_url=params.pop("base_url", None) or params.pop("api_base", None),
            param_api_key=params.pop("api_key", None),
        )
        timeout = params.pop("timeout", None)
        num_retries = params.pop("num_retries", None)
        params.pop("custom_llm_provider", None)
        extra_body = {**dict(params.pop("extra_body", None) or {}), **params}
        extra_body.pop("custom_llm_provider", None)

        return cls(
            model=str(model),
            api_base=api_base,
            api_key=api_key,
            extra_body=extra_body,
            timeout=timeout,
            num_retries=num_retries,
            provider=config.provider,
            compress_token_limit=compress_token_limit,
            compress_message_cnt=compress_message_cnt,
        )

    @classmethod
    def from_env_cfg(
        cls,
        cfg: Mapping[str, Any],
        *,
        compress_token_limit: int | None = None,
        compress_message_cnt: int | None = None,
    ) -> LLMClient:
        """由 ``env.llm_configs`` 扁平项构造（Flex 输出 ``api_base``，此处对齐为 ``base_url``）。"""
        params = dict(cfg)
        if params.get("base_url") is None and params.get("api_base") is not None:
            params["base_url"] = params.pop("api_base")
        model = params.pop("model", None)
        base_url = params.pop("base_url", None)
        api_key = params.pop("api_key", None)
        provider = params.pop("provider", None)
        if not model:
            raise ValueError("LLM config must include top-level 'model'")
        if not base_url or not api_key:
            raise ValueError("LLM config must include base_url and api_key")
        timeout = params.pop("timeout", None)
        num_retries = params.pop("num_retries", None)
        params.pop("custom_llm_provider", None)
        extra_body = {**dict(params.pop("extra_body", None) or {}), **params}
        extra_body.pop("custom_llm_provider", None)
        return cls(
            model=str(model),
            api_base=str(base_url),
            api_key=str(api_key),
            extra_body=extra_body,
            timeout=timeout,
            num_retries=num_retries,
            provider=provider,
            compress_token_limit=compress_token_limit,
            compress_message_cnt=compress_message_cnt,
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> LLMClient:
        """绑定工具信息，返回包含该工具的新 LLMClient 实例。

        ``kwargs`` 中除控制参数（``timeout`` / ``num_retries``）外，按 extra_body 透传合并。
        """
        bound = tools if isinstance(tools, list) else [tools]
        timeout = kwargs.pop("timeout", self._timeout)
        num_retries = kwargs.pop("num_retries", self._num_retries)
        return LLMClient(
            model=self._model,
            api_base=self._api_base,
            api_key=self._api_key,
            tools=bound,
            extra_body={**self._extra_body, **kwargs},
            timeout=timeout,
            num_retries=num_retries,
            provider=self._provider,
            compress_token_limit=self._compress_token_limit,
            compress_message_cnt=self._compress_message_cnt,
        )

    def invoke(self, messages: list[Any], **kwargs: Any) -> LLMClientMessage:
        """以同步方式调用模型生成回复。"""
        payload = self._build_payload(messages, kwargs, stream=False)
        timeout = self._resolve_timeout(kwargs)
        max_attempts = self._resolve_max_attempts(kwargs)
        logger.debug("invoke.request model={} payload_keys={}", self._model, sorted(payload.keys()))

        def _call() -> LLMClientMessage:
            with httpx.Client(timeout=timeout, verify=httpx_verify()) as client:
                resp = client.post(self._endpoint(), headers=self._headers(), json=payload)
                resp.raise_for_status()
                msg = self._wrap_response(resp.json())
                self._check_repetition(msg.content, messages, tool_calls=msg.tool_calls)
                return msg

        return self._with_transient_retry(_call, max_attempts=max_attempts)

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> LLMClientMessage:
        """以异步方式调用模型生成回复。"""
        payload = self._build_payload(messages, kwargs, stream=False)
        timeout = self._resolve_timeout(kwargs)
        max_attempts = self._resolve_max_attempts(kwargs)
        logger.debug("ainvoke.request model={} payload_keys={}", self._model, sorted(payload.keys()))

        async def _call() -> LLMClientMessage:
            async with httpx.AsyncClient(timeout=timeout, verify=httpx_verify()) as client:
                resp = await client.post(self._endpoint(), headers=self._headers(), json=payload)
                resp.raise_for_status()
                msg = self._wrap_response(resp.json())
                self._check_repetition(msg.content, messages, tool_calls=msg.tool_calls)
                return msg

        return await self._awith_transient_retry(_call, max_attempts=max_attempts)

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[LLMClientMessage]:
        """以异步方式流式调用模型，逐步产生消息块。"""
        payload = self._build_payload(messages, kwargs, stream=True)
        timeout = self._resolve_timeout(kwargs)
        max_attempts = self._resolve_max_attempts(kwargs)
        request_url = self._endpoint()

        for attempt in range(max_attempts + 1):
            stream_state = _AStreamState()
            try:
                async for msg in self._astream_iter(
                    payload=payload,
                    timeout=timeout,
                    request_url=request_url,
                    messages=messages,
                    state=stream_state,
                ):
                    yield msg
            except LLMRepetitionError as e:
                if not self._astream_should_retry_repetition(e, attempt, max_attempts):
                    raise
                await self._astream_retry_with_backoff(
                    attempt,
                    max_attempts,
                    category=e.category,
                    detail=e.detail,
                    snippet=e.content_snippet[:200],
                )
                continue
            except Exception as e:
                mapped = map_httpx_exception(e, model=self._model, api_base=self._api_base)
                if self._astream_can_retry(mapped, stream_state, attempt, max_attempts):
                    await self._astream_retry_with_backoff(
                        attempt,
                        max_attempts,
                        category=mapped.category,
                        detail=mapped.message,
                        status_code=mapped.status_code,
                    )
                    continue
                raise mapped from e

            finish_error = self._astream_finish_error(stream_state)
            if finish_error is not None:
                if self._astream_can_retry(finish_error, stream_state, attempt, max_attempts):
                    await self._astream_retry_with_backoff(
                        attempt,
                        max_attempts,
                        category=finish_error.category,
                        detail=finish_error.message,
                        status_code=finish_error.status_code,
                    )
                    continue
                raise finish_error

            try:
                async for msg in self._astream_finalize_yield(stream_state, messages):
                    yield msg
            except LLMRepetitionError as e:
                if not self._astream_should_retry_repetition(e, attempt, max_attempts):
                    raise
                await self._astream_retry_with_backoff(
                    attempt,
                    max_attempts,
                    category=e.category,
                    detail=e.detail,
                    snippet=e.content_snippet[:200],
                )
                continue
            return

        raise RuntimeError("Unexpected error in transient stream retry loop")

    def _astream_can_retry(
        self,
        mapped: LLMCallError,
        state: _AStreamState,
        attempt: int,
        max_attempts: int,
    ) -> bool:
        if self._astream_has_emitted_output(state):
            logger.warning(
                "astream.retry.skip_after_output category={} status_code={} detail={} finish_reason={} "
                "content_len={} reasoning_len={} model={}",
                mapped.category,
                mapped.status_code,
                mapped.message,
                state.finish_reason,
                len("".join(state.content_parts)),
                len("".join(state.reasoning_parts)),
                self._model,
            )
            return False
        return _should_retry_stream_error(mapped, attempt=attempt, max_attempts=max_attempts)

    def _astream_finish_error(self, state: _AStreamState) -> LLMCallError | None:
        if state.has_interrupted:
            return LLMCallError(
                LLMErrorCategory.STREAM_INTERRUPTED,
                "Stream ended before receiving finish_reason.",
                model=self._model,
                request_url=self._endpoint(),
            )

        content_len = len("".join(state.content_parts))
        reasoning_len = len("".join(state.reasoning_parts))
        if state.finish_reason == "length" and not state.by_index and content_len + reasoning_len <= 16:
            return LLMCallError(
                LLMErrorCategory.STREAM_INTERRUPTED,
                "Stream was truncated by the model output limit before producing a usable response.",
                model=self._model,
                request_url=self._endpoint(),
            )
        return None

    async def _astream_retry_with_backoff(
        self,
        attempt: int,
        max_attempts: int,
        *,
        category: LLMErrorCategory,
        detail: str = "",
        snippet: str = "",
        status_code: int | None = None,
    ) -> None:
        delay = _retry_backoff_seconds(attempt)
        logger.warning(
            "astream.retry attempt={}/{} model={} category={} status_code={} detail={} snippet={} backoff={:.3f}s",
            attempt + 1,
            max_attempts,
            self._model,
            category,
            status_code,
            detail,
            snippet,
            delay,
        )
        await asyncio.sleep(delay)

    async def _astream_finalize_yield(
        self,
        state: _AStreamState,
        messages: list[Any],
    ) -> AsyncIterator[LLMClientMessage]:
        final_tc, final_invalid = self._finalize_stream_tool_calls_for_lc(
            state.by_index,
            state.finish_reason,
        )
        if final_tc or final_invalid:
            self._check_repetition("", messages, tool_calls=[*final_tc, *final_invalid])
            yield LLMClientMessage(
                content="",
                reasoning_content="",
                tool_calls=final_tc,
                invalid_tool_calls=final_invalid,
                raw=None,
            )

    async def _astream_iter(
        self,
        *,
        payload: dict[str, Any],
        timeout: httpx.Timeout | None,
        request_url: str,
        messages: list[Any],
        state: _AStreamState,
    ) -> AsyncIterator[LLMClientMessage]:
        client = httpx.AsyncClient(timeout=timeout, verify=httpx_verify())
        try:
            req = client.build_request("POST", request_url, headers=self._headers(), json=payload)
            resp = await client.send(req, stream=True)
            if resp.is_error:
                await resp.aread()
                resp.raise_for_status()

            async for line in resp.aiter_lines():
                msg = self._astream_parse_line(line, messages, state)
                if msg is not None:
                    yield msg
            if not state.finish_reason:
                state.has_interrupted = True
        finally:
            await client.aclose()

    def _astream_parse_line(
        self,
        line: str,
        messages: list[Any],
        state: _AStreamState,
    ) -> LLMClientMessage | None:
        chunk = self._parse_sse_line(line)
        if chunk is None:
            return None
        state.chunk_count += 1
        if self._is_meaningful_stream_chunk(chunk):
            state.received_meaningful = True
        finish_reason = self._feed_stream_tool_call_deltas(chunk, state.by_index)
        if finish_reason:
            state.finish_reason = finish_reason
        wrapped = self._wrap_stream_chunk(chunk)
        if self._astream_should_drop_unusable_length_chunk(wrapped, state, finish_reason):
            return None
        if wrapped.content:
            state.content_parts.append(wrapped.content)
        if wrapped.reasoning_content:
            state.reasoning_parts.append(wrapped.reasoning_content)
        if state.by_index:
            self._check_repetition(
                "",
                messages,
                tool_calls=self._stream_tool_calls_for_repetition(state.by_index),
            )
        if state.chunk_count % 20 == 0:
            self._check_repetition("".join(state.content_parts), messages)
        return LLMClientMessage(
            content=wrapped.content,
            reasoning_content=wrapped.reasoning_content,
            tool_calls=[],
            invalid_tool_calls=[],
            usage_metadata=wrapped.usage_metadata,
            raw=wrapped.raw,
        )

    def _astream_should_retry_repetition(
        self,
        e: LLMRepetitionError,
        attempt: int,
        max_attempts: int,
    ) -> bool:
        if attempt >= max_attempts:
            logger.error(
                "astream.repetition_exhausted model={} detection={} detail={} snippet={}",
                self._model,
                e.detection_type,
                e.detail,
                e.content_snippet[:200],
            )
            return False
        return True

    def _astream_should_drop_unusable_length_chunk(
        self,
        wrapped: LLMClientMessage,
        state: _AStreamState,
        finish_reason: str,
    ) -> bool:
        output_len = len(wrapped.content or "") + len(wrapped.reasoning_content or "")
        if (
            finish_reason == "length"
            and not state.by_index
            and not state.content_parts
            and not state.reasoning_parts
            and output_len <= 16
        ):
            logger.warning(
                "astream.length_short_output dropped content_len={} reasoning_len={} model={}",
                len(wrapped.content or ""),
                len(wrapped.reasoning_content or ""),
                self._model,
            )
            return True
        return False

    def _endpoint(self) -> str:
        return f"{self._api_base.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[Any],
        kwargs: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """组装请求体：model + messages + extra_body 透传；可选 tools / stream。

        cache_control 处理（替代 litellm monkey-patch，见设计文档 §1.7）：
        - 支持显式缓存的模型（Qwen/Claude/百炼 deepseek-v3.2 等）：注入 bp0-bp4 断点
        - 不支持的模型：剥离消息中预置的 cache_control（来自 session restore 等），避免 API 报错
        """
        msgs: list[Any] = messages
        if msgs and not isinstance(msgs[0], dict):
            msgs = self._lc_messages_to_dicts(msgs)

        if _supports_explicit_cache_control(self._model, self._provider):
            msgs = self._apply_cache_control_with_anchors(
                msgs,
                compress_token_limit=self._compress_token_limit,
                compress_message_cnt=self._compress_message_cnt,
            )
        else:
            msgs = _strip_cache_control(msgs)

        # per-call kwargs（除控制参数外）按 extra_body 语义合并透传
        call_extra = {k: v for k, v in kwargs.items() if k not in ("timeout", "num_retries", "stream")}
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": msgs,
            **self._extra_body,
            **call_extra,
        }
        if self._tools:
            openai_tools = self._tools_to_openai(self._tools)
            if openai_tools:
                payload["tools"] = openai_tools
        if stream:
            payload["stream"] = True
            stream_options = dict(payload.get("stream_options") or {})
            stream_options.setdefault("include_usage", True)
            payload["stream_options"] = stream_options
        return payload

    def _resolve_timeout(self, kwargs: dict[str, Any]) -> httpx.Timeout | None:
        timeout = kwargs.get("timeout", self._timeout)
        return httpx.Timeout(timeout) if timeout is not None else None

    def _resolve_max_attempts(self, kwargs: dict[str, Any]) -> int:
        num_retries = kwargs.get("num_retries", self._num_retries)
        return int(num_retries) if num_retries is not None else DEFAULT_LLM_MAX_RETRIES

    def _with_transient_retry(self, operation, *, max_attempts: int) -> Any:
        for attempt in range(max_attempts + 1):
            try:
                return operation()
            except LLMRepetitionError as e:
                if attempt >= max_attempts:
                    logger.error(
                        "llm.repetition_exhausted model={} detection={} detail={} snippet={}",
                        self._model,
                        e.detection_type,
                        e.detail,
                        e.content_snippet[:200],
                    )
                    raise
                delay = _retry_backoff_seconds(attempt)
                logger.warning(
                    "llm.repetition_retry attempt={}/{} model={} detection={} detail={} snippet={} backoff={:.3f}s",
                    attempt + 1,
                    max_attempts,
                    self._model,
                    e.detection_type,
                    e.detail,
                    e.content_snippet[:200],
                    delay,
                )
                time.sleep(delay)
            except Exception as e:
                mapped = map_httpx_exception(e, model=self._model, api_base=self._api_base)
                if attempt < max_attempts and mapped.category in _RETRYABLE_CATEGORIES:
                    delay = _retry_backoff_seconds(attempt)
                    logger.debug(
                        "invoke.retry attempt={}/{} model={} category={} status_code={} detail={} backoff={:.3f}s",
                        attempt + 1,
                        max_attempts,
                        self._model,
                        mapped.category,
                        mapped.status_code,
                        mapped.message,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise mapped from e
        raise RuntimeError("Unexpected error in transient retry loop")  # pragma: no cover

    async def _awith_transient_retry(self, operation, *, max_attempts: int) -> Any:
        for attempt in range(max_attempts + 1):
            try:
                return await operation()
            except LLMRepetitionError as e:
                if attempt >= max_attempts:
                    logger.error(
                        "llm.repetition_exhausted model={} detection={} detail={} snippet={}",
                        self._model,
                        e.detection_type,
                        e.detail,
                        e.content_snippet[:200],
                    )
                    raise
                delay = _retry_backoff_seconds(attempt)
                logger.warning(
                    "llm.repetition_retry attempt={}/{} model={} detection={} detail={} snippet={} backoff={:.3f}s",
                    attempt + 1,
                    max_attempts,
                    self._model,
                    e.detection_type,
                    e.detail,
                    e.content_snippet[:200],
                    delay,
                )
                await asyncio.sleep(delay)
            except Exception as e:
                mapped = map_httpx_exception(e, model=self._model, api_base=self._api_base)
                if attempt < max_attempts and mapped.category in _RETRYABLE_CATEGORIES:
                    delay = _retry_backoff_seconds(attempt)
                    logger.debug(
                        "ainvoke.retry attempt={}/{} model={} category={} status_code={} detail={} backoff={:.3f}s",
                        attempt + 1,
                        max_attempts,
                        self._model,
                        mapped.category,
                        mapped.status_code,
                        mapped.message,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise mapped from e
        raise RuntimeError("Unexpected error in transient retry loop")  # pragma: no cover

    def _wrap_response(self, resp: dict[str, Any]) -> LLMClientMessage:
        """将 httpx JSON 响应（OpenAI 兼容）包装为 LLMClientMessage。"""
        choices = resp.get("choices") or []
        msg = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        msg = msg if isinstance(msg, dict) else {}
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        rc = msg.get("reasoning_content") or ""
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            tool_calls = []
        thinking = msg.get("thinking_blocks")
        return LLMClientMessage(
            content=content,
            reasoning_content=str(rc) if rc else "",
            tool_calls=self._tool_calls_to_dicts(tool_calls),
            thinking_blocks=list(thinking) if isinstance(thinking, list) and thinking else None,
            usage_metadata=self._usage_to_metadata(resp.get("usage")),
            raw=resp,
        )

    def _wrap_stream_chunk(self, chunk: dict[str, Any]) -> LLMClientMessage:
        """将流式 chunk dict 转为 LLMClientMessage，仅取文本与 usage。"""
        choices = chunk.get("choices") or []
        usage_dict = self._usage_to_metadata(chunk.get("usage"))
        if not choices or not isinstance(choices[0], dict):
            return LLMClientMessage(content="", usage_metadata=usage_dict, raw=chunk)
        delta = choices[0].get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}
        content = delta.get("content") or ""
        rc = delta.get("reasoning_content") or ""
        return LLMClientMessage(
            content=str(content) if content else "",
            reasoning_content=str(rc) if rc else "",
            usage_metadata=usage_dict,
            raw=chunk,
        )

    def _check_repetition(
        self,
        content: str,
        messages: list[Any],
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """检测到重复输出时抛出 ``LLMRepetitionError``（调用方在重试循环中捕获）。"""
        previous = self._extract_previous_assistant_content(messages)
        is_rep, detail = _detect_repetition(content, previous_content=previous)
        if is_rep:
            raise LLMRepetitionError(
                detection_type="repetition",
                detail=detail or "",
                content_snippet=content[:500],
                model=self._model,
            )

        tool_calls = tool_calls or []
        is_tool_rep, tool_detail, tool_call_text = self._detect_tool_call_repetition(tool_calls)
        if is_tool_rep:
            raise LLMRepetitionError(
                detection_type="tool_call_repetition",
                detail=tool_detail or "",
                content_snippet=tool_call_text[:500],
                model=self._model,
            )


def llm_adapter_from_env_cfg(
    cfg: dict[str, Any],
    logical_name: str,
    *,
    compress_token_limit: int | None = None,
    compress_message_cnt: int | None = None,
) -> Any:
    """``env.llm_configs[logical_name]`` → ``LangChainChatModelAdapter``（供 ``Runtime.llm`` 懒加载缓存）。

    ``compress_token_limit`` / ``compress_message_cnt`` 来自 ``runtime.env`` 的 CONTEXT
    压缩参数，透传给 :class:`LLMClient`，使 cache_control 断点策略与 pruner 实际压缩
    阈值保持一致。
    """
    from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter

    return LangChainChatModelAdapter(
        LLMClient.from_env_cfg(
            cfg,
            compress_token_limit=compress_token_limit,
            compress_message_cnt=compress_message_cnt,
        ),
        _llm_config_for_adapter(cfg, logical_name),
    )
