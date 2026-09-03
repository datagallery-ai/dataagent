"""Unified configuration schema for constructing a native Deep Agent."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from deepagents import DeepAgentState
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.subagents import CompiledSubAgent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

AgentTool = BaseTool | Callable[..., Any] | dict[str, Any]


@dataclass(frozen=True)
class DeepAgentConfig:
    """Unified configuration passed to the native Deep Agent constructor."""

    models: Mapping[str, BaseChatModel]
    primary_model_name: str
    tools: tuple[AgentTool, ...]
    middleware: tuple[AgentMiddleware[Any, Any, Any], ...]
    backend: BackendProtocol
    subagents: tuple[CompiledSubAgent, ...]
    name: str
    system_prompt: str
    state_schema: type[DeepAgentState]
    skills: tuple[str, ...] = ()
    permissions: tuple[FilesystemPermission, ...] = ()
    checkpointer: Checkpointer | bool | None = None
    store: BaseStore | None = None
    debug: bool = False
    max_iter: int | None = None

    @property
    def primary_model(self) -> BaseChatModel:
        """Return the model used by the main Deep Agent loop."""
        model = self.models.get(self.primary_model_name)
        if model is None:
            raise ValueError(f"Primary model '{self.primary_model_name}' is not available.")
        return model

    def to_deep_agent_kwargs(self) -> dict[str, Any]:
        """Return the complete keyword arguments for ``create_deep_agent``."""
        return {
            "model": self.primary_model,
            "tools": list(self.tools),
            "middleware": list(self.middleware),
            "backend": self.backend,
            "subagents": list(self.subagents) if self.subagents else None,
            "skills": list(self.skills) if self.skills else None,
            "permissions": list(self.permissions) if self.permissions else None,
            "system_prompt": self.system_prompt,
            "state_schema": self.state_schema,
            "checkpointer": self.checkpointer,
            "store": self.store,
            "debug": self.debug,
            "name": self.name,
        }
