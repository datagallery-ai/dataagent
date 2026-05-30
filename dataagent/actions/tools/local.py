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
from typing import get_type_hints

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.managers.action_manager.base import BaseTool, ErrorType, ToolResult, ToolType, classify_exception
from dataagent.core.managers.action_manager.schemas import ToolSchema
from dataagent.utils.constants import TOOL_BINDING_CONFIG_KEYS


def _classify_exception(exc: Exception) -> ErrorType:
    """根据异常类型分类错误（保持向后兼容，内部委托给统一函数）"""
    err_type, _ = classify_exception(exc)
    return err_type


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
        return ToolSchema.from_function(self.func, self.name, self.description)

    def call(self, **kwargs) -> ToolResult:
        """执行本地函数"""
        try:
            # 验证参数
            is_valid, error = self.validate_input(**kwargs)
            if not is_valid:
                return ToolResult(success=False, error=f"Invalid input parameters for tool '{self.name}': {error}")

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
            error_type = _classify_exception(e)
            return ToolResult(
                success=False,
                error=str(e),
                metadata={"tool_type": "local_function", "error_type": type(e).__name__},
                error_type=error_type,
            )

    async def acall(self, **kwargs) -> ToolResult:
        """异步执行本地函数"""
        try:
            is_valid, error = self.validate_input(**kwargs)
            if not is_valid:
                return ToolResult(success=False, error=f"Invalid input parameters for tool '{self.name}': {error}")

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
            error_type = _classify_exception(e)
            return ToolResult(
                success=False,
                error=str(e),
                metadata={"tool_type": "local_function", "error_type": type(e).__name__},
                error_type=error_type,
            )

    def _tool_binding_config(self) -> dict:
        """Return YAML ``config`` keys bound to this tool instance (e.g. ``llm_model``)."""
        return {k: v for k, v in (self.config or {}).items() if k in TOOL_BINDING_CONFIG_KEYS}

    def _build_injected_tool_context(self) -> ToolExecutionContext:
        """Merge per-Agent context, per-tool YAML ``config``, and active ``Runtime``.

        ``Runtime`` is taken from :func:`~dataagent.core.framework_adapters.runtime.context.get_current_runtime`
        when the tool runs inside a workflow node (Flex / LangGraph / openjiuwen).
        """
        from dataagent.core.framework_adapters.runtime.context import get_current_runtime

        base = self.tool_context
        tool_config = self._tool_binding_config()
        return ToolExecutionContext(
            config_manager=base.config_manager if base is not None else None,
            tool_config=tool_config,
            runtime=get_current_runtime(),
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
        except Exception as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                metadata={"tool_type": "local_function", "function_name": self.func.__name__},
            )
        kwargs["_tool_context"] = injected_context
        return None
