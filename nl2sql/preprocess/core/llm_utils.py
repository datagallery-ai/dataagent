from __future__ import annotations

import json
import logging
import time
from typing import Dict

try:
    from ... import config
except ImportError:
    import config

from ...client import LlmClient

logger = logging.getLogger(__name__)


def safe_json_chat(
    llm_client: LlmClient,
    prompt: str,
    default: Dict[str, str] | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    retry_delay: float | None = None,
    backoff_multiplier: float | None = None,
) -> Dict[str, str]:
    """统一重试的 LLM JSON 调用。

    调用失败（网络/超时/HTTP 错误/LLMMaxRetriesExceeded）与 JSON 解析失败
    共用同一个重试预算 max_retries，默认读 config.LLM_PARSE_FAIL_MAX_RETRIES。
    重试间隔采用指数退避（LLM_RETRY_DELAY * LLM_BACKOFF_MULTIPLIER^k）。
    用尽后返回 default，不抛异常，交由上层 fallback 处理。
    """
    eff_max = int(max_retries if max_retries is not None else getattr(config, "LLM_PARSE_FAIL_MAX_RETRIES", 10))
    eff_max = max(1, eff_max)
    eff_delay = float(retry_delay if retry_delay is not None else getattr(config, "LLM_RETRY_DELAY", 2))
    eff_mult = float(backoff_multiplier if backoff_multiplier is not None else getattr(config, "LLM_BACKOFF_MULTIPLIER", 2))

    delay = eff_delay
    last_err: Exception | None = None
    for attempt in range(1, eff_max + 1):
        try:
            response = llm_client.chat(prompt, temperature=temperature)
            parsed = json.loads(response)
            if isinstance(parsed, dict):
                return parsed
            last_err = ValueError("LLM response is not a JSON object")
        except Exception as exc:
            last_err = exc
        logger.warning(
            "safe_json_chat: attempt %d/%d failed: %s%s",
            attempt,
            eff_max,
            last_err,
            " (retrying...)" if attempt < eff_max else " (giving up, using default)",
        )
        if attempt < eff_max:
            time.sleep(delay)
            delay *= eff_mult
    return dict(default or {})
