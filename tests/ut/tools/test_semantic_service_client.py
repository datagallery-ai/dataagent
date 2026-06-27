from __future__ import annotations

from typing import Any

import pytest

from dataagent.actions.tools.semantic_tool import semantic_client
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeHttpErrorResponse(_FakeResponse):
    def __init__(self, payload: Any, *, status_code: int, text: str = "") -> None:
        super().__init__(payload)
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        raise semantic_client.requests.HTTPError("bad request", response=self)


class _FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.auth: tuple[str, str] | None = None
        self.calls: list[tuple[str, str, Any, Any, float, bool]] = []

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float,
        verify: bool,
    ) -> _FakeResponse:
        self.calls.append(("GET", url, params, headers, timeout, verify))
        return _FakeResponse({"ok": True})

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float,
        verify: bool,
    ) -> _FakeResponse:
        self.calls.append(("POST", url, json, headers, timeout, verify))
        return _FakeResponse({"ok": True})


def test_client_uses_semantic_v1_paths_for_metadata_apis(monkeypatch) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(semantic_client.requests, "Session", lambda: fake_session)

    client = SemanticServiceClient("http://semantic.local:41000")

    assert client.get_table_columns_info("db.table", limit=100) == {"ok": True}
    method, url, params, headers, timeout, verify = fake_session.calls[-1]
    assert method == "GET"
    assert url == "http://semantic.local:41000/api/semantic/v1/advanced-search/table-columns-info"
    assert "/api/metaVisor/v3" not in url
    assert params == {"tableName": "db.table", "limit": 100}
    assert headers is None
    assert timeout == 30.0
    assert verify is True

    assert client.list_retrieval_tables() == {"ok": True}
    method, url, params, headers, timeout, verify = fake_session.calls[-1]
    assert method == "GET"
    assert url == "http://semantic.local:41000/api/semantic/v1/retrieval/tables"
    assert params is None

    assert client.get_retrieval_table_schema("data_table") == {"ok": True}
    method, url, params, headers, timeout, verify = fake_session.calls[-1]
    assert method == "GET"
    assert url == "http://semantic.local:41000/api/semantic/v1/retrieval/tables/data_table/schema"


def test_http_error_exposes_semantic_service_error_fields() -> None:
    client = SemanticServiceClient("http://semantic.local:41000")

    class _FailingSession(_FakeSession):
        def get(
            self,
            url: str,
            *,
            params: Any = None,
            headers: dict[str, str] | None = None,
            timeout: float,
            verify: bool,
        ) -> _FakeHttpErrorResponse:
            return _FakeHttpErrorResponse(
                {"errorCode": "METAVISOR-400-00-002", "errorMessage": "sql is required"},
                status_code=400,
            )

    client.session = _FailingSession()

    with pytest.raises(semantic_client.SemanticServiceError) as exc_info:
        client.search_fulltext("订单")

    err = exc_info.value
    assert isinstance(err, semantic_client.requests.HTTPError)
    assert err.status_code == 400
    assert err.error_code == "METAVISOR-400-00-002"
    assert err.error_message == "sql is required"
    assert err.method == "GET"
    assert err.path == "search/fulltext"
