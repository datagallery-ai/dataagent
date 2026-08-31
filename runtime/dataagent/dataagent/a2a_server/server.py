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
"""Assemble and run an A2A 1.0 server for a DataAgent."""

from a2a.server.agent_execution import AgentExecutor
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import (
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import AgentCard, SendMessageRequest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_A2A_STREAMING_METADATA_KEY = "dataagent_streaming"


class _DataAgentRequestHandler(DefaultRequestHandler):
    """Mark A2A message mode so DataAgentExecutor can choose chat or astream."""

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext):
        """Handle non-streaming messages through DataAgent.chat()."""
        params.metadata[_A2A_STREAMING_METADATA_KEY] = False
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params: SendMessageRequest, context: ServerCallContext):
        """Handle streaming messages through DataAgent.astream()."""
        params.metadata[_A2A_STREAMING_METADATA_KEY] = True
        async for event in super().on_message_send_stream(params, context):
            yield event


def _make_bearer_middleware(token: str, exclude_paths: tuple[str, ...]):
    """Create a Bearer token authentication middleware."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        """Validate Bearer token on every request, skipping excluded paths."""

        async def dispatch(self, request: Request, call_next):
            """Check Authorization header against the expected token.

            Returns 401 if the token is missing or invalid, otherwise
            forwards the request to the next middleware/route handler.
            """
            if request.url.path in exclude_paths:
                return await call_next(request)

            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != token:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: invalid or missing Bearer token"},
                )
            return await call_next(request)

    return Middleware(BearerAuthMiddleware)


def _create_dynamic_agent_card_routes(
    agent_card: AgentCard,
    jsonrpc_path: str,
    rest_path: str,
) -> list:
    """Create agent card routes that use the requestʼs Host header for interface URLs.

    Clients from any network can discover the correct URLs instead of always
    receiving 127.0.0.1.
    """

    async def _get_agent_card(request: Request) -> JSONResponse:
        base_card = agent_card_to_dict(agent_card)
        host = request.headers.get("Host", "127.0.0.1:9999")
        scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme or "http")
        base_url = f"{scheme}://{host}"

        for iface in base_card.get("supportedInterfaces", []):
            protocol = iface.get("protocolBinding", "")
            if protocol == "JSONRPC":
                iface["url"] = f"{base_url}{jsonrpc_path}"
            elif protocol == "HTTP+JSON":
                iface["url"] = f"{base_url}{rest_path}"

        return JSONResponse(base_card)

    return [
        Route(path=AGENT_CARD_WELL_KNOWN_PATH, endpoint=_get_agent_card, methods=["GET"]),
    ]


def create_a2a_server(
    agent_card: AgentCard,
    executor: AgentExecutor,
    jsonrpc_path: str = "/a2a/jsonrpc",
    rest_path: str = "/a2a/rest",
    enable_v0_3_compat: bool = False,
    auth_token: str | None = None,
    auth_exclude_paths: tuple[str, ...] = ("/.well-known/agent-card.json",),
) -> Starlette:
    """Create a Starlette application with A2A 1.0 routes.

    Args:
        agent_card: The A2A AgentCard for this server.
        executor: The AgentExecutor that handles requests.
        jsonrpc_path: JSON-RPC route path (default: /a2a/jsonrpc).
        rest_path: REST route path (default: /a2a/rest).
        enable_v0_3_compat: Enable backward compatibility with A2A v0.3 clients.
        auth_token: Bearer token for authentication. If None, no auth is required.
        auth_exclude_paths: Paths to exclude from auth checks (default: AgentCard discovery).

    Returns:
        A configured Starlette application.
    """
    task_store = InMemoryTaskStore()

    request_handler = _DataAgentRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    routes: list = []
    routes.extend(_create_dynamic_agent_card_routes(agent_card, jsonrpc_path, rest_path))
    routes.extend(create_jsonrpc_routes(request_handler, rpc_url=jsonrpc_path, enable_v0_3_compat=enable_v0_3_compat))
    routes.extend(create_rest_routes(request_handler, path_prefix=rest_path, enable_v0_3_compat=enable_v0_3_compat))

    middleware: list = []
    if auth_token:
        middleware.append(_make_bearer_middleware(auth_token, exclude_paths=auth_exclude_paths))

    app = Starlette(routes=routes, middleware=middleware)
    return app


def run_a2a_server(
    agent_card: AgentCard,
    executor: AgentExecutor,
    host: str = "",
    port: int = 9999,
    jsonrpc_path: str = "/a2a/jsonrpc",
    rest_path: str = "/a2a/rest",
    auth_token: str | None = None,
    auth_exclude_paths: tuple[str, ...] = ("/.well-known/agent-card.json",),
) -> None:
    """Run the A2A server using uvicorn.

    Args:
        agent_card: The A2A AgentCard.
        executor: The AgentExecutor.
        host: Server host.
        port: Server port.
        jsonrpc_path: JSON-RPC route path.
        rest_path: REST route path.
        auth_token: Bearer token for authentication. If None, no auth is required.
        auth_exclude_paths: Paths excluded from auth checks.
    """
    import uvicorn

    if not host:
        raise ValueError("Host must be specified")
    app = create_a2a_server(
        agent_card=agent_card,
        executor=executor,
        jsonrpc_path=jsonrpc_path,
        rest_path=rest_path,
        auth_token=auth_token,
        auth_exclude_paths=auth_exclude_paths,
    )

    uvicorn.run(app, host=host, port=port, log_level="info")
