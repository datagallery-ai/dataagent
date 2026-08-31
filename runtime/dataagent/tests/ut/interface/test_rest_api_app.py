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

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from dataagent.interface.rest_api.app import app, get_data_agent_service


class _StubDataAgentService:
    """Return the submitted query without invoking the real agent."""

    async def query(self, query: str) -> dict[str, str]:
        return {"query": query}


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
