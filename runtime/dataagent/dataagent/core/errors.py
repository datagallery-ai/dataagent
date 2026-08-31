"""Unified DataAgent error model: source, component, fact, trace_id."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

ErrorSource = str

_VALID_SOURCES = frozenset({"config", "llm", "tool", "internal", "constraint"})
_SOURCE_FACTS: dict[str, str] = {
    "config": "配置无效",
    "llm": "模型调用失败",
    "tool": "工具执行失败",
    "constraint": "触发约束",
    "internal": "内部错误",
}
_SECRET_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "api-key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "client_secret",
    }
)
_SECRET_KEY_PARTS = frozenset({"token", "secret", "password", "authorization", "apikey"})
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*"
    r"(?:(?:bearer|token|basic)\s+)?"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s；;,?&]+)"
    r"|bearer\s+[^\s；;,]+)"
)
_QUOTED_SECRET_PAIR_RE = re.compile(
    r'(?i)((?P<q>[\'"])(?:(?:access|refresh|id)[_-]?token|client[_-]?secret|api[_-]?key|'
    r"token|password|secret|authorization)(?P=q))\s*:\s*"
    r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,}\]]+)'
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _current_trace_id() -> str | None:
    """Return the current log-context ``trace_id``, or ``None`` if unavailable."""
    try:
        from dataagent.utils.log.dataagent_logger import get_log_context
    except Exception:
        return None
    value = get_log_context().get("trace_id")
    return str(value) if value else None


def _is_secret_key(key: str) -> bool:
    """Return True when a mapping key looks like a secret name."""
    lowered = str(key).lower()
    normalized = lowered.replace("-", "_")
    compact = lowered.replace("-", "").replace("_", "")
    if normalized in _SECRET_KEYS:
        return True
    if compact in _SECRET_KEYS:
        return True
    parts = re.split(r"[-_]", normalized)
    if any(part in _SECRET_KEY_PARTS for part in parts):
        return True
    return "apikey" in compact


def _redact_if_secret_key(key: str, value: Any) -> Any:
    """Return ``***`` when ``key`` looks secret, otherwise ``value``."""
    if _is_secret_key(str(key)):
        return "***"
    return value


def _redact_netloc(netloc: str) -> str:
    """Redact userinfo in a URL netloc, leaving host and port intact."""
    if "@" not in netloc:
        return netloc
    userinfo, hostport = netloc.rsplit("@", 1)
    if ":" in userinfo:
        return f"***:***@{hostport}"
    return f"***@{hostport}"


def _redact_url(match: re.Match[str]) -> str:
    """Redact userinfo and secret query parameters in a matched URL."""
    url = match.group(0)
    trailing = ""
    while url and url[-1] in ".,;)]":
        trailing = url[-1] + trailing
        url = url[:-1]
    parts = urlsplit(url)
    netloc = _redact_netloc(parts.netloc)
    query = parts.query
    if query:
        pairs = [(key, _redact_if_secret_key(key, value)) for key, value in parse_qsl(query, keep_blank_values=True)]
        query = urlencode(pairs)
    rebuilt = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return rebuilt + trailing


def _replace_secret_assignment(match: re.Match[str]) -> str:
    """Replace a ``key=value`` or Bearer token assignment with a redacted form."""
    key = match.group(1)
    if key:
        return f"{key}=***"
    return "Bearer ***"


def _sanitize_scalar_text(value: str) -> str:
    """Redact secrets in a plain-text scalar."""
    redacted = _URL_RE.sub(_redact_url, value)
    return _SECRET_VALUE_RE.sub(_replace_secret_assignment, redacted)


def _redact_json_value(value: Any) -> Any:
    """Recursively redact secret keys and text inside JSON-like values."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                redacted[key] = "***"
            else:
                redacted[key] = _redact_json_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_scalar_text(value)
    return value


def _sanitize_text(value: str) -> str:
    """Redact secrets in free-form text, including JSON payloads."""
    stripped = value.strip()
    if stripped[:1] in "{[":
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(_redact_json_value(parsed), ensure_ascii=False)
    redacted = _QUOTED_SECRET_PAIR_RE.sub(r'\1:"***"', value)
    return _sanitize_scalar_text(redacted)


def _short_exception_text(exc: Exception, *, limit: int = 160) -> str:
    """Sanitize and truncate an exception message for fact text."""
    text = _sanitize_text(str(exc)).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _normalize_source(source: str | None) -> ErrorSource:
    """Validate ``source`` against the allowed set."""
    if source in _VALID_SOURCES:
        return source
    raise ValueError(f"invalid error source: {source!r}")


class DataAgentError(Exception):
    """Structured DataAgent failure: ``source``, ``component``, ``fact``, ``trace_id``."""

    def __init__(
        self,
        *,
        source: str,
        component: str = "dataagent",
        fact: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Build a structured error; ``fact`` falls back to the source default."""
        self.source = _normalize_source(source)
        self.component = component
        self.fact = _sanitize_text(str(fact or _SOURCE_FACTS[self.source]))
        self.trace_id = trace_id or _current_trace_id() or uuid4().hex
        super().__init__(self.fact)

    def __str__(self) -> str:
        """Return the sanitized fact."""
        return self.fact

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DataAgentError:
        """Restore a ``DataAgentError`` from a four-field payload."""
        if "source" not in payload:
            raise ValueError("error payload missing source")
        return cls(
            source=str(payload["source"]),
            component=str(payload.get("component") or "dataagent"),
            fact=str(payload["fact"]) if payload.get("fact") not in (None, "") else None,
            trace_id=str(payload["trace_id"]) if payload.get("trace_id") else None,
        )

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        component: str = "dataagent",
        trace_id: str | None = None,
    ) -> DataAgentError:
        """Wrap or pass through an exception as ``DataAgentError``."""
        if isinstance(exc, cls):
            if trace_id and exc.trace_id != trace_id:
                return cls(
                    source=exc.source,
                    fact=exc.fact,
                    component=exc.component,
                    trace_id=trace_id,
                )
            return exc
        if isinstance(exc, TimeoutError):
            short = _short_exception_text(exc)
            error = cls(
                source="constraint",
                fact=f"TimeoutError: {short}" if short else "TimeoutError",
                component=component,
                trace_id=trace_id,
            )
            error.__cause__ = exc
            return error
        short = _short_exception_text(exc)
        name = type(exc).__name__
        return cls(
            source="internal",
            fact=f"{name}: {short}" if short else name,
            component=component,
            trace_id=trace_id,
        )

    def actor_text(self) -> str:
        """Human-readable Actor / ToolMessage line: ``[source/component] fact``."""
        return f"[{self.source}/{self.component}] {_sanitize_text(self.fact)}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the four public fields."""
        return {
            "source": self.source,
            "component": self.component,
            "fact": _sanitize_text(self.fact),
            "trace_id": self.trace_id,
        }
