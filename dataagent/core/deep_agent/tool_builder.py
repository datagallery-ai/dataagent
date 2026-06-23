# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Build jiuwen Tools from YAML config + business tool registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openjiuwen.core.foundation.tool import McpServerConfig, Tool, ToolCard
    from openjiuwen.core.sys_operation import SysOperation


def build_harness_tools(
    sys_operation: SysOperation,
    language: str = "cn",
    *,
    read_sys_operation: SysOperation | None = None,
    bash_allowlist: tuple[str, ...] | None = None,
) -> list[Tool]:
    """Build the standard harness tool set.

    All file-system, shell, and todo tools require a ``SysOperation`` instance.
    """
    from openjiuwen.harness.tools.filesystem import (
        EditFileTool,
        GlobTool,
        GrepTool,
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )
    from openjiuwen.harness.tools.todo import TodoCreateTool, TodoGetTool, TodoListTool, TodoModifyTool
    from openjiuwen.harness.tools.web_tools import WebFetchWebpageTool, WebFreeSearchTool

    from dataagent.core.deep_agent.builders.tools.bash import build_bash_tool

    read_operation = read_sys_operation or sys_operation
    tools = [
        ReadFileTool(operation=read_operation, language=language),
        WriteFileTool(operation=sys_operation, language=language),
        EditFileTool(operation=sys_operation, language=language),
        GlobTool(operation=read_operation, language=language),
        GrepTool(operation=read_operation, language=language),
        ListDirTool(operation=read_operation, language=language),
        WebFetchWebpageTool(language=language),
        WebFreeSearchTool(language=language),
        TodoCreateTool(operation=sys_operation, language=language),
        TodoListTool(operation=sys_operation, language=language),
        TodoModifyTool(operation=sys_operation, language=language),
        TodoGetTool(operation=sys_operation, language=language),
    ]
    bash_tool = build_bash_tool(
        sys_operation,
        language=language,
        allowlist=bash_allowlist,
    )
    if bash_tool is not None:
        tools.insert(6, bash_tool)
    return tools


def build_business_tools(config: Any) -> list[McpServerConfig]:
    """Compatibility entry point returning MCP server configs.

    MCP resources must be passed to ``create_deep_agent(mcps=...)`` rather than
    appended to the Tool list. New code should use ``DeepAgentAdapter.build_mcps``.
    """
    from dataagent.core.deep_agent.builders.tools import build_mcp_servers
    from dataagent.core.deep_agent.spec import DeepAgentBuildSpec

    return build_mcp_servers(DeepAgentBuildSpec.from_config(config).mcp_servers)


def build_all_tools(
    sys_operation: SysOperation,
    config: Any,
    language: str = "cn",
) -> list[Tool | ToolCard]:
    """Build the full tool set through the central DataAgent adapter."""
    from dataagent.core.deep_agent.adapter import DeepAgentAdapter

    return DeepAgentAdapter(config).build_tools(sys_operation, language=language)
