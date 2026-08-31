from __future__ import annotations

import os
from typing import Any

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient

DEFAULT_BASE_URL = "http://localhost:31000/api/semantic"
DEFAULT_TIMEOUT_SEC = 180.0


def resolve_base_url() -> str:
    """Resolve the semantic service base URL from environment or the default."""
    return str(os.environ.get("SEMANTIC_SERVICE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def resolve_timeout_sec() -> float:
    """Resolve the semantic service HTTP timeout in seconds from environment or the default."""
    raw = str(os.environ.get("SEMANTIC_SERVICE_TIMEOUT_SEC") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SEC
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def semantic_retrieve(
    query: str,
    *,
    base_url: str | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Call semantic-service unified retrieval and return a SemanticBundle dict."""
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query is required")

    timeout = resolve_timeout_sec() if timeout_sec is None else max(1.0, float(timeout_sec))
    client = SemanticServiceClient(base_url or resolve_base_url(), timeout=timeout)
    try:
        payload = client.semantic_retrieve(normalized_query)
    finally:
        client.client.close()
    if isinstance(payload, dict):
        return payload
    from dataagent.core.errors import DataAgentError

    raise DataAgentError(
        source="tool",
        component="semantic-service",
        fact="语义服务返回非对象 payload",
    )
