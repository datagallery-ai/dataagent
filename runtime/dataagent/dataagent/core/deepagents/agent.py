"""Create a native Deep Agent from compiled DataAgent configuration."""

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from dataagent.core.deepagents.config import DeepAgentConfig


async def create_data_agent(config: DeepAgentConfig) -> CompiledStateGraph:
    """Create a native Deep Agent from the unified compiled configuration."""
    return create_deep_agent(**config.to_deep_agent_kwargs())
