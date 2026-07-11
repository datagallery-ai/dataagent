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
"""Canonical LLM usage 归一与 cache 命中率共享模块。

本模块是 token/cache 字段提取的**唯一实现入口**（见
``openspec/changes/add-subagent-token-cache-aggregation`` §3.3 与
``main-agent-cache-control`` spec "缓存命中指标统一提取"）。所有链路
（``llm_client._usage_to_metadata`` / ``adapters.normalize_usage_metadata`` /
``performance.summarize_llm_usage`` / ``message_history`` round summary /
e2e 报告 / 子 Agent ``perf_summary``）SHALL 调用本模块，禁止各自维护厂商
字段映射。

canonical 6 字段语义：
    input_tokens              = 本次请求逻辑总输入 token（必须包含 cache read 与 creation）
    output_tokens             = 输出 token
    total_tokens              = input_tokens + output_tokens
    input_cache_read_tokens   = 命中缓存读取的输入 token
    input_cache_creation_tokens = 本次写入缓存的输入 token
    output_reasoning_tokens   = 推理 token

归一规则：
    - OpenAI/Qwen/DashScope/DeepSeek：原始 ``input_tokens`` 已包含 cached tokens，
      保持不变。
    - Anthropic：原始 ``input_tokens`` 不含 cache read/creation，canonical
      ``input_tokens = raw_input + cache_read + cache_creation``。
    - ``total_tokens`` 缺失或不等于 canonical input/output 之和时，使用
      ``input_tokens + output_tokens`` 作为 canonical total。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "TOKEN_FIELDS",
    "usage_to_metadata",
    "normalize_usage_metadata",
    "summarize_usage",
    "cache_hit_rate",
    "empty_usage",
]

# canonical 6 字段（顺序固定，便于性能层与报告层稳定序列化）。
TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_cache_read_tokens",
    "input_cache_creation_tokens",
    "output_reasoning_tokens",
)


def empty_usage() -> dict[str, int]:
    """返回全零的 canonical usage 字典。"""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "input_cache_read_tokens": 0,
        "input_cache_creation_tokens": 0,
        "output_reasoning_tokens": 0,
    }


def _safe_int(val: Any) -> int:
    """把任意值安全转为非负 int；非法值返回 0。"""
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _has_attr(obj: Any, attr: str) -> bool:
    """对象属性存在且非 None 时返回 True（兼容 Pydantic / dataclass / mock）。"""
    try:
        return getattr(obj, attr, None) is not None
    except Exception:
        return False


def _extract_detail_tokens_from_obj(usage: Any, target: dict[str, int]) -> bool:
    """从对象形态 usage（litellm/Pydantic）提取 cache/reasoning 子字段。

    返回是否命中 Anthropic flat 字段路径（用于 canonical input 补齐判定）。
    """
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    is_anthropic_flat = False
    if prompt_details is not None:
        target["input_cache_read_tokens"] = _safe_int(getattr(prompt_details, "cached_tokens", None))
        target["input_cache_creation_tokens"] = _safe_int(
            getattr(prompt_details, "cache_creation_tokens", None)
            or getattr(prompt_details, "cache_creation_input_tokens", None)
        )
    else:
        cache_read = _safe_int(getattr(usage, "cache_read_input_tokens", None))
        cache_creation = _safe_int(getattr(usage, "cache_creation_input_tokens", None))
        target["input_cache_read_tokens"] = cache_read
        target["input_cache_creation_tokens"] = cache_creation
        is_anthropic_flat = bool(cache_read or cache_creation)

    if not target.get("input_cache_read_tokens"):
        target["input_cache_read_tokens"] = _safe_int(getattr(usage, "prompt_cache_hit_tokens", None))

    completion_details = getattr(usage, "completion_tokens_details", None)
    if completion_details is not None:
        target["output_reasoning_tokens"] = _safe_int(getattr(completion_details, "reasoning_tokens", None))
    else:
        target["output_reasoning_tokens"] = _safe_int(getattr(usage, "reasoning_tokens", None))
    return is_anthropic_flat


def _extract_detail_tokens_from_dict(usage: Mapping[str, Any], target: dict[str, int]) -> bool:
    """从 dict 形态 usage 提取 cache/reasoning 子字段（流式 / httpx JSON 路径）。

    返回是否命中 Anthropic flat 字段路径。
    """
    prompt_details = usage.get("prompt_tokens_details")
    is_anthropic_flat = False
    if isinstance(prompt_details, Mapping):
        target["input_cache_read_tokens"] = _safe_int(prompt_details.get("cached_tokens"))
        target["input_cache_creation_tokens"] = _safe_int(
            prompt_details.get("cache_creation_tokens") or prompt_details.get("cache_creation_input_tokens")
        )
    else:
        cache_read = _safe_int(usage.get("cache_read_input_tokens"))
        cache_creation = _safe_int(usage.get("cache_creation_input_tokens"))
        target["input_cache_read_tokens"] = cache_read
        target["input_cache_creation_tokens"] = cache_creation
        is_anthropic_flat = bool(cache_read or cache_creation)

    if not target.get("input_cache_read_tokens"):
        target["input_cache_read_tokens"] = _safe_int(usage.get("prompt_cache_hit_tokens"))

    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        target["output_reasoning_tokens"] = _safe_int(completion_details.get("reasoning_tokens"))
    else:
        target["output_reasoning_tokens"] = _safe_int(usage.get("reasoning_tokens"))
    return is_anthropic_flat


def usage_to_metadata(raw_usage: Any) -> dict[str, int]:
    """将厂商原始 usage（dict 或对象）归一为 canonical 6 字段。

    覆盖 OpenAI/Qwen/DashScope（``prompt_tokens_details.cached_tokens``）、
    Anthropic（flat ``cache_read_input_tokens`` / ``cache_creation_input_tokens``）、
    DeepSeek（``prompt_cache_hit_tokens``）三种格式。Anthropic canonical
    ``input_tokens`` 补齐为 ``raw_input + cache_read + cache_creation``。
    """
    target: dict[str, int] = empty_usage()
    if raw_usage is None:
        return target
    if isinstance(raw_usage, Mapping):
        raw_input = _safe_int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens"))
        raw_output = _safe_int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens"))
        raw_total = _safe_int(raw_usage.get("total_tokens"))
        is_anthropic_flat = _extract_detail_tokens_from_dict(raw_usage, target)
    else:
        raw_input = _safe_int(getattr(raw_usage, "prompt_tokens", None) or getattr(raw_usage, "input_tokens", None))
        raw_output = _safe_int(
            getattr(raw_usage, "completion_tokens", None) or getattr(raw_usage, "output_tokens", None)
        )
        raw_total = _safe_int(getattr(raw_usage, "total_tokens", None))
        is_anthropic_flat = _extract_detail_tokens_from_obj(raw_usage, target)

    cache_read = target["input_cache_read_tokens"]
    cache_creation = target["input_cache_creation_tokens"]
    canonical_input = raw_input + cache_read + cache_creation if is_anthropic_flat else raw_input
    canonical_output = raw_output
    canonical_total = (
        raw_total
        if raw_total and raw_total == canonical_input + canonical_output
        else canonical_input + canonical_output
    )

    target["input_tokens"] = canonical_input
    target["output_tokens"] = canonical_output
    target["total_tokens"] = canonical_total
    return target


def normalize_usage_metadata(usage: Any) -> dict[str, int]:
    """补齐和校正已归一或半归一 usage，保证字段语义与 :func:`usage_to_metadata` 一致。

    适用于已经过 ``usage_to_metadata`` 或 langchain 归一后的 ``usage_metadata``
    （canonical input 已含 cache），也兼容少数半归一场景中残留的 Anthropic
    风格字段名（``cache_read_input_tokens`` / ``cache_creation_input_tokens`` /
    ``reasoning_tokens``）。**不**对 input 二次补齐 cache，避免重复计数；
    ``total_tokens`` 取已有值（缺失为 0），不在本层重算——canonical total 的
    补齐仅在 :func:`usage_to_metadata`（厂商原始 → canonical）处发生。
    """
    if not isinstance(usage, Mapping):
        return empty_usage()
    return {
        "input_tokens": _safe_int(usage.get("input_tokens")),
        "output_tokens": _safe_int(usage.get("output_tokens")),
        "total_tokens": _safe_int(usage.get("total_tokens")),
        "input_cache_read_tokens": _safe_int(
            usage.get("input_cache_read_tokens") or usage.get("cache_read_input_tokens")
        ),
        "input_cache_creation_tokens": _safe_int(
            usage.get("input_cache_creation_tokens") or usage.get("cache_creation_input_tokens")
        ),
        "output_reasoning_tokens": _safe_int(usage.get("output_reasoning_tokens") or usage.get("reasoning_tokens")),
    }


def summarize_usage(usage: Any) -> dict[str, int]:
    """性能、message history、报告层统一调用的汇总入口。

    语义等同于 :func:`normalize_usage_metadata`：消费 canonical 6 字段（或半归一
    字段），输出 canonical 6 字段。**不**对厂商原始字段做 Anthropic 补齐——
    该补齐已在 :func:`usage_to_metadata`（LLM 调用层）完成，此处只做归一/补零。
    """
    return normalize_usage_metadata(usage)


def cache_hit_rate(usage: Mapping[str, Any] | None) -> float | None:
    """返回 ``input_cache_read_tokens / input_tokens`` 的 0-1 小数。

    ``input_tokens == 0`` 时返回 ``None``。``usage`` 为空时同样返回 ``None``。
    """
    if not isinstance(usage, Mapping):
        return None
    input_tokens = _safe_int(usage.get("input_tokens"))
    if input_tokens == 0:
        return None
    cache_read = _safe_int(usage.get("input_cache_read_tokens"))
    return round(cache_read / input_tokens, 6)
