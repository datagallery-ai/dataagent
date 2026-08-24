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
import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from loguru import logger

from dataagent.core.managers.action_manager.base import BaseTool, ToolError, ToolResult, ToolType
from dataagent.core.managers.action_manager.registry import ToolRegistry
from dataagent.core.managers.action_manager.schemas import ToolSchema
from dataagent.utils.constants import DEFAULT_BUILTIN_LOCAL_TOOLS

# Full catalog of code-defined builtin local tools (enabled subset from constants.DEFAULT_BUILTIN_LOCAL_TOOLS).
# File-ops / bash / plan tools are intentionally omitted; enable via YAML TOOLS.builtin or local_functions if needed.
_BUILTIN_LOCAL_TOOL_CATALOG: dict[str, dict[str, str]] = {}


def _builtin_local_tool_specs_from_constants() -> list[dict[str, Any]]:
    """Resolve DEFAULT_BUILTIN_LOCAL_TOOLS against the catalog (intersection by tool name)."""
    return _builtin_local_tool_specs(DEFAULT_BUILTIN_LOCAL_TOOLS, "DEFAULT_BUILTIN_LOCAL_TOOLS")


def _builtin_local_tool_name(entry: Any) -> str:
    """Extract only the requested builtin name from one YAML entry."""
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, Mapping):
        return ""
    name = entry.get("name") or entry.get("function")
    return name.strip() if isinstance(name, str) else ""


def _builtin_local_tool_specs(entries: Sequence[Any], source: str) -> list[dict[str, Any]]:
    """Resolve requested names to canonical catalog entries and skip all other values."""
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in entries:
        name = _builtin_local_tool_name(raw_entry)
        if not name:
            logger.warning("{}: invalid builtin tool entry skipped", source)
            continue
        if name in seen:
            continue
        seen.add(name)
        entry = _BUILTIN_LOCAL_TOOL_CATALOG.get(name)
        if entry is None:
            logger.warning("{}: unknown builtin tool name {!r}, skipped", source, name)
            continue
        specs.append(dict(entry))
    return specs


def _get_local_tool_wrapper():
    from dataagent.actions.tools.local import LocalToolWrapper

    return LocalToolWrapper


def call_sync_with_event_loop(func, /, **kwargs):
    """Run a sync callable in a worker thread with a per-thread event loop."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return func(**kwargs)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class ToolManager:
    """工具管理器，每个 Agent 实例拥有独立的 ToolManager"""

    def __init__(self, config_manager: Any | None = None):
        """Initialize a per-Agent ToolManager.

        Args:
            config_manager: Per-Agent :class:`~dataagent.config.config_manager.ConfigManager`.
                When set, local tools that declare ``_tool_context`` receive a
                :class:`~dataagent.actions.tools.context.ToolExecutionContext` with this instance.
                Same object reference as ``AgentEnv.config_manager`` / ``Runtime.config_manager``.
        """
        self.config_manager = config_manager
        self._tool_instances: dict[str, BaseTool] = {}
        self._tool_schemas: dict[str, ToolSchema] = {}
        # 每个 ToolManager 拥有独立的 ToolRegistry
        self.tool_registry = ToolRegistry()

    @staticmethod
    def workspace_allow_path_list(config: Mapping[str, Any]) -> list[str]:
        """Parse ``WORKSPACE.allow_path`` — absolute path list, read-only like skill roots."""
        workspace_cfg = config.get("WORKSPACE")
        if not isinstance(workspace_cfg, Mapping):
            return []
        raw = workspace_cfg.get("allow_path")
        if raw is None:
            return []
        if isinstance(raw, (str, bytes)):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings, not a single string.")
        if not isinstance(raw, Sequence):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings.")
        out: list[str] = []
        for item in raw:
            s = str(item).strip()
            if not s:
                continue
            out.append(s)
        return out

    @staticmethod
    def _generate_schema(tool: BaseTool) -> ToolSchema:
        """生成工具Schema"""
        return tool.get_schema()

    @staticmethod
    def _load_hooks_from_tool_config(entry: dict[str, Any]):
        """Parse ``hooks`` from a TOOLS registry entry (local_functions)."""
        from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config

        return load_tool_hooks_from_config(entry.get("hooks"))

    def register_local_tool(
        self,
        func_or_class,
        name: str | None = None,
        category: str = "general",
        description: str | None = None,
        **kwargs,
    ) -> Callable:
        """注册本地工具。

        Args:
            func_or_class: 可调用函数或 ``BaseTool`` 子类。
            name: 工具名；默认取函数名。
            category: 工具分类。
            description: 工具描述；``None`` 时使用函数 docstring。
            **kwargs: 透传给工具实例的额外配置。
        """
        tool_name = name or getattr(func_or_class, "__name__", None)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("Local tool name must be a non-empty string.")
        if callable(func_or_class) and not inspect.isclass(func_or_class):
            LocalToolWrapper = _get_local_tool_wrapper()
            tool_context = self._build_tool_execution_context()
            tool_wrapper = LocalToolWrapper(
                func_or_class,
                tool_name,
                category,
                description,
                tool_context=tool_context,
                **kwargs,
            )
        elif inspect.isclass(func_or_class) and issubclass(func_or_class, BaseTool):
            tool_wrapper = func_or_class(name=tool_name, category=category, description=description, **kwargs)
        else:
            raise ValueError(f"Unsupported tool type: {type(func_or_class)}")
        self.tool_registry.register(tool_name, tool_wrapper)
        self._tool_instances[tool_name] = tool_wrapper
        schema = self._generate_schema(tool_wrapper)
        self._tool_schemas[tool_name] = schema
        return func_or_class

    def init_from_config(self, config: dict[str, Any]):
        """从配置字典初始化工具

        Args:
            config: 包含 TOOLS 配置和其他配置的字典。
        """
        logger.trace("=== Initializing Tool Manager 🛠️ ===")

        tools_config = config.get("TOOLS", {})

        self._register_builtin_local_tools(tools_config)

        if not tools_config:
            return
        if "local_functions" in tools_config:
            self._register_local_tools(tools_config["local_functions"])

    def get(self, name: str) -> BaseTool:
        """获取工具实例。"""
        if name in self._tool_instances:
            return self._tool_instances[name]
        raise ToolError(f"Tool '{name}' not found")

    def exists(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tool_instances

    def call(self, name: str, **kwargs) -> ToolResult:
        """调用工具（同步版本）"""
        tool = self.get(name)
        return tool.call(**kwargs)

    async def aget(self, name: str) -> BaseTool:
        """异步获取工具实例。"""
        if name in self._tool_instances:
            return self._tool_instances[name]
        raise ToolError(f"Tool '{name}' not found")

    async def acall(self, name: str, **kwargs) -> ToolResult:
        """调用工具（异步版本，支持懒加载）"""
        tool = await self.aget(name)  # 使用异步获取方法
        acall = getattr(tool, "acall", None)
        if callable(acall):
            result = acall(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        else:
            # 对于仅提供同步接口但内部可能依赖 asyncio.get_event_loop() 的工具，
            # 在线程中显式挂载一个事件循环，避免 "There is no current event loop"。
            result = await asyncio.to_thread(call_sync_with_event_loop, tool.call, **kwargs)

        # 当工具返回失败结果时，抛出 ToolError 以触发调用方的重试逻辑
        if isinstance(result, ToolResult) and not result.success:
            from dataagent.core.managers.action_manager.base import ErrorType

            raise ToolError(
                message=result.error or "Tool execution failed",
                error_type=result.error_type or ErrorType.UNKNOWN,
                retriable=result.retriable,
                max_retries=result.max_retries,
            )
        return result

    def list_tools(self, category: str | None = None, tool_type: ToolType | None = None) -> list[str]:
        """列出工具名称"""
        tools = self._tool_instances.items()

        if category is not None:
            tools = [(name, tool) for name, tool in tools if tool.category == category]

        if tool_type is not None:
            tools = [(name, tool) for name, tool in tools if tool.tool_type == tool_type]

        return [name for name, tool in tools]

    def get_all_tool_instances(self) -> list[BaseTool]:
        """Return all registered tool instances (for Runtime.get_tools_for_llm)."""
        return list(self._tool_instances.values())

    def get_schema(self, name: str) -> ToolSchema:
        """获取工具Schema"""
        if name not in self._tool_schemas:
            raise ToolError(f"Schema for tool '{name}' not found")
        return self._tool_schemas[name]

    def get_langchain_tool(self, name: str):
        """获取LangChain兼容的工具"""
        tool = self.get(name)
        return tool.to_langchain_tool()

    def get_tools_for_llm(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """获取用于LLM function calling的工具定义"""
        tools = []
        for name in tool_names:
            if self.exists(name):
                schema = self.get_schema(name)
                tools.append(schema.to_openai_function())
        return tools

    def get_tools_by_type(self, tool_type: ToolType) -> dict[str, BaseTool]:
        """按类型获取工具"""
        return {name: tool for name, tool in self._tool_instances.items() if tool.tool_type == tool_type}

    def get_tool_info(self, name: str) -> dict[str, Any]:
        """获取工具详细信息"""
        if name not in self._tool_instances:
            raise ToolError(f"Tool '{name}' not found")

        tool = self._tool_instances[name]
        schema = self._tool_schemas[name]

        metadata_schema = schema.to_metadata()
        metadata_schema_with_name = {"name": tool.name, **metadata_schema}

        return {
            "name": tool.name,
            "type": tool.tool_type.value,
            "category": tool.category,
            "description": tool.description,
            "schema": metadata_schema_with_name,
            "config": getattr(tool, "config", {}),
        }

    def list_tool_categories(self) -> list[str]:
        """列出所有工具分类"""
        categories = set()
        for tool in self._tool_instances.values():
            categories.add(tool.category)
        return sorted(categories)

    def get_tools_summary(self) -> dict[str, Any]:
        """获取工具总览信息"""
        total_tools = len(self._tool_instances)
        by_type = {}
        by_category = {}

        for tool in self._tool_instances.values():
            # 按类型统计
            tool_type = tool.tool_type.value
            by_type[tool_type] = by_type.get(tool_type, 0) + 1

            # 按分类统计
            category = tool.category
            by_category[category] = by_category.get(category, 0) + 1

        return {
            "total_tools": total_tools,
            "by_type": by_type,
            "by_category": by_category,
            "available_categories": sorted(by_category.keys()),
        }

    async def health_check(self) -> dict[str, Any]:
        """检查所有工具的健康状态"""
        return {
            "local_tools": len([t for t in self._tool_instances.values() if t.tool_type == ToolType.LOCAL_FUNCTION]),
            "total_tools": len(self._tool_instances),
        }

    async def cleanup(self):
        """清理本 Agent 自己的工具实例。"""
        logger.debug("🧹 清理 per-Agent 资源...")

        self._tool_instances.clear()
        self._tool_schemas.clear()
        if self.tool_registry:
            self.tool_registry.clear()

        logger.debug("   ✅ per-Agent 工具管理器资源清理完成")

    def _build_tool_execution_context(self):
        """Build :class:`~dataagent.actions.tools.context.ToolExecutionContext` for local tool execution."""
        from dataagent.actions.tools.context import ToolExecutionContext

        return ToolExecutionContext(config_manager=self.config_manager)

    def _register_builtin_local_tools(self, tools_config: Mapping[str, Any] | None) -> None:
        """Register only canonical catalog tools selected by defaults or YAML names."""
        specs = _builtin_local_tool_specs_from_constants()
        if isinstance(tools_config, Mapping) and "builtin" in tools_config:
            raw = tools_config.get("builtin")
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                specs = _builtin_local_tool_specs(raw, "TOOLS.builtin")
            else:
                logger.warning("TOOLS.builtin must be a list of catalog tool names; no builtin tools registered")
                specs = []
        self._register_local_tools(specs)

    def _register_local_tools(self, tools: list[dict[str, Any]]):
        """Register local function tools from YAML ``TOOLS.local_functions`` entries."""
        for tool_config in tools:
            if not isinstance(tool_config, dict):
                continue
            module_path = tool_config.get("module")
            name = tool_config.get("function") or tool_config.get("name")
            category = tool_config.get("category", "general")
            config = tool_config.get("config", {})
            if not isinstance(config, dict):
                config = {}
            if not module_path or not name:
                continue
            try:
                import importlib

                module = importlib.import_module(module_path)
                func = getattr(module, name)
                register_kwargs: dict[str, Any] = {"name": name, "category": category, **config}
                # 允许 local_functions 覆盖已注册的同名 builtin 工具，以便重新挂 hooks
                if name in self._tool_instances:
                    self.tool_registry.unregister(name)
                self.register_local_tool(func, **register_kwargs)
                hook_lists = self._load_hooks_from_tool_config(tool_config)
                if hook_lists.pre or hook_lists.post:
                    from dataagent.actions.tools.hooks.config import attach_hooks_to_tool

                    attach_hooks_to_tool(self._tool_instances[name], hook_lists)
                logger.trace(f"✅ Local tool: '{name}' registered.")
            except Exception as e:
                logger.warning(f"❌ Local tool: '{name}' registration failed: {e}.")
