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
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from dataagent.agents.nl2sql.errors import SchemaNotFoundError
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.nodes.udn_perceptor import UDNPerceptorNode
from dataagent.agents.nl2sql.workflow.state import get_default_state


def _schema() -> dict:
    return {
        "orders": {
            "description": "Orders",
            "columns": {
                "id": {
                    "description": "Order ID",
                    "value_type": "INTEGER",
                    "example_values": "1|2",
                }
            },
        }
    }


@pytest.mark.asyncio
async def test_default_schema_mode_routes_to_full_schema() -> None:
    node = PerceptorNode()
    full_schema = Mock(return_value=(_schema(), []))
    node.full_schema = full_schema
    node.schema_linking = Mock(side_effect=AssertionError("schema_linking must not be called"))

    result = await node._aprocess(get_default_state("list orders"))

    full_schema.assert_called_once_with()
    assert result.get("schema", {}) == _schema()
    assert "CREATE TABLE `orders`" in result.get("schema_str", "")


@pytest.mark.asyncio
async def test_schema_linking_mode_routes_with_question() -> None:
    node = PerceptorNode(schema_mode="schema_linking")
    schema_linking = Mock(return_value=(_schema(), []))
    node.schema_linking = schema_linking
    node.full_schema = Mock(side_effect=AssertionError("full_schema must not be called"))

    result = await node._aprocess(get_default_state("list orders"))

    schema_linking.assert_called_once_with(["list orders"])
    assert result.get("schema", {}) == _schema()


@pytest.mark.asyncio
async def test_schema_linking_empty_result_raises_structured_error() -> None:
    node = PerceptorNode(schema_mode="schema_linking")
    node.schema_linking = Mock(return_value=({}, []))

    with pytest.raises(SchemaNotFoundError, match="未检索到可用的数据库 Schema"):
        await node._aprocess(get_default_state("unknown question"))


def test_invalid_schema_mode_is_rejected_during_initialization() -> None:
    with pytest.raises(ValueError, match="invalid_mode.*full_schema, schema_linking"):
        PerceptorNode(schema_mode="invalid_mode")


@pytest.mark.asyncio
async def test_user_schema_bypasses_configured_schema_mode(tmp_path) -> None:
    schema_file = tmp_path / "schema.md"
    schema_file.write_text("CREATE TABLE supplied_schema (id INTEGER);", encoding="utf-8")
    node = PerceptorNode(schema_mode="schema_linking", user_schema=str(schema_file))
    node.schema_linking = Mock(side_effect=AssertionError("schema_linking must not be called"))
    node.full_schema = Mock(side_effect=AssertionError("full_schema must not be called"))

    result = await node._aprocess(get_default_state("list supplied rows"))

    assert result.get("schema_str", "") == "CREATE TABLE supplied_schema (id INTEGER);"


def test_schema_linking_handles_table_missing_from_table_list() -> None:
    config_manager = SimpleNamespace(get=lambda key, default=None: "db" if key == "DATABASE.db_id" else default)
    semantic_client = SimpleNamespace(
        semantic_search_columns=lambda *_args, **_kwargs: [{"result": {"column_name_search": [{"db.orders.id": {}}]}}]
    )
    node = PerceptorNode(schema_mode="schema_linking", config_manager=config_manager)
    node._semantic_client = cast(Any, semantic_client)
    node._get_table_list = Mock(return_value=[])
    node._get_table_columns_info = Mock(
        return_value={
            "db.orders.id": {
                "column_short_description": "Order ID",
                "value_type": "INTEGER",
                "value_description": "1|2",
            }
        }
    )
    node._get_joinable_tables = Mock(return_value=[])

    schema, joins = node.schema_linking(["orders"])

    assert schema.get("orders", {}).get("description", "") == ""
    assert schema.get("orders", {}).get("columns", {}).get("id", {}).get("value_type", "") == "INTEGER"
    assert joins == []


@pytest.mark.asyncio
async def test_udn_perceptor_keeps_its_independent_schema_path() -> None:
    config_manager = SimpleNamespace(get=lambda _key, default=None: default)
    node = UDNPerceptorNode(schema_mode="invalid_mode", config_manager=config_manager)
    node.udn_schema_linking = AsyncMock(return_value=(_schema(), [], {}))
    node.schema_linking = Mock(side_effect=AssertionError("ordinary schema_linking must not be called"))

    result = await node._aprocess(get_default_state("list orders"))

    node.udn_schema_linking.assert_called_once_with("list orders")
    assert result.get("schema", {}) == _schema()
