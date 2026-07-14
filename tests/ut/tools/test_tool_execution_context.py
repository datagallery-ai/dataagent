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
"""ToolExecutionContext injection and per-Agent ConfigManager isolation tests."""

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.config.config_manager import ConfigManager
from dataagent.core.managers.action_manager.base import ErrorType, ToolError
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.managers.action_manager.schemas import ToolSchema


def read_db_id_sync(query: str, *, _tool_context: ToolExecutionContext) -> str:
    """Read DATABASE.db_id from injected config (sync)."""
    _ = query
    return str(_tool_context.config_manager.get("DATABASE.db_id"))


async def read_db_id_async(query: str, *, _tool_context: ToolExecutionContext) -> str:
    """Read DATABASE.db_id from injected config (async)."""
    _ = query
    return str(_tool_context.config_manager.get("DATABASE.db_id"))


def read_db_id_no_context(query: str, *, _tool_context: ToolExecutionContext) -> str:
    """Tool that requires _tool_context but may be registered without one."""
    _ = query
    return str(_tool_context.config_manager.get("DATABASE.db_id"))


def read_tool_llm_model(query: str, *, _tool_context: ToolExecutionContext) -> str:
    """Read llm_model from injected per-tool YAML config."""
    _ = query
    return str((_tool_context.tool_config or {}).get("llm_model"))


class TestToolExecutionContextSchema:
    """LLM-visible schema must hide internal _tool_context parameter."""

    def test_schema_hides_underscore_params(self):
        """ToolSchema.from_function must not expose _tool_context to the LLM."""
        schema = ToolSchema.from_function(read_db_id_sync, "read_db_id_sync")
        param_names = [p.name for p in schema.parameters]
        assert param_names == ["query"]
        assert "_tool_context" not in param_names


@pytest.mark.asyncio
class TestToolExecutionContextInjection:
    """LocalToolWrapper injects ToolExecutionContext when tool declares it."""

    async def test_sync_tool_reads_config_via_context(self):
        """Sync local tool receives config via _tool_context injection."""
        cm = ConfigManager()
        cm.set("DATABASE.db_id", "A_DB")
        tm = ToolManager(config_manager=cm)
        tm.register_local_tool(read_db_id_sync, name="read_db_id_sync", category="test")
        result = await tm.acall("read_db_id_sync", query="x")
        assert result.success is True
        assert result.data == "A_DB"
        await tm.cleanup()

    async def test_async_tool_reads_config_via_context(self):
        """Async local tool receives config via _tool_context injection."""
        cm = ConfigManager()
        cm.set("DATABASE.db_id", "ASYNC_DB")
        tm = ToolManager(config_manager=cm)
        tm.register_local_tool(read_db_id_async, name="read_db_id_async", category="test")
        result = await tm.acall("read_db_id_async", query="x")
        assert result.success is True
        assert result.data == "ASYNC_DB"
        await tm.cleanup()

    async def test_missing_config_manager_returns_error_at_runtime(self):
        """Config-only tool fails when ToolExecutionContext has no ConfigManager."""
        tm = ToolManager()
        tm.register_local_tool(read_db_id_no_context, name="read_db_id_no_context", category="test")
        with pytest.raises(ToolError, match="Tool execution failed.") as exc_info:
            await tm.acall("read_db_id_no_context", query="x")
        assert exc_info.value.error_type is ErrorType.UNKNOWN
        assert exc_info.value.retriable is True
        assert exc_info.value.max_retries == 1
        await tm.cleanup()

    async def test_tool_config_injected_from_registration(self):
        """Per-tool YAML config (llm_model) is merged into _tool_context at call time."""
        cm = ConfigManager()
        tm = ToolManager(config_manager=cm)
        tm.register_local_tool(
            read_tool_llm_model,
            name="read_tool_llm_model",
            category="test",
            llm_model="chat_model",
        )
        result = await tm.acall("read_tool_llm_model", query="x")
        assert result.success is True
        assert result.data == "chat_model"
        await tm.cleanup()

    async def test_tool_config_includes_arbitrary_yaml_keys(self):
        """Per-tool YAML config passes arbitrary keys (not only llm_model) into _tool_context."""
        cm = ConfigManager()
        tm = ToolManager(config_manager=cm)

        def read_custom_key(query: str, *, _tool_context: ToolExecutionContext) -> str:
            _ = query
            return str((_tool_context.tool_config or {}).get("abc"))

        tm.register_local_tool(
            read_custom_key,
            name="read_custom_key",
            category="test",
            llm_model="chat_model",
            abc="abc_value",
        )
        result = await tm.acall("read_custom_key", query="x")
        assert result.success is True
        assert result.data == "abc_value"
        await tm.cleanup()

    async def test_different_tools_get_different_tool_config(self):
        """Two tools registered on one ToolManager receive各自 llm_model binding."""
        cm = ConfigManager()
        tm = ToolManager(config_manager=cm)
        tm.register_local_tool(
            read_tool_llm_model,
            name="tool_a",
            category="test",
            llm_model="model_a",
        )
        tm.register_local_tool(
            read_tool_llm_model,
            name="tool_b",
            category="test",
            llm_model="model_b",
        )
        result_a = await tm.acall("tool_a", query="x")
        result_b = await tm.acall("tool_b", query="x")
        assert result_a.success is True
        assert result_a.data == "model_a"
        assert result_b.success is True
        assert result_b.data == "model_b"
        await tm.cleanup()


@pytest.mark.asyncio
class TestToolManagerPerAgentConfigIsolation:
    """Two ToolManagers with different ConfigManagers must not cross-read."""

    async def test_same_tool_different_config_managers(self):
        """Same Python function registered on two ToolManagers reads各自配置."""
        cm_a = ConfigManager()
        cm_a.set("DATABASE.db_id", "A_DB")
        cm_b = ConfigManager()
        cm_b.set("DATABASE.db_id", "B_DB")

        tm_a = ToolManager(config_manager=cm_a)
        tm_b = ToolManager(config_manager=cm_b)
        tm_a.register_local_tool(read_db_id_sync, name="read_db_id", category="test")
        tm_b.register_local_tool(read_db_id_sync, name="read_db_id", category="test")

        result_a = await tm_a.acall("read_db_id", query="x")
        result_b = await tm_b.acall("read_db_id", query="x")
        assert result_a.success is True
        assert result_a.data == "A_DB"
        assert result_b.success is True
        assert result_b.data == "B_DB"
        await tm_a.cleanup()
        await tm_b.cleanup()
