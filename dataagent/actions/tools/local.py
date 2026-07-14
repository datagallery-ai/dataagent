# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import get_type_hints

from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.jobs.envelope import SUBMIT_JOB_TOOLS, build_base_job_envelope, finalize_job_envelope
from dataagent.core.managers.action_manager.base import BaseTool, ErrorType, ToolResult, ToolType, classify_exception
from dataagent.core.managers.action_manager.schemas import ToolSchema

_PUBLIC_TOOL_ERROR = "Tool execution failed."


class LocalToolWrapper(BaseTool):
    """本地函数工具包装器"""

    def __init__(
        self,
        func: callable,
        name: str,
        category: str = "general",
        description: str | None = None,
        tool_context: ToolExecutionContext | None = None,
        **kwargs,
    ):
        """Wrap a local callable as a tool.

        Args:
            func: Python function to invoke.
            name: Registered tool name.
            category: Tool category label.
            description: Tool description for the LLM. When ``None``, falls back to ``func.__doc__``.
                When set (including an empty string), the explicit value is used as-is.
            tool_context: Per-Agent internal context injected into tools that declare ``_tool_context``.
            **kwargs: Extra configuration stored on the tool instance.
        """
        resolved = (func.__doc__ or "") if description is None else description
        super().__init__(name, category, resolved, **kwargs)
        self.func = func
        self.tool_type = ToolType.LOCAL_FUNCTION
        self._signature = inspect.signature(func)
        self._type_hints = get_type_hints(func)
        self.tool_context = tool_context

    def get_schema(self) -> ToolSchema:
        """生成工具Schema"""
        return ToolSchema.from_function(self.func, self.name)

    def call(self, **kwargs) -> ToolResult:
        """执行本地函数"""
        try:
            # 验证参数
            is_valid, error = self.validate_input(**kwargs)
            if not is_valid:
                return ToolResult(
                    success=False,
                    error=f"Invalid input parameters for tool '{self.name}': {error}",
                    error_type=ErrorType.VALIDATION_ERROR,
                )

            if inspect.iscoroutinefunction(self.func):
                return ToolResult(
                    success=False,
                    error=f"Async local tool '{self.name}' must be called via acall().",
                    metadata={"tool_type": "local_function", "function_name": self.func.__name__},
                )

            inject_err = self._inject_tool_context(kwargs)
            if inject_err is not None:
                return inject_err

            result = self.func(**kwargs)

            return ToolResult(
                success=True, data=result, metadata={"tool_type": "local_function", "function_name": self.func.__name__}
            )

        except Exception as e:
            error_type, policy = classify_exception(e)
            logger.exception("Local tool '{}' failed with {}", self.name, type(e).__name__)
            return ToolResult(
                success=False,
                error=_PUBLIC_TOOL_ERROR,
                metadata={"tool_type": "local_function", "error_type": type(e).__name__},
                error_type=error_type,
                retriable=policy.retriable,
                max_retries=policy.max_retries,
            )

    async def acall(self, **kwargs) -> ToolResult:
        """异步执行本地函数"""
        try:
            is_valid, error = self.validate_input(**kwargs)
            if not is_valid:
                return ToolResult(
                    success=False,
                    error=f"Invalid input parameters for tool '{self.name}': {error}",
                    error_type=ErrorType.VALIDATION_ERROR,
                )

            inject_err = self._inject_tool_context(kwargs)
            if inject_err is not None:
                return inject_err

            if inspect.iscoroutinefunction(self.func):
                result = await self.func(**kwargs)
            else:
                result = await asyncio.to_thread(self.func, **kwargs)

            return ToolResult(
                success=True, data=result, metadata={"tool_type": "local_function", "function_name": self.func.__name__}
            )
        except Exception as e:
            error_type, policy = classify_exception(e)
            logger.exception("Local tool '{}' failed with {}", self.name, type(e).__name__)
            return ToolResult(
                success=False,
                error=_PUBLIC_TOOL_ERROR,
                metadata={"tool_type": "local_function", "error_type": type(e).__name__},
                error_type=error_type,
                retriable=policy.retriable,
                max_retries=policy.max_retries,
            )

    def _tool_per_call_config(self) -> dict:
        """Return a shallow copy of this tool's YAML ``TOOLS.local_functions[].config`` slice."""
        return dict(self.config or {})

    def _build_injected_tool_context(self) -> ToolExecutionContext:
        """Merge per-Agent context, per-tool YAML ``config``, and active ``Runtime``.

        ``Runtime`` is taken from :func:`~dataagent.core.framework_adapters.runtime.context.get_current_runtime`
        when the tool runs inside a workflow node (Flex / LangGraph / openjiuwen).
        """
        from dataagent.core.framework_adapters.runtime.context import get_current_runtime

        base = self.tool_context
        tool_config = self._tool_per_call_config()
        return ToolExecutionContext(
            config_manager=base.config_manager if base is not None else None,
            tool_config=tool_config,
            runtime=get_current_runtime(),
            job_envelope=dict(base.job_envelope) if base is not None and base.job_envelope else {},
        )

    def _inject_tool_context(self, kwargs: dict) -> ToolResult | None:
        """Inject ``_tool_context`` when the wrapped function declares it.

        Returns:
            A failed :class:`ToolResult` when injection cannot proceed; ``None`` when OK to call ``func``.
        """
        if "_tool_context" not in self._signature.parameters:
            return None
        if self.tool_context is None:
            return ToolResult(
                success=False,
                error=(
                    f"Tool '{self.name}' requires _tool_context but ToolManager did not provide ToolExecutionContext."
                ),
                metadata={"tool_type": "local_function", "function_name": self.func.__name__},
            )
        try:
            injected_context = self._build_injected_tool_context()
            injected_context = self._attach_job_envelope(injected_context, kwargs)
        except ValueError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                error_type=ErrorType.VALIDATION_ERROR,
                metadata={"tool_type": "local_function", "function_name": self.func.__name__},
            )
        except Exception as exc:
            logger.exception("Local tool context injection failed for '{}' with {}", self.name, type(exc).__name__)
            return ToolResult(
                success=False,
                error="Tool context injection failed.",
                metadata={"tool_type": "local_function", "function_name": self.func.__name__},
            )
        kwargs["_tool_context"] = injected_context
        return None

    def _attach_job_envelope(self, context: ToolExecutionContext, kwargs: dict) -> ToolExecutionContext:
        """Build and finalize a submit-tool envelope onto the injected tool context.

        Args:
            context: Per-call tool context assembled by :meth:`_build_injected_tool_context`.
            kwargs: LLM-visible tool arguments for the current invocation.

        Returns:
            Context with ``job_envelope`` populated for submit lifecycle tools.
        """
        if self.name not in SUBMIT_JOB_TOOLS:
            return context
        base = build_base_job_envelope(self.name, kwargs)
        if base is None:
            return context
        candidate = dict(context.job_envelope) if context.job_envelope else dict(base)
        finalized = finalize_job_envelope(self.name, base, candidate)
        return replace(context, job_envelope=finalized)
