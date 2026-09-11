"""Request options shared by the standalone BIRD tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REQUEST_CONTROL_KEYS = frozenset({"thinking", "enable_thinking", "reasoning_effort"})
_RESERVED_EXTRA_KEYS = REQUEST_CONTROL_KEYS | {
    "model",
    "messages",
    "stream",
    "stream_options",
    "max_tokens",
    "max_completion_tokens",
    "timeout",
    "num_retries",
    "base_url",
    "api_base",
    "extra_body",
    "headers",
    "extra_headers",
    "generator_max_tokens",
    "llm_max_concurrency",
    "case_timeout",
}


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                part in normalized
                for part in ("api_key", "apikey", "authorization", "password", "secret", "credential")
            ) or normalized in {"auth", "token", "access_token", "bearer", "cookie"}:
                raise ValueError("extra_body cannot contain credentials or authentication fields")
            _reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_credentials(child)


def normalize_extra_body(
    *,
    thinking: str = "omit",
    enable_thinking: bool | str = "omit",
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate extensions and add only explicitly requested thinking fields."""
    if not isinstance(thinking, str) or thinking.strip().lower() not in {"enabled", "disabled", "omit"}:
        raise ValueError("thinking must be enabled, disabled, or omit")
    thinking = thinking.strip().lower()
    if isinstance(enable_thinking, str):
        flag = enable_thinking.strip().lower()
        if flag not in {"true", "false", "omit"}:
            raise ValueError("enable_thinking must be true, false, or omit")
        enable_thinking = {"true": True, "false": False, "omit": "omit"}[flag]
    elif not isinstance(enable_thinking, bool):
        raise ValueError("enable_thinking must be true, false, or omit")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise ValueError("reasoning_effort must be a nonempty string or omit")
        reasoning_effort = reasoning_effort.strip()
        if reasoning_effort.lower() == "omit":
            reasoning_effort = None
    if extra_body is not None and not isinstance(extra_body, dict):
        raise ValueError("extra_body must be a JSON object")
    extra_body = extra_body or {}
    if not all(isinstance(key, str) for key in extra_body):
        raise ValueError("extra_body keys must be strings")
    if set(extra_body) & _RESERVED_EXTRA_KEYS:
        raise ValueError("extra_body contains a controlled option; use its dedicated argument")
    _reject_credentials(extra_body)
    try:
        result = json.loads(json.dumps(extra_body, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("extra_body must contain only finite JSON values") from exc
    if thinking != "omit":
        result["thinking"] = {"type": thinking}
    if enable_thinking != "omit":
        result["enable_thinking"] = enable_thinking
    if reasoning_effort is not None:
        result["reasoning_effort"] = reasoning_effort
    return result


def normalize_api_base(value: str) -> str:
    """Accept a gateway root, API base, or full chat-completions endpoint."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("api_base must be a nonempty HTTP(S) URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("api_base must be a nonempty HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("api_base cannot contain credentials, query parameters, or fragments")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    elif not path:
        path = "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
