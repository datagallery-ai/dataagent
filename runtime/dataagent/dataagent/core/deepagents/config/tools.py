"""Compile legacy ``TOOLS.local_functions`` entries into LangChain tools."""

from __future__ import annotations

import copy
import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool, create_schema_from_function

from dataagent.config import ConfigManager
from dataagent.core.deepagents.config.tool_hooks import tag_tool
from dataagent.core.deepagents.tool_context import ToolExecutionContext
from dataagent.utils.log import logger

_REMOVED_LOCAL_TOOLS = {
    "advance_data_analysis_workflow",
    "bash",
    "cancel_job",
    "cancel_subagent",
    "collect_job",
    "collect_subagent",
    "inspect_workspace",
    "inspect_data_analysis_workflow",
    "list_resources",
    "poll_job",
    "poll_subagent",
    "search_workspaces",
    "shell",
    "start_data_analysis_workflow",
    "sub_agent_tool",
    "submit_resource_job",
    "submit_subagent",
    "control_data_analysis_workflow",
}
_NATIVE_DEEPAGENT_TOOL_NAMES = {
    "delete",
    "edit_file",
    "glob",
    "grep",
    "ls",
    "read_file",
    "write_file",
}


class LocalToolConfigCompiler:
    """Compile the compatible subset of legacy local-function tool declarations."""

    def __init__(self, config: Mapping[str, Any], config_manager: ConfigManager | None = None) -> None:
        self._config = config
        self._config_manager = (
            config_manager.copy() if config_manager is not None else self._build_config_manager(config)
        )

    def compile(self) -> tuple[BaseTool, ...]:
        """Compile ``TOOLS.local_functions`` into native LangChain tools."""
        tools_config = self._as_mapping(self._config.get("TOOLS", {}))
        raw_entries = tools_config.get("local_functions", [])
        if raw_entries is None:
            return ()
        if not isinstance(raw_entries, list):
            raise ValueError("TOOLS.local_functions must be a list.")

        compiled: list[BaseTool] = []
        names: set[str] = set()
        for index, raw_entry in enumerate(raw_entries):
            entry = self._as_mapping(raw_entry)
            if not entry:
                raise ValueError(f"TOOLS.local_functions[{index}] must be a mapping.")
            tool = self._compile_entry(entry, index)
            if tool is None:
                continue
            if tool.name in names:
                raise ValueError(f"Duplicate local tool name: {tool.name!r}.")
            names.add(tool.name)
            compiled.append(tag_tool(tool, "local", tool.name))
        return tuple(compiled)

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _build_config_manager(config: Mapping[str, Any]) -> ConfigManager:
        config_manager = ConfigManager()
        config_manager.update(copy.deepcopy(dict(config)))
        return config_manager

    def _compile_entry(self, entry: Mapping[str, Any], index: int) -> BaseTool | None:
        path = f"TOOLS.local_functions[{index}]"
        module_name = str(entry.get("module", "")).strip()
        function_name = str(entry.get("function") or entry.get("name") or "").strip()
        tool_name = str(entry.get("name") or function_name).strip()
        if not module_name:
            raise ValueError(f"{path}.module is required.")
        if not function_name:
            raise ValueError(f"{path}.function or {path}.name is required.")
        if not tool_name:
            raise ValueError(f"{path}.name must be non-empty.")
        if function_name in _REMOVED_LOCAL_TOOLS or tool_name in _REMOVED_LOCAL_TOOLS:
            logger.warning("Skipping removed local tool '{}'.", tool_name)
            return None
        if tool_name in _NATIVE_DEEPAGENT_TOOL_NAMES:
            logger.info("Using native Deep Agents tool '{}' instead of its legacy local implementation.", tool_name)
            return None

        module = importlib.import_module(module_name)
        target = getattr(module, function_name, None)
        if target is None:
            raise AttributeError(f"Module '{module_name}' has no attribute '{function_name}'.")
        if isinstance(target, BaseTool):
            return self._configure_base_tool(target, tool_name, entry)
        if not callable(target) or inspect.isclass(target):
            raise TypeError(f"{module_name}.{function_name} must be a function or BaseTool instance.")

        tool_config = self._as_mapping(entry.get("config", {}))
        return self._build_structured_tool(target, tool_name, entry, tool_config)

    @staticmethod
    def _configure_base_tool(tool: BaseTool, name: str, entry: Mapping[str, Any]) -> BaseTool:
        updates: dict[str, Any] = {"name": name}
        if "description" in entry:
            updates["description"] = str(entry.get("description") or "")
        return tool.model_copy(update=updates)

    def _build_structured_tool(
        self,
        function: Callable[..., Any],
        name: str,
        entry: Mapping[str, Any],
        tool_config: Mapping[str, Any],
    ) -> StructuredTool:
        description = str(entry.get("description") or "") if "description" in entry else None
        signature = inspect.signature(function)
        if "_tool_context" not in signature.parameters:
            if inspect.iscoroutinefunction(function):
                return StructuredTool.from_function(coroutine=function, name=name, description=description)
            return StructuredTool.from_function(func=function, name=name, description=description)

        args_schema = create_schema_from_function(name, function, filter_args=("_tool_context",))
        context_config = copy.deepcopy(dict(tool_config))

        if inspect.iscoroutinefunction(function):

            async def async_call_with_context(runtime: ToolRuntime, **kwargs: Any) -> Any:
                context = self._build_tool_context(context_config, runtime)
                return await function(**kwargs, _tool_context=context)

            return StructuredTool.from_function(
                coroutine=async_call_with_context,
                name=name,
                description=description,
                args_schema=args_schema,
            )

        def sync_call_with_context(runtime: ToolRuntime, **kwargs: Any) -> Any:
            context = self._build_tool_context(context_config, runtime)
            return function(**kwargs, _tool_context=context)

        return StructuredTool.from_function(
            func=sync_call_with_context,
            name=name,
            description=description,
            args_schema=args_schema,
        )

    def _build_tool_context(self, tool_config: dict[str, Any], runtime: ToolRuntime) -> ToolExecutionContext:
        return ToolExecutionContext(
            config_manager=self._config_manager,
            tool_config=copy.deepcopy(tool_config),
            runtime=runtime,  # type: ignore[arg-type]
        )
