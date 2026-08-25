from __future__ import annotations

from typing import Any, Optional

import httpx
import pytest

from dataagent.actions.tools.semantic_tool import semantic_client
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient


class _FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: Optional[dict[str, str]] = None,  # noqa: UP045
        text: str = "",
        url: str = "http://semantic.local:41000/api/semantic/v1/ok",
        reason_phrase: str = "OK",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text or ("" if payload is None else str(payload))
        self.url = url
        self.reason_phrase = reason_phrase

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", str(self.url))
            raise httpx.HTTPStatusError("bad request", request=request, response=self)  # type: ignore[arg-type]
        return None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, str, Any, Any]] = []
        self._next_response: _FakeResponse | None = None

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append(("GET", url, params, headers))
        if self._next_response is not None:
            return self._next_response
        return _FakeResponse({"ok": True})

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append(("POST", url, json, headers))
        if self._next_response is not None:
            return self._next_response
        return _FakeResponse({"ok": True})


def test_client_uses_semantic_v1_paths_for_metadata_apis(monkeypatch) -> None:
    fake_client = _FakeClient()
    monkeypatch.setattr(semantic_client.httpx, "Client", lambda *a, **k: fake_client)

    client = SemanticServiceClient("http://semantic.local:41000")

    assert client.get_table_columns_info("db.table", limit=100) == {"ok": True}
    method, url, params, headers = fake_client.calls[-1]
    assert method == "GET"
    assert url == "http://semantic.local:41000/api/semantic/v1/advanced-search/table-columns-info"
    assert "/api/metaVisor/v3" not in url
    assert params == {"tableName": "db.table", "limit": 100}
    assert headers is None

    assert client.list_retrieval_tables() == {"ok": True}
    method, url, params, headers = fake_client.calls[-1]
    assert method == "GET"
    assert url == "http://semantic.local:41000/api/semantic/v1/retrieval/tables"
    assert params is None

    assert client.get_retrieval_table_schema("data_table") == {"ok": True}
    method, url, params, headers = fake_client.calls[-1]
    assert method == "GET"
    assert url == "http://semantic.local:41000/api/semantic/v1/retrieval/tables/data_table/schema"

    assert client.hybrid_table_columns([" db.table ", "", "db.other"]) == {"ok": True}
    method, url, payload, headers = fake_client.calls[-1]
    assert method == "POST"
    assert url == "http://semantic.local:41000/api/semantic/v1/hybrid/table-columns"
    assert payload == {"tables": ["db.table", "db.other"]}
    assert headers == {"Content-Type": "application/json"}


def test_http_error_exposes_semantic_service_error_fields(monkeypatch) -> None:
    fake_client = _FakeClient()
    fake_client._next_response = _FakeResponse(
        {"errorCode": "METAVISOR-400-00-002", "errorMessage": "sql is required"},
        status_code=400,
        text='{"errorCode":"METAVISOR-400-00-002","errorMessage":"sql is required"}',
        url="http://semantic.local:41000/api/semantic/v1/search/fulltext",
        reason_phrase="Bad Request",
        headers={"Set-Cookie": "session=secret", "X-Request-ID": "request-123", "Content-Type": "application/json"},
    )
    monkeypatch.setattr(semantic_client.httpx, "Client", lambda *a, **k: fake_client)

    client = SemanticServiceClient("http://semantic.local:41000")

    with pytest.raises(semantic_client.SemanticServiceError) as exc_info:
        client.search_fulltext("订单")

    err = exc_info.value
    assert isinstance(err, httpx.HTTPError)
    assert err.status_code == 400
    assert err.error_code == "METAVISOR-400-00-002"
    assert err.error_message == "sql is required"
    assert err.method == "GET"
    assert err.path == "search/fulltext"
    error_text = str(err)
    assert "response_url='http://semantic.local:41000/api/semantic/v1/search/fulltext'" in error_text
    assert "response_reason='Bad Request'" in error_text
    assert "'Set-Cookie': '***REDACTED***'" in error_text
    assert "'X-Request-ID': 'request-123'" in error_text
    assert 'response_body=\'{"errorCode":"METAVISOR-400-00-002","errorMessage":"sql is required"}\'' in error_text


def test_from_config_does_not_read_verify_ssl(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _CM:
        def get(self, key: str, default: Any = None) -> Any:
            values = {
                "SEMANTIC_LAYER.base_url": "http://semantic.local:41000",
                "SEMANTIC_LAYER.timeout": 12.0,
                "SEMANTIC_LAYER.verify_ssl": False,
            }
            return values.get(key, default)

    def _fake_client(*args: Any, **kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(semantic_client.httpx, "Client", _fake_client)
    monkeypatch.setattr(semantic_client, "httpx_verify", lambda service="llm": False)

    client = SemanticServiceClient.from_config(_CM())
    assert client.timeout == 12.0
    assert captured.get("auth") is None
    assert "verify" in captured
    assert captured["verify"] is False
    assert not hasattr(client, "verify")


def test_client_uses_httpx_verify_for_semantic_layer(monkeypatch) -> None:
    sentinel = object()
    seen: list[str] = []

    def _fake_verify(service: str = "llm"):
        seen.append(service)
        return sentinel

    monkeypatch.setattr(semantic_client, "httpx_verify", _fake_verify)

    fake = _FakeClient()
    monkeypatch.setattr(semantic_client.httpx, "Client", lambda *a, **k: fake)

    SemanticServiceClient("http://semantic.local:41000")
    assert seen == ["semantic_layer"]
    # Client construction receives verify from httpx_verify("semantic_layer")
    # Re-run with capturing kwargs:
    captured: dict[str, Any] = {}

    def _capturing_client(*args: Any, **kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(semantic_client.httpx, "Client", _capturing_client)
    SemanticServiceClient("http://semantic.local:41000")
    assert captured["verify"] is sentinel
