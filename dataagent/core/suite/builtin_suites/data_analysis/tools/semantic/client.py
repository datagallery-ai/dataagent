from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE_URL = "http://localhost:31000/api/semantic"
DEFAULT_TIMEOUT_SEC = 180.0


def resolve_base_url() -> str:
    return str(os.environ.get("SEMANTIC_SERVICE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def resolve_timeout_sec() -> float:
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

    url = f"{(base_url or resolve_base_url())}/v1/semantic/retrieve"
    timeout = resolve_timeout_sec() if timeout_sec is None else max(1.0, float(timeout_sec))
    try:
        response = requests.post(
            url,
            json={"query": normalized_query},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"semantic-service request failed: {exc}") from exc

    if response.status_code == 200:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise RuntimeError("semantic-service returned a non-object JSON payload")

    message = _error_message(response)
    raise RuntimeError(f"semantic-service HTTP {response.status_code}: {message}")


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or response.reason or "unknown error"
    if isinstance(payload, dict):
        error_message = str(payload.get("errorMessage") or "").strip()
        error_code = str(payload.get("errorCode") or "").strip()
        if error_message and error_code:
            return f"{error_code}: {error_message}"
        if error_message:
            return error_message
        if error_code:
            return error_code
    return response.text[:500] or response.reason or "unknown error"
