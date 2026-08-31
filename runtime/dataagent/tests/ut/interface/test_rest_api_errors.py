from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from dataagent.core.errors import DataAgentError
from dataagent.interface.rest_api import app as rest_app
from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.sdk.agent import DataAgent


class _FakeAgent:
    type = "react"

    async def chat(self, query: str):
        raise DataAgentError(source="tool", component="nl2sql", trace_id="trace-rest")

    def astream(self, *args, **kwargs):
        async def _gen():
            raise DataAgentError(source="tool", component="sdk", fact="请求等待响应超时", trace_id="trace-sse")
            yield  # pragma: no cover

        return _gen()


@pytest.mark.asyncio
async def test_rest_query_serializes_dataagent_error() -> None:
    service = DataAgentService()
    service._agent = _FakeAgent()
    service._cached_agent_type = "react"
    with pytest.raises(DataAgentError) as caught:
        await service.query("hi")
    assert caught.value.trace_id == "trace-rest"
    actor = caught.value.to_dict()
    assert "detail" not in actor
    assert actor["source"] == "tool"
    assert actor["component"] == "nl2sql"
    assert actor["fact"]
    assert "locator" not in actor
    assert "retryable" not in actor
    assert "http_status" not in actor


@pytest.mark.asyncio
async def test_rest_query_returns_503_when_service_missing() -> None:
    previous = rest_app._data_agent_service
    rest_app._data_agent_service = None
    try:
        transport = ASGITransport(app=rest_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/query", json={"query": "hi"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "DataAgent service unavailable"}
        assert "WORKFLOW-AGENT" not in resp.text
    finally:
        rest_app._data_agent_service = previous


@pytest.mark.asyncio
async def test_rest_stream_returns_503_when_service_missing() -> None:
    previous = rest_app._data_agent_service
    rest_app._data_agent_service = None
    try:
        transport = ASGITransport(app=rest_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/query", json={"query": "hi", "stream": True})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "DataAgent service unavailable"}
    finally:
        rest_app._data_agent_service = previous


@pytest.mark.asyncio
async def test_rest_query_http_returns_200_with_error_body() -> None:
    service = DataAgentService()
    service._agent = _FakeAgent()
    service._cached_agent_type = "react"
    previous = rest_app._data_agent_service
    rest_app._data_agent_service = service
    try:
        transport = ASGITransport(app=rest_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/query", json={"query": "hi"})
        assert resp.status_code == 200
        payload = resp.json()["result"]
        assert payload["source"] == "tool"
        assert payload["component"] == "nl2sql"
        assert payload["fact"]
        assert payload["trace_id"] == "trace-rest"
        assert "http_status" not in payload
        assert "detail" not in payload
    finally:
        rest_app._data_agent_service = previous


@pytest.mark.asyncio
async def test_rest_stream_http_returns_200_with_result_event() -> None:
    service = DataAgentService()
    service._agent = _FakeAgent()
    service._cached_agent_type = "react"
    previous = rest_app._data_agent_service
    rest_app._data_agent_service = service
    try:
        transport = ASGITransport(app=rest_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/query", json={"query": "hi", "stream": True})
        assert resp.status_code == 200
        assert "event: result" in resp.text
        assert "event: error" not in resp.text
        assert "trace-sse" in resp.text
        assert '"source": "tool"' in resp.text
        assert "fact" in resp.text
    finally:
        rest_app._data_agent_service = previous


@pytest.mark.asyncio
async def test_rest_stream_emits_single_terminal_error() -> None:
    service = DataAgentService()
    service._agent = _FakeAgent()
    events = [item async for item in service.stream_query("hi")]
    assert len(events) == 1
    assert events[0]["event"] == "result"
    payload = events[0]["data"]["result"]
    assert payload["source"] == "tool"
    assert "http_status" not in payload
    assert "code" not in payload
    assert payload["trace_id"] == "trace-sse"
    assert payload["fact"]
    assert "detail" not in payload
    assert "locator" not in payload


def test_initialize_reraises_dataagent_error(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "agent.yaml"
    cfg.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    service = DataAgentService(config_path=cfg)

    def _boom(_path):
        raise DataAgentError(
            fact="SEMANTIC_LAYER.base_url 未配置",
            source="config",
            component="sdk",
            trace_id="trace-init",
        )

    monkeypatch.setattr(DataAgent, "from_config", _boom)
    with pytest.raises(DataAgentError) as caught:
        service.initialize()
    assert caught.value.source == "config"
    assert "SEMANTIC_LAYER.base_url" in caught.value.fact


def test_format_result_preserves_nl2sql_string_error_fact() -> None:
    service = DataAgentService()
    service._cached_agent_type = "nl2sql"
    with pytest.raises(DataAgentError) as caught:
        service._format_result({"sql": "", "error": "no such table: t", "session_id": "s"})
    payload = caught.value.to_dict()
    assert payload["source"] == "tool"
    assert payload["component"] == "nl2sql"
    assert "no such table: t" in payload["fact"]
    assert payload["fact"] != "RuntimeError: Agent failed"
    assert payload.get("success") is not True


def test_format_result_empty_sql_without_error_stays_success() -> None:
    service = DataAgentService()
    service._cached_agent_type = "nl2sql"
    payload = service._format_result({"sql": "", "error": None, "session_id": "s"})["result"]
    assert payload["success"] is True
    assert payload["sql"] == ""


def test_coerce_error_keeps_structured_payloads() -> None:
    service = DataAgentService()
    service._cached_agent_type = "nl2sql"
    original = DataAgentError(source="llm", component="sdk", fact="model down", trace_id="tid")
    assert service._coerce_error(original) is original
    structured = service._coerce_error({"source": "tool", "component": "nl2sql", "fact": "keep me", "trace_id": "tid"})
    assert structured.to_dict() == {
        "source": "tool",
        "component": "nl2sql",
        "fact": "keep me",
        "trace_id": "tid",
    }
