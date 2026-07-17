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

import json
import os
import traceback
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.actions.environment import from_config as env_from_config
from dataagent.actions.tools.local_tool.sandbox import (
    SandboxPolicy,
    build_workspace_mount_lists,
    create_sandbox,
)
from dataagent.config.config_manager import ConfigManager, build_prompt_append
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import ContextFactory, build_context_init_options
from dataagent.core.flex.flex_runtime_from_config import build_agent_env_from_flex_config
from dataagent.core.flex.hooks.agent_turn import session_history_restore
from dataagent.core.flex.hooks.cross_session_recall import cross_session_recall
from dataagent.core.flex.hooks.registry import resolve_builtin_hook
from dataagent.core.flex.workflow.router import FlexRouter, LimitReachedError
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.framework_adapters.runtime.workflow_backend_factory import create_workflow_backend
from dataagent.core.suite.allow_paths import effective_workspace_allow_paths
from dataagent.core.utils.performance import callable_perf_name, get_current_collector, make_perf_state_holder
from dataagent.utils.cli.rich_renderer import RICH_AVAILABLE, StreamRenderer, reset_active_renderer, set_active_renderer
from dataagent.utils.env_utils import get_env_bool
from dataagent.utils.import_utils import import_class


class FlexAgent(BaseAgent):
    """
    ReAct-like Agent with customizable workflow stages.

    Supports three workflow stages:
    - Pre-workflow: Optional preprocessing nodes
    - Actor-workflow: Main actor nodes (loops until complete)
    - Post-workflow: Optional postprocessing nodes
    """

    def __init__(
        self,
        actor_nodes: list[BaseNode],
        pre_nodes: list[BaseNode] | None = None,
        post_nodes: list[BaseNode] | None = None,
        *,
        backend: str = "langgraph",
        mode: str | None = None,
        debug: bool | None = False,
        config: dict[str, Any] | None = None,
        gym_env: Any = None,
        config_manager: ConfigManager | None = None,
    ):
        """
        Initialize FlexAgent.

        Args:
            actor_nodes: List of actor nodes (required, must not be empty)
            pre_nodes: List of preprocessing nodes (optional)
            post_nodes: List of postprocessing nodes (optional)
            mode: SCENARIO key for instructions lookup (default "chat") (optional)
            debug: debug mode (optional)
            config: Configuration dictionary (optional)
            config_manager: Per-Agent ConfigManager (same instance as DataAgent.config when applicable).

        Raises:
            ValueError: If actor_nodes is empty
        """
        if not actor_nodes:
            raise ValueError("Must provide at least one actor node")

        super().__init__(config=config)
        self.actor_nodes = actor_nodes
        self.pre_nodes = pre_nodes or []
        self.post_nodes = post_nodes or []
        self.backend = (backend or "langgraph").lower()
        self.mode = mode or "chat"
        self.debug = debug

        # Extract node names for router
        actor_node_names = [node.name for node in actor_nodes]
        pre_node_names = [node.name for node in self.pre_nodes]
        post_node_names = [node.name for node in self.post_nodes]

        # Combine all nodes for workflow
        all_nodes = self.pre_nodes + self.actor_nodes + self.post_nodes

        # AgentEnv（YAML → 不可变配置）：agent 生命周期内共享，不随 chat() 调用变化
        # Runtime 由每次 chat()/astream() 入口新建，引用此 env
        self.config_manager = config_manager
        self.env_config = build_agent_env_from_flex_config(
            config or {}, mode=self.mode, gym_env=gym_env, config_manager=config_manager
        )
        self.router = FlexRouter(
            actor_nodes=actor_node_names,
            pre_nodes=pre_node_names if pre_node_names else None,
            post_nodes=post_node_names if post_node_names else None,
            max_iter=self.env_config.max_iter,
            token_limit=getattr(self.env_config, "token_limit", None),
        )

        # If HITL is enabled, create HumanFeedbackNode
        enable_hitl = (config or {}).get("AGENT_CONFIG", {}).get("enable_human_feedback", False)
        if enable_hitl is True:
            from dataagent.core.flex.nodes.human_feedback import HumanFeedbackNode

            hitl_node = HumanFeedbackNode(name="human_feedback")
            all_nodes.append(hitl_node)
            logger.trace("[FlexAgent] HITL 功能已启用，创建 human_feedback 节点")
            # 注意：request_human_feedback 工具已经在 DataAgent.global_init() 中注册过了

        # state_class 在 langgraph 下用于 StateGraph schema；
        # 在 openjiuwen 下仅用于解析 Annotated reducer（messages/add 等），不影响执行引擎。
        state_class = FlexState
        self.workflow_backend = create_workflow_backend(
            backend=self.backend,
            nodes=all_nodes,
            router=self.router,
            state_class=state_class,
            config=config,
        )

        agent_info = self.get_agent_info()
        logger.trace(f"FlexAgent initialized: {json.dumps(agent_info, indent=2)}")

        # 所有节点的快捷索引（供 hook 注册时按名查找）
        self._nodes: dict[str, BaseNode] = {node.name: node for node in all_nodes}

        # 在 YAML HOOKS 之前运行的内置 agent pre 链（可单测替换 ``_builtin_agent_pre_hooks``）
        self._builtin_agent_pre_hooks: list[Any] = []

        # 从 config 的 HOOKS 段挂载内置 hook（见 registry；memory/metadata 亦通过 YAML 声明）
        self._register_hooks_from_config(config or {})

    @staticmethod
    def import_hook_from_suite_root(
        relative_spec: str,
        *,
        root: Path,
        suite_name: str,
        location: str,
    ) -> Any:
        """Import a hook callable relative to a Suite root directory."""
        from dataagent.utils.import_utils import import_callable_from_suite_root

        try:
            return import_callable_from_suite_root(
                relative_spec,
                root=root,
                suite_name=suite_name,
            )
        except Exception as exc:
            raise ValueError(f"{location}: failed to import Suite hook {relative_spec!r} from {root}: {exc}") from exc

    @staticmethod
    def _create_nodes_from_config(
        nodes_config: list[dict[str, Any]],
        env: Env | None = None,
    ) -> list[BaseNode]:
        """
        Create nodes from configuration list.

        Args:
            nodes_config: List of node configurations, each containing:
                - node: Node identifier/name
                - module: Python module path (e.g., "dataagent.core.flex.nodes.planner.Planner")
                - chat_model: Optional chat model config
                - prompt_template: Optional prompt append config (per message_type 支持单条
                  ``path``/``content`` 或 spec 列表；不配则节点走 ``PromptTemplate.from_package_relative`` 缺省回落)
                - Other node-specific parameters (merged into constructor kwargs)
            env: Shared environment for nodes that accept ``env``.

        Returns:
            List of instantiated BaseNode instances
        """
        nodes = []

        reserved_keys = {"node", "module", "chat_model", "prompt_template"}

        for node_config in nodes_config:
            node_identifier = node_config.get("node")
            if not node_identifier:
                raise ValueError("Node configuration missing required 'node' field")

            module_path = node_config.get("module")
            if not module_path:
                raise ValueError(f"Node '{node_identifier}' missing required 'module' field")

            # Dynamically import the node class using import_utils
            try:
                node_class: type[BaseNode] = import_class(module_path)
            except (ValueError, ImportError, AttributeError, TypeError) as e:
                raise type(e)(f"Failed to import node class for '{node_identifier}': {e}") from e

            node_kwargs = {
                "name": node_identifier,
                "env": env,
            }

            # Handle LLM config
            chat_model_config = node_config.get("chat_model", {})
            if chat_model_config:
                node_kwargs["chat_model"] = chat_model_config.get("name")

            prompt_template_config = node_config.get("prompt_template", {})
            if prompt_template_config:
                prompt_appends = {}
                for mt in ("system", "user"):
                    spec = prompt_template_config.get(mt)
                    if spec:
                        prompt_appends[mt] = build_prompt_append(spec)
                if prompt_appends:
                    node_kwargs["prompt_appends"] = prompt_appends
            for key, value in node_config.items():
                if key not in reserved_keys and key not in node_kwargs:
                    node_kwargs[key] = value
            # Instantiate node
            try:
                node = node_class(**node_kwargs)
                nodes.append(node)
                logger.trace(f"Created node: {node.name} from {module_path}")
            except Exception as e:
                raise RuntimeError(f"Failed to instantiate node '{node_identifier}' from {module_path}: {e}") from e

        return nodes

    @staticmethod
    def _is_interrupt_stream_item(item: Any, *, log_parse_error: bool = False) -> bool:
        """
        识别中断事件：("updates", {"__interrupt__": ...}) 或 (xxx, "updates", {"__interrupt__": ...})
        """
        try:
            if not isinstance(item, tuple):
                return False
            if len(item) == 3:
                _, stream_mode, event = item
            elif len(item) == 2:
                stream_mode, event = item
            else:
                raise ValueError(f"Unexpected stream item tuple length: {len(item)}")
            return stream_mode == "updates" and isinstance(event, dict) and "__interrupt__" in event
        except (TypeError, ValueError, KeyError) as e:
            if log_parse_error:
                logger.debug(f"Failed to parse stream item for interrupt detection: {e}")
            return False
        except Exception:
            return False

    @staticmethod
    def _resolve_config_manager(
        runtime: Any = None, *, agent_config_manager: ConfigManager | None = None
    ) -> ConfigManager | None:
        """
        Resolve per-call ConfigManager, aligned with planner/executor.

        Prefer ``runtime.config_manager`` when a Runtime is available; otherwise fall back
        to the FlexAgent's ``config_manager``.
        """
        if runtime is not None:
            cm = getattr(runtime, "config_manager", None)
            if cm is not None:
                return cm
        return agent_config_manager

    @staticmethod
    def _import_hook_from_suite_root(
        relative_spec: str,
        *,
        root: Path,
        suite_name: str,
        location: str,
    ) -> Any:
        """Backward-compatible alias for :meth:`import_hook_from_suite_root`."""
        return FlexAgent.import_hook_from_suite_root(
            relative_spec,
            root=root,
            suite_name=suite_name,
            location=location,
        )

    @classmethod
    def from_config(cls, config: dict[str, Any], config_manager: ConfigManager | None = None) -> "FlexAgent":
        """
        Create FlexAgent from configuration dictionary.

        Args:
            config: Configuration dictionary with structure:
                - AGENT_CONFIG: Agent configuration
                - SCENARIO: Per-scenario instructions (keyed by mode, default chat)
                - mode: Optional top-level SCENARIO key (default chat)
                - PRE_WORKFLOW: List of node configs
                - ACTOR_LOOP: List of node configs (required)
                - POST_WORKFLOW: List of node configs
            config_manager: Per-Agent ConfigManager for runtime/tool configuration access.

        Returns:
            FlexAgent instance

        Raises:
            ValueError: If ACTOR_LOOP is empty or missing
        """
        # Extract agent config and mode
        agent_config = config.get("AGENT_CONFIG", {})
        debug = (
            get_env_bool("AGENT_CONFIG_DEBUG")
            if "AGENT_CONFIG_DEBUG" in os.environ
            else agent_config.get("debug", False)
        )
        backend = agent_config.get("backend", "langgraph")
        mode = config.get("mode")

        # Initialize environment
        if config and config.get("ENV"):
            try:
                env = env_from_config(config.get("ENV"), config_manager=config_manager)
                env.init()
            except Exception as e:
                logger.error(f"Failed to initialize environment: {e}")
                raise e
        else:
            env = None

        # Parse workflow stages
        pre_nodes = cls._create_nodes_from_config(config.get("PRE_WORKFLOW", []), env)
        actor_nodes = cls._create_nodes_from_config(config.get("ACTOR_LOOP", []), env)
        post_nodes = cls._create_nodes_from_config(config.get("POST_WORKFLOW", []), env)

        return cls(
            actor_nodes=actor_nodes,
            pre_nodes=pre_nodes if pre_nodes else None,
            post_nodes=post_nodes if post_nodes else None,
            backend=str(backend),
            mode=mode,
            config=config,
            debug=debug,
            gym_env=env,
            config_manager=config_manager,
        )

    def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator:
        """
        兼容 server 侧 langgraph 原生调用：
        - astream(input=..., config=..., stream_mode=..., checkpointer=...)
        openjiuwen 侧保持 workflow.astream 的参数协议。
        """
        if "input" in kwargs:
            return self._astream_langgraph(**kwargs)
        return self._astream_openjiuwen(*args, **kwargs)

    async def chat(
        self,
        message: str,
        initial_state: dict[str, Any],
        **kwargs,
    ) -> dict[str, Any]:
        """与 agent 进行一轮对话；性能数据只通过 ``.performance/*.jsonl`` 落盘与日志查看。"""
        initial_state["user_query"] = message
        initial_state["raw_user_query"] = message

        # 🆕 注入 HITL 配置到 initial_state（从 self.config 读取）
        agent_config = (self.config or {}).get("AGENT_CONFIG", {})
        enable_hitl = agent_config.get("enable_human_feedback", False)
        # chat 接口下 terminal_mode 默认 True
        terminal_mode = agent_config.get("terminal_mode", True)

        initial_state.setdefault("enable_human_feedback", enable_hitl)
        initial_state.setdefault("terminal_mode", terminal_mode)
        initial_state.setdefault(
            "enable_portrait",
            bool(agent_config.get("enable_portrait", False)),
        )

        # 对齐 _get_or_init_context 的兜底逻辑：会话持久化需要非空的 user_id / session_id
        initial_state.setdefault("user_id", self.config.get("USER_ID", "anonymous"))
        initial_state.setdefault("session_id", self.config.get("SESSION_ID", "default_session"))

        logger.debug(f"[FlexAgent] chat() 注入 HITL 配置: enable={enable_hitl}, terminal_mode={terminal_mode}")

        # 每次 chat() 新建 Runtime（per-call），引用共享的不可变 env_config
        runtime = self._create_call_runtime()
        if str(message or "").strip():
            runtime.reset_flex_planner_user_sync()
        runtime.update_from_state(initial_state)
        self._bind_workflow_runtime(runtime)
        self._refresh_workspace_runtime_context(initial_state, runtime)
        logger.trace(f"[FlexAgent] runtime updated: workspace={runtime.workspace_dir}, hierarchy={runtime.hierarchy}")

        call_context = self._get_or_init_context(initial_state, runtime)
        if call_context:
            try:
                # Register query node once per run (per context)
                if not getattr(call_context, "has_initial_pt", False):
                    call_context.register_query(query=message, additional_files=[])
            except Exception as e:
                logger.debug(f"Context registration skipped: {e}")

        latest: dict[str, Any] = {"state": initial_state}
        with self._performance_run(
            state=initial_state,
            backend=getattr(self, "backend", None),
            flush_state_provider=lambda: latest["state"],
        ):
            initial_state = self._run_agent_pre_hooks(initial_state, runtime)
            latest["state"] = initial_state
            final_state: dict[str, Any] = {}
            try:
                if self.debug and RICH_AVAILABLE:
                    renderer = StreamRenderer()
                    renderer.start(initial_node="planner")
                    renderer_token = set_active_renderer(renderer)
                    runtime.on_subagent_progress = renderer.update_subagent_hint
                    try:
                        stream = self.workflow_backend.astream(initial_state, stream_mode=["values", "custom"])
                        async for chunk in stream:
                            mode, data = chunk
                            if mode == "custom" and isinstance(data, dict):
                                renderer.handle_event(data)
                            elif mode in ("values", "updates") and isinstance(data, dict):
                                final_state = data
                    finally:
                        reset_active_renderer(renderer_token)
                        renderer.stop()

                    if not final_state or not isinstance(final_state, dict) or not final_state.get("messages"):
                        logger.warning("No valid values event in stream, falling back to ainvoke")
                        final_state = await self.workflow_backend.ainvoke(initial_state)

                    logger.trace("Chat completed (debug mode with streaming)")
                else:
                    final_state = await self.workflow_backend.ainvoke(initial_state)
                    logger.trace(f"Chat completed ({self.mode} mode)")

                final_state = self._run_agent_post_hooks(final_state, runtime)

                if call_context:
                    try:
                        await call_context.wait_pending_tasks()
                        call_context.persist_to_json()
                        call_context.show()
                    except Exception as e:
                        logger.warning(f"Failed to persist context after chat completion: {e}")

                latest["state"] = final_state
                return final_state

            except LimitReachedError as e:
                state = dict(e.state) if getattr(e, "state", None) else {}
                messages = list(state.get("messages", []))
                messages.append(
                    AIMessage(
                        content="已达运行上限，对话结束。",
                        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    )
                )
                state["messages"] = messages
                state["complete"] = True
                logger.warning(f"Limit reached: {e}")
                if call_context:
                    try:
                        await call_context.wait_pending_tasks()
                        call_context.persist_to_json()
                        call_context.show()
                    except Exception as pe:
                        logger.warning(f"Failed to persist context after limit reached: {pe}")
                latest["state"] = state
                return state

            except Exception as e:
                if call_context:
                    try:
                        await call_context.wait_pending_tasks()
                        call_context.persist_to_json()
                        call_context.show()
                    except Exception as persist_error:
                        logger.warning(f"Failed to persist context after chat error: {persist_error}")
                logger.error(f"Chat execution failed: {e}\nTraceback: {traceback.format_exc()}")
                raise RuntimeError(f"Chat failed: {e}") from e

    def get_agent_info(self) -> dict[str, Any]:
        """
        Get information about the agent.

        Returns:
            Dictionary with agent structure information
        """
        return {
            "type": "FlexAgent",
            "backend": self.backend,
            "nodes": {
                "pre_workflow": [node.name for node in self.pre_nodes],
                "actor_loop": [node.name for node in self.actor_nodes],
                "post_workflow": [node.name for node in self.post_nodes],
            },
            "entry_point": self.router.entry_point,
        }

    def update_config(self, new_config: dict[str, Any]):
        """hot reconfiguration. Only supports Env updates for now"""

        env_config = new_config.get("ENV")
        if env_config is None and env_config == self.config.get("ENV"):
            return

        self.config.update({"ENV": env_config})

        env = env_from_config(env_config, config_manager=self.config_manager)
        env.init()
        self.env = env

        for node in self.pre_nodes + self.actor_nodes + self.post_nodes:
            node.reconfig(env=env)

    async def _astream_langgraph(self, **kwargs: Any) -> AsyncGenerator[Any, None]:
        initial_state_for_persist: dict[str, Any] | None = None
        kw = dict(kwargs)
        initial_state_arg: dict[str, Any] = {}
        if "initial_state" in kw:
            cand = kw.pop("initial_state")
            if isinstance(cand, dict):
                initial_state_arg = cand

        input_val = kw.get("input")
        # 若未提供 input，但提供了 initial_state，则 backend 会将 initial_state 作为 input 使用；
        # 此时 Context 初始化也应基于该 state。
        context_state = input_val if isinstance(input_val, dict) else initial_state_arg

        # Per-call runtime for astream (same pattern as chat())
        runtime = self._create_call_runtime()

        if isinstance(context_state, dict) and context_state:
            if "user_query" in context_state:
                context_state["raw_user_query"] = str(context_state.get("user_query") or "")
            initial_state_for_persist = context_state
            self._prepare_context_for_langgraph_stream(context_state, runtime)
            if str(context_state.get("user_query") or "").strip():
                runtime.reset_flex_planner_user_sync()

        langgraph_config = kw.get("config") if isinstance(kw.get("config"), dict) else None
        langgraph_checkpointer = kw.get("checkpointer")
        langgraph_store = kw.get("store")
        latest, flush_state_provider = make_perf_state_holder(context_state)
        with self._performance_run(
            state=context_state if isinstance(context_state, dict) else None,
            backend=getattr(self, "backend", None),
            flush_state_provider=flush_state_provider,
        ):
            context_state = self._run_agent_pre_hooks(context_state, runtime)
            initial_state_for_persist = context_state
            latest["state"] = context_state
            if isinstance(input_val, dict):
                kw["input"] = context_state
            else:
                initial_state_arg = dict(context_state)
            stream = self.workflow_backend.astream(initial_state_arg, **kw)
            async for item in self._stream_with_finalization(
                stream,
                initial_state_for_persist=initial_state_for_persist,
                runtime=runtime,
                log_parse_error=False,
                langgraph_config=langgraph_config,
                langgraph_checkpointer=langgraph_checkpointer,
                langgraph_store=langgraph_store,
                latest_state=latest,
            ):
                yield item

    async def _astream_openjiuwen(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        """_astream_openjiuwen"""
        initial_state = kwargs.pop("initial_state", None)
        start_at = kwargs.pop("start_at", None)
        checkpoint_id = kwargs.pop("checkpoint_id", None)
        message = kwargs.pop("message", None)

        if args and isinstance(args[0], dict) and initial_state is None:
            initial_state = args[0]
        if not isinstance(initial_state, dict):
            initial_state = {}

        initial_state_for_persist: dict[str, Any] | None = initial_state

        # Per-call runtime for astream (same pattern as chat())
        runtime = self._create_call_runtime()

        if checkpoint_id:
            stream = self.workflow_backend.astream_resume(
                checkpoint_id=str(checkpoint_id),
                message=str(message or ""),
                session_id=None,
                **kwargs,
            )
            latest, flush_state_provider = make_perf_state_holder(initial_state)
            with self._performance_run(
                state=initial_state,
                backend=getattr(self, "backend", None),
                flush_state_provider=flush_state_provider,
            ):
                async for item in self._stream_with_finalization(
                    stream,
                    initial_state_for_persist=initial_state_for_persist,
                    runtime=runtime,
                    log_parse_error=True,
                    latest_state=latest,
                ):
                    yield item
            return

        # OpenJiuWen 分支：对齐 LangGraph 行为，补齐 Context + initial_pt
        if initial_state:
            if "user_query" in initial_state:
                initial_state["raw_user_query"] = str(initial_state.get("user_query") or "")
            try:
                self._refresh_workspace_runtime_context(initial_state, runtime)
                self._ensure_context_with_query(initial_state, runtime)
            except Exception as e:  # pragma: no cover - 仅兜底日志
                logger.debug(f"Context initialization skipped in astream (openjiuwen): {e}")
            if str(initial_state.get("user_query") or "").strip():
                runtime.reset_flex_planner_user_sync()

        latest, flush_state_provider = make_perf_state_holder(initial_state)
        with self._performance_run(
            state=initial_state,
            backend=getattr(self, "backend", None),
            flush_state_provider=flush_state_provider,
        ):
            initial_state = self._run_agent_pre_hooks(initial_state, runtime)
            initial_state = dict(initial_state)
            initial_state_for_persist = initial_state
            latest["state"] = initial_state
            self._bind_workflow_runtime(runtime)
            stream = self.workflow_backend.astream(initial_state, start_at=start_at, **kwargs)
            async for item in self._stream_with_finalization(
                stream,
                initial_state_for_persist=initial_state_for_persist,
                runtime=runtime,
                log_parse_error=True,
                latest_state=latest,
            ):
                yield item

    def _create_call_runtime(self) -> "Runtime":
        """Create a fresh Runtime for a single chat()/astream() call.

        The Runtime wraps the shared, immutable ``self.env_config`` and carries
        all per-call mutable state (workspace, sandbox, caches, callbacks).
        """
        return Runtime(self.env_config)

    def _bind_workflow_runtime(self, runtime: Any) -> None:
        """Publish per-call Runtime and merged config for node/router layout resolution."""
        self.workflow_backend.set_runtime(runtime)
        config_manager = getattr(self, "config_manager", None)
        merged = config_manager.get_all() if config_manager is not None else (self.config or {})
        router = getattr(self, "router", None)
        if router is not None:
            router.set_merged_config(merged if isinstance(merged, Mapping) else None)

    def _ensure_context_with_query(self, state: dict[str, Any], runtime: Any = None) -> None:
        """
        确保 Context 已初始化且包含首个 Query 节点。

        Planner 用户模板会从 trajectory 的 ``initial_pt`` 对应节点读取首轮用户问题（与 ``state['user_query']`` 对齐注册）。

        - 同步 workspace 到 runtime，设置 ContextVar
        - 根据 user_id/session_id/run_id/sub_id 获取或创建 Context
        - 若尚未注册 initial_pt，则用 state['user_query'] 注册一个 Query 节点
        """
        try:
            if runtime is not None:
                runtime.update_from_state(state)
                self._bind_workflow_runtime(runtime)
        except Exception as e:
            logger.debug(f"Runtime sync skipped in _ensure_context_with_query: {e}")

        try:
            call_context = self._get_or_init_context(state, runtime)
        except Exception as e:  # pragma: no cover - 仅兜底日志，不影响主流程
            logger.debug(f"Context initialization failed in _ensure_context_with_query: {e}")
            return

        if not call_context:
            return

        # 注意：不再将 Context 塞入 state，避免 LangGraph checkpoint 序列化失败

        # 已经有 initial_pt/has_initial_pt 的情况直接跳过，避免重复建点
        if getattr(call_context, "has_initial_pt", False):
            return

        query_text = str(state.get("user_query", "") or "")
        if not query_text:
            # 没有明确的 user_query，就不要强行建 Query 节点，避免语义混乱
            logger.debug("No user_query in state; skip registering initial Query node.")
            return

        try:
            call_context.register_query(query=query_text, additional_files=[])
        except Exception as e:  # pragma: no cover - 仅兜底日志
            logger.debug(f"register_query failed in _ensure_context_with_query: {e}")

    def _run_agent_pre_hooks(self, state: dict[str, Any], runtime: Any) -> dict[str, Any]:
        """运行内置及配置的 agent pre-hooks，并记录各 hook 的性能事件。"""
        current_state = self._run_builtin_agent_pre_hooks(state, runtime)
        collector = get_current_collector()
        for hook in self._pre_hooks:
            with collector.measure(
                "hook",
                callable_perf_name(hook),
                hook_scope="agent",
                hook_phase="pre",
            ):
                current_state = hook(current_state, runtime)  # type: ignore[assignment]
        return current_state

    def _run_agent_post_hooks(self, state: dict[str, Any], runtime: Any) -> dict[str, Any]:
        """执行 agent 级 post-hooks，与 ``chat()`` 收尾一致。"""
        final_state = dict(state)
        collector = get_current_collector()
        for hook in self._post_hooks:
            with collector.measure(
                "hook",
                callable_perf_name(hook),
                hook_scope="agent",
                hook_phase="post",
            ):
                final_state = hook(final_state, runtime)  # type: ignore[assignment]
        return dict(final_state)

    async def _read_langgraph_final_state_after_stream(
        self,
        *,
        stream_final_state: dict[str, Any],
        langgraph_config: dict[str, Any] | None,
        langgraph_checkpointer: Any,
        langgraph_store: Any = None,
    ) -> dict[str, Any]:
        """astream 结束后解析图终态：有 checkpointer 时优先读取，否则用 stream 收集到的 values。"""
        aget_fn = getattr(self.workflow_backend, "aget_graph_state", None)
        if langgraph_config and langgraph_checkpointer is not None and callable(aget_fn):
            try:
                resolved = await aget_fn(
                    config=langgraph_config,
                    checkpointer=langgraph_checkpointer,
                    store=langgraph_store,
                )
                if resolved:
                    logger.debug(
                        "[FlexAgent] astream final state from checkpointer, messages={}",
                        len(resolved.get("messages") or []),
                    )
                    return resolved
            except Exception as e:
                logger.warning(f"[FlexAgent] aget_graph_state failed: {e}")
        return dict(stream_final_state) if stream_final_state else {}

    async def _finalize_context_after_stream(
        self,
        initial_state: dict[str, Any] | None,
        interrupted: bool,
        *,
        final_state: dict[str, Any] | None = None,
        runtime: Any = None,
    ) -> None:
        """
        在 streaming 结束后统一做 Context 的持久化。

        约定：
        - 对于”中断轮次”（存在 __interrupt__）：仅做一次 JSON/meta 快照，用于后续跨 worker/重启恢复；
          不写 PG，避免将”半截轨迹”当作最终版本。
        - 对于”完整结束轮次”（无 __interrupt__）：与 chat() 对齐，做一次 profiling + PG + JSON/meta 持久化。
        """
        # 通过 state 中的 ID 重新获取 Context（ContextFactory.get_context 是幂等的）
        ctx = None
        if isinstance(initial_state, dict):
            uid = str(initial_state.get("user_id", self.config.get("USER_ID", "anonymous")))
            sid = str(initial_state.get("session_id", self.config.get("SESSION_ID", "default_session")))
            rid = int(initial_state.get("run_id", self.config.get("RUN_ID", 0)))
            subid = int(initial_state.get("sub_id", self.config.get("SUB_ID", 0)))
            ctx = ContextFactory.get_context(uid, sid, rid, subid)
        if ctx is None:
            return

        # 中断轮次：只用于 checkpoint + 快照恢复。
        if interrupted:
            try:
                ctx.persist_to_json()
                ctx.persist_meta_to_json()
                ctx.show()
            except Exception as e:
                logger.warning(f"Failed to persist context snapshot to JSON/meta after interrupt: {e}")
            return

        try:
            # 与 chat() 保持一致：profiling + 等待异步任务 + JSON/meta 持久化
            try:
                if runtime.get_config("CONTEXT.enable_profiling", False):
                    ctx.profiling()
                await ctx.wait_pending_tasks()
            except Exception as e:
                logger.warning(f"Context profiling / pending task wait failed: {e}")

            try:
                ctx.persist_to_json()
                ctx.persist_meta_to_json()
                ctx.show()
            except Exception as e:
                logger.warning(f"Failed to persist context to JSON/meta after streaming completion: {e}")
        except Exception as e:
            logger.warning(f"Context finalization after streaming failed: {e}")

        # post-hooks：与 chat() 相同，只使用图终态（不用 initial 快照补 messages 等字段）
        if self._post_hooks and final_state:
            logger.debug(f"[FlexAgent] Running {len(self._post_hooks)} post-hooks")
            logger.debug(
                "[FlexAgent] post-hook state keys: {} messages_count={}",
                list(final_state.keys()),
                len(final_state.get("messages") or []),
            )
            try:
                self._run_agent_post_hooks(dict(final_state), runtime)
            except Exception as e:
                logger.warning(f"Post-hooks failed in astream: {e}\n{traceback.format_exc()}")

    def _get_or_init_context(self, req: dict[str, Any], runtime: Any = None):
        """Return context for ids in request; create new if not exists.

        Additionally, when `sub_id==0` (main agent) under a multi-turn conversation,
        we restore all **previous** runs (with smaller `run_id`) that share the same
        `session_id` so the newly created Context contains the complete trajectory
        of the current session.  This allows the agent to reason over historical
        context when answering the current turn.

        Args:
            req: Request/state dict containing user_id, session_id, run_id, sub_id.
            runtime: Per-call Runtime; its ``config_manager`` is used when present.
        """
        config_manager = self._resolve_config_manager(runtime, agent_config_manager=self.config_manager)
        options = (
            build_context_init_options(config_manager, workspace=req.get("workspace"))
            if config_manager is not None
            else None
        )
        uid = str(req.get("user_id", self.config.get("USER_ID", "anonymous")))
        sid = str(req.get("session_id", self.config.get("SESSION_ID", "default_session")))
        rid = int(req.get("run_id", self.config.get("RUN_ID", 0)))
        subid = int(req.get("sub_id", self.config.get("SUB_ID", 0)))
        logger.trace(
            f"Getting or creating context for user_id: {uid}, session_id: {sid}, run_id: {rid}, sub_id: {subid}"
        )
        # Get or create the Context instance for the current identifiers.
        ctx = ContextFactory.get_context(uid, sid, rid, subid, options=options)

        if subid == 0 and rid > 0 and not getattr(ctx, "restored", False):
            ctx.restore_previous_runs(user_id=uid, session_id=sid, current_run_id=rid, sub_id=0)

        return ctx

    def _maybe_warn_hook_llm_missing(self, hook: Any, hook_llm_key: str | None, location: str) -> Any:
        """带 ``model`` 的 hook 应在 ``env.llm_configs`` 中有与 YAML ``name`` 同名的键；缺失时仅 warning。

        不包装 ``Runtime``；hook 内须 ``runtime.llm("<name>")`` 与 YAML ``name`` 一致。
        """
        if not hook_llm_key:
            return hook
        llm_cfg = getattr(self.env_config, "llm_configs", {}) or {}
        if isinstance(llm_cfg, dict) and hook_llm_key not in llm_cfg:
            logger.warning(
                f"[FlexAgent] Hook at {location} expects env.llm_configs[{hook_llm_key!r}] "
                "— set HOOKS name + model so flex_runtime merges MODEL into llm_configs under that name."
            )
        return hook

    def _prepare_context_for_langgraph_stream(self, input_val: dict[str, Any], runtime: Any = None) -> None:
        """
        LangGraph 原生调用分支下：
        - 注入 HITL 配置到 input
        - 同步 workspace 到 runtime，设置 ContextVar
        - 初始化/获取 Context，并在首次运行时注册 Query
        """
        try:
            agent_config = (self.config or {}).get("AGENT_CONFIG", {})
            enable_hitl = agent_config.get("enable_human_feedback", False)
            terminal_mode = agent_config.get("terminal_mode", False)

            input_val.setdefault("enable_human_feedback", enable_hitl)
            input_val.setdefault("terminal_mode", terminal_mode)
            input_val.setdefault(
                "enable_portrait",
                bool(agent_config.get("enable_portrait", False)),
            )
            input_val.setdefault("user_id", self.config.get("USER_ID", "anonymous"))
            input_val.setdefault("session_id", self.config.get("SESSION_ID", "default_session"))

            if runtime is not None:
                runtime.update_from_state(input_val)
                self._bind_workflow_runtime(runtime)
            self._refresh_workspace_runtime_context(input_val, runtime)

            call_context = self._get_or_init_context(input_val, runtime)
            if call_context and not getattr(call_context, "has_initial_pt", False):
                query_text = str(input_val.get("user_query", "") or "")
                call_context.register_query(query=query_text, additional_files=[])
            # 注意：不再将 Context 塞入 input_val，避免 LangGraph checkpoint 序列化失败
        except Exception as e:
            logger.debug(f"Context initialization skipped in langgraph stream prepare: {e}")

    def _refresh_workspace_runtime_context(self, state: dict[str, Any], runtime: Any = None) -> None:
        """Refresh runtime-local skills snapshot and rebuild sandbox before one run."""
        user_id = str(state.get("user_id") or "").strip() or None
        tm = self.env_config.tool_manager
        if tm is not None:
            tm.refresh_user_skills(user_id=user_id)
        workspace = state.get("workspace") or (runtime.workspace_dir if runtime else None)
        if workspace is None:
            raise RuntimeError("workspace is required before refreshing runtime workspace context")
        skills = tm.list_skills() if tm is not None else []
        skill_aliases = {skill["name"]: Path(str(skill["path"])).resolve() for skill in skills}
        cm = getattr(self, "config_manager", None)
        settings = cm.get_all() if cm is not None else (self.config or {})
        activated_suites = getattr(cm, "activated_suites", None) or []
        allow_read_roots = [
            Path(p).expanduser().resolve() for p in effective_workspace_allow_paths(settings, activated_suites)
        ]
        resolved_workspace = Path(str(workspace)).expanduser().resolve()

        # 默认开启：若系统未安装 bwrap，则 create_sandbox 会自动回退为 NoopSandbox 并打印 warning。
        sandbox_enabled = get_env_bool("DATAAGENT_SANDBOX_ENABLED", default=True)
        readonly_binds, writable_binds = build_workspace_mount_lists(
            resolved_workspace=resolved_workspace,
            allow_read_roots=allow_read_roots,
            skill_aliases=skill_aliases,
        )
        sandbox = create_sandbox(
            enabled=sandbox_enabled,
            policy=SandboxPolicy(
                writable_binds=writable_binds,
                readonly_binds=readonly_binds,
            ),
            workspace_root=resolved_workspace,
            skill_aliases=skill_aliases,
            allow_read_roots=allow_read_roots,
        )

        if runtime is not None:
            runtime.set_sandbox(sandbox)
        logger.debug(
            "[sandbox refresh] user={} workspace={} skills={{{}}} allow={} sandbox={}",
            user_id,
            resolved_workspace,
            ", ".join(f"{k}: {v}" for k, v in skill_aliases.items()),
            allow_read_roots,
            type(sandbox).__name__,
        )

    def _register_hooks_from_config(self, config: dict[str, Any]) -> None:
        """从 ``config['HOOKS']`` 挂载 agent / 节点级 hook。

        **字符串项** 可为内置短名（见 :data:`dataagent.core.flex.hooks.registry.BUILTIN_HOOK_REGISTRY`）
        或 ``module.path.callable``（与 tool hook 相同）。YAML 字典项禁止 ``import`` 字段。

        **``name``**（字典项）：内置短名，用于解析实现；若带 ``model``，合并结果写入 ``env.llm_configs[name]``。
        Hook 内须 ``runtime.llm("<name>")`` 与该 ``name`` 字符串一致。

        **``model``**：引用当前 YAML 里 **``MODEL`` 已声明的槽名**（如 ``qwen3``），语义同节点 ``chat_model``；
        校验与合并见 :func:`dataagent.core.flex.flex_runtime_from_config._merge_hook_llm_configs`。

        **需要 LLM 的 hook**（如 ``portraiter``、``pruner``）须满足其一，否则运行期 ``runtime.llm("pruner")`` 等会缺配置：

        - 在 ``MODEL`` 下保留 **与 hook ``name`` 同名** 的槽（例如 ``MODEL.pruner``），或
        - 使用 **字典** 并写 ``model: <MODEL 槽名>``（推荐在只声明 ``qwen3`` 时用 ``model: qwen3``）。

        **字符串项**（如 ``- pruner``）不会触发 ``_merge_hook_llm_configs``；若 ``MODEL`` 仅有 ``qwen3`` 而无
        ``pruner``/``portraiter`` 槽，**不要**对依赖 LLM 的 hook 使用字符串简写。

        项类型：

        - **可调用对象**：仅代码/测试注入 ``(state)`` / ``(state, runtime)``；
        - **字符串**：内置短名或 ``module.path.callable``；
        - **字典**：必填 ``name``；需要按某 MODEL 槽填 ``model``。

        同侧多个 hook 按列表自上而下顺序注册。

        **内置 agent pre（非 YAML）**：在任意 ``agent.pre`` 之前仅执行 ``session_history_restore``（见
        :mod:`dataagent.core.flex.hooks.agent_turn`）。本轮用户模板 Human 由
        :meth:`dataagent.core.cbb.runtime.Runtime.reset_flex_planner_user_sync` 与
        :func:`dataagent.core.flex.utils.planner_prompt_builder.sync_flex_planner_user_human_to_state` 写入。测试可替换
        ``FlexAgent._builtin_agent_pre_hooks``。

        示例（仅 ``MODEL.qwen3`` 时）::

            HOOKS:
              agent:
                post:
                  - name: portraiter
                    model: qwen3
              nodes:
                executor:
                  post:
                    - name: pruner
                      model: qwen3
        """
        self._builtin_agent_pre_hooks = [
            BaseAgent._validate_hook(session_history_restore, "agent.builtin.session_history_restore"),
            BaseAgent._validate_hook(cross_session_recall, "agent.builtin.cross_session_recall"),
        ]

        hooks_config = config.get("HOOKS", {}) or {}

        agent_hooks = hooks_config.get("agent", {}) or {}
        for hook_item in agent_hooks.get("pre", []) or []:
            self.add_pre_hook(self._resolve_hook_item(hook_item, "agent.pre"))
        for hook_item in agent_hooks.get("post", []) or []:
            self.add_post_hook(self._resolve_hook_item(hook_item, "agent.post"))

        for node_name, node_hook_cfg in (hooks_config.get("nodes", {}) or {}).items():
            node = self._nodes.get(node_name)
            if node is None:
                logger.warning(f"[FlexAgent] Hook configured for unknown node '{node_name}', skipped")
                continue
            for hook_item in node_hook_cfg.get("pre", []) or []:
                node.add_pre_hook(self._resolve_hook_item(hook_item, f"nodes.{node_name}.pre"))
            for hook_item in node_hook_cfg.get("post", []) or []:
                node.add_post_hook(self._resolve_hook_item(hook_item, f"nodes.{node_name}.post"))

    def _resolve_hook_item(self, item: Any, location: str) -> Any:
        """解析单个 hook 项；字符串项不经过 ``_merge_hook_llm_configs``，依赖 LLM 的 hook 须用字典 + ``model`` 或 MODEL 同名槽。

        **配置字段绑定**：字典项中除 ``name`` / ``model`` / ``import`` 外的字段视为 hook
        配置，经 ``functools.partial`` 绑定为 keyword-only 参数（hook 须以带默认值的
        keyword-only 形参接收，见 :meth:`BaseAgent._validate_hook`）。例如::

            - name: plan_enforcer
              require_plan_skills:
                - create-neutralization-experiment

        会将 ``require_plan_skills`` 绑定到 hook 的同名 kwarg。
        """
        hook_llm_key: str | None = None
        if isinstance(item, dict):
            hook_name = str(item.get("name") or "").strip()
            if not hook_name:
                raise ValueError(f"{location}: hook mapping requires non-empty 'name'")
            if item.get("import") is not None:
                raise ValueError(
                    f"{location}: 'import' is not supported in HOOKS YAML; use 'name' (built-in short name, "
                    "framework dotted path, or Suite-prefixed path)"
                )
            raw_model = item.get("model")
            has_model = False
            if raw_model is not None and raw_model != "":
                if isinstance(raw_model, dict):
                    has_model = bool(raw_model)
                elif isinstance(raw_model, str):
                    has_model = bool(raw_model.strip())
                else:
                    has_model = True
            if has_model:
                hook_llm_key = hook_name
            fn = self._resolve_hook_callable(hook_name, location=location)
            fn = self._bind_hook_config(fn, item, location=location)
            BaseAgent._validate_hook(fn, location)
            return self._maybe_warn_hook_llm_missing(fn, hook_llm_key, location)
        if isinstance(item, str):
            spec = item.strip()
            fn = self._resolve_hook_callable(spec, location=location)
            BaseAgent._validate_hook(fn, location)
            return self._maybe_warn_hook_llm_missing(fn, None, location)
        fn = BaseAgent._validate_hook(item, location)
        return self._maybe_warn_hook_llm_missing(fn, None, location)

    def _resolve_hook_callable(self, spec: str, *, location: str) -> Any:
        """
        Resolve a hook spec to a callable.

        Supports built-in short names, framework dotted paths, and ``{suite_name}.`` prefixed
        Suite hooks loaded from the activated suite root.
        """
        suite_fn = self._try_resolve_suite_hook(spec, location=location)
        if suite_fn is not None:
            return suite_fn
        return resolve_builtin_hook(spec)

    def _try_resolve_suite_hook(self, spec: str, *, location: str) -> Any | None:
        """Load a Suite hook when ``spec`` starts with an activated Suite name prefix."""
        activated = getattr(getattr(self, "config_manager", None), "activated_suites", None) or []
        ordered = sorted(
            (entry for entry in activated if isinstance(entry, dict)),
            key=lambda item: len(str(item.get("name") or "")),
            reverse=True,
        )
        for entry in ordered:
            suite_name = str(entry.get("name") or "").strip()
            root_raw = entry.get("root")
            if not suite_name or not root_raw:
                continue
            prefix = f"{suite_name}."
            if not spec.startswith(prefix):
                continue
            relative = spec[len(prefix) :]
            if not relative:
                raise ValueError(f"{location}: invalid Suite hook spec {spec!r}")
            return self.import_hook_from_suite_root(
                relative,
                root=Path(str(root_raw)),
                suite_name=suite_name,
                location=location,
            )
        return None

    def _run_builtin_agent_pre_hooks(self, state: dict[str, Any], runtime: Any = None) -> dict[str, Any]:
        """运行默认内置 agent pre 链（先于 YAML ``HOOKS.agent.pre``）。

        实现见 :mod:`dataagent.core.flex.hooks.agent_turn`；单测可替换 ``self._builtin_agent_pre_hooks``。
        """
        s = state
        collector = get_current_collector()
        for hook in self._builtin_agent_pre_hooks:
            with collector.measure(
                "hook",
                callable_perf_name(hook),
                hook_scope="agent",
                hook_phase="pre",
            ):
                from dataagent.core.cbb.base_hook import invoke_hook

                out = invoke_hook(hook, s, runtime)
            if isinstance(out, dict):
                s = out
        return s

    async def _stream_with_finalization(
        self,
        stream: AsyncIterator[Any],
        *,
        initial_state_for_persist: dict[str, Any] | None,
        runtime: Any = None,
        log_parse_error: bool = False,
        langgraph_config: dict[str, Any] | None = None,
        langgraph_checkpointer: Any = None,
        langgraph_store: Any = None,
        latest_state: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """消费 workflow stream，结束后读取图终态并触发 Context / post-hook 收尾。"""
        interrupted = False
        stream_final_state: dict[str, Any] = {}
        try:
            async for item in stream:
                if self._is_interrupt_stream_item(item, log_parse_error=log_parse_error):
                    interrupted = True
                if isinstance(item, dict):
                    stream_final_state = item
                elif isinstance(item, tuple):
                    mode: Any = None
                    data: Any = None
                    if len(item) == 3:
                        _, mode, data = item
                    elif len(item) == 2:
                        mode, data = item
                    if mode == "values" and isinstance(data, dict):
                        stream_final_state = data
                yield item
        finally:
            resolved_final_state: dict[str, Any] = {}
            if not interrupted:
                resolved_final_state = await self._read_langgraph_final_state_after_stream(
                    stream_final_state=stream_final_state,
                    langgraph_config=langgraph_config,
                    langgraph_checkpointer=langgraph_checkpointer,
                    langgraph_store=langgraph_store,
                )
            if latest_state is not None:
                latest_state["state"] = resolved_final_state or stream_final_state
            await self._finalize_context_after_stream(
                initial_state_for_persist,
                interrupted,
                final_state=resolved_final_state,
                runtime=runtime,
            )
