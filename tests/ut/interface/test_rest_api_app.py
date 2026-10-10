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

from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from dataagent.interface.rest_api.app import app, get_data_agent_service
from dataagent.interface.rest_api.start_service import UnknownScenarioError

OPERATION_PATH = "/api/agent/operation"
LEGACY_QUERY_PATH = "/api/agent/query"


class _StubDataAgentService:
    """Return the submitted query without invoking the real agent."""

    def resolve_scenario(self, scenario: str | None) -> None:
        """Accept the default scenario used by endpoint tests."""
        _ = scenario

    async def query(self, query: str, scenario: str | None = None) -> dict[str, str]:
        """Return the query for endpoint validation tests."""
        _ = scenario
        return {"query": query}


class _StubSQLSecurityErrorService:
    """Return one mapped SQL security error through either REST mode."""

    _result = {
        "result": {
            "success": False,
            "code": "NL2SQL-SEC-014",
            "message": "Source column is not allowed: missing_column.",
            "http_status": 422,
            "component": "sql_security",
            "retryable": False,
            "errors": [
                {
                    "code": "NL2SQL-SEC-014",
                    "message": "Source column is not allowed: missing_column.",
                }
            ],
        }
    }

    def resolve_scenario(self, scenario: str | None) -> None:
        """Accept the default scenario used by endpoint tests."""
        _ = scenario

    async def query(self, query: str, scenario: str | None = None) -> dict[str, Any]:
        """Return a mapped SQL security error."""
        _ = query, scenario
        return self._result

    async def stream_query(self, query: str, scenario: str | None = None) -> AsyncGenerator[dict[str, Any], None]:
        """Yield a mapped SQL security error as the final stream result."""
        _ = query, scenario
        yield {"event": "result", "data": self._result}


class _StubPromptService:
    """Echo the scenario and markdown length through canned view/update payloads."""

    async def view_prompt(self, scenario: str) -> dict[str, Any]:
        """Echo the scenario through the markdown field."""
        return {
            "result": {
                "success": True,
                "markdown": f"view:{scenario}",
                "updated_at": "2026-09-30T08:15:23.123456+00:00",
            }
        }

    async def update_prompt(self, scenario: str, markdown: str) -> dict[str, Any]:
        """Echo the scenario and markdown length through the updated_at field."""
        return {"result": {"success": True, "updated_at": f"update:{scenario}:{len(markdown)}"}}


class _StubUnknownScenarioService:
    """Raise UnknownScenarioError for both prompt operations."""

    async def view_prompt(self, scenario: str) -> dict[str, Any]:
        """Reject the scenario the same way the query operation does."""
        raise UnknownScenarioError(scenario)

    async def update_prompt(self, scenario: str, markdown: str) -> dict[str, Any]:
        """Reject the scenario the same way the query operation does."""
        _ = markdown
        raise UnknownScenarioError(scenario)


class _StubPromptFailureService:
    """Return the file-layer failure envelope through both prompt operations."""

    _result = {
        "result": {
            "success": False,
            "code": "WORKFLOW-AGENT-001",
            "message": "CORE.perceptor.user_sql_rules is not configured: /data/business_twin.yaml",
            "http_status": 500,
            "component": "agent",
            "retryable": False,
        }
    }

    async def view_prompt(self, scenario: str) -> dict[str, Any]:
        """Return a file-layer failure envelope."""
        _ = scenario
        return self._result

    async def update_prompt(self, scenario: str, markdown: str) -> dict[str, Any]:
        """Return a file-layer failure envelope."""
        _ = scenario, markdown
        return self._result


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_data_agent_service] = _StubDataAgentService
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)


def test_query_endpoint_requires_type_and_content(client: TestClient) -> None:
    """type is the discriminator; missing type is rejected."""
    response = client.post(OPERATION_PATH, json={"content": {"query": "hello"}})

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "union_tag_not_found"


def test_query_endpoint_accepts_type_query(client: TestClient) -> None:
    """Query body is type + content{query, stream}. Output shape stays the same."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "query", "content": {"query": "hello", "stream": False}},
    )

    assert response.status_code == 200
    assert response.json() == {"query": "hello"}


def test_legacy_query_path_keeps_original_body(client: TestClient) -> None:
    """POST /api/agent/query keeps the original {query, stream} contract."""
    response = client.post(
        LEGACY_QUERY_PATH,
        json={"query": "hello", "stream": False},
    )

    assert response.status_code == 200
    assert response.json() == {"query": "hello"}


def test_legacy_query_path_rejects_operation_body(client: TestClient) -> None:
    """Old query callers must not be required to send type/content."""
    response = client.post(
        LEGACY_QUERY_PATH,
        json={"type": "query", "content": {"query": "hello", "stream": False}},
    )

    assert response.status_code == 422


def test_query_endpoint_rejects_string_content(client: TestClient) -> None:
    """content must be an object for type=query, not a string."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "query", "content": "hello"},
    )

    assert response.status_code == 422


def test_operation_endpoint_rejects_unknown_type(client: TestClient) -> None:
    """Operations outside the registered types must not fall through to query."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "other", "content": {"scenario": "business_twin"}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "union_tag_invalid"


def test_query_endpoint_rejects_legacy_field_names(client: TestClient) -> None:
    """Old top-level field names operation/query are not accepted."""
    response = client.post(
        OPERATION_PATH,
        json={"operation": "query", "query": "hello"},
    )

    assert response.status_code == 422


def test_query_endpoint_rejects_unknown_body_fields(client: TestClient) -> None:
    response = client.post(
        LEGACY_QUERY_PATH,
        json={"query": "hello", "unknown_field": True},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"][-1] == "unknown_field"


def test_operation_endpoint_rejects_unknown_body_fields(client: TestClient) -> None:
    response = client.post(
        OPERATION_PATH,
        json={"type": "query", "content": {"query": "hello"}, "unknown_field": True},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"][-1] == "unknown_field"


def test_legacy_query_path_returns_detailed_sql_security_error() -> None:
    """Original /query body still returns mapped SQL security details."""
    app.dependency_overrides[get_data_agent_service] = _StubSQLSecurityErrorService
    try:
        response = TestClient(app).post(
            LEGACY_QUERY_PATH,
            json={"query": "show missing column"},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 422
    assert response.json() == _StubSQLSecurityErrorService._result


def test_query_endpoint_returns_detailed_sql_security_error() -> None:
    """Non-streaming REST responses should preserve mapped SQL security details."""
    app.dependency_overrides[get_data_agent_service] = _StubSQLSecurityErrorService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "query", "content": {"query": "show missing column"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 422
    assert response.json() == _StubSQLSecurityErrorService._result


def test_streaming_query_returns_detailed_sql_security_error() -> None:
    """Streaming REST responses should preserve mapped SQL security details in the result event."""
    app.dependency_overrides[get_data_agent_service] = _StubSQLSecurityErrorService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "query", "content": {"query": "show missing column", "stream": True}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 200
    assert "event: result" in response.text
    assert '"code": "NL2SQL-SEC-014"' in response.text
    assert "Source column is not allowed: missing_column." in response.text
    assert "SCHEMA-002" not in response.text


def test_view_operation_returns_markdown_and_updated_at() -> None:
    """type=view dispatches to the service and keeps the result envelope."""
    app.dependency_overrides[get_data_agent_service] = _StubPromptService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "view", "content": {"scenario": "business_twin"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 200
    assert response.json() == {
        "result": {
            "success": True,
            "markdown": "view:business_twin",
            "updated_at": "2026-09-30T08:15:23.123456+00:00",
        }
    }


def test_update_operation_returns_updated_at() -> None:
    """type=update passes scenario and markdown through to the service."""
    app.dependency_overrides[get_data_agent_service] = _StubPromptService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "update", "content": {"scenario": "traffic_insight", "markdown": "# new rules"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 200
    assert response.json() == {"result": {"success": True, "updated_at": "update:traffic_insight:11"}}


def test_update_operation_accepts_max_length_markdown() -> None:
    """65536 characters is the inclusive upper bound and passes validation."""
    app.dependency_overrides[get_data_agent_service] = _StubPromptService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "update", "content": {"scenario": "business_twin", "markdown": "x" * 65536}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 200
    assert response.json() == {"result": {"success": True, "updated_at": "update:business_twin:65536"}}


def test_update_operation_rejects_empty_markdown(client: TestClient) -> None:
    """An empty markdown is rejected before the service is called; no file write."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "update", "content": {"scenario": "business_twin", "markdown": ""}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "string_too_short"
    assert detail["loc"][-1] == "markdown"


def test_update_operation_rejects_overlong_markdown(client: TestClient) -> None:
    """One character over the bound is rejected before the service is called; no file write."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "update", "content": {"scenario": "business_twin", "markdown": "x" * 65537}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "string_too_long"
    assert detail["loc"][-1] == "markdown"


def test_view_operation_requires_scenario(client: TestClient) -> None:
    """content.scenario is mandatory for type=view."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "view", "content": {}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "missing"
    assert detail["loc"][-1] == "scenario"


def test_view_operation_rejects_empty_scenario(client: TestClient) -> None:
    """An empty scenario is rejected instead of falling back to the default."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "view", "content": {"scenario": ""}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "string_too_short"
    assert detail["loc"][-1] == "scenario"


def test_view_operation_rejects_unknown_content_fields(client: TestClient) -> None:
    """Undeclared content fields are rejected for type=view."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "view", "content": {"scenario": "business_twin", "foo": 1}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"][-1] == "foo"


def test_view_operation_rejects_query_fields(client: TestClient) -> None:
    """view never inherits query/stream from the query operation."""
    response = client.post(
        OPERATION_PATH,
        json={"type": "view", "content": {"scenario": "business_twin", "query": "hello", "stream": True}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"][-1] in {"query", "stream"}


def test_view_operation_unknown_scenario_maps_to_422() -> None:
    """Unregistered scenarios keep the query operation's 422 detail body."""
    app.dependency_overrides[get_data_agent_service] = _StubUnknownScenarioService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "view", "content": {"scenario": "missing"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown scenario: missing"}


def test_update_operation_unknown_scenario_maps_to_422() -> None:
    """Unregistered scenarios keep the query operation's 422 detail body."""
    app.dependency_overrides[get_data_agent_service] = _StubUnknownScenarioService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "update", "content": {"scenario": "missing", "markdown": "# rules"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown scenario: missing"}


def test_view_operation_file_failure_maps_to_500() -> None:
    """File-layer failures keep the agent failure envelope and its http_status."""
    app.dependency_overrides[get_data_agent_service] = _StubPromptFailureService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "view", "content": {"scenario": "business_twin"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 500
    assert response.json() == _StubPromptFailureService._result


def test_update_operation_file_failure_maps_to_500() -> None:
    """File-layer failures keep the agent failure envelope and its http_status."""
    app.dependency_overrides[get_data_agent_service] = _StubPromptFailureService
    try:
        response = TestClient(app).post(
            OPERATION_PATH,
            json={"type": "update", "content": {"scenario": "business_twin", "markdown": "# rules"}},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 500
    assert response.json() == _StubPromptFailureService._result
