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
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from dataagent.interface.rest_api.middleware import SecurityLimitsMiddleware, load_rest_api_limits
from dataagent.interface.rest_api.service import DataAgentService


class DataAgentQueryRequest(BaseModel):
    """Original /api/agent/query body. Callers of this path keep this contract."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    stream: bool = False


class QueryContent(BaseModel):
    """Body of the query operation. stream belongs here, not on other types."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    stream: bool = False


class QueryRequest(BaseModel):
    """One northbound operation. type selects the operation; query is implemented first."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["query"] = Field(description="Operation type. query is one operation; more can be added later.")
    content: QueryContent


# Discriminated by `type`. First wave registers query only.
# Later operations add another model; they will not inherit query/stream.
DataAgentRequest = Annotated[QueryRequest, Field(discriminator="type")]
_DATA_AGENT_REQUEST_ADAPTER: TypeAdapter[QueryRequest] = TypeAdapter(DataAgentRequest)


_data_agent_service: DataAgentService | None = None
_CONFIG_ENV_NAME = "DATAAGENT_REST_CONFIG"


def get_data_agent_service() -> DataAgentService:
    """Return the singleton DataAgent service."""
    if _data_agent_service is None:
        raise RuntimeError("DataAgent service is not initialized.")
    return _data_agent_service


async def get_data_agent_request(http_request: Request) -> QueryRequest:
    """Parse a type-discriminated body. Other operations are not registered yet."""
    try:
        payload = await http_request.json()
    except Exception as exc:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body",),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": str(exc)},
                }
            ]
        ) from exc
    try:
        return _DATA_AGENT_REQUEST_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = err.get("loc", ())
            if not loc or loc[0] != "body":
                err = {**err, "loc": ("body", *loc)}
            errors.append(err)
        raise RequestValidationError(errors, body=payload) from exc


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


app = FastAPI(title="DataAgent Service", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
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


async def _dispatch_query(query: str, stream: bool, service: DataAgentService):
    """Run the query operation. Output shape is unchanged."""
    if stream:
        return StreamingResponse(stream_agent_events(query, service), media_type="text/event-stream")

    result = await service.query(query)
    payload = agent_error_payload(result)
    if payload is not None:
        return JSONResponse(status_code=int(payload.get("http_status", 500)), content=result)
    return result


@app.post("/api/agent/query")
async def query_agent(
    request: DataAgentQueryRequest,
    service: DataAgentService = Depends(get_data_agent_service),
):
    """Original query path. Body stays {query, stream}."""
    return await _dispatch_query(request.query, request.stream, service)


@app.post("/api/agent/operation")
async def agent_operation(
    request: QueryRequest = Depends(get_data_agent_request),
    service: DataAgentService = Depends(get_data_agent_service),
):
    """New operation path. First wave only implements type=query."""
    if isinstance(request, QueryRequest):
        return await _dispatch_query(request.content.query, request.content.stream, service)
    raise AssertionError(f"unsupported type: {getattr(request, 'type', request)}")
