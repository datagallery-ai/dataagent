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
import json
import os
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Body, Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from dataagent.interface.rest_api.middleware import SecurityLimitsMiddleware, load_rest_api_limits
from dataagent.interface.rest_api.service import DataAgentService


class DataAgentQueryRequest(BaseModel):
    """DataAgent query request."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    stream: bool = False


_data_agent_service: DataAgentService | None = None
_CONFIG_ENV_NAME = "DATAAGENT_REST_CONFIG"


def get_data_agent_service() -> DataAgentService:
    """Return the singleton DataAgent service."""
    if _data_agent_service is None:
        raise RuntimeError("DataAgent service is not initialized.")
    return _data_agent_service


def agent_error_payload(result: Any) -> dict[str, Any] | None:
    """Return an agent error payload when result carries one."""
    if not isinstance(result, dict):
        return None
    payload = result.get("result")
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return payload
    return None


def sse_event(event: str, data: Any) -> str:
    """Serialize one server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def stream_agent_events(query: str, service: DataAgentService) -> AsyncGenerator[str, None]:
    """Yield DataAgent server-sent events."""
    async for item in service.stream_query(query):
        event = item.get("event")
        data = item.get("data")
        if event is None:
            continue
        if event == "result":
            yield sse_event("result", data)
            continue
        if event == "message":
            yield sse_event("message", data)


app = FastAPI(
    title="DataAgent Service",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_middleware_installed = False


def create_app() -> FastAPI:
    """Create the DataAgent FastAPI app with ingress limits middleware."""
    global _data_agent_service, _middleware_installed
    config_path = os.getenv(_CONFIG_ENV_NAME)
    _data_agent_service = DataAgentService(config_path=config_path) if config_path else None
    limits = load_rest_api_limits(config_path)
    if not _middleware_installed:
        app.add_middleware(SecurityLimitsMiddleware, limits=limits)
        _middleware_installed = True
    return app


@app.on_event("startup")
async def startup_data_agent_service():
    """Initialize DataAgent service during startup."""
    get_data_agent_service().initialize()


@app.get("/health")
async def health_check():
    """Service availability: agent finished initialize() and can accept traffic."""
    service = _data_agent_service
    if service is None or not service.is_ready():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready"},
        )
    return {"status": "ok"}


@app.post("/api/agent/query")
async def query_agent(
    request: DataAgentQueryRequest = Body(...),
    service: DataAgentService = Depends(get_data_agent_service),
):
    """Run one DataAgent query."""
    if request.stream:
        return StreamingResponse(stream_agent_events(request.query, service), media_type="text/event-stream")

    result = await service.query(request.query)
    payload = agent_error_payload(result)
    if payload is not None:
        return JSONResponse(status_code=int(payload.get("http_status", 500)), content=result)
    return result
