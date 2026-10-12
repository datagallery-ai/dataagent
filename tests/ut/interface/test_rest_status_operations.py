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
from __future__ import annotations

from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from dataagent.actions.tools.semantic_tool import semantic_client
from dataagent.interface.rest_api import app as rest_app
from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.rest_api.start_service import build_startup_plan

OPERATION_PATH = "/api/agent/operation"
_AGENT_FAILURE = {
    "success": False,
    "code": "WORKFLOW-AGENT-001",
    "http_status": 500,
    "component": "agent",
    "retryable": False,
}


class _Config:
    def __init__(self, base_url: str, scenario: str | None = None) -> None:
        self._base_url = base_url
        self._scenario = scenario

    def get(self, key: str, default: Any = None) -> Any:
        if key == "SEMANTIC_LAYER.base_url":
            return self._base_url
        if key == "SEMANTIC_LAYER.scenario":
            return self._scenario
        return default


class _Agent:
    def __init__(self, base_url: str, scenario: str | None = None) -> None:
        self.type = "nl2sql"
        self.config = _Config(base_url, scenario)


class _Response:
    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.text = "" if payload is None else str(payload)
        self.url = "http://semantic.local/api/semantic/v1/status"
        self.reason_phrase = "OK" if status_code < 400 else "Error"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", str(self.url))
            raise httpx.HTTPStatusError("semantic error", request=request, response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._payload


class _Http:
    calls: list[dict[str, Any]] = []
    response: _Response | None = None


class _Client:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def get(self, url: str, *, params: Any = None, headers: dict[str, str] | None = None) -> _Response:
        _Http.calls.append({"url": url, "params": params, "headers": headers})
        assert _Http.response is not None
        return _Http.response

    def close(self) -> None:
        return None


def _trace_body(status: str) -> dict[str, Any]:
    return {
        "items": [
            {
                "traceId": "T1",
                "ingestionScenario": "traffic_insight",
                "status": status,
                "createdAt": "2026-09-24T04:00:00Z",
                "updatedAt": "2026-09-24T04:00:00Z",
            }
        ],
        "limit": 20,
        "offset": 0,
        "total": 1,
    }


def _scene(tmp_path, scenario: str):
    path = tmp_path / f"{scenario}.yaml"
    path.write_text(yaml.safe_dump({"DATABASE": {"perceptor_type": scenario}}), encoding="utf-8")
    return path


def _routed_service(
    tmp_path,
    urls: dict[str, str],
    scenarios: dict[str, str] | None = None,
) -> DataAgentService:
    plan = build_startup_plan([f"{name}={_scene(tmp_path, name)}" for name in urls])
    assert plan.table is not None
    service = DataAgentService(table=plan.table)
    configured = scenarios or {}
    service._agents = {name: _Agent(url, configured.get(name)) for name, url in urls.items()}
    service._agent = service._agents[plan.table.default]
    service._cached_agent_type = "nl2sql"
    return service


def _install(monkeypatch: pytest.MonkeyPatch, service: DataAgentService) -> TestClient:
    _Http.calls = []
    _Http.response = None
    monkeypatch.setattr(semantic_client.httpx, "Client", _Client)
    monkeypatch.setattr(semantic_client, "httpx_verify", lambda service="llm": False)
    monkeypatch.setattr(service, "initialize", lambda: None)
    rest_app.app.dependency_overrides.clear()
    rest_app._data_agent_service = service
    return TestClient(rest_app.app)


def _assert_failure(response, message: str) -> None:
    assert response.status_code == 500
    assert response.json() == {"result": {**_AGENT_FAILURE, "message": message}}


def test_semantic_status_up_uses_default_scenario_base_url(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _routed_service(
        tmp_path,
        {
            "business_twin": "http://default.example:1",
            "traffic_insight": "http://traffic.example:2",
        },
    )
    with _install(monkeypatch, service) as client:
        _Http.response = _Response({"status": "UP", "timestamp": "2026-07-31T19:35:05Z"})
        response = client.post(OPERATION_PATH, json={"type": "semantic_status", "content": {}})

    assert response.status_code == 200
    assert response.json() == {"result": {"success": True, "status": "UP"}}
    assert _Http.calls == [
        {
            "url": "http://default.example:1/api/semantic/v1/health",
            "params": None,
            "headers": None,
        }
    ]


def test_semantic_status_on_bare_config_uses_that_base_url(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "only.yaml"
    path.write_text("{}", encoding="utf-8")
    service = DataAgentService(config_path=path)
    service._agent = _Agent("http://only.example:9")
    service._cached_agent_type = "nl2sql"

    with _install(monkeypatch, service) as client:
        _Http.response = _Response({"status": "UP"})
        response = client.post(OPERATION_PATH, json={"type": "semantic_status", "content": {}})

    assert response.status_code == 200
    assert response.json()["result"] == {"success": True, "status": "UP"}
    assert _Http.calls[0]["url"] == "http://only.example:9/api/semantic/v1/health"
    assert _Http.calls[0]["headers"] is None


@pytest.mark.parametrize(
    "response_body",
    [
        _Response(
            {"error": "down", "errorCode": "SEMANTIC_DOWN", "errorMessage": "semantic exploded"},
            status_code=500,
        ),
        _Response({"status": "DOWN", "timestamp": "2026-07-31T19:35:05Z"}),
    ],
)
def test_semantic_status_downstream_failure_returns_agent_envelope(
    tmp_path, monkeypatch: pytest.MonkeyPatch, response_body: _Response
) -> None:
    service = _routed_service(tmp_path, {"business_twin": "http://default.example:1"})

    with _install(monkeypatch, service) as client:
        _Http.response = response_body
        response = client.post(OPERATION_PATH, json={"type": "semantic_status", "content": {}})

    _assert_failure(response, "semantic service is not UP")
    assert "semantic exploded" not in response.text
    assert "errorCode" not in response.text
    assert "errorMessage" not in response.text


@pytest.mark.parametrize(
    ("status", "completed"),
    [("normal", True), ("updating", False), ("failed", False)],
)
def test_ingestion_status_completion(tmp_path, monkeypatch: pytest.MonkeyPatch, status: str, completed: bool) -> None:
    service = _routed_service(
        tmp_path,
        {
            "business_twin": "http://default.example:1",
            "traffic_insight": "http://traffic.example:2",
        },
        scenarios={"traffic_insight": "traffic_insight"},
    )
    with _install(monkeypatch, service) as client:
        _Http.response = _Response(_trace_body(status))
        response = client.post(
            OPERATION_PATH,
            json={"type": "ingestion_status", "content": {"scenario": "traffic_insight", "traceId": "T1"}},
        )

    assert response.status_code == 200
    assert response.json() == {"result": {"success": True, "status": status, "completed": completed}}
    assert _Http.calls == [
        {
            "url": "http://traffic.example:2/api/semantic/v1/ingestion-traces",
            "params": {"traceId": "T1"},
            "headers": {"X-Semantic-Scenario": "traffic_insight"},
        }
    ]


def test_ingestion_updating_and_failed_keep_distinct_status(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both are incomplete. Callers tell them apart by the semantic ``status`` string."""
    service = _routed_service(
        tmp_path,
        {"traffic_insight": "http://traffic.example:2"},
        scenarios={"traffic_insight": "traffic_insight"},
    )
    body = {"type": "ingestion_status", "content": {"scenario": "traffic_insight", "traceId": "T1"}}

    with _install(monkeypatch, service) as client:
        _Http.response = _Response(_trace_body("updating"))
        updating = client.post(OPERATION_PATH, json=body)
        _Http.response = _Response(_trace_body("failed"))
        failed = client.post(OPERATION_PATH, json=body)

    assert updating.status_code == 200
    assert failed.status_code == 200
    updating_result = updating.json()["result"]
    failed_result = failed.json()["result"]
    assert updating_result == {"success": True, "status": "updating", "completed": False}
    assert failed_result == {"success": True, "status": "failed", "completed": False}
    assert updating_result["status"] != failed_result["status"]
    assert [call["url"] for call in _Http.calls] == [
        "http://traffic.example:2/api/semantic/v1/ingestion-traces",
        "http://traffic.example:2/api/semantic/v1/ingestion-traces",
    ]
    assert all(call["params"] == {"traceId": "T1"} for call in _Http.calls)
    assert all(call["headers"] == {"X-Semantic-Scenario": "traffic_insight"} for call in _Http.calls)


def test_ingestion_status_omits_header_when_scenario_unset(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _routed_service(tmp_path, {"traffic_insight": "http://traffic.example:2"})

    with _install(monkeypatch, service) as client:
        _Http.response = _Response(_trace_body("normal"))
        response = client.post(
            OPERATION_PATH,
            json={"type": "ingestion_status", "content": {"scenario": "traffic_insight", "traceId": "T1"}},
        )

    assert response.status_code == 200
    assert _Http.calls[0]["headers"] is None


def test_semantic_status_ignores_configured_scenario(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _routed_service(
        tmp_path,
        {"business_twin": "http://default.example:1"},
        scenarios={"business_twin": "business_twin"},
    )

    with _install(monkeypatch, service) as client:
        _Http.response = _Response({"status": "UP", "timestamp": "2026-07-31T19:35:05Z"})
        response = client.post(OPERATION_PATH, json={"type": "semantic_status", "content": {}})

    assert response.status_code == 200
    assert _Http.calls == [
        {
            "url": "http://default.example:1/api/semantic/v1/health",
            "params": None,
            "headers": None,
        }
    ]


def test_ingestion_status_unknown_scenario_is_422(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _routed_service(tmp_path, {"traffic_insight": "http://traffic.example:2"})

    with _install(monkeypatch, service) as client:
        response = client.post(
            OPERATION_PATH,
            json={"type": "ingestion_status", "content": {"scenario": "missing", "traceId": "T1"}},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown scenario: missing"}
    assert _Http.calls == []


def test_ingestion_status_on_bare_config_is_unknown_scenario(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "only.yaml"
    path.write_text("{}", encoding="utf-8")
    service = DataAgentService(config_path=path)
    service._agent = _Agent("http://only.example:9")
    service._cached_agent_type = "nl2sql"

    with _install(monkeypatch, service) as client:
        response = client.post(
            OPERATION_PATH,
            json={"type": "ingestion_status", "content": {"scenario": "traffic_insight", "traceId": "T1"}},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown scenario: traffic_insight"}
    assert _Http.calls == []


def test_ingestion_trace_not_found_returns_agent_envelope(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _routed_service(tmp_path, {"traffic_insight": "http://traffic.example:2"})

    with _install(monkeypatch, service) as client:
        _Http.response = _Response(
            {
                "error": "METAVISOR-404-00-040",
                "errorCode": "INGESTION_TRACE_NOT_FOUND",
                "errorMessage": "ingestion trace not found: T1",
            },
            status_code=404,
        )
        response = client.post(
            OPERATION_PATH,
            json={"type": "ingestion_status", "content": {"scenario": "traffic_insight", "traceId": "T1"}},
        )

    _assert_failure(response, "ingestion trace not found")
    assert "METAVISOR-404-00-040" not in response.text
    assert "errorCode" not in response.text
    assert "errorMessage" not in response.text
    assert "ingestion trace not found: T1" not in response.text
