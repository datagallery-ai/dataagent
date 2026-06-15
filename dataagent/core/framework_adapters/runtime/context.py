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
import contextvars
import inspect
import json
import queue as _queue
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, cast

_current_runtime: contextvars.ContextVar[Any] = contextvars.ContextVar("dataagent_current_runtime", default=None)
_current_backend_runtime: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "dataagent_current_backend_runtime", default=None
)

_current_stream_queue: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "dataagent_current_stream_queue", default=None
)

# openjiuwen astream 侧的“旁路事件队列”：
# - 用 thread-safe queue.Queue，避免跨线程/跨 event-loop 写入丢失
# - 通过 runtime 对象属性传递（比 contextvar 更稳：openjiuwen 可能跨线程执行 node）
_STREAM_QUEUE_ATTR = "_dataagent_stream_queue"


def _resolve_ojw_global_state_key() -> str:
    """Return the openjiuwen 0.1.14 global state key."""
    from openjiuwen.core.session.state.base import GLOBAL_STATE_KEY  # type: ignore[import-not-found]

    return GLOBAL_STATE_KEY


def _resolve_ojw_interrupt_types() -> tuple[type[Any], type[Any]]:
    """Return the openjiuwen 0.1.14 interrupt classes."""
    from openjiuwen.core.graph.pregel.base import GraphInterrupt, Interrupt  # type: ignore[import-not-found]

    return GraphInterrupt, Interrupt


def set_current_runtime(runtime: Any) -> None:
    """设置当前 Context 中的运行时对象。"""
    _current_runtime.set(runtime)


def get_current_runtime() -> Any:
    """获取当前 Context 中的运行时对象。"""
    return _current_runtime.get()


def clear_current_runtime() -> None:
    """清除当前 Context 中的运行时对象。"""
    _current_runtime.set(None)


def set_current_backend_runtime(runtime: Any) -> None:
    """设置当前 Context 中的 backend 运行时对象（如 openjiuwen runtime）。"""
    _current_backend_runtime.set(runtime)


def get_current_backend_runtime() -> Any:
    """获取当前 Context 中的 backend 运行时对象。"""
    return _current_backend_runtime.get()


def clear_current_backend_runtime() -> None:
    """清除当前 Context 中的 backend 运行时对象。"""
    _current_backend_runtime.set(None)


def _is_openjiuwen_runtime(runtime: Any) -> bool:
    """Best-effort 判断当前 runtime 是否为 openjiuwen 风格运行时。"""
    if runtime is None:
        return False
    if getattr(runtime, "write_stream", None) is not None:
        return True
    if callable(getattr(runtime, "update_global_state", None)):
        return True
    if callable(getattr(runtime, "state", None)):
        return True
    return bool(callable(getattr(runtime, "base", None)))


def set_current_stream_queue(q: Any) -> None:
    """设置当前 Context 中的流式输出队列。"""
    _current_stream_queue.set(q)


def get_current_stream_queue() -> Any:
    """获取当前 Context 中的流式输出队列。"""
    return _current_stream_queue.get()


def clear_current_stream_queue() -> None:
    """清除当前 Context 中的流式输出队列。"""
    _current_stream_queue.set(None)


def require_current_runtime() -> Any:
    """获取当前的运行时对象，若不存在则抛出异常。"""
    runtime = get_current_runtime()
    if runtime is None:
        raise RuntimeError("No active runtime in context. Did you call workflow invoke/ainvoke?")
    return runtime


class GlobalStateProxy(dict):
    """
    openjiuwen global_state 的 dict-like 代理。

    设计目标：
    - 节点侧像操作 dict 一样读写 state
    - 底层通过 runtime.get_global_state / runtime.update_global_state 读写
    - 需要全量快照时通过 runtime.base().state().get_state() 获取
    """

    def __init__(self, runtime: Any):
        """初始化全局状态代理对象。"""
        super().__init__()
        self._runtime = runtime
        # openjiuwen：update_global_state 可能是“延迟提交”的（commit/step 结束才进入 state().get_state())。
        # 为保证“同一节点调用内写后读一致”，这里维护一份本地 delta 缓存并在读取时合并。
        self._local_updates: dict[str, Any] = {}
        # openjiuwen global_state 可能缺省某些 key；为保持与 langgraph 行为一致（避免 KeyError 打断路由/节点），
        # 这里对少数关键字段提供默认值。注意：这只影响 openjiuwen 侧的 state 读取，不改变 langgraph 的状态模型。

        self._defaults: dict[str, Any] = {
            "messages": [],
            "plan": [],
            "in_progress_tasks": ["<empty_marker>"],
            "feedback": "",
            "feedback_summary": "",
            "require_human_feedback": False,
            "require_reflection": False,
            "reflected_nodes": set(),
            "user_id": None,
            "session_id": None,
            "run_id": 0,
            "sub_id": 0,
        }

    def update(self, other=(), /, **kwargs) -> None:  # type: ignore[override]
        """批量更新全局状态。"""
        delta: dict[str, Any] = {}
        if other:
            if isinstance(other, dict):
                delta.update(other)
            else:
                for k, v in other:
                    delta[k] = v
        if kwargs:
            delta.update(kwargs)
        if delta:
            self._local_updates.update(delta)
            self._runtime.update_global_state(delta)

    def _snapshot(self) -> dict[str, Any]:
        """获取底层 global_state 的全量快照。"""
        try:
            global_state_key = _resolve_ojw_global_state_key()

            # openjiuwen：runtime 可能是 wrapper，base() 可能为 None。
            # 这时仍应从自身 state() 读取 GLOBAL_STATE_KEY，而不是直接返回空 dict。
            base_fn = getattr(self._runtime, "base", None)
            base_runtime = base_fn() if callable(base_fn) else None
            state_obj = None
            if base_runtime is not None:
                state_obj = getattr(base_runtime, "state", lambda: None)()
            if state_obj is None:
                state_obj = getattr(self._runtime, "state", lambda: None)()
            if state_obj is None:
                return {}
            state_dict = state_obj.get_state() or {}
            stored_global_state = state_dict.get(global_state_key) or {}
            snap = stored_global_state if isinstance(stored_global_state, dict) else {}
            # 合并本地更新，确保写后读一致（local 覆盖 snap）
            if self._local_updates:
                return {**snap, **self._local_updates}
            return snap
        except Exception:
            # 即使 snapshot 失败，也尽量返回本地更新
            return dict(self._local_updates) if self._local_updates else {}

    def __getitem__(self, key: str) -> Any:  # type: ignore[override]
        """获取指定 key 的状态值，不存在则尝试返回默认值。"""
        # 优先从全量快照读取（更稳定：不同 openjiuwen wrapper 的 get_global_state 可能读不到最新写入）
        snap = self._snapshot()
        if key in snap:
            return snap.get(key)

        val = self._runtime.get_global_state(key)
        if val is None:
            if key in self._defaults:
                return self._defaults[key]
            raise KeyError(key)
        return val

    def __setitem__(self, key: str, value: Any) -> None:  # type: ignore[override]
        """更新指定 key 的全局状态。"""
        self._local_updates[key] = value
        self._runtime.update_global_state({key: value})

    def __delitem__(self, key: str) -> None:  # type: ignore[override]
        """将指定 key 的值更新为 None（并不真正删除键）。"""
        self._local_updates[key] = None
        self._runtime.update_global_state({key: None})

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        """迭代全局状态的键。"""
        return iter(self._snapshot().keys())

    def __len__(self) -> int:  # type: ignore[override]
        """返回全局状态的键值对数量。"""
        return len(self._snapshot())

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        """安全获取状态值，支持自定义默认值。"""
        snap = self._snapshot()
        if key in snap:
            return snap.get(key)

        val = self._runtime.get_global_state(key)
        if val is None:
            if key in self._defaults:
                return self._defaults[key]
            return default
        return val


def global_state() -> GlobalStateProxy:
    """获取当前运行时的全局状态代理。"""
    runtime = get_current_backend_runtime()
    if runtime is None:
        raise RuntimeError("No active backend runtime in context. Did you call workflow invoke/ainvoke?")
    return GlobalStateProxy(runtime)


def get_global_state_snapshot(runtime: Any | None = None) -> dict[str, Any]:
    """
    获取 global_state 的全量快照（plain dict）。

    因此通过 runtime.base().state().get_state()[GLOBAL_STATE_KEY] 读取快照。
    """
    runtime_obj = runtime or get_current_backend_runtime()
    if runtime_obj is None:
        raise RuntimeError("No active backend runtime in context. Did you call workflow invoke/ainvoke?")
    try:
        global_state_key = _resolve_ojw_global_state_key()

        # openjiuwen：有时 current_runtime 会被设置为 base runtime（WorkflowRuntime），其 base() 可能为 None。
        # 这时仍应从自身 state() 读取 GLOBAL_STATE_KEY，而不是直接返回空 dict。
        base_fn = getattr(runtime_obj, "base", None)
        base_runtime = base_fn() if callable(base_fn) else None
        state_obj = None
        if base_runtime is not None:
            state_obj = getattr(base_runtime, "state", lambda: None)()
        if state_obj is None:
            state_obj = getattr(runtime_obj, "state", lambda: None)()
        if state_obj is None:
            return {}
        state_dict = state_obj.get_state() or {}
        stored_global_state = state_dict.get(global_state_key) or {}
        return stored_global_state if isinstance(stored_global_state, dict) else {}
    except Exception:
        return {}


def get_stream_writer() -> Callable[[dict[str, Any]], None]:
    """
    在 langgraph backend 下：复用 langgraph.config.get_stream_writer
    在 openjiuwen backend 下：使用 runtime.write_stream
    """
    runtime = get_current_backend_runtime()
    if not _is_openjiuwen_runtime(runtime):
        # fallback to langgraph
        from langgraph.config import get_stream_writer as _lg_get_stream_writer  # type: ignore[import-not-found]

        try:
            return _lg_get_stream_writer()
        # 防止在langgraph外运行单个原子功能时报错，返回空函数
        except RuntimeError:

            def null_writer(data: dict[str, Any]) -> None:
                _ = data

            return null_writer

    # openjiuwen：WrappedNodeRuntime 可能没有 write_stream（或无法 setattr），但 base runtime 通常有
    write_stream = getattr(runtime, "write_stream", None)
    if write_stream is None:
        try:
            base_fn = getattr(runtime, "base", None)
            base_runtime = base_fn() if callable(base_fn) else None
            if base_runtime is not None:
                write_stream = getattr(base_runtime, "write_stream", None)
        except Exception:
            write_stream = None

    def _writer(data: dict[str, Any]) -> None:
        # 1) dataagent 旁路：如果 invoke 显式注入了 stream_queue，优先写入（保证前端能收到）
        try:
            stream_queue = get_current_stream_queue()
            if isinstance(stream_queue, _queue.Queue):
                stream_queue.put(data)
        except Exception:
            pass

        # 2) 尝试写入 openjiuwen 自带 stream（若存在）
        if write_stream is None:
            return
        try:
            loop = asyncio.get_running_loop()
            # openjiuwen 的 write_stream 入口可能返回 sync 或 async 结果
            result = write_stream(data)
            if inspect.isawaitable(result):
                loop.create_task(cast(Coroutine[Any, Any, Any], result))
        except RuntimeError:
            # 没有 event loop：如果是 coroutine 则 asyncio.run，否则直接调用即可
            result = write_stream(data)
            if inspect.isawaitable(result):
                asyncio.run(cast(Coroutine[Any, Any, Any], result))

    return _writer


def interrupt(message: Any) -> Any:
    """
    在 langgraph backend 下：langgraph.types.interrupt
    在 openjiuwen backend 下：抛 openjiuwen GraphInterrupt
    """
    runtime = get_current_backend_runtime()
    if not _is_openjiuwen_runtime(runtime):
        from langgraph.types import interrupt as _lg_interrupt  # type: ignore[import-not-found]

        return _lg_interrupt(message)

    graph_interrupt_cls, interrupt_cls = _resolve_ojw_interrupt_types()

    msg = json.dumps(message, ensure_ascii=False) if isinstance(message, (dict, list)) else str(message)

    # openjiuwen 的 GraphInterrupt 可能被引擎包装为 JiuWenBaseException，导致上层无法直接拿到 message。
    # 这里把中断文案同步写入 global_state，供 workflow 侧 fallback 识别与落 checkpoint。
    def _commit_base_state(_runtime: Any) -> None:
        try:
            base_fn = getattr(_runtime, "base", None)
            base_runtime = base_fn() if callable(base_fn) else None
            if base_runtime is None:
                return
            state_obj = getattr(base_runtime, "state", lambda: None)()
            if state_obj is None:
                return
            if hasattr(state_obj, "commit"):
                state_obj.commit()  # type: ignore[attr-defined]
        except Exception:
            return

    try:
        update_fn = getattr(runtime, "update_global_state", None)
        if callable(update_fn):
            update_fn({"__last_interrupt__": msg})
        else:
            state_obj = runtime.state()
            if hasattr(state_obj, "update_global"):
                state_obj.update_global({"__last_interrupt__": msg})  # type: ignore[attr-defined]
        # best-effort commit
        try:
            state_obj2 = runtime.state()
            if hasattr(state_obj2, "commit"):
                state_obj2.commit()  # type: ignore[attr-defined]
        except Exception:
            pass
        # 再尝试 commit base runtime（某些 openjiuwen wrapper 的 state().commit 不会提交 global_state_updates）
        _commit_base_state(runtime)
    except Exception:
        pass
    raise graph_interrupt_cls(interrupt_cls(msg))
