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
import contextlib
import inspect
import queue
import re
import traceback
import uuid
from collections.abc import AsyncIterator
from typing import Any, get_type_hints

from loguru import logger
from sqlalchemy.engine.url import make_url

from dataagent.common_utils.storer_utils import deserialize_state_from_store, serialize_state_for_store
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_router import BaseRouter
from dataagent.core.context.context import ContextFactory, build_context_init_options
from dataagent.core.framework_adapters.checkpoints.sqlite_store import SqliteCheckpointStore
from dataagent.core.framework_adapters.runtime.context import (
    GlobalStateProxy,
    _resolve_ojw_global_state_key,
    clear_current_backend_runtime,
    clear_current_runtime,
    clear_current_stream_queue,
    get_global_state_snapshot,
    set_current_backend_runtime,
    set_current_runtime,
    set_current_stream_queue,
)


def _resolve_ojw_workflow_runtime_class() -> type[Any]:
    """Return the openjiuwen 0.1.14 workflow session class."""
    from openjiuwen.core.session.internal.workflow import WorkflowSession  # type: ignore[import-not-found]

    return WorkflowSession


def _resolve_ojw_inputs_and_config_keys() -> tuple[str, str]:
    """Return the openjiuwen 0.1.14 graph input/config keys."""
    from openjiuwen.core.graph.base import CONFIG_KEY, INPUTS_KEY  # type: ignore[import-not-found]

    return INPUTS_KEY, CONFIG_KEY


def _resolve_ojw_workflow_types() -> tuple[type[Any], type[Any], type[Any], Any, Any]:
    """Return the openjiuwen 0.1.14 workflow/component/end types."""
    from openjiuwen.core.graph.pregel.constants import END  # type: ignore[import-not-found]
    from openjiuwen.core.workflow import Workflow as OJWWorkflow  # type: ignore[import-not-found]
    from openjiuwen.core.workflow.components.component import (  # type: ignore[import-not-found]
        ComponentExecutable,
        WorkflowComponent,
    )
    from openjiuwen.core.workflow.components.flow.end_comp import End  # type: ignore[import-not-found]

    return WorkflowComponent, ComponentExecutable, End, OJWWorkflow, END


def _resolve_ojw_graph_interrupt_type() -> type[Exception]:
    """Return the openjiuwen 0.1.14 GraphInterrupt type."""
    from openjiuwen.core.graph.pregel.base import GraphInterrupt  # type: ignore[import-not-found]

    return GraphInterrupt


def _get_workflow_internal(workflow: Any) -> Any | None:
    """Return workflow internal object when available."""
    return getattr(workflow, "_internal", None)


async def _reset_workflow_internal(workflow: Any) -> None:
    """Reset workflow internal state when supported."""
    internal_workflow = _get_workflow_internal(workflow)
    reset = getattr(internal_workflow, "reset", None)
    if not callable(reset):
        return
    reset_result = reset()
    if inspect.isawaitable(reset_result):
        await reset_result


def _compile_workflow_internal(workflow: Any, runtime: Any) -> Any:
    """Compile workflow against the current runtime/session."""
    internal_workflow = _get_workflow_internal(workflow)
    if internal_workflow is None:
        raise ValueError("Workflow internal compiler is unavailable")
    return internal_workflow.compile(runtime, context=None)


class OpenJiuWenWorkflow:
    """
    openjiuwen Workflow 适配：
    - state 使用 runtime 的 global_state 承载
    - 节点仍然是 BaseNode.process(state)->dict（与原逻辑一致）
    """

    def __init__(
        self,
        nodes: list[BaseNode],
        router: BaseRouter,
        state_class: type[Any] | None = None,
        config: Any | None = None,
    ):
        """初始化工作流适配器，配置节点映射与路由。"""
        self.nodes = {n.name: n for n in nodes}
        self.router = router
        self.state_class = state_class
        self.config = config
        self.workflow = None
        # 仅缓存「图是否已按 start_at 构建」；**不要**缓存 compile() 返回值。
        # openjiuwen 的 Workflow.compile(runtime) 会绑定当前 runtime（见 workflow/base.py：self._runtime.set_runtime(runtime)）；
        # 若跨多次 ainvoke 复用同一份 ExecutableGraph，第二轮仍会附着第一次的 global_state，导致 run_id/session 等错位。
        self._graph_built_for_start: set[str] = set()
        # astream 期间的事件队列（线程安全），由节点 invoke 注入到 contextvar，确保 writer() 能回传前端
        self._active_stream_queue: queue.Queue[dict[str, Any]] | None = None
        self._end_comp_id = "__dataagent_end__"
        # reducer 规则（用于 openjiuwen 写回时的 delta merge）
        self._reducers: dict[str, Any] = self._extract_reducers_from_state_class(state_class)
        # FlexAgent 通过 set_runtime 注入的 DataAgent Runtime（含 llm / workspace_dir）；与 openjiuwen 引擎 runtime 分离。
        self._agent_runtime: Any | None = None
        # 注意：不要在 __init__（同步上下文）里构图：

    @staticmethod
    def _extract_reducers_from_state_class(state_class: type[Any] | None) -> dict[str, Any]:
        """
        从 state_class 的 typing.Annotated 声明中提取 reducer。

        约定：
        - 只解析形如 `field: Annotated[T, reducer]` 的声明，reducer 必须是可调用对象。
        - 该 reducer 用于 openjiuwen 写回 global_state 时，将 node 的 delta 与已有 state 做聚合（而非覆盖）。
        """
        if state_class is None:
            return {}
        reducers: dict[str, Any] = {}
        try:
            hints = get_type_hints(state_class, include_extras=True)
        except (TypeError, NameError, AttributeError):
            # get_type_hints 可能因 forward reference 未解决或类型定义异常而失败，此时返回空 reducers
            return {}
        for key, tp in hints.items():
            # typing.Annotated 在运行时会暴露 __metadata__，其中可包含 reducer（如 operator.add）
            metadata = getattr(tp, "__metadata__", None)
            if not metadata:
                continue
            if not isinstance(metadata, tuple):
                continue
            for meta in metadata:
                if callable(meta):
                    reducers[key] = meta
                    break
        return reducers

    @staticmethod
    def _ojw_is_graph_interrupt(e: Exception) -> bool:
        return isinstance(e, _resolve_ojw_graph_interrupt_type())

    @staticmethod
    def _ojw_try_write_global(runtime: Any, merged_delta: dict[str, Any]) -> None:
        try:
            _, base_runtime = _unwrap_runtime(runtime)
            st_obj = base_runtime.state() if base_runtime is not None else runtime.state()  # type: ignore[assignment]
            if hasattr(st_obj, "update_global"):
                st_obj.update_global(merged_delta)  # type: ignore[attr-defined]
            else:
                update_global_state = getattr(runtime, "update_global_state", None)
                if callable(update_global_state):
                    update_global_state(merged_delta)
        except Exception:
            update_global_state = getattr(runtime, "update_global_state", None)
            if callable(update_global_state):
                update_global_state(merged_delta)

    @staticmethod
    def _ojw_try_commit(runtime: Any) -> None:
        try:
            _, base_runtime = _unwrap_runtime(runtime)
            st_obj2 = base_runtime.state() if base_runtime is not None else runtime.state()  # type: ignore[assignment]
            if hasattr(st_obj2, "commit"):
                st_obj2.commit()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("[workflow_openjiuwen] commit skipped: %s", runtime)

    @staticmethod
    def _ojw_reset_global_state(runtime: Any, initial_state: dict[str, Any]) -> None:
        state_obj = None
        try:
            _, base_runtime = _unwrap_runtime(runtime)
            state_obj = runtime.state()  # type: ignore[assignment]
            if base_runtime:
                state_obj = base_runtime.state()  # type: ignore[assignment]
        except Exception:
            state_obj = None
            logger.debug("[workflow_openjiuwen] reset global state skipped: %s", runtime)

        if state_obj is not None:
            try:
                state_dict = state_obj.get_state() or {}
                resolved_global_state_key = _resolve_ojw_global_state_key()
                state_dict[resolved_global_state_key] = dict(initial_state)
                if hasattr(state_obj, "commit"):
                    state_obj.commit()  # type: ignore[attr-defined]
                return
            except Exception:
                logger.debug("[workflow_openjiuwen] reset global state skipped: %s", state_obj)

        try:
            update_global_state = getattr(runtime, "update_global_state", None)
            if callable(update_global_state):
                update_global_state(dict(initial_state))
                OpenJiuWenWorkflow._ojw_try_commit(runtime)
        except Exception:
            logger.debug("[workflow_openjiuwen] reset global state skipped: %s", runtime)

    @staticmethod
    def _ojw_try_ensure_context_query(state: Any, runtime: Any) -> None:
        if runtime is None or not isinstance(state, dict):
            return
        query_text = str(state.get("user_query") or "").strip()
        if not query_text:
            return
        user_id = str(state.get("user_id") or "").strip()
        session_id = str(state.get("session_id") or "").strip()
        if not user_id or not session_id:
            return
        try:
            run_id = int(state.get("run_id", 0) or 0)
            sub_id = int(state.get("sub_id", 0) or 0)
            options = None
            config_manager = getattr(runtime, "config_manager", None)
            if config_manager is not None:
                options = build_context_init_options(config_manager, workspace=state.get("workspace"))
            call_context = ContextFactory.get_context(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                sub_id=sub_id,
                options=options,
            )
            if not getattr(call_context, "has_initial_pt", False):
                call_context.register_query(query=query_text, additional_files=[])
        except Exception as exc:
            logger.debug(f"[workflow_openjiuwen] ensure context query skipped: {exc}")

    @staticmethod
    def _ojw_try_ensure_planner_user_message(state: Any, runtime: Any, node_name: str) -> None:
        if node_name != "planner" or runtime is None or not isinstance(state, dict):
            return
        query_text = str(state.get("user_query") or "").strip()
        if not query_text:
            return
        state["user_query"] = query_text
        raw_messages = state.get("messages")
        messages = [] if raw_messages is None else list(raw_messages)
        for message in messages:
            content = str(getattr(message, "content", "") or "")
            if query_text and query_text in content:
                return
        from dataagent.utils.messages_utils import build_human_message

        messages.append(build_human_message(prompt_str=query_text))
        state["messages"] = messages

    @staticmethod
    async def _ojw_call_node(node: BaseNode, state_for_node: Any, runtime: Any = None) -> Any:
        # 关键：openjiuwen 侧执行入口需兼容多类节点实现：
        # - 子类直接覆盖 aprocess（async）
        # - 仅覆盖 _aprocess（flex 节点如 Planner/Executor，仍继承 BaseNode.aprocess）
        # - 把 process 实现为 async（langgraph/openjiuwen 都可 await）
        # runtime 一般为 FlexAgent 注入的 DataAgent Runtime；无 set_runtime 时与 set_current_runtime 解包值一致。
        if BaseNode.should_use_async_aprocess(node.__class__):
            return await node.aprocess(state_for_node, runtime)  # type: ignore[arg-type]
        process_func = node.process(state_for_node, runtime)
        if inspect.isawaitable(process_func):
            return await process_func  # type: ignore[misc]
        return process_func

    def set_runtime(self, runtime: Any) -> None:
        """由 FlexAgent 在每轮 chat/astream 前注入，供节点 ``aprocess(state, runtime)``（与 LangGraphWorkflow 对齐）。"""
        self._agent_runtime = runtime

    def astream(
        self,
        initial_state: dict[str, Any],
        runtime: Any | None = None,
        start_at: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        流式调用（DataAgent 侧实现）。

        思路：
        - 节点通过 `get_stream_writer()` -> runtime.write_stream(data) 输出事件（output_msg/break/等）
        - 我们在 workflow 运行期间 monkey-patch runtime.write_stream，把写入旁路到 asyncio.Queue
        - astream() 负责实时消费队列并 yield 事件

        注意：
        - 这不依赖 openjiuwen runner/controller，也不改变节点逻辑
        - 仍会在最后 yield 一次 “updates” 的最终 state（便于上层拿 file_save_path 等）
        """

        async def _gen() -> AsyncIterator[Any]:
            _ = kwargs
            workflow_runtime_cls = _resolve_ojw_workflow_runtime_class()

            rt = runtime or workflow_runtime_cls(workflow_id="dataagent_openjiuwen")
            # 注意：openjiuwen 的节点执行可能发生在不同的 event loop/thread。
            # asyncio.Queue 跨 loop put 会失败（并被我们吞掉），导致前端收不到 output_msg。
            # 这里使用线程安全的 queue.Queue 作为事件缓冲。
            q: queue.Queue[dict[str, Any]] = queue.Queue()

            # 关键：将队列挂在 workflow 实例上（比挂 runtime 更稳），供节点 invoke 注入 contextvar
            self._active_stream_queue = q

            task = asyncio.create_task(self.ainvoke(initial_state, runtime=rt, start_at=start_at))
            final_state: dict[str, Any] | None = None
            try:
                while True:
                    if task.done() and q.empty():
                        break
                    try:
                        item = q.get_nowait()
                        yield ("custom", item)
                    except queue.Empty:
                        logger.debug("queue empty, sleep a bit")
                        await asyncio.sleep(0.05)
                        continue
                final_state = await task
            finally:
                if not task.done():
                    task.cancel()
                # 清理队列引用，避免跨请求串流
                self._active_stream_queue = None

            # 对齐 langgraph：interrupt 走 "updates" 且包含 "__interrupt__"
            if isinstance(final_state, dict) and "__interrupt__" in final_state:
                yield ("updates", final_state)
                return
            # 结束时给一次 updates（上层可选读取，不强依赖）
            if isinstance(final_state, dict):
                yield ("updates", final_state)

        return _gen()

    async def ainvoke(
        self,
        initial_state: dict[str, Any],
        runtime: Any | None = None,
        *,
        start_at: str | None = None,
        # openjiuwen 的 recursion_limit 是“硬上限”，flex 的工具链/LLM 往往需要较多 step 才能 complete；
        # 这里给一个更宽松的默认值，并允许上层显式覆盖。
        recursion_limit: int | None = 200,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """异步执行工作流，处理状态初始化、执行循环及中断检查点保存。"""
        inputs_key, config_key = _resolve_ojw_inputs_and_config_keys()
        workflow_runtime_cls = _resolve_ojw_workflow_runtime_class()

        rt = runtime or workflow_runtime_cls(workflow_id="dataagent_openjiuwen")
        start_node = start_at or self.router.entry_point
        compiled_graph = self._ensure_compiled(rt, start_at=start_node)

        # 构建 openjiuwen config（字典操作，不会抛异常）
        # openjiuwen 的 Pregel 引擎默认 recursion_limit 很小（常见为 1），
        # 对于 flex 这种“actor-loop”会导致还没 complete 就被提前截停。
        # 这里默认提高到与 langgraph 一致的 50，并允许上层覆盖。
        ojw_cfg: dict[str, Any] | None = None
        if recursion_limit is not None:
            # 注意：openjiuwen 在 graph.after_step 里会读取 loop.config['ns'] 做日志；
            # 如果我们传入一个“只有 recursion_limit”的 config，会覆盖掉其默认 config，
            # 导致 KeyError: 'ns'。
            #
            # 因此这里构造一个完整 config：
            # - 尽量从 runtime.context() 读取默认字段
            # - 至少补齐 session_id/ns
            # - 再覆写 recursion_limit
            base_cfg: dict[str, Any] = {}
            ctx_fn = getattr(rt, "context", None)
            if callable(ctx_fn):
                try:
                    raw_ctx = ctx_fn()  # runtime.context() 返回值未必严格是 dict[str, Any]
                    # 统一 key 为 str，避免部分实现返回 bytes 等导致类型/序列化问题
                    base_cfg = {str(k): v for k, v in raw_ctx.items()} if isinstance(raw_ctx, dict) else {}
                except Exception:
                    base_cfg = {}
                    logger.debug("failed to extract runtime context")

            # best-effort：从 initial_state 提取会话 id
            sid = (
                initial_state.get("conversation_id")
                or initial_state.get("session_id")
                or base_cfg.get("session_id")
                or str(uuid.uuid4())
            )
            base_cfg.setdefault("session_id", str(sid))
            base_cfg.setdefault("ns", f"{sid}:{start_node}:1")
            base_cfg["recursion_limit"] = int(recursion_limit)

            # 允许调用方额外透传 openjiuwen config（若有）
            extra_cfg = kwargs.pop("config", None)
            if isinstance(extra_cfg, dict):
                base_cfg.update(extra_cfg)
            ojw_cfg = base_cfg

        initial_state = dict(initial_state or {})
        # 对齐 dataagent_jiuwen：至少保证 messages 存在
        initial_state.setdefault("messages", [])
        # dispatcher/executor/router 会用到，提前补齐避免 KeyError
        initial_state.setdefault("in_progress_tasks", ["<empty_marker>"])
        self._ojw_reset_global_state(rt, initial_state)
        try:
            if hasattr(rt.state(), "commit_user_inputs"):
                rt.state().commit_user_inputs(initial_state)  # type: ignore[attr-defined]
            else:
                # 对齐节点写入方式：优先 runtime.update_global_state
                upd = getattr(rt, "update_global_state", None)
                if callable(upd):
                    upd(initial_state)
                elif hasattr(rt.state(), "update_global"):
                    rt.state().update_global(initial_state)  # type: ignore[attr-defined]
                if hasattr(rt.state(), "commit"):
                    rt.state().commit()  # type: ignore[attr-defined]

            await compiled_graph.invoke({inputs_key: initial_state, config_key: ojw_cfg}, rt)
            return self._finalize_workflow_result(rt)
        except OpenJiuWenInterrupt as intr:
            # 1) snapshot 当前 global_state
            gs = self._snapshot_global_state(rt)
            # 2) 变成可 JSON 化的 state（用于文件持久化）
            serial_state = serialize_state_for_store(gs)
            # 3) 落 checkpoint（使用统一 DATABASE_URL，支持 Postgres/SQLite）
            store = self._get_checkpoint_store()
            checkpoint_id = store.save(
                start_at=intr.node_name,
                interrupt_message=intr.message,
                state=serial_state,
            )
            logger.debug("[workflow_openjiuwen] execution interrupted, checkpoint_id=%s", checkpoint_id)
            # 4) 返回一个“可被上层识别的中断响应”（对齐 langgraph 的 __interrupt__ 语义）
            return {
                "__interrupt__": [{"value": intr.message}],
                "checkpoint_id": checkpoint_id,
                "start_at": intr.node_name,
            }
        except Exception as e:
            # openjiuwen 可能会把 GraphInterrupt / 我们的 OpenJiuWenInterrupt 包装为 JiuWenBaseException，
            # 导致这里拿不到原始中断类型。通过 global_state 的 __last_interrupt__ 与错误文本里的 component 名做兜底识别。
            err_str = str(e)
            gs = self._snapshot_global_state(rt)
            last_intr = ""
            try:
                last_intr = str(gs.get("__last_interrupt__", "") or "")
            except Exception:
                last_intr = ""
            comp = _parse_component_name_from_error(err_str) or start_node

            is_wrapped_interrupt = False
            if last_intr or "Interrupt object" in err_str or "GraphInterrupt" in err_str:
                is_wrapped_interrupt = True

            if is_wrapped_interrupt:
                serial_state = serialize_state_for_store(gs)
                store = self._get_checkpoint_store()
                checkpoint_id = store.save(
                    start_at=str(comp or "human_feedback"),
                    interrupt_message=last_intr or err_str,
                    state=serial_state,
                )
                return {
                    "__interrupt__": [{"value": last_intr or err_str}],
                    "checkpoint_id": checkpoint_id,
                    "start_at": str(comp or "human_feedback"),
                }

            logger.error(f"OpenJiuWen workflow execution failed: {e}\nTraceback: {traceback.format_exc()}")
            raise
        finally:
            with contextlib.suppress(Exception):
                await rt.close()
            if self.workflow is not None:
                with contextlib.suppress(Exception):
                    await _reset_workflow_internal(self.workflow)

    def load_checkpoint_state(self, checkpoint_id: str) -> tuple[str, dict[str, Any]]:
        """
        读取 openjiuwen human_feedback checkpoint：
        - 返回 (start_at, state_dict)
        - state_dict 会反序列化 messages 为 WorkflowState 的 Message 对象（与节点逻辑一致）
        """
        store = self._get_checkpoint_store()
        rec = store.load(checkpoint_id)
        recovered = deserialize_state_from_store(rec.state)
        return rec.start_at or "human_feedback", dict(recovered)

    async def invoke_node_component(
        self,
        node: BaseNode,
        runtime: Any,
        snapshot_global_state: Any,
        merge_delta: Any,
        inputs: Any | None = None,
    ) -> dict[str, Any]:
        """
        执行节点组件逻辑（从 _build_graph 中提取，降低复杂度）。

        Args:
            node: 要执行的节点
            runtime: openjiuwen 运行时
            snapshot_global_state: 快照全局状态的方法
            merge_delta: 合并状态 delta 的方法

        Returns:
            合并后的状态 delta
        """
        _current, base_runtime = _unwrap_runtime(runtime)
        backend_runtime = base_runtime if base_runtime is not None else runtime
        runtime_for_node = self._agent_runtime if self._agent_runtime is not None else backend_runtime
        set_current_runtime(runtime_for_node)
        set_current_backend_runtime(backend_runtime)
        self._ojw_try_set_stream_queue()
        try:
            state_proxy: Any = GlobalStateProxy(runtime)
            if isinstance(inputs, dict) and inputs:
                state_proxy.update(inputs)
            self._ojw_try_ensure_context_query(state_proxy, runtime_for_node)
            self._ojw_try_ensure_planner_user_message(state_proxy, runtime_for_node, node.name)
            if isinstance(inputs, dict):
                # Log only field lengths to keep user input out of logs.
                query = inputs.get("query", None)
                user_query = inputs.get("user_query", None)
                logger.info(
                    "[workflow_openjiuwen] node={} input_keys={} query_len={} user_query_len={} messages_type={}",
                    node.name,
                    sorted(inputs.keys()),
                    len(str(query)) if query is not None else 0,
                    len(str(user_query)) if user_query is not None else 0,
                    type(inputs.get("messages", None)).__name__,
                )
            node_name = node.name
            state_for_node: Any = state_proxy
            # 节点侧需要 DataAgent Runtime（runtime.llm 等）；无注入时回退 openjiuwen 解包结果（非 flex 场景）。
            delta = await self._ojw_execute_node_with_interrupt(node, state_for_node, node_name, runtime_for_node)
            delta = {} if delta is None else delta
            merged_delta = self._ojw_merge_and_commit_delta(runtime, snapshot_global_state, merge_delta, delta)
            return merged_delta
        finally:
            clear_current_backend_runtime()
            clear_current_runtime()
            clear_current_stream_queue()

    def _ojw_try_set_stream_queue(self) -> None:
        try:
            stream_queue = getattr(self, "_active_stream_queue", None)
            if isinstance(stream_queue, queue.Queue):
                set_current_stream_queue(stream_queue)
        except Exception:
            logger.debug("failed to set stream queue")

    async def _ojw_execute_node_with_interrupt(
        self,
        node: BaseNode,
        state_for_node: Any,
        node_name: str,
        runtime: Any | None = None,
    ) -> Any:
        try:
            return await self._ojw_call_node(node, state_for_node, runtime)
        except Exception as e:
            # GraphInterrupt 是正常流程控制（human_feedback 等），封装成 OpenJiuWenInterrupt 以便上层落 checkpoint
            if self._ojw_is_graph_interrupt(e):
                msg = _extract_interrupt_message(e)
                raise OpenJiuWenInterrupt(node_name=node_name, message=msg, raw=e) from e
            logger.exception(f"Error in node {node_name}: {e}")
            raise

    def _ojw_merge_and_commit_delta(
        self,
        runtime: Any,
        snapshot_global_state: Any,
        merge_delta: Any,
        delta: Any,
    ) -> dict[str, Any]:
        # 写回 global_state 供路由/后续节点读取；但返回给 openjiuwen 图的仍应是“当前节点原始 delta”，
        # 避免把整份 merged state 当作节点 outputs 继续传给后继/end，导致与 langgraph 语义偏离。
        current_state = snapshot_global_state(runtime)
        node_delta = dict(delta) if isinstance(delta, dict) else {}
        merged_delta = merge_delta(current_state, node_delta)

        # 某些 openjiuwen wrapper 的 update_global_state 可能只写入"局部视图"；优先写 state.update_global，再 commit。
        self._ojw_try_write_global(runtime, merged_delta)
        self._ojw_try_commit(runtime)
        return node_delta

    def _build_graph(self, *, start_at: str) -> None:
        """构建 openjiuwen 计算图，定义节点封装器与条件路由逻辑。"""
        resolved_workflow_types = _resolve_ojw_workflow_types()
        (
            workflow_component_cls,
            component_executable_cls,
            end_cls,
            workflow_cls,
            end_symbol,
        ) = resolved_workflow_types

        # 在闭包中捕获需要的方法，避免直接访问受保护成员
        snapshot_global_state = self._snapshot_global_state
        merge_delta = self._merge_delta
        self_outer = self

        class _NodeComponent(workflow_component_cls, component_executable_cls):
            def __init__(self, node: BaseNode):
                """初始化节点组件适配器。"""
                super().__init__()
                self._node = node

            async def invoke(self, inputs: Any, runtime: Any, context: Any) -> dict[str, Any]:
                """封装节点执行逻辑，处理上下文注入、机制触发及异常转换。"""
                return await self_outer.invoke_node_component(
                    self._node,
                    runtime,
                    snapshot_global_state,
                    merge_delta,
                    inputs=inputs,
                )

        wf = workflow_cls()
        start_node_component: _NodeComponent | None = None
        for name, node in self.nodes.items():
            node_component = _NodeComponent(node)
            if name == start_at:
                start_node_component = node_component
                continue
            wf.add_workflow_comp(name, node_component)
        if start_node_component is None:
            raise ValueError(f"Start node not found: {start_at}")
        wf.set_start_comp(start_at, start_node_component)
        wf.set_end_comp(self._end_comp_id, end_cls())

        for name in self.nodes:
            route_func = self.router.routing_rules.get(name)
            if route_func is None:
                raise ValueError(f"No routing function found for node: {name}")

            def _make_router(f):
                def _router(*, session):
                    # openjiuwen 0.1.14 对带 session 参数的普通 router 会注入 RouterSession。
                    # 路由阶段不要依赖节点 invoke 时设置的 ContextVar；直接基于 session 读 global_state 才稳定。
                    nxt = f(GlobalStateProxy(session))
                    if nxt in ("__end__", end_symbol):
                        return self._end_comp_id
                    return nxt

                return _router

            wf.add_conditional_connection(name, _make_router(route_func))

        self.workflow = wf

    def _ensure_compiled(self, runtime: Any, *, start_at: str) -> Any:
        """按 start_at 懒构建 Workflow，并基于当前 session runtime 生成 compiled graph。"""
        if start_at not in self._graph_built_for_start:
            self._build_graph(start_at=start_at)
            self._graph_built_for_start.add(start_at)
        assert self.workflow is not None
        return _compile_workflow_internal(self.workflow, runtime)

    def _snapshot_global_state(self, runtime: Any) -> dict[str, Any]:
        """获取当前运行时的全局状态快照（plain dict）。"""
        # 统一复用 core/runtime/context.py 的实现：
        # - base() 为空时会回退到 runtime.state()
        # - 读取 GLOBAL_STATE_KEY
        return get_global_state_snapshot(runtime)

    def _finalize_workflow_result(self, runtime: Any) -> dict[str, Any]:
        """对齐 openjiuwen Workflow.invoke()：返回 end 节点 outputs，并补齐 global_state 中的完整 FlexState 字段。"""
        global_state = self._snapshot_global_state(runtime)
        try:
            state_obj = runtime.state()
            get_outputs = getattr(state_obj, "get_outputs", None)
            if callable(get_outputs):
                outputs = get_outputs(self._end_comp_id)
                if isinstance(outputs, dict):
                    output_payload = outputs.get("output")
                    if isinstance(output_payload, dict):
                        merged = dict(global_state)
                        merged.update(output_payload)
                        return merged
                    merged = dict(global_state)
                    merged.update(outputs)
                    return merged
        except Exception:
            logger.debug("failed to extract structured outputs", exc_info=True)
        return global_state

    def _get_checkpoint_store(self) -> SqliteCheckpointStore:
        """
        openjiuwen checkpoint：SQLite。
        """
        cfg = self.config
        database_url: str | None = None
        table = "dataagent_checkpoints"
        try:
            if cfg is not None:
                val = getattr(cfg, "get", None)
                if callable(val):
                    database_url = cfg.get("DATABASE_URL", None)
                    table = str(cfg.get("AGENT_CONFIG.checkpoint_postgres_table", table) or table)
        except Exception:
            database_url = None
            logger.debug("failed to extract checkpoint config from workflow config", exc_info=True)
        if not database_url:
            raise ValueError("openjiuwen checkpoint 未配置数据库连接。请配置 DATABASE_URL。")

        url_obj: Any = make_url(str(database_url))
        # sqlite:///./x.db -> ./x.db ; sqlite:////abs/x.db -> /abs/x.db ; sqlite:///:memory: -> :memory:
        sqlite_path = str(getattr(url_obj, "database", "") or "").strip() or ":memory:"
        return SqliteCheckpointStore(sqlite_path, table_name=table)

    def _merge_delta(self, current_state: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        """
        openjiuwen 下的 state 合并策略（最小但正确）：
        - messages：始终按“追加”语义合并（兼容单条 message 或 list[message]）
        - Annotated reducer：按 reducer(current, delta) 聚合（如 operator.add）
        - 其他字段：默认覆盖
        """

        def _msg_sig(m: Any) -> tuple[Any, ...]:
            """生成 message 的轻量签名，用于判断“是否为同一条消息”（避免对象 identity 不一致）。"""
            if isinstance(m, dict):
                typ = m.get("type") or m.get("role") or "dict"
                content = m.get("content")
                name = m.get("name") or (m.get("additional_kwargs", {}) or {}).get("name")
                tool_call_id = m.get("tool_call_id") or m.get("id")
                source = m.get("source")
                return (str(typ), str(content), str(name), str(tool_call_id), str(source))
            cls = getattr(getattr(m, "__class__", None), "__name__", "obj")
            content = getattr(m, "content", None)
            name = getattr(m, "name", None)
            tool_call_id = getattr(m, "tool_call_id", None)
            source = getattr(m, "source", None)
            return (str(cls), str(content), str(name), str(tool_call_id), str(source))

        def _is_prefix(old_list: list[Any], new_list: list[Any]) -> bool:
            """判断 old_list 是否为 new_list 的前缀（基于 message 签名）。"""
            if not old_list:
                return True
            if len(new_list) < len(old_list):
                return False
            try:
                return [_msg_sig(x) for x in new_list[: len(old_list)]] == [_msg_sig(x) for x in old_list]
            except Exception:
                logger.debug("failed to _is_prefix on non-message list")
                return False

        def _is_remove_all_message(message: Any) -> bool:
            """识别 LangGraph add_messages 使用的清空消息指令。"""
            cls_name = getattr(getattr(message, "__class__", None), "__name__", "")
            return cls_name == "RemoveMessage" and getattr(message, "id", None) == "__remove_all__"

        merged: dict[str, Any] = {}
        for k, v in (delta or {}).items():
            if k == "messages":
                old = current_state.get("messages", [])
                if old is None:
                    old_list: list[Any] = []
                elif isinstance(old, list):
                    old_list = old
                else:
                    old_list = [old]

                if v is None:
                    new_list: list[Any] = []
                elif isinstance(v, list):
                    new_list = v
                else:
                    new_list = [v]

                if any(_is_remove_all_message(message) for message in new_list):
                    messages: list[Any] = [*old_list]
                    for message in new_list:
                        if _is_remove_all_message(message):
                            messages = []
                            continue
                        messages.append(message)
                    merged[k] = messages
                    continue

                # 兼容两种上游节点实现：
                # 1) 正确 delta：只返回“新增消息” -> 追加
                # 2) 旧实现：返回“全量 messages”（state["messages"] + [new]） -> 覆盖，避免重复追加导致上下文膨胀/死循环
                if isinstance(v, list) and _is_prefix(old_list, new_list):
                    merged[k] = new_list
                else:
                    merged[k] = [*old_list, *new_list]
                continue

            reducer = self._reducers.get(k)
            if callable(reducer):
                cur = current_state.get(k)
                # 常见数值累加场景：None 视为 0
                if cur is None:
                    cur = 0
                try:
                    merged[k] = reducer(cur, v)
                except Exception:
                    # reducer 失败则兜底覆盖，避免把整个节点执行打崩
                    logger.debug(f"failed to reduce state field {k}", exc_info=True)
                    merged[k] = v
                continue

            merged[k] = v
        return merged
        # openjiuwen 在构造 Graph/Vertex 时可能创建 asyncio.Future，
        # 在没有 event loop 的同步初始化阶段会报错（例如 DataAgent lazy init / import 阶段）。
        # 统一改为在 ainvoke/astream 的 async 上下文里通过 _ensure_compiled 懒构图+compile。


class OpenJiuWenInterrupt(Exception):
    """
    openjiuwen GraphInterrupt 的轻量封装，携带触发中断的节点名与中断消息。
    """

    def __init__(self, *, node_name: str, message: str, raw: Any | None = None):
        """初始化中断异常，记录触发节点、消息及原始异常对象。"""
        super().__init__(message)
        self.node_name = node_name
        self.message = message
        self.raw = raw


def _unwrap_runtime(runtime: Any) -> tuple[Any, Any | None]:
    """
    openjiuwen 的 runtime 在不同阶段可能被 RouterRuntime/WrappedNodeRuntime 包一层，
    某些 wrapper 的 base() 会返回 None。

    返回：
    - 解包后的 runtime（尽量为最内层）
    - base_runtime（可为 None）
    """
    seen: set[int] = set()
    current_runtime = runtime
    while current_runtime is not None and id(current_runtime) not in seen:
        seen.add(id(current_runtime))
        base_fn = getattr(current_runtime, "base", None)
        if callable(base_fn):
            try:
                base_runtime = base_fn()
            except Exception:
                base_runtime = None
                logger.debug("failed to unwrap runtime", exc_info=True)
            if base_runtime is not None:
                return current_runtime, base_runtime

        # 常见 wrapper 字段：_runtime / runtime
        next_runtime = getattr(current_runtime, "_runtime", None) or getattr(current_runtime, "runtime", None)
        if next_runtime is not None and next_runtime is not current_runtime:
            current_runtime = next_runtime
            continue
        break
    return current_runtime, None


def _extract_interrupt_message(e: Exception) -> str:
    """
    尽量从 openjiuwen GraphInterrupt 中提取原始 message；提取失败则 fallback 到 str(e)。
    """
    # 常见形态：GraphInterrupt(Interrupt(msg))，Interrupt.value 为 msg
    try:
        # openjiuwen / langgraph 都可能有 .interrupts
        interrupts = getattr(e, "interrupts", None)
        if interrupts and isinstance(interrupts, list) and interrupts:
            first = interrupts[0]
            val = getattr(first, "value", None)
            if val is not None:
                return str(val)
    except Exception:
        logger.debug("failed to extract interrupt message from e.interrupts", exc_info=True)
    try:
        if e.args:
            first = e.args[0]
            val = getattr(first, "value", None)
            if val is not None:
                return str(val)
    except Exception:
        logger.debug("failed to extract interrupt message from e.args[0].value", exc_info=True)
    return str(e)


def _parse_component_name_from_error(err: str) -> str | None:
    """
    从 openjiuwen 的包装异常文本中解析 component 名：
    形如：component [human_feedback] encountered ...
    """
    try:
        m = re.search(r"component\s+\[([^\]]+)\]", err)
        if m:
            return m.group(1)
    except Exception:
        logger.debug("failed to parse component name from error", exc_info=True)
    return None
