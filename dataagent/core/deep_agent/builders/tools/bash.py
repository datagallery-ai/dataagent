# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""OpenJiuWen BashTool adapter for DataAgent command allowlists."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.shell import BashTool
from openjiuwen.harness.tools.shell.bash._semantics import (
    _extract_base_command,
    _split_pipeline,
)


def build_bash_tool(
    operation: SysOperation,
    *,
    language: str,
    allowlist: tuple[str, ...] | None,
) -> Any | None:
    """Build Jiuwen BashTool with DataAgent's compound-command semantics."""
    if allowlist == ():
        return None
    if allowlist is None:
        return BashTool(operation=operation, language=language)
    return AllowlistedBashTool(
        operation=operation,
        language=language,
        allowlist=allowlist,
    )


class AllowlistedBashTool(BashTool):
    """Jiuwen BashTool with per-segment DataAgent allowlist validation."""

    def __init__(
        self,
        *,
        operation: SysOperation,
        language: str,
        allowlist: tuple[str, ...],
    ) -> None:
        super().__init__(operation=operation, language=language)
        self._allowed_commands = frozenset(
            _extract_base_command(command) for command in allowlist
        )

    def _denied_commands(self, command: str) -> list[str]:
        segments = [
            segment
            for line in command.splitlines()
            for segment in _split_pipeline(line)
        ]
        commands = [_extract_base_command(segment) for segment in segments]
        return sorted(
            {
                command
                for command in commands
                if command and command not in self._allowed_commands
            }
        )

    @staticmethod
    def _denied_output(denied: list[str]) -> ToolOutput:
        return ToolOutput(
            success=False,
            error=(
                "command not allowed by BASH_TOOL_WHITELIST: "
                + ", ".join(denied)
            ),
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        denied = self._denied_commands(str(inputs.get("command") or ""))
        if denied:
            return self._denied_output(denied)
        return await super().invoke(inputs, **kwargs)

    async def stream(
        self,
        inputs: dict[str, Any],
        **kwargs: Any,
    ) -> AsyncIterator[ToolOutput]:
        denied = self._denied_commands(str(inputs.get("command") or ""))
        if denied:
            yield self._denied_output(denied)
            return
        async for output in super().stream(inputs, **kwargs):
            yield output
