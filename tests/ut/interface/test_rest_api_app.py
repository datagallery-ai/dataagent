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


class _StubDataAgentService:
    """Return the submitted query without invoking the real agent."""

    async def query(self, query: str) -> dict[str, str]:
        """Return the query for endpoint validation tests."""
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

    async def query(self, query: str) -> dict[str, Any]:
        """Return a mapped SQL security error."""
        _ = query
        return self._result

    async def stream_query(self, query: str) -> AsyncGenerator[dict[str, Any], None]:
        """Yield a mapped SQL security error as the final stream result."""
        _ = query
        yield {"event": "result", "data": self._result}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_data_agent_service] = _StubDataAgentService
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)


def test_query_endpoint_rejects_unknown_body_fields(client: TestClient) -> None:
    response = client.post(
        "/api/agent/query",
        json={"query": "hello", "unknown_field": True},
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"] == ["body", "unknown_field"]


def test_query_endpoint_returns_detailed_sql_security_error() -> None:
    """Non-streaming REST responses should preserve mapped SQL security details."""
    app.dependency_overrides[get_data_agent_service] = _StubSQLSecurityErrorService
    try:
        response = TestClient(app).post("/api/agent/query", json={"query": "show missing column"})
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 422
    assert response.json() == _StubSQLSecurityErrorService._result


def test_streaming_query_returns_detailed_sql_security_error() -> None:
    """Streaming REST responses should preserve mapped SQL security details in the result event."""
    app.dependency_overrides[get_data_agent_service] = _StubSQLSecurityErrorService
    try:
        response = TestClient(app).post(
            "/api/agent/query",
            json={"query": "show missing column", "stream": True},
        )
    finally:
        app.dependency_overrides.pop(get_data_agent_service, None)

    assert response.status_code == 200
    assert "event: result" in response.text
    assert '"code": "NL2SQL-SEC-014"' in response.text
    assert "Source column is not allowed: missing_column." in response.text
    assert "SCHEMA-002" not in response.text
