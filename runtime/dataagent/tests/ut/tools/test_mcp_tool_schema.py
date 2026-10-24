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
"""MCPToolWrapper schema conversion for properties with no top-level ``type``.

A JSON Schema optional parameter is normally written ``anyOf: [{type: X},
{type: null}]`` with ``default: null`` — it is what FastMCP emits for every
``Optional[...]`` annotation — and such a property has no ``type`` key at all.
Reading only ``prop_def["type"]`` therefore fell through to ``str`` for the
whole class of them, and the tool then rejected both the value the model was
supposed to send and the null it sends when it wants the default.
"""

from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig, MCPToolWrapper
from dataagent.core.managers.action_manager.manager import call_sync_with_event_loop


class _RecordingClient(MCPClientWrapper):
    """Records the arguments a call would have sent instead of reaching a server."""

    def __init__(self) -> None:
        super().__init__(MCPServerConfig.create_stdio_config("recorder", "python", ["-m", "dummy"]))
        self.sent: dict[str, Any] | None = None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.sent = arguments
        return CallToolResult(content=[TextContent(type="text", text="ok")])


def _tool(properties: dict[str, Any], required: list[str] | None = None) -> MCPToolWrapper:
    return MCPToolWrapper(
        _RecordingClient(),
        MCPTool(
            name="explore",
            description="Explore the graph",
            inputSchema={"type": "object", "properties": properties, "required": required or []},
        ),
    )


def _param(tool: MCPToolWrapper, name: str):
    return next(p for p in tool.get_schema().parameters if p.name == name)


def test_anyof_optional_integer_is_a_nullable_int_not_a_string() -> None:
    """``anyOf: [integer, null]`` + ``default: null`` — what FastMCP emits for ``Optional[int]``."""
    tool = _tool(
        {
            "node_id": {"type": "string"},
            "max_nodes": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "default": None,
                "description": "Maximum nodes to return",
            },
        },
        required=["node_id"],
    )
    schema = tool.get_schema()

    max_nodes = _param(tool, "max_nodes")
    assert max_nodes.type == int | None
    assert max_nodes.required is False
    # The model is shown an integer, not the string the top-level-``type`` read produced.
    assert schema.to_openai_function()["parameters"]["properties"]["max_nodes"]["type"] == "integer"

    assert schema.validate_input({"node_id": "n1"}) == (True, None)
    assert schema.validate_input({"node_id": "n1", "max_nodes": 50}) == (True, None)
    assert schema.validate_input({"node_id": "n1", "max_nodes": None}) == (True, None)
    # Still a real constraint: nullable widens the type, it does not remove it.
    assert schema.validate_input({"node_id": "n1", "max_nodes": "fifty"})[0] is False


def test_oneof_is_read_the_same_way_as_anyof() -> None:
    """``oneOf`` carries the same union; the first non-null branch is the type."""
    tool = _tool({"threshold": {"oneOf": [{"type": "number"}, {"type": "null"}], "default": None}})
    schema = tool.get_schema()

    assert _param(tool, "threshold").type == float | None
    # "float" is what ``_type_to_string`` has always spelled ``number``; pinned as-is
    # because nothing on the LLM path reads it — the planner binds tools through
    # ``to_langchain_tool``, which round-trips the server's own ``anyOf`` instead.
    assert schema.to_openai_function()["parameters"]["properties"]["threshold"]["type"] == "float"
    assert schema.validate_input({"threshold": 0.5}) == (True, None)
    assert schema.validate_input({"threshold": None}) == (True, None)
    assert schema.validate_input({}) == (True, None)


def test_property_without_a_type_constrains_nothing() -> None:
    """No declared type means no narrowing — ``str`` would reject valid values."""
    tool = _tool({"payload": {"description": "Anything the server accepts"}})
    schema = tool.get_schema()

    assert _param(tool, "payload").type is Any
    for value in (42, "text", None, {"k": "v"}, [1, 2]):
        assert schema.validate_input({"payload": value}) == (True, None), value


def test_type_list_with_null_is_nullable() -> None:
    """The ``type: [X, "null"]`` spelling of the same union."""
    tool = _tool({"limit": {"type": ["integer", "null"], "default": None}})
    schema = tool.get_schema()

    assert _param(tool, "limit").type == int | None
    assert schema.validate_input({"limit": 10}) == (True, None)
    assert schema.validate_input({"limit": None}) == (True, None)


@pytest.mark.asyncio
async def test_null_defaulted_argument_is_not_forwarded_to_the_server() -> None:
    """An explicit ``None`` for a null-defaulted parameter means "unset"; the server decides."""
    tool = _tool(
        {
            "node_id": {"type": "string"},
            "max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        },
        required=["node_id"],
    )

    await tool._async_call(node_id="n1", max_nodes=None)
    assert tool.mcp_client.sent == {"node_id": "n1"}

    await tool._async_call(node_id="n1", max_nodes=50)
    assert tool.mcp_client.sent == {"node_id": "n1", "max_nodes": 50}


@pytest.mark.asyncio
async def test_explicit_none_survives_when_the_parameter_has_a_real_default() -> None:
    """Dropping the null there would silently swap it for a different value."""
    tool = _tool({"max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 100}})

    await tool._async_call(max_nodes=None)
    assert tool.mcp_client.sent == {"max_nodes": None}


def test_call_accepts_the_optional_integer_end_to_end() -> None:
    """The whole path a call takes: validate, then send what the server should see.

    Driven through ``call_sync_with_event_loop`` because that is how ``ToolManager``
    reaches a sync tool, and because ``call`` needs a mounted loop that
    ``asyncio.run`` then clears out from under the next call.
    """
    tool = _tool(
        {
            "node_id": {"type": "string"},
            "max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        },
        required=["node_id"],
    )

    result = call_sync_with_event_loop(tool.call, node_id="n1", max_nodes=None)
    assert result.success is True, result.error
    assert tool.mcp_client.sent == {"node_id": "n1"}

    result = call_sync_with_event_loop(tool.call, node_id="n1", max_nodes=50)
    assert result.success is True, result.error
    assert tool.mcp_client.sent == {"node_id": "n1", "max_nodes": 50}
