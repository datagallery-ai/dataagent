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

from types import SimpleNamespace

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool import basic_retrieval, get_join_relations
from dataagent.core.errors import DataAgentError
from dataagent.core.managers.action_manager.manager import ToolManager


def test_list_semantic_layer_tables_propagates_dataagent_error(monkeypatch) -> None:
    class _FailingClient:
        def list_retrieval_tables(self) -> dict:
            raise DataAgentError(
                source="tool",
                component="semantic-service",
                fact="语义服务 HTTP 失败：GET retrieval/tables",
            )

    monkeypatch.setattr(
        basic_retrieval.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: _FailingClient()),
    )

    context = ToolExecutionContext(config_manager=SimpleNamespace())

    with pytest.raises(DataAgentError) as exc_info:
        basic_retrieval.list_semantic_layer_tables(_tool_context=context)

    assert exc_info.value.source == "tool"


def test_get_semantic_layer_table_schema_propagates_dataagent_error(monkeypatch) -> None:
    class _FailingClient:
        def get_retrieval_table_schema(self, table: str) -> dict:
            raise DataAgentError(
                source="tool",
                component="semantic-service",
                fact=f"语义服务 HTTP 500：GET retrieval/tables/{table}/schema",
            )

    monkeypatch.setattr(
        basic_retrieval.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: _FailingClient()),
    )

    context = ToolExecutionContext(config_manager=SimpleNamespace())

    with pytest.raises(DataAgentError) as exc_info:
        basic_retrieval.get_semantic_layer_table_schema("orders", _tool_context=context)

    assert "retrieval/tables/orders/schema" in exc_info.value.fact


@pytest.mark.asyncio
async def test_tool_manager_raises_dataagent_error_from_semantic_failure(monkeypatch) -> None:
    class _FailingClient:
        def get_retrieval_table_schema(self, table: str) -> dict:
            raise DataAgentError(
                source="tool",
                component="semantic-service",
                fact=f"语义服务 HTTP 500：GET retrieval/tables/{table}/schema",
            )

    monkeypatch.setattr(
        basic_retrieval.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: _FailingClient()),
    )

    tool_manager = ToolManager(config_manager=SimpleNamespace())
    tool_manager.register_local_tool(
        basic_retrieval.get_semantic_layer_table_schema,
        name="get_semantic_layer_table_schema",
    )

    try:
        with pytest.raises(DataAgentError) as exc_info:
            await tool_manager.acall("get_semantic_layer_table_schema", table="orders")
    finally:
        await tool_manager.cleanup()

    assert exc_info.value.source == "tool"
    assert "retrieval/tables/orders/schema" in exc_info.value.fact


def test_get_join_relations_propagates_dataagent_error(monkeypatch) -> None:
    class _FailingClient:
        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list:
            raise DataAgentError(
                source="tool",
                component="semantic-service",
                fact="语义服务 HTTP 失败：GET advanced-search/joinable-tables",
            )

    monkeypatch.setattr(
        get_join_relations.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: _FailingClient()),
    )

    context = ToolExecutionContext(config_manager=SimpleNamespace())

    with pytest.raises(DataAgentError) as exc_info:
        get_join_relations.get_join_relations(["demo.orders"], _tool_context=context)

    assert exc_info.value.source == "tool"
