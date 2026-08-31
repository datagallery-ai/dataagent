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

import traceback
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional, cast

from langgraph.errors import GraphInterrupt  # type: ignore[import-not-found]
from langgraph.graph import StateGraph  # type: ignore[import-not-found]
from langgraph.runtime import Runtime as LangGraphRuntime  # type: ignore[import-not-found]
from loguru import logger

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_router import BaseRouter
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.framework_adapters.runtime.context import (
    reset_current_runtime,
    set_current_runtime,
)


@dataclass(frozen=True, slots=True)
class _WorkflowContext:
    """LangGraph run-scoped context carrying the DataAgent Runtime."""

    runtime: Any


class LangGraphWorkflow:
    def __init__(
        self,
        nodes: list[BaseNode],
        router: BaseRouter,
        state_class: type[BaseState],
    ):
        self.nodes = {node.name: node for node in nodes}
        self.router = router
        self.state_class = state_class
        self.graph: Any = None
        self.compiled_graph: Any = None
        self._runtime_context: ContextVar[Any] = ContextVar(
            f"dataagent_langgraph_runtime_{id(self)}",
            default=None,
        )
        self._build_graph()

    @staticmethod
    def _build_run_context(runtime: Any) -> _WorkflowContext:
        """Wrap a DataAgent Runtime in LangGraph's typed run context."""
        return _WorkflowContext(runtime=runtime)

    def set_runtime(self, runtime: Any) -> None:
        """Bind a compatibility Runtime default in the current async context."""
        self._runtime_context.set(runtime)

    def resolve_recursion_limit(self, runtime: Optional[Any] = None) -> int:  # noqa: UP045
        """从本次调用的 Runtime.max_iter 推导图引擎步数上限。"""
        rt = self._resolve_runtime(runtime)
        if rt is not None:
            resolve_fn = getattr(rt, "resolve_workflow_recursion_limit", None)
            if callable(resolve_fn):
                return int(cast(Any, resolve_fn()))
            return Runtime.resolve_recursion_limit_from_max_iter(getattr(rt, "max_iter", None))
        return Runtime.resolve_recursion_limit_from_max_iter(None)

    def merge_run_config(
        self,
        config: Optional[dict[str, Any]] = None,  # noqa: UP045
        runtime: Optional[Any] = None,  # noqa: UP045
    ) -> dict[str, Any]:
        """合并调用方 config，并写入 recursion_limit（调用方已显式设置时不覆盖）。"""
        run_config = dict(config) if isinstance(config, dict) else {}
        run_config.setdefault("recursion_limit", self.resolve_recursion_limit(runtime))
        return run_config

    def invoke(
        self,
        initial_state: dict[str, Any],
        store: Any = None,
        *,
        runtime: Optional[Any] = None,  # noqa: UP045
        checkpointer: Any = None,
        config: Optional[dict[str, Any]] = None,  # noqa: UP045
    ) -> dict[str, Any]:
        """
        Invoke the workflow with a given initial state.

        Args:
            initial_state (dict[str, Any]): The initial state for the workflow execution.
            store (optional): Optional store object for the workflow backend.

        Returns:
            dict[str, Any]: The final state after workflow execution.

        Raises:
            Exception: If workflow execution fails, the original exception is raised.
        """
        compiled = self._get_compiled_graph(store=store, checkpointer=checkpointer)
        call_runtime = self._resolve_runtime(runtime)
        try:
            with self._activate_runtime(call_runtime):
                return compiled.invoke(
                    initial_state,
                    config=self.merge_run_config(config, call_runtime),
                    context=self._build_run_context(call_runtime),
                )
        except Exception as e:
            logger.error(f"LangGraph workflow execution failed: {e}\n Traceback: {traceback.format_exc()}")
            raise e

    async def ainvoke(
        self,
        initial_state: Any,
        store: Any = None,
        *,
        runtime: Optional[Any] = None,  # noqa: UP045
        checkpointer: Any = None,
        config: Optional[dict[str, Any]] = None,  # noqa: UP045
    ) -> dict[str, Any]:
        """
        Asynchronously invoke the workflow with a given initial state.

        Args:
            initial_state (dict[str, Any]): The initial state for the workflow execution.
            store (optional): Optional store object for the workflow backend.

        Returns:
            dict[str, Any]: The final state after workflow execution.

        Raises:
            Exception: If workflow execution fails, the original exception is raised.
        """
        compiled = self._get_compiled_graph(store=store, checkpointer=checkpointer)
        call_runtime = self._resolve_runtime(runtime)
        with self._activate_runtime(call_runtime):
            result = await compiled.ainvoke(
                initial_state,
                config=self.merge_run_config(config, call_runtime),
                context=self._build_run_context(call_runtime),
            )
            return result

    async def astream(
        self,
        initial_state: Any,
        store: Any = None,
        *,
        runtime: Optional[Any] = None,  # noqa: UP045
        checkpointer: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any] | tuple[str, dict[str, Any]]]:
        """
        Asynchronously stream the execution of the workflow with a given initial state.

        Args:
            initial_state (dict[str, Any]): The initial state for the workflow execution.
            store (optional): Optional store object for the workflow backend.
            **kwargs: Additional keyword arguments to configure streaming.

        Returns:
            AsyncIterator[Union[dict[str, Any], Tuple[str, dict[str, Any]]]]:
                An asynchronous iterator over the workflow execution results.

        Raises:
            Exception: If workflow execution fails, the original exception is raised.
        """
        compiled = self._get_compiled_graph(store=store, checkpointer=checkpointer)
        call_runtime = self._resolve_runtime(runtime)
        run_config = self.merge_run_config(kwargs.pop("config", None), call_runtime)
        with self._activate_runtime(call_runtime):
            async for result in compiled.astream(
                initial_state,
                config=run_config,
                context=self._build_run_context(call_runtime),
                **kwargs,
            ):
                yield result

    def get_graph_info(self) -> dict[str, Any]:
        """
        Get information about the workflow graph.

        Returns:
            dict[str, Any]: A dictionary containing graph information, including:
                - "nodes": List of all node names in the workflow.
                - "use_langgraph": Whether the workflow uses LangGraph execution (bool).
                - "compiled": Whether the workflow graph has been compiled (bool).
        """
        return {"nodes": list(self.nodes.keys()), "compiled": self.compiled_graph is not None}

    def ensure_graph_built(self) -> None:
        """
        Ensure graph has been built.
        - `_build_graph()` 是内部实现细节（受保护成员）。
        - backend 封装（如 `LangGraphWorkflowBackend`）需要确保 graph 存在时，应调用该公开方法，
          避免在类外直接访问 `_build_graph()`。
        """
        if self.graph is None:
            self._build_graph()

    def _build_graph(self) -> None:
        """Build and store the workflow graph."""
        self.graph = StateGraph(self.state_class, context_schema=_WorkflowContext)
        for name in self.nodes:
            self.graph.add_node(name, self._wrap_process(name))
        self.graph.set_entry_point(self.router.entry_point)
        for name in self.nodes:
            route_func = self.router.routing_rules.get(name)
            if route_func is None:
                raise ValueError(f"No routing function found for node: {name}")
            self.graph.add_conditional_edges(name, route_func)

    def _wrap_process(self, node_name: str):
        node = self.nodes[node_name]

        async def wrapped_process(state: Any, runtime: LangGraphRuntime[_WorkflowContext]):
            call_runtime = runtime.context.runtime
            token = set_current_runtime(call_runtime)
            try:
                result = await node.aprocess(state, call_runtime)
            except GraphInterrupt:
                logger.debug(f"Node {node_name} interrupted for human feedback")
                raise
            except Exception as e:
                logger.exception(f"Error in node {node_name}: {e}")
                raise
            finally:
                reset_current_runtime(token)
            return result

        return wrapped_process

    def _get_compiled_graph(self, store: Any = None, checkpointer: Any = None) -> Any:
        """Return a compiled graph, avoiding checkpointer instances in the shared cache."""
        if self.graph is None:
            self._build_graph()
        if checkpointer is not None:
            return self.graph.compile(store=store, checkpointer=checkpointer)
        if self.compiled_graph is None:
            self.compiled_graph = self.graph.compile(store=store)
        return self.compiled_graph

    def _resolve_runtime(self, runtime: Optional[Any]) -> Any:  # noqa: UP045
        """Resolve an explicit per-invocation Runtime before the compatibility default."""
        return runtime if runtime is not None else self._runtime_context.get()

    @contextmanager
    def _activate_runtime(self, runtime: Any) -> Iterator[None]:
        """Expose one invocation's Runtime through ContextVar for routers and tools."""
        token = set_current_runtime(runtime)
        try:
            yield
        finally:
            reset_current_runtime(token)
