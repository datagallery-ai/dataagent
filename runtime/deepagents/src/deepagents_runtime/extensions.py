from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def extra_tools() -> Sequence[Any]:
    """追加到 create_deep_agent(tools=...) 的业务工具。当前为空，占位。"""
    return ()


def extra_middleware() -> Sequence[Any]:
    """追加到 create_deep_agent(middleware=...) 的中间件。当前为空，占位。"""
    return ()


def extra_subagents() -> Sequence[Any] | None:
    """create_deep_agent(subagents=...) 的子 agent。None 表示不覆盖默认。"""
    return None


def extra_skills() -> list[str] | None:
    """create_deep_agent(skills=...) 的 Skill 路径。None 表示不启用。"""
    return None


def extra_backend() -> Any | None:
    """create_deep_agent(backend=...) 的文件系统 / sandbox。None 用 SDK 默认。"""
    return None


def extra_interrupt_on() -> dict[str, Any] | None:
    """create_deep_agent(interrupt_on=...) 的 HITL 配置。None 表示不打断。"""
    return None


def extra_system_prompt(base: str) -> str:
    """在控制面默认 system prompt 上叠业务说明。默认原样返回。"""
    return base
