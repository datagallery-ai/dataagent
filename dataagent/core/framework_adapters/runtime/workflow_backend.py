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

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from dataagent.core.cbb.runtime import Runtime


@runtime_checkable
class WorkflowBackend(Protocol):
    """
    Core 层统一的 Workflow Backend 抽象（接口声明）。

    说明：
    - 使用 Protocol（结构化类型），实现类不必显式继承，只要方法“长得一样”即可被当作 backend 使用。
    - Flow/Flex 只依赖该接口，不直接 import LangGraph/openjiuwen。
    """

    async def ainvoke(self, initial_state: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        """异步执行 workflow，返回最终 state（dict）。"""
        ...

    def astream(self, initial_state: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        """流式执行 workflow，yield 事件（不同 backend 可能是 tuple/dict 等）。"""
        ...

    def set_runtime(self, runtime: Any) -> None:
        """将 Agent Runtime 注入 workflow，供 _wrap_process 显式传给节点。"""
        ...

    def load_checkpoint_state(self, checkpoint_id: str) -> tuple[str, dict[str, Any]]:
        """读取 checkpoint，返回 (start_at, recovered_state)。不支持则抛异常。"""
        ...

    async def resume(self, *, checkpoint_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        """
        从中断恢复，继续执行。

        约定：
        - openjiuwen：checkpoint_id 是 core 存储的 checkpoint 记录 id
        - langgraph：checkpoint_id 通常对应 thread_id（由上层 checkpointer 体系维护）
        """
        ...

    def astream_resume(self, *, checkpoint_id: str, message: str, **kwargs: Any) -> AsyncIterator[Any]:
        """从中断恢复，继续执行（流式版本）。"""
        ...


class LangGraphWorkflowBackend:
    """
    LangGraph backend 封装：
    - 统一负责 graph.compile 的细节（store/checkpointer）
    - 兼容 server 侧的原生调用签名：astream(input=..., config=..., stream_mode=..., checkpointer=...)
    """

    def __init__(self, workflow: Any):
        """创建 LangGraph backend 封装，内部持有 LangGraphWorkflow。"""
        self._wf = workflow

    def set_runtime(self, runtime: Any) -> None:
        """将 Agent Runtime 传递给底层 workflow，供 _wrap_process 显式注入节点。

        在每次 chat()/astream() 调用前执行，保证节点收到最新的 workspace_dir / hierarchy。
        """
        if hasattr(self._wf, "set_runtime"):
            self._wf.set_runtime(runtime)

    async def ainvoke(self, initial_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """异步执行（langgraph）。kwargs 可包含 store。"""
        store = kwargs.pop("store", None)
        _ = kwargs
        return await self._wf.ainvoke(initial_state, store=store)

    def astream(self, initial_state: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        """
        流式执行（langgraph）。

        兼容两类调用：
        - 统一接口：astream(initial_state=...)
        - server 原生接口：astream(input=..., config=..., stream_mode=..., checkpointer=...)
        """
        if "input" not in kwargs:
            kwargs["input"] = initial_state
        return self._astream_native(**kwargs)

    def load_checkpoint_state(self, checkpoint_id: str) -> tuple[str, dict[str, Any]]:
        """LangGraph 的恢复语义不在此处实现（依赖 checkpointer/thread_id/Command）。"""
        raise NotImplementedError(
            "LangGraph backend 的恢复语义依赖 checkpointer/thread_id，上层应直接传入 thread_id 进行 resume。"
        )

    async def resume(self, *, checkpoint_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        """
        LangGraph 恢复：
        - 需要 checkpointer 参与
        - 输入使用 Command(resume=message)
        - config.configurable.thread_id 使用 checkpoint_id
        """
        from langgraph.types import Command  # type: ignore[import-not-found]

        store = kwargs.pop("store", None)
        checkpointer = kwargs.pop("checkpointer", None)
        config = kwargs.pop("config", None) or {"configurable": {"thread_id": str(checkpoint_id)}}
        if checkpointer is None:
            raise ValueError("LangGraph resume requires 'checkpointer'.")
        if getattr(self._wf, "graph", None) is None:
            self._wf.ensure_graph_built()  # type: ignore[attr-defined]
        compiled = self._wf.graph.compile(store=store, checkpointer=checkpointer)
        run_config = self._merge_langgraph_config(config)
        return await compiled.ainvoke(Command(resume=message), config=run_config)

    def astream_resume(self, *, checkpoint_id: str, message: str, **kwargs: Any) -> AsyncIterator[Any]:
        """LangGraph 流式恢复（需要 checkpointer，输入使用 Command(resume=message)）。"""
        from langgraph.types import Command  # type: ignore[import-not-found]

        store = kwargs.pop("store", None)
        checkpointer = kwargs.pop("checkpointer", None)
        config = kwargs.pop("config", None) or {"configurable": {"thread_id": str(checkpoint_id)}}
        if checkpointer is None:
            raise ValueError("LangGraph astream_resume requires 'checkpointer'.")
        if getattr(self._wf, "graph", None) is None:
            self._wf.ensure_graph_built()  # type: ignore[attr-defined]
        compiled = self._wf.graph.compile(store=store, checkpointer=checkpointer)
        run_config = self._merge_langgraph_config(config)
        return compiled.astream(input=Command(resume=message), config=run_config, **kwargs)

    async def aget_graph_state(
        self,
        *,
        config: dict[str, Any],
        checkpointer: Any,
        store: Any = None,
    ) -> dict[str, Any]:
        """读取 checkpointer 中当前 thread 的最新图 state。"""
        if getattr(self._wf, "graph", None) is None:
            self._wf.ensure_graph_built()  # type: ignore[attr-defined]
        compiled = self._wf.graph.compile(store=store, checkpointer=checkpointer)
        snapshot = await compiled.aget_state(config)
        values = getattr(snapshot, "values", None)
        if isinstance(values, dict):
            return dict(values)
        return {}

    def _astream_native(self, **kwargs: Any) -> AsyncIterator[Any]:
        """
        langgraph 原生 astream 入口（内部使用）。

        - 若传入 checkpointer：每次重新 compile，避免复用已关闭的 checkpointer
        - 否则：复用 LangGraphWorkflow 内部 compiled_graph 缓存
        """
        store = kwargs.pop("store", None)
        checkpointer = kwargs.pop("checkpointer", None)
        if checkpointer is not None:
            if getattr(self._wf, "graph", None) is None:
                self._wf.ensure_graph_built()  # type: ignore[attr-defined]
            compiled = self._wf.graph.compile(store=store, checkpointer=checkpointer)
            kwargs["config"] = self._merge_langgraph_config(kwargs.get("config"))
            return compiled.astream(**kwargs)

        input_state = kwargs.pop("input", {})
        return self._wf.astream(input_state, store=store, **kwargs)

    def _merge_langgraph_config(self, config: Any) -> dict[str, Any]:
        """合并 LangGraph run config，写入由 max_iter 推导的 recursion_limit。"""
        merge_fn = getattr(self._wf, "merge_run_config", None)
        if callable(merge_fn):
            return merge_fn(config if isinstance(config, dict) else None)
        run_config = dict(config) if isinstance(config, dict) else {}
        resolve_fn = getattr(self._wf, "resolve_recursion_limit", None)
        if callable(resolve_fn):
            run_config.setdefault("recursion_limit", resolve_fn())
        else:
            run_config.setdefault("recursion_limit", Runtime.resolve_recursion_limit_from_max_iter(None))
        return run_config


class OpenJiuWenWorkflowBackend:
    """openjiuwen backend 的薄封装：直接代理 OpenJiuWenWorkflow。"""

    def __init__(self, workflow: Any):
        """创建 openjiuwen backend 封装，内部持有 OpenJiuWenWorkflow。"""
        self._wf = workflow

    def set_runtime(self, runtime: Any) -> None:
        """将 Agent Runtime 传递给底层 workflow。"""
        if hasattr(self._wf, "set_runtime"):
            self._wf.set_runtime(runtime)

    async def ainvoke(self, initial_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """异步执行（openjiuwen），kwargs 透传给 OpenJiuWenWorkflow.ainvoke。"""
        return await self._wf.ainvoke(initial_state, **kwargs)

    def astream(self, initial_state: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        """流式执行（openjiuwen），kwargs 透传给 OpenJiuWenWorkflow.astream。"""
        return self._wf.astream(initial_state, **kwargs)

    def load_checkpoint_state(self, checkpoint_id: str) -> tuple[str, dict[str, Any]]:
        """读取 openjiuwen checkpoint（通常为 human_feedback 中断点）。"""
        try:
            return self._wf.load_checkpoint_state(checkpoint_id)
        except Exception as e:
            logger.error(f"Failed to load openjiuwen checkpoint: {checkpoint_id}, err={e}")
            raise

    async def resume(self, *, checkpoint_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        """
        openjiuwen 恢复：
        - load checkpoint -> (start_at, state)
        - 注入 __human_feedback_resume__
        - 从 start_at 继续 ainvoke
        """
        session_id = kwargs.pop("session_id", None)
        start_at, recovered_state = self.load_checkpoint_state(str(checkpoint_id))
        recovered_state["__human_feedback_resume__"] = message
        recovered_state.setdefault("developer_mode", False)
        if session_id:
            recovered_state.setdefault("conversation_id", session_id)
        return await self._wf.ainvoke(recovered_state, start_at=start_at)

    def astream_resume(self, *, checkpoint_id: str, message: str, **kwargs: Any) -> AsyncIterator[Any]:
        """openjiuwen 流式恢复：load checkpoint + 注入恢复输入 + 从 start_at 继续 astream。"""
        session_id = kwargs.pop("session_id", None)
        start_at, recovered_state = self.load_checkpoint_state(str(checkpoint_id))
        recovered_state["__human_feedback_resume__"] = message
        recovered_state.setdefault("developer_mode", False)
        if session_id:
            recovered_state.setdefault("conversation_id", session_id)
        return self._wf.astream(recovered_state, start_at=start_at, **kwargs)
