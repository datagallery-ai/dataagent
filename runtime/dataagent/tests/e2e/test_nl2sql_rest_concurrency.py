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
"""Real-LLM REST concurrency regression for the NL2SQL engine."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dataagent.core.context.context import ContextFactory
from dataagent.interface.rest_api.middleware import RestApiLimits, SecurityLimitsMiddleware
from dataagent.interface.rest_api.service import DataAgentService


def _write_nl2sql_config(tmp_path: Path) -> Path:
    """Write a minimal NL2SQL config that calls the real DeepSeek-compatible endpoint."""
    schema_path = tmp_path / "schema.md"
    schema_path.write_text(
        "CREATE TABLE employees (employee_id INTEGER, employee_name TEXT, department TEXT);",
        encoding="utf-8",
    )
    config_path = tmp_path / "nl2sql.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "SESSION_ID": "configured-shared-session",
                "AGENT_CONFIG": {"name": "NL2SQL REST load test", "backend": "langgraph", "type": "nl2sql"},
                "MODEL": {
                    "deepseek": {
                        "model_type": "chat",
                        "provider": "deepseek",
                        "params": {"model": "deepseek-v4-flash", "temperature": 0.0},
                    }
                },
                "CORE": {
                    "perceptor": {"schema_mode": "full_schema", "user_schema": str(schema_path)},
                    "generator": {"strategies": ["prompt"], "num_workers": 1, "num_samples": 1},
                },
                "DATABASE": {"db_id": "", "dialect": "sqlite", "engine": "sqlite", "config": {"path": ""}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def _require_deepseek_e2e() -> None:
    """Skip unless the real DeepSeek NL2SQL endpoint is explicitly enabled and configured."""
    if os.getenv("RUN_DEEPSEEK_E2E") != "1":
        pytest.skip("Set RUN_DEEPSEEK_E2E=1 to run the real DeepSeek NL2SQL load regression.")
    if not os.getenv("DEEPSEEK_BASE_URL") or not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required.")


def _assert_no_default_session_workspaces(dataagent_home: Path) -> None:
    """Assert REST created no default session workspace while allowing main's persistent logs."""
    anonymous_root = dataagent_home / "anonymous"
    if not anonymous_root.exists():
        return
    session_workspaces = [path for path in anonymous_root.iterdir() if path.is_dir() and path.name != "logs"]
    assert session_workspaces == []


@pytest.mark.asyncio
async def test_nl2sql_rest_isolates_16_concurrent_real_llm_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sixteen real NL2SQL requests must complete without retained anonymous workspaces or Contexts."""
    _require_deepseek_e2e()

    dataagent_home = tmp_path / "dataagent-home"
    monkeypatch.setenv("DATAAGENT_HOME", str(dataagent_home))
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "false")
    service = DataAgentService(config_path=_write_nl2sql_config(tmp_path))
    service.initialize()

    async def query(request: Request) -> JSONResponse:
        """Run one request through the production DataAgentService boundary."""
        body = await request.json()
        result = await service.query(str(body.get("query", "")))
        payload = result.get("result", {})
        status = int(payload.get("http_status", 500)) if payload.get("success") is False else 200
        return JSONResponse(result, status_code=status)

    app = Starlette(routes=[Route("/api/agent/query", query, methods=["POST"])])
    limits = RestApiLimits(max_concurrency=16, rate_limit_per_minute=10_000)
    app.add_middleware(SecurityLimitsMiddleware, limits=limits)
    transport = ASGITransport(app=app)
    prompt = "Return SQL that lists employee_name for employees in the Sales department."
    ContextFactory.clear_context()

    async with AsyncClient(transport=transport, base_url="http://test", timeout=180.0) as client:
        responses = await asyncio.gather(
            *(client.post("/api/agent/query", json={"query": prompt}) for _ in range(16)),
        )

    assert [response.status_code for response in responses] == [200] * 16
    payloads = [response.json().get("result", {}) for response in responses]
    assert all(payload.get("success") is True for payload in payloads)
    assert all(payload.get("sql") for payload in payloads)
    _assert_no_default_session_workspaces(dataagent_home)
    assert ContextFactory._instances == {}


@pytest.mark.asyncio
async def test_nl2sql_rest_stream_releases_real_llm_request_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real NL2SQL stream must finish successfully and release its request resources."""
    _require_deepseek_e2e()
    dataagent_home = tmp_path / "dataagent-home"
    monkeypatch.setenv("DATAAGENT_HOME", str(dataagent_home))
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "false")
    service = DataAgentService(config_path=_write_nl2sql_config(tmp_path))
    service.initialize()
    ContextFactory.clear_context()

    events = [
        event
        async for event in service.stream_query(
            "Return SQL that lists employee_name for employees in the Sales department."
        )
    ]

    result = next(event.get("data", {}).get("result", {}) for event in events if event.get("event") == "result")
    assert result.get("success") is True
    assert result.get("sql")
    _assert_no_default_session_workspaces(dataagent_home)
    assert ContextFactory._instances == {}
