# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Build jiuwen Model from YAML config."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openjiuwen.core.foundation.llm.model import Model


def _resolve_env(value: str) -> str:
    """Resolve ``$env{KEY}`` placeholders."""
    if not isinstance(value, str):
        return value
    import re

    def _repl(m: re.Match) -> str:
        return os.getenv(m.group(1), "")

    return re.sub(r"\$env\{([^}]+)\}", _repl, value)


def build_model_from_config(config: Any, model_key: str = "chat_model") -> Model:
    """Build a jiuwen ``Model`` instance from a YAML MODEL section.

    Reads ``config.get("MODEL")``, looks up ``model_key`` (default ``chat_model``),
    and constructs ``ModelClientConfig`` + ``ModelRequestConfig``.

    Args:
        config: ConfigManager or plain dict with a ``MODEL`` section.
        model_key: Key under ``MODEL`` (e.g. ``chat_model``).

    Returns:
        Configured jiuwen ``Model``.
    """
    from openjiuwen.core.foundation.llm.model import Model
    from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

    model_section: dict[str, Any] = {}
    raw = config.get("MODEL", {}) if hasattr(config, "get") else {}
    if isinstance(raw, dict):
        model_section = raw

    entry = model_section.get(model_key)
    if not isinstance(entry, dict):
        raise ValueError(f"MODEL.{model_key} is missing or not a dict. Available keys: {list(model_section.keys())}")

    provider = str(entry.get("provider", "")).strip()
    if not provider:
        raise ValueError(f"MODEL.{model_key}.provider is required")

    params: dict[str, Any] = dict(entry.get("params", {}) or {})

    # api_base: params.base_url > params.api_base > env ${PROVIDER}_BASE_URL
    api_base = str(params.pop("base_url", "") or params.pop("api_base", "") or "").strip() or _resolve_env(
        str(os.getenv(f"{provider.upper()}_BASE_URL", ""))
    )
    if not api_base:
        raise ValueError(
            f"Missing api_base for MODEL.{model_key}. Set params.base_url or {provider.upper()}_BASE_URL env var."
        )

    # api_key: params.api_key > env ${PROVIDER}_API_KEY
    api_key = str(params.pop("api_key", "") or "").strip()
    if api_key.startswith("$env{"):
        api_key = _resolve_env(api_key)
    if not api_key:
        api_key = os.getenv(f"{provider.upper()}_API_KEY", "")
    if not api_key:
        raise ValueError(
            f"Missing api_key for MODEL.{model_key}. Set params.api_key or {provider.upper()}_API_KEY env var."
        )

    model_name = str(params.pop("model", "") or "").strip()
    if not model_name:
        raise ValueError(f"MODEL.{model_key}.params.model is required")

    temperature = float(params.pop("temperature", 0.95))
    timeout = float(params.pop("timeout", 300))
    max_retries = int(params.pop("max_retries", 3))
    top_p = float(params.pop("top_p", 0.1))
    max_tokens = params.pop("max_tokens", None)
    verify_ssl = str(params.pop("verify_ssl", "false")).lower() in ("true", "1", "yes")

    client_config = ModelClientConfig(
        client_provider=provider,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        max_retries=max_retries,
        verify_ssl=verify_ssl,
    )

    request_config = ModelRequestConfig(
        model=model_name,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )

    return Model(model_client_config=client_config, model_config=request_config)
