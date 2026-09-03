"""Build the default native LangChain middleware for DataAgent."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeAlias

from langchain.agents.middleware import (
    HostExecutionPolicy,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    ShellToolMiddleware,
    ToolErrorMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from dataagent.core.errors import DataAgentError

_DEFAULT_SHELL_COMMAND_TIMEOUT = 600.0
_BASH_COMMAND_SEPARATORS = re.compile(r"[;&|\n]")
_NESTED_BASH_COMMAND = re.compile(r"\$\((?!\()|`|[<>]\(")
_VARIABLE_ASSIGNMENT = re.compile(r"^[a-zA-Z_]\w*=")
_SHELL_TOOL_NAMES = frozenset({"bash", "shell"})

ToolExecutionResult: TypeAlias = ToolMessage | Command[Any]


def _extract_base_commands(command: str) -> tuple[str, ...]:
    """Extract base command names with the legacy bash-whitelist parsing rules."""
    commands: list[str] = []
    for segment in _BASH_COMMAND_SEPARATORS.split(command):
        words = segment.strip().split()
        for word in words:
            if _VARIABLE_ASSIGNMENT.match(word):
                continue
            commands.append(word.rsplit("/", 1)[-1])
            break
    return tuple(commands)


class BashWhitelistMiddleware(AgentMiddleware[Any, None, Any]):
    """Prevent native shell tools from running commands outside a legacy whitelist."""

    def __init__(self, whitelist: tuple[str, ...]) -> None:
        self._whitelist = whitelist
        self._allowed_commands = frozenset(whitelist)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolExecutionResult]],
    ) -> ToolExecutionResult:
        """Return a validation error before an unapproved shell command executes."""
        command = self._command_for(request)
        if command is None:
            return await handler(request)

        error = self._validation_error(command, request)
        if error is not None:
            return error
        return await handler(request)

    @staticmethod
    def _command_for(request: ToolCallRequest) -> str | None:
        tool_name = str(request.tool_call.get("name", "")).strip()
        if tool_name not in _SHELL_TOOL_NAMES:
            return None
        args = request.tool_call.get("args", {})
        if not isinstance(args, Mapping):
            return None
        command = args.get("command")
        if command is None:
            return None
        command_text = str(command)
        return command_text if command_text.strip() else None

    def _validation_error(self, command: str, request: ToolCallRequest) -> ToolMessage | None:
        allowed_hint = ", ".join(sorted(self._whitelist))
        if _NESTED_BASH_COMMAND.search(command):
            message = (
                "Bash command whitelist validation failed: nested command syntax is not allowed.\n"
                f"Allowed commands: [{allowed_hint}]\n"
                "Hint: Reconstruct the shell call without command or process substitution."
            )
            return self._error_message(request, message)

        disallowed = [name for name in _extract_base_commands(command) if name not in self._allowed_commands]
        if not disallowed:
            return None
        message = (
            f"Bash command whitelist validation failed: command(s) {disallowed!r} not in allowed list.\n"
            f"Allowed commands: [{allowed_hint}]\n"
            "Hint: Reconstruct the shell call using only allowed commands."
        )
        return self._error_message(request, message)

    @staticmethod
    def _error_message(request: ToolCallRequest, message: str) -> ToolMessage:
        tool_name = str(request.tool_call.get("name", "shell")).strip() or "shell"
        tool_call_id = str(request.tool_call.get("id") or request.runtime.tool_call_id or "unknown")
        return ToolMessage(content=message, name=tool_name, tool_call_id=tool_call_id, status="error")


class NativeMiddlewareConfigCompiler:
    """Build fault-handling and shell middleware for the native agent runtime."""

    def __init__(
        self,
        models: Mapping[str, BaseChatModel],
        primary_model_name: str,
        workspace_root: Path | None,
        shell_enabled: bool,
        shell_tool_whitelist: tuple[str, ...] | None = None,
    ) -> None:
        self._models = models
        self._primary_model_name = primary_model_name
        self._workspace_root = workspace_root
        self._shell_enabled = shell_enabled
        self._shell_tool_whitelist = shell_tool_whitelist

    def compile(self) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
        """Return the default native middleware in execution-aware order."""
        fallback_models = tuple(model for name, model in self._models.items() if name != self._primary_model_name)
        middleware: list[AgentMiddleware[Any, Any, Any]] = [
            ToolErrorMiddleware(on_error=self._format_tool_error),
        ]
        if self._shell_tool_whitelist is not None:
            middleware.append(BashWhitelistMiddleware(self._shell_tool_whitelist))
        if fallback_models:
            middleware.append(ModelFallbackMiddleware(*fallback_models))
        middleware.append(ModelRetryMiddleware(on_failure="error"))
        if self._shell_enabled:
            middleware.append(
                ShellToolMiddleware(
                    workspace_root=self._workspace_root,
                    execution_policy=HostExecutionPolicy(command_timeout=_DEFAULT_SHELL_COMMAND_TIMEOUT),
                )
            )
        return tuple(middleware)

    @staticmethod
    def _format_tool_error(exc: Exception, request: ToolCallRequest) -> str:
        tool_name = request.tool.name if request.tool is not None else str(request.tool_call.get("name", "tool"))
        error = DataAgentError.from_exception(exc, component="tool")
        return (
            f"Tool '{tool_name}' failed: {error.fact}. "
            "Review the arguments and retry only if the operation is safe to repeat."
        )
