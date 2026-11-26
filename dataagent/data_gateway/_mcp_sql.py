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
"""SQL access through a Streamable HTTP MCP read tool."""

import asyncio
import json
from typing import Any

import httpx
import mcp
from mcp.client import streamable_http
from mcp.shared.exceptions import McpError

from ._sql_drivers import MCPConfig, MCPInfrastructureError, SqlService


class MCPSqlService(SqlService):
    """SQL service called in worker threads, with one MCP session per query."""

    def __init__(self, config: MCPConfig):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    async def _call_query_async(self, sql: str):
        try:
            async with asyncio.timeout(self.config.timeout):
                async with httpx.AsyncClient(timeout=self.config.timeout) as http_client:
                    async with streamable_http.streamable_http_client(
                        self.config.path, http_client=http_client
                    ) as (read, write, _):
                        async with mcp.ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            while not any(
                                tool.name == self.config.read_tool for tool in tools.tools
                            ):
                                if not tools.nextCursor:
                                    raise MCPInfrastructureError(
                                        f"MCP SQL read tool not found: {self.config.read_tool}"
                                    )
                                tools = await session.list_tools(
                                    params=mcp.types.PaginatedRequestParams(cursor=tools.nextCursor)
                                )
                            try:
                                return await session.call_tool(
                                    self.config.read_tool, {"query": sql}
                                )
                            except McpError as exc:
                                if exc.error.code in {
                                    httpx.codes.REQUEST_TIMEOUT,
                                    mcp.types.CONNECTION_CLOSED,
                                } or (
                                    exc.error.code == 32600
                                    and exc.error.message == "Session terminated"
                                ):
                                    raise
                                return exc
        except TimeoutError as exc:
            raise MCPInfrastructureError(
                f"MCP SQL request timed out after {self.config.timeout} seconds"
            ) from exc
        except Exception as exc:
            raise MCPInfrastructureError(exc) from exc

    def execute(
        self, sql: str, *, limit: int = -1
    ) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        try:
            response = asyncio.run(self._call_query_async(sql))
            if isinstance(response, McpError):
                return None, None, response.error.message or str(response)
            texts = [
                item.text for item in response.content if getattr(item, "type", None) == "text"
            ]
            if response.isError:
                return None, None, "\n".join(texts) or "MCP SQL tool failed"
            payload = getattr(response, "structuredContent", None)
            if payload is None:
                if len(texts) != 1:
                    raise ValueError(
                        "MCP SQL tool must return one JSON result or structuredContent"
                    )
                payload = json.loads(texts[0])
            if not isinstance(payload, dict):
                raise ValueError("MCP SQL result must be an object")
            if payload.get("success") is False or payload.get("error"):
                return (
                    None,
                    None,
                    str(payload.get("error") or payload.get("message") or "MCP SQL query failed"),
                )
            columns, rows = payload.get("columns"), payload.get("rows")
            if not isinstance(columns, list) or not all(
                isinstance(column, str) for column in columns
            ):
                raise ValueError("MCP SQL result must contain a columns list of strings")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("MCP SQL result must contain a rows list of objects")
            # The remote tool has no fetch-limit argument; retain one extra row for truncation.
            rows = rows[: limit + 1] if limit >= 0 else rows
            try:
                values = [tuple(row[column] for column in columns) for row in rows]
            except KeyError as exc:
                raise ValueError(f"MCP SQL row missing column: {exc.args[0]!r}") from exc
            return columns, values, None
        except MCPInfrastructureError:
            raise
        except Exception as exc:
            raise MCPInfrastructureError(exc) from exc

    def explain(self, sql: str) -> str | None:
        """Submit EXPLAIN through the same read tool, without executing a fallback query."""
        _, _, error = self.execute(f"{self.config.explain_prefix} {sql}")
        return error
