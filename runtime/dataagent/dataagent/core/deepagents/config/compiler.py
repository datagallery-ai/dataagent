"""Compile legacy DataAgent YAML into the unified Deep Agent configuration."""

import asyncio
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware import SummarizationMiddleware, SummarizationToolMiddleware
from deepagents.middleware.summarization import TriggerClause, create_summarization_middleware
from langchain.agents.middleware import ModelCallLimitMiddleware, TodoListMiddleware
from langchain.agents.middleware.summarization import ContextSize
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.types import Checkpointer

from dataagent.actions.tools.request_human_feedback import request_human_feedback
from dataagent.config import ConfigManager
from dataagent.core.deepagents.config.a2a import A2AToolConfigCompiler
from dataagent.core.deepagents.config.hooks import HookConfigCompiler
from dataagent.core.deepagents.config.mcp import MCPToolConfigCompiler
from dataagent.core.deepagents.config.middleware import NativeMiddlewareConfigCompiler
from dataagent.core.deepagents.config.models import ModelConfigCompiler
from dataagent.core.deepagents.config.schema import AgentTool, DeepAgentConfig
from dataagent.core.deepagents.config.skills import SkillConfigCompiler
from dataagent.core.deepagents.config.subagents import SubagentConfigCompiler
from dataagent.core.deepagents.config.tool_hooks import ToolHookConfigCompiler
from dataagent.core.deepagents.config.tools import LocalToolConfigCompiler
from dataagent.core.deepagents.config.workspace import WorkspaceConfigCompiler
from dataagent.core.deepagents.state import DataAgentState
from dataagent.utils.constants import HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX
from dataagent.utils.log import logger

_LEGACY_COMPRESSION_TRIGGER_RATIO = 1.2
_LEGACY_COMPRESSION_KEEP_RATIO = 0.6


class DeepAgentConfigCompiler:
    """Compile compatible fields from the existing DataAgent YAML structure."""

    def __init__(
        self,
        config: Mapping[str, Any],
        config_manager: ConfigManager | None = None,
        models: Mapping[str, BaseChatModel] | None = None,
        primary_model_name: str | None = None,
        tools: Sequence[AgentTool] | None = None,
        middleware: Sequence[AgentMiddleware[Any, Any, Any]] | None = None,
        backend: BackendProtocol | None = None,
        checkpointer: Checkpointer | bool | None = None,
        store: BaseStore | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        subagent_paths: frozenset[Path] = frozenset(),
    ) -> None:
        self._config = config
        self._config_manager = config_manager
        self._models = dict(models) if models is not None else None
        self._primary_model_name = primary_model_name
        self._tools = tuple(tools or ())
        self._middleware = tuple(middleware or ())
        self._backend = backend
        self._checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self._store = store if store is not None else InMemoryStore()
        self._user_id = user_id
        self._session_id = session_id
        self._subagent_paths = frozenset(subagent_paths)

    async def compile(self) -> DeepAgentConfig:
        """Compile the currently supported subset of the existing configuration."""
        agent_config = self._as_mapping(self._config.get("AGENT_CONFIG", {}))
        scenario_config = self._as_mapping(self._config.get("SCENARIO", {}))
        chat_config = self._as_mapping(scenario_config.get("chat", {}))
        human_feedback_enabled = self._as_bool(agent_config.get("enable_human_feedback", False))
        max_iter = self._as_positive_int(agent_config.get("max_iter"), "AGENT_CONFIG.max_iter")
        model_compiler = ModelConfigCompiler(self._config)
        models = self._models if self._models is not None else model_compiler.compile()
        configured_primary = str(agent_config.get("primary_model", "") or "").strip() or None
        primary_model_name = model_compiler.resolve_primary_model_name(
            models,
            self._primary_model_name or configured_primary,
        )
        primary_model = models.get(primary_model_name)
        if primary_model is None:
            raise ValueError(f"Primary model '{primary_model_name}' is not available.")
        workspace_config = WorkspaceConfigCompiler(
            self._config,
            self._backend,
            user_id=self._user_id,
            session_id=self._session_id,
        ).compile()
        self._validate_shell_tools((), workspace_config.shell_enabled)
        mcp_tools, a2a_tools = await asyncio.gather(
            MCPToolConfigCompiler(self._config).compile(),
            A2AToolConfigCompiler(self._config).compile(),
        )
        tools = (
            *LocalToolConfigCompiler(self._config, config_manager=self._config_manager).compile(),
            *mcp_tools,
            *a2a_tools,
            *self._tools,
        )
        has_feedback_tool = any(getattr(tool, "name", None) == request_human_feedback.name for tool in tools)
        if human_feedback_enabled and not has_feedback_tool:
            tools = (*tools, request_human_feedback)
        self._validate_shell_tools(tools, workspace_config.shell_enabled)
        skill_config = SkillConfigCompiler(self._config, workspace_config.backend).compile()
        subagents = await SubagentConfigCompiler(
            self._config,
            models,
            primary_model_name,
            skill_config.backend,
            store=self._store,
            user_id=self._user_id,
            session_id=self._session_id,
            visited_paths=self._subagent_paths,
        ).compile()
        shell_tool_whitelist = self._compile_shell_tool_whitelist(workspace_config.shell_enabled)
        native_middleware = NativeMiddlewareConfigCompiler(
            models=models,
            primary_model_name=primary_model_name,
            workspace_root=workspace_config.workspace_root,
            shell_enabled=workspace_config.shell_enabled,
            shell_tool_whitelist=shell_tool_whitelist,
        ).compile()
        hook_middleware = HookConfigCompiler(self._config, models).compile()
        tool_hook_middleware = ToolHookConfigCompiler(self._config).compile()
        summarization = self._create_summarization_middleware(primary_model, skill_config.backend)
        middleware = (
            *((ModelCallLimitMiddleware(run_limit=max_iter, exit_behavior="error"),) if max_iter is not None else ()),
            *native_middleware,
            TodoListMiddleware(),
            summarization,
            SummarizationToolMiddleware(summarization),
            *((hook_middleware,) if hook_middleware is not None else ()),
            *((tool_hook_middleware,) if tool_hook_middleware is not None else ()),
            *self._middleware,
        )

        name = str(agent_config.get("name", "DataAgent")).strip() or "DataAgent"
        instructions = str(chat_config.get("instructions", "")).strip()
        prompt_appends = self._compile_prompt_appends(chat_config)
        system_prompt = self._load_system_prompt()
        if workspace_config.system_prompt:
            system_prompt = f"{system_prompt}\n\n{workspace_config.system_prompt}"
        if shell_tool_whitelist:
            system_prompt = f"{system_prompt}\n\n{self._shell_whitelist_system_prompt(shell_tool_whitelist)}"
        if prompt_appends:
            system_prompt = f"{system_prompt}\n\n{prompt_appends}"
        if human_feedback_enabled:
            system_prompt = f"{system_prompt}\n\n{self._human_feedback_system_prompt(chat_config)}"
        if instructions:
            system_prompt = f"{system_prompt}\n\n# Scenario Instructions\n{instructions}"

        return DeepAgentConfig(
            models=models,
            primary_model_name=primary_model_name,
            tools=tools,
            middleware=middleware,
            backend=skill_config.backend,
            subagents=subagents,
            skills=skill_config.sources,
            permissions=(*workspace_config.permissions, *skill_config.permissions),
            name=name,
            system_prompt=system_prompt,
            state_schema=DataAgentState,
            checkpointer=self._checkpointer,
            store=self._store,
            debug=self._as_bool(agent_config.get("debug", False)),
            max_iter=max_iter,
        )

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _load_system_prompt() -> str:
        prompt_path = files("dataagent.core.deepagents").joinpath("prompts", "system.md")
        return prompt_path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _as_positive_int(value: Any, path: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{path} must be a positive integer or null.")
        return value

    @staticmethod
    def _compile_prompt_appends(chat_config: Mapping[str, Any]) -> str:
        """Render generic static system and user prompt append lists from effective configuration."""
        raw = chat_config.get("prompt_appends")
        if raw is None:
            return ""
        if not isinstance(raw, Mapping):
            raise ValueError("SCENARIO.chat.prompt_appends must be a mapping.")

        sections: list[str] = []
        for key, title in (("system", "# Additional System Instructions"), ("user", "# User Prompt Templates")):
            values = DeepAgentConfigCompiler._as_prompt_strings(raw.get(key), f"SCENARIO.chat.prompt_appends.{key}")
            if values:
                sections.append(f"{title}\n" + "\n\n".join(values))
        return "\n\n".join(sections)

    @staticmethod
    def _as_prompt_strings(value: Any, path: str) -> tuple[str, ...]:
        """Normalize one generic prompt append value into non-empty strings."""
        if value is None:
            return ()
        if isinstance(value, str):
            return (value.strip(),) if value.strip() else ()
        if isinstance(value, bytes) or not isinstance(value, Sequence):
            raise ValueError(f"{path} must be a string or a list of strings.")
        return tuple(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def _human_feedback_system_prompt(chat_config: Mapping[str, Any]) -> str:
        """Build the native HITL instruction block from compatible scenario configuration."""
        lines = [
            "# Human-in-the-loop (HITL)",
            "When reliable progress requires clarification, confirmation, missing business context, or user approval, "
            "call ask_user immediately. Ask for the smallest blocking point and do not guess.",
        ]
        raw_conditions = chat_config.get("human_feedback_conditions")
        conditions = DeepAgentConfigCompiler._as_human_feedback_conditions(raw_conditions)
        if conditions:
            lines.extend(("", "The following entries are configured HITL conditions:"))
            lines.extend(f"- {condition}，{HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX}" for condition in conditions)
        return "\n".join(lines)

    @staticmethod
    def _as_human_feedback_conditions(value: Any) -> tuple[str, ...]:
        """Normalize one string or list of strings from ``human_feedback_conditions``."""
        if value is None:
            return ()
        if isinstance(value, str):
            text = value.strip()
            return (text,) if text else ()
        if not isinstance(value, Sequence) or isinstance(value, bytes):
            logger.warning("SCENARIO.chat.human_feedback_conditions must be a string or list; ignoring it.")
            return ()
        return tuple(str(item).strip() for item in value if item is not None and str(item).strip())

    def _compile_shell_tool_whitelist(self, shell_enabled: bool) -> tuple[str, ...] | None:
        raw_whitelist = self._config.get("SHELL_TOOL_WHITELIST")
        if raw_whitelist is not None and not shell_enabled:
            raise ValueError(
                "SHELL_TOOL_WHITELIST cannot be used with WORKSPACE.backend: state because shell is disabled."
            )
        if raw_whitelist is None:
            return None
        if not isinstance(raw_whitelist, list):
            raise ValueError("SHELL_TOOL_WHITELIST must be a list of base command names.")
        return tuple(str(command).strip() for command in raw_whitelist if str(command).strip())

    def _validate_shell_tools(self, tools: Sequence[AgentTool], shell_enabled: bool) -> None:
        """Reject explicitly configured shell tools when state-backed files are selected."""
        if shell_enabled:
            return
        blocked: list[str] = []
        tools_config = self._as_mapping(self._config.get("TOOLS", {}))
        local_entries = tools_config.get("local_functions", [])
        if isinstance(local_entries, Sequence) and not isinstance(local_entries, (str, bytes)):
            for entry in local_entries:
                if not isinstance(entry, Mapping):
                    continue
                names = (entry.get("name"), entry.get("function"))
                blocked.extend(
                    str(name).strip().lower() for name in names if str(name or "").strip().lower() in {"bash", "shell"}
                )
        for tool in tools:
            if isinstance(tool, Mapping):
                name = str(tool.get("name", "") or "").strip().lower()
            else:
                name = str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or "").strip().lower()
            if name in {"bash", "shell"}:
                blocked.append(name)
        if blocked:
            names = ", ".join(sorted(set(blocked)))
            raise ValueError(f"Tool(s) {names} cannot be used with WORKSPACE.backend: state because shell is disabled.")

    @staticmethod
    def _shell_whitelist_system_prompt(whitelist: tuple[str, ...]) -> str:
        """Describe the compatible shell allowlist in the native agent prompt."""
        allowed_commands = ", ".join(f"`{command}`" for command in sorted(whitelist))
        return (
            "# Shell command whitelist\n"
            "The `shell` tool may run only the listed base commands, including every command in a pipeline or chain.\n"
            f"Allowed commands: {allowed_commands}\n"
            "Do not use command substitution, backticks, or process substitution."
        )

    def _create_summarization_middleware(
        self,
        model: BaseChatModel,
        backend: BackendProtocol,
    ) -> SummarizationMiddleware:
        context_config = self._as_mapping(self._config.get("CONTEXT", {}))
        token_limit = self._as_positive_int(
            context_config.get("compress_token_limit"),
            "CONTEXT.compress_token_limit",
        )
        message_limit = self._as_positive_int(
            context_config.get("compress_message_cnt"),
            "CONTEXT.compress_message_cnt",
        )
        if token_limit is None and message_limit is None:
            return create_summarization_middleware(model, backend)

        trigger: list[ContextSize | TriggerClause] = []
        if token_limit is not None:
            trigger.append(("tokens", int(token_limit * _LEGACY_COMPRESSION_TRIGGER_RATIO)))
        if message_limit is not None:
            trigger.append(("messages", message_limit))

        if token_limit is not None:
            keep: ContextSize = ("tokens", max(1, int(token_limit * _LEGACY_COMPRESSION_KEEP_RATIO)))
        else:
            assert message_limit is not None
            keep = ("messages", max(1, int(message_limit * _LEGACY_COMPRESSION_KEEP_RATIO)))
        return SummarizationMiddleware(model=model, backend=backend, trigger=trigger, keep=keep)
