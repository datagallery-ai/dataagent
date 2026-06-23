# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Build system prompt from YAML SCENARIO section."""

from __future__ import annotations

from typing import Any


def build_system_prompt(config: Any) -> str:
    """Build a system prompt from the YAML config.

    Combines:
    1. ``AGENT_CONFIG.description`` as the role description
    2. ``SCENARIO.chat.task`` as the task description
    3. ``SCENARIO.chat.instructions`` as additional instructions

    Args:
        config: ConfigManager or dict.

    Returns:
        System prompt string.
    """
    parts: list[str] = []

    # Agent identity
    agent_config = config.get("AGENT_CONFIG", {}) if hasattr(config, "get") else {}
    if isinstance(agent_config, dict):
        name = agent_config.get("name", "")
        desc = agent_config.get("description", "")
        if desc:
            parts.append(f"你是 {name}，{desc}" if name else f"你是 {desc}")
        elif name:
            parts.append(f"你是 {name}，一个智能数据分析助手。")

    # Scenario instructions
    scenario = config.get("SCENARIO", {}) if hasattr(config, "get") else {}
    if isinstance(scenario, dict):
        chat = scenario.get("chat", {})
        if isinstance(chat, dict):
            task = chat.get("task", "")
            instructions = chat.get("instructions", "")
            if task:
                parts.append(f"## 任务目标\n{task}")
            if instructions:
                parts.append(f"## 指令\n{instructions}")

    # Workspace info
    workspace_config = config.get("WORKSPACE", {}) if hasattr(config, "get") else {}
    if isinstance(workspace_config, dict):
        ws_path = workspace_config.get("path", "")
        if ws_path:
            parts.append(f"## 工作目录\n你的工作目录是 `{ws_path}`。")

    # Bash whitelist
    bash_whitelist = config.get("BASH_TOOL_WHITELIST") if hasattr(config, "get") else None
    if bash_whitelist == []:
        parts.append("## Bash 命令限制\nBash 工具已禁用。")
    elif bash_whitelist:
        cmds = ", ".join(f"`{c}`" for c in bash_whitelist)
        parts.append(f"## Bash 命令限制\n你只能执行以下命令: {cmds}")

    return "\n\n".join(parts) if parts else "你是一个智能数据分析助手。"
