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
"""A2A schema conversion for properties with no top-level ``type``.

The same defect that ``test_mcp_tool_schema`` pins, on the A2A side: a JSON
Schema optional parameter is normally written ``anyOf: [{type: X}, {type:
null}]`` with ``default: null`` and carries no ``type`` key at all, so reading
only ``prop_def["type"]`` degraded the whole class of them to ``str``. The
parameter then rejected both a correct value and the explicit ``null`` a model
sends when it wants the default, and the ``{"type": ["integer", "null"]}``
spelling raised ``TypeError: unhashable type: 'list'`` outright.

Both A2A readers are covered: ``A2AToolWrapper.get_schema`` on the live path,
and ``ToolSchema.from_a2a_tool``, which nothing calls today but which is public
API and carried the identical loop.
"""

import json
from typing import Any

import pytest

import dataagent.actions.tools.a2a as a2a_module
from dataagent.actions.tools.a2a import A2AClientWrapper, A2AToolWrapper, AgentConfig
from dataagent.core.managers.action_manager.schemas import ToolSchema


class _RecordingClient(A2AClientWrapper):
    """Records the parameters a call would have sent instead of reaching an agent."""

    sent: dict[str, Any] | None = None

    async def call_tool(self, tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        type(self).sent = parameters
        return {"result": "ok"}


@pytest.fixture(autouse=True)
def _recording_client(monkeypatch):
    """Make the fresh per-call ``A2AClientWrapper`` a recorder, and reset between tests."""
    _RecordingClient.sent = None
    monkeypatch.setattr(a2a_module, "A2AClientWrapper", _RecordingClient)
    yield
    _RecordingClient.sent = None


def _tool(properties: dict[str, Any], required: list[str] | None = None) -> A2AToolWrapper:
    config = AgentConfig(agent_id="explorer", base_url="http://localhost:11000")
    return A2AToolWrapper(
        _RecordingClient(config),
        "explore",
        {
            "name": "explore",
            "description": "Explore the graph",
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    )


def _param(schema: ToolSchema, name: str):
    return next(p for p in schema.parameters if p.name == name)


# --------------------------------------------------------------------------
# A2AToolWrapper.get_schema
# --------------------------------------------------------------------------


def test_anyof_optional_integer_is_a_nullable_int_not_a_string() -> None:
    """``anyOf: [integer, null]`` + ``default: null`` — the ordinary spelling of ``Optional[int]``."""
    schema = _tool(
        {
            "node_id": {"type": "string"},
            "max_nodes": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "default": None,
                "description": "Maximum nodes to return",
            },
        },
        required=["node_id"],
    ).get_schema()

    max_nodes = _param(schema, "max_nodes")
    assert max_nodes.type == int | None
    assert max_nodes.required is False
    assert max_nodes.description == "Maximum nodes to return"
    # The model is shown an integer, not the string the top-level-``type`` read produced.
    assert schema.to_openai_function()["parameters"]["properties"]["max_nodes"]["type"] == "integer"

    assert schema.validate_input({"node_id": "n1"}) == (True, None)
    assert schema.validate_input({"node_id": "n1", "max_nodes": 50}) == (True, None)
    assert schema.validate_input({"node_id": "n1", "max_nodes": None}) == (True, None)
    # Still a real constraint: nullable widens the type, it does not remove it.
    assert schema.validate_input({"node_id": "n1", "max_nodes": "fifty"})[0] is False


def test_oneof_is_read_the_same_way_as_anyof() -> None:
    """``oneOf`` carries the same union; the first non-null branch is the type."""
    schema = _tool({"threshold": {"oneOf": [{"type": "number"}, {"type": "null"}], "default": None}}).get_schema()

    assert _param(schema, "threshold").type == float | None
    assert schema.validate_input({"threshold": 0.5}) == (True, None)
    assert schema.validate_input({"threshold": None}) == (True, None)
    assert schema.validate_input({}) == (True, None)


def test_property_without_a_type_constrains_nothing() -> None:
    """No declared type means no narrowing — ``str`` would reject valid values."""
    schema = _tool({"payload": {"description": "Anything the agent accepts"}}).get_schema()

    assert _param(schema, "payload").type is Any
    for value in (42, "text", None, {"k": "v"}, [1, 2]):
        assert schema.validate_input({"payload": value}) == (True, None), value


def test_type_list_with_null_is_nullable() -> None:
    """The ``type: [X, "null"]`` spelling, which used to raise ``unhashable type: 'list'``."""
    schema = _tool({"limit": {"type": ["integer", "null"], "default": None}}).get_schema()

    assert _param(schema, "limit").type == int | None
    assert schema.validate_input({"limit": 10}) == (True, None)
    assert schema.validate_input({"limit": None}) == (True, None)


def test_plain_types_are_unchanged() -> None:
    """The shapes that already worked still resolve the same way."""
    schema = _tool(
        {"name": {"type": "string"}, "count": {"type": "integer"}, "ratio": {"type": "number"}},
        required=["name"],
    ).get_schema()

    assert _param(schema, "name").type is str
    assert _param(schema, "count").type is int
    assert _param(schema, "ratio").type is float
    assert _param(schema, "name").required is True
    assert _param(schema, "count").required is False


# --------------------------------------------------------------------------
# ToolSchema.from_a2a_tool — uncalled today, but public API with the same loop
# --------------------------------------------------------------------------


def test_from_a2a_tool_reads_the_same_shapes() -> None:
    schema = ToolSchema.from_a2a_tool(
        {
            "name": "explore",
            "description": "Explore the graph",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
                    "limit": {"type": ["integer", "null"], "default": None},
                    "payload": {"description": "untyped"},
                },
                "required": ["node_id"],
            },
        },
        agent_id="explorer",
    )

    assert _param(schema, "node_id").type is str
    assert _param(schema, "max_nodes").type == int | None
    assert _param(schema, "limit").type == int | None
    assert _param(schema, "payload").type is Any
    assert schema.metadata["agent_id"] == "explorer"
    assert schema.validate_input({"node_id": "n1", "max_nodes": None, "limit": 5, "payload": 42}) == (True, None)


# --------------------------------------------------------------------------
# _omit_unset_nullable_args
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_defaulted_argument_is_not_forwarded_to_the_agent() -> None:
    """An explicit ``None`` for a null-defaulted parameter means "unset"; the agent decides."""
    tool = _tool(
        {
            "node_id": {"type": "string"},
            "max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        },
        required=["node_id"],
    )

    assert (await tool.acall(node_id="n1", max_nodes=None)).success is True
    assert _RecordingClient.sent == {"node_id": "n1"}

    assert (await tool.acall(node_id="n1", max_nodes=50)).success is True
    assert _RecordingClient.sent == {"node_id": "n1", "max_nodes": 50}


@pytest.mark.asyncio
async def test_explicit_none_survives_when_the_parameter_has_a_real_default() -> None:
    """Dropping the null there would silently swap it for a different value."""
    tool = _tool({"max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 100}})

    assert (await tool.acall(max_nodes=None)).success is True
    assert _RecordingClient.sent == {"max_nodes": None}


@pytest.mark.asyncio
async def test_required_none_is_forwarded_untouched() -> None:
    """A required parameter is the caller's to fill; "unset" is not a reading available here."""
    tool = _tool({"node_id": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, required=["node_id"])

    assert (await tool.acall(node_id=None)).success is True
    assert _RecordingClient.sent == {"node_id": None}


@pytest.mark.asyncio
async def test_an_optional_message_null_no_longer_empties_the_outgoing_text() -> None:
    """The concrete cost of forwarding the null on this transport.

    ``A2AClientWrapper.call_tool`` picks the outgoing text with
    ``parameters.get("message", json.dumps(parameters))``, and ``get`` falls back
    only on a *missing* key. So a forwarded ``"message": None`` resolved to
    ``None`` and the remote agent was handed an empty message instead of the
    serialized parameters.
    """
    tool = _tool(
        {
            "query": {"type": "string"},
            "message": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
        required=["query"],
    )

    assert (await tool.acall(query="who ships this?", message=None)).success is True
    sent = _RecordingClient.sent
    assert sent == {"query": "who ships this?"}

    # The line the client would then run, on exactly what it was handed.
    message_text = sent.get("message", json.dumps(sent, ensure_ascii=False))
    assert message_text == '{"query": "who ships this?"}'


def test_sync_call_strips_unset_nulls_on_the_same_terms() -> None:
    """``call`` and ``acall`` reach the agent by different routes; both go through the filter."""
    tool = _tool(
        {
            "node_id": {"type": "string"},
            "max_nodes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        },
        required=["node_id"],
    )

    result = tool.call(node_id="n1", max_nodes=None)
    assert result.success is True, result.error
    assert _RecordingClient.sent == {"node_id": "n1"}

    result = tool.call(node_id="n1", max_nodes=50)
    assert result.success is True, result.error
    assert _RecordingClient.sent == {"node_id": "n1", "max_nodes": 50}
