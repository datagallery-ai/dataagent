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

"""
Base Agent class for all DataAgent agents.

This module provides the abstract base class that all DataAgent agents should inherit from,
defining the common interface and shared functionality.
"""

import functools
import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from dataagent.core.cbb.base_hook import BaseHook
from dataagent.core.utils.performance import bind_agent_performance


class BaseAgent(ABC):
    """
    Abstract base class for all DataAgent agents.

    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Initialize the base agent.

        Args:
            config: Optional configuration dictionary
        """
        self.config = config or {}
        self._pre_hooks: list[BaseHook] = []
        self._post_hooks: list[BaseHook] = []

    @staticmethod
    def _validate_hook(hook: object, location: str) -> BaseHook:
        """Validate that hook has the correct signature: (state) or (state, runtime).

        Additional **keyword-only** parameters with defaults are permitted; they are
        bound by the framework via ``functools.partial`` from HOOKS YAML config fields
        (see :meth:`FlexAgent._resolve_hook_item`). Positional parameters beyond
        ``state`` / ``runtime`` are rejected.

        Allowed forms:
        - ``def hook(state)``
        - ``def hook(state, runtime)``
        - ``def hook(state, runtime, *, config=None)``
        - ``def hook(state, *, config=None)``  ← no ``runtime``, only keyword-only
        """
        if not callable(hook):
            raise TypeError(f"Invalid hook at {location}: expected callable, got {type(hook).__name__}")
        params = list(inspect.signature(hook).parameters.values())
        if not params or params[0].name != "state":
            raise TypeError(f"Invalid hook at {location}: first parameter must be named 'state'")
        # second param: if positional, must be "runtime"; if keyword-only, it's a config param
        if len(params) >= 2:
            second = params[1]
            if (
                second.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                and second.name != "runtime"
            ):
                raise TypeError(f"Invalid hook at {location}: second positional parameter must be named 'runtime'")
        for param in params[2:]:
            kind = param.kind
            if kind == inspect.Parameter.VAR_KEYWORD:
                continue
            if kind != inspect.Parameter.KEYWORD_ONLY or param.default is inspect.Parameter.empty:
                raise TypeError(
                    f"Invalid hook at {location}: only (state) or (state, runtime) "
                    "are allowed; extra parameters must be keyword-only with defaults"
                )
        return hook  # type: ignore[return-value]

    @staticmethod
    def _bind_hook_config(fn: Any, item: Mapping[str, Any], *, location: str) -> Any:
        """Bind HOOKS YAML config fields (except name/model/import) to the hook via ``functools.partial``.

        Only keyword params actually declared by the hook are bound; unknown fields
        raise ``TypeError`` so misconfigured hooks surface immediately. Returns ``fn``
        unchanged when no config fields are present.
        """
        reserved = {"name", "model", "import"}
        config_fields = {k: v for k, v in item.items() if k not in reserved}
        if not config_fields:
            return fn
        sig_params = inspect.signature(fn).parameters
        bindable: dict[str, Any] = {}
        for key, value in config_fields.items():
            if key not in sig_params:
                raise TypeError(
                    f"{location}: hook config field {key!r} is not accepted by hook "
                    f"{getattr(fn, '__name__', fn)!r}; declared params: {list(sig_params)}"
                )
            bindable[key] = value
        return functools.partial(fn, **bindable)

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict[str, Any]) -> "BaseAgent":
        """
        Create an agent instance from a configuration dictionary.

        This method should parse the configuration and instantiate the agent
        with all necessary components (nodes, router, workflow).

        Args:
            config: Configuration dictionary containing agent settings

        Returns:
            Instance of the agent

        Raises:
            ValueError: If configuration is invalid or missing required fields
        """
        pass

    @abstractmethod
    async def chat(self, message: str, initial_state: dict[str, Any] | None = None, **kwargs) -> Any:
        """
        Process a chat message through the agent workflow.

        Args:
            message: User input message
            initial_state: Optional initial state for the workflow
            **kwargs: Additional arguments specific to the agent implementation

        Returns:
            Response from the agent (format may vary by implementation)

        Raises:
            RuntimeError: If chat execution fails
        """
        pass

    @abstractmethod
    def astream(self, *args, **kwargs) -> AsyncGenerator:
        """
        Stream responses from the agent asynchronously.

        Args:
            *args: Positional arguments for streaming
            **kwargs: Keyword arguments for streaming

        Returns:
            Async generator yielding response chunks

        Raises:
            NotImplementedError: If streaming is not supported
        """
        pass

    def add_pre_hook(self, hook: BaseHook, side: str = "right") -> None:
        """Add a pre-hook to run on state before workflow execution."""
        if side == "left":
            self._pre_hooks.insert(0, hook)
            return
        if side != "right":
            raise ValueError("side must be 'left' or 'right'")
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: BaseHook, side: str = "right") -> None:
        """Add a post-hook to run on state after workflow execution."""
        if side == "left":
            self._post_hooks.insert(0, hook)
            return
        if side != "right":
            raise ValueError("side must be 'left' or 'right'")
        self._post_hooks.append(hook)

    def _performance_run(
        self,
        *,
        state: Mapping[str, Any] | None = None,
        backend: str | None = None,
        flush_state_provider: Any = None,
        summary_sink: Any = None,
    ):
        """委托 :func:`~dataagent.core.utils.performance.bind_agent_performance`。

        若 ``state`` 是可变 dict 且含 ``_performance_summary_sink`` 临时字段，则在此
        读取并**移除**，避免进入业务 workflow state（见设计文档 §7/D4）。该字段由
        子进程入口 ``sub_agent_entry._run_agent`` 注入，用于把局部 summary 回传给
        调用方而不写入长期存活的 Agent 实例字段。
        """
        sink = summary_sink
        if sink is None and isinstance(state, dict):
            sink = state.pop("_performance_summary_sink", None)
        return bind_agent_performance(
            self,
            state=state,
            backend=backend,
            flush_state_provider=flush_state_provider,
            summary_sink=sink,
        )
