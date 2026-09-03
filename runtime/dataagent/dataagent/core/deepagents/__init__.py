"""Deep Agents based DataAgent runtime with cycle-safe lazy exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dataagent.core.deepagents.agent import create_data_agent
    from dataagent.core.deepagents.config import DeepAgentConfig, DeepAgentConfigCompiler
    from dataagent.core.deepagents.state import DataAgentState

__all__ = ["DataAgentState", "DeepAgentConfig", "DeepAgentConfigCompiler", "create_data_agent"]


def __getattr__(name: str) -> Any:
    """Load public runtime objects without importing the entire agent graph eagerly."""
    if name == "create_data_agent":
        from dataagent.core.deepagents.agent import create_data_agent

        return create_data_agent
    if name in {"DeepAgentConfig", "DeepAgentConfigCompiler"}:
        from dataagent.core.deepagents.config import DeepAgentConfig, DeepAgentConfigCompiler

        return DeepAgentConfig if name == "DeepAgentConfig" else DeepAgentConfigCompiler
    if name == "DataAgentState":
        from dataagent.core.deepagents.state import DataAgentState

        return DataAgentState
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
