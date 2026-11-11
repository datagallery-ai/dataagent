import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, TextContent

from dataagent.actions.tools.mcp import MCPClientWrapper
from dataagent.agents.nl2sql.utils.sql_service import build_sql_service
from dataagent.core.errors import DataAgentError


@pytest.fixture
def backend(monkeypatch):
    call = AsyncMock()

    async def connected(self, operation):
        return await operation(SimpleNamespace(call_tool=call))

    monkeypatch.setattr(MCPClientWrapper, "_execute_with_connection", connected)
    return call


@pytest.mark.parametrize("prefix", [None, "EXPLAIN"])
@pytest.mark.parametrize("structured", [False, True])
def test_query_and_explain(backend, prefix, structured):
    config = {"explain_prefix": prefix} if prefix else {}
    payload = {"columns": ["id", "name"], "rows": [{"name": None, "id": 1}]}
    backend.return_value = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])
    if structured:
        backend.return_value = CallToolResult(content=[], structuredContent=payload)
    with build_sql_service("mcp", {"path": "http://mcp.invalid/mcp", **config}) as service:
        assert service.execute("SELECT 1") == (["id", "name"], [(1, None)], None)
        assert service.explain("SELECT 1") is None
    backend.assert_awaited_with(name="dme_read_query", arguments={"query": f"{prefix or 'EXPLAIN PLAN FOR'} SELECT 1"})


@pytest.mark.parametrize("kind", ["direct", "nested", "transport"])
def test_errors(backend, kind):
    error = McpError(ErrorData(code=-32603, message="SQL rejected"))
    backend.side_effect = error if kind == "direct" else ExceptionGroup("outer", [ExceptionGroup("inner", [error])])
    if kind == "transport":
        backend.side_effect = OSError("disconnected")
    service = build_sql_service("mcp", {"path": "http://mcp.invalid/mcp"})
    if kind == "transport":
        with pytest.raises(DataAgentError, match="disconnected"):
            service.execute("SELECT 1")
    else:
        assert service.execute("SELECT 1") == (None, None, "SQL rejected")
