"""Deep Agents 二次开发扩展。由 apps/api 在 create_deep_agent() 时合并，不是独立服务。"""

from deepagents_runtime.extensions import (
    extra_backend,
    extra_interrupt_on,
    extra_middleware,
    extra_skills,
    extra_subagents,
    extra_system_prompt,
    extra_tools,
)

__all__ = [
    "extra_backend",
    "extra_interrupt_on",
    "extra_middleware",
    "extra_skills",
    "extra_subagents",
    "extra_system_prompt",
    "extra_tools",
]
