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
"""Runtime: per-invocation context for galatea-style agents.

Wraps an ``Env`` instance and provides:
- **LLM**：``runtime.llm(name)`` 按 ``name`` 查 ``env.llm_configs``（flex 在 Agent 初始化时由
  :mod:`dataagent.core.flex.flex_runtime_from_config` 写入），懒加载
  :func:`~dataagent.core.managers.llm_manager.llm_client.llm_adapter_from_env_cfg` 并缓存。
  ``name`` 与配置里 ``llm_configs`` 的键一致（多为节点名，或 ``MODEL`` 槽位如 ``portraiter``）。
- Cancellation check via an optional ``threading.Event`` on the ``Env``
- Convenience properties for ``workspace_dir``, ``hierarchy``, and ``instructions``
- ``update_from_state(state)`` to sync ``workspace_dir`` and session identity
  (``user_id`` / ``session_id`` / ``run_id`` / ``sub_id``) from LangGraph state
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from dataagent.actions.tools.local_tool.sandbox import Sandbox
from dataagent.core.cbb.agent_env import Env

if TYPE_CHECKING:
    from dataagent.config.config_manager import ConfigManager
    from dataagent.core.managers.action_manager.base import BaseTool, ToolResult


@dataclass(slots=True)
class StreamCursor:
    """Incremental UI stream position for one agent invocation (step ids + message cursor)."""

    _emitted_len: int | None = None
    _step_seq: int = 0

    @property
    def emitted(self) -> int:
        """Count of messages already surfaced to the UI stream."""
        return 0 if self._emitted_len is None else self._emitted_len

    def bootstrap(self, baseline: Any) -> None:
        """Initialize from ``state['_stream_emitted_from']`` on first streamer pass."""
        if self._emitted_len is None:
            self._emitted_len = int(baseline) if isinstance(baseline, int) else 0

    def take_step_id(self) -> int:
        """Next monotonic step id for ``event_sink`` payloads."""
        step = self._step_seq
        self._step_seq += 1
        return step

    def advance_to(self, message_count: int) -> None:
        """Mark all messages up to ``message_count`` as emitted."""
        self._emitted_len = message_count


@dataclass(slots=True)
class FileMetadataScratch:
    """Per-turn workspace snapshot + tool-call context for Galatea metadata hooks."""

    before_snapshot: dict[str, Any] | None = None
    workspace_dir: Path | None = None
    turn_context: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""


class Runtime:
    """Runtime: per-invocation context for galatea-style agents"""

    def __init__(self, env: Env) -> None:
        self.env = env
        _mi = getattr(env, "max_iter", None)
        self.max_iter: int | None = None if _mi is None else int(_mi)
        self._llms: dict[str, Any] = {}
        self._stream = StreamCursor()
        self._file_metadata = FileMetadataScratch()
        self._sandbox: Sandbox | None = None
        # Flex：本轮是否尚未在 Planner 中写入与 LLM 一致的用户模板 Human
        # 见 dataagent.core.flex.utils.planner_prompt_builder.sync_flex_planner_user_human_to_state
        self._flex_planner_user_sync_pending: bool = False
        self._cache: dict[str, Any] = {}  # 通用缓存，供各模块使用
        # Subagent 实时进度回调（CLI 侧 StreamRenderer 通过延迟绑定注入）
        self.on_subagent_progress: Callable[[str, str], None] | None = None
        self._runtime_environment: dict[str, Any] | None = None  # 运行环境信息
        # 与 Flex state / ContextFactory 对齐的会话四元组（由 update_from_state 刷新）
        self.user_id: str = "anonymous"
        self.session_id: str = "default_session"
        self.run_id: int = 0
        self.sub_id: int = 0
        self.user_query: Optional[str] = None  # noqa: UP045
        self.parent_user_query: Optional[str] = None  # noqa: UP045
        self._job_stack_workspace: Path | None = None
        self._job_service: Any = None
        self._agent_service: Any = None
        self._agent_registry: Any = None
        self._resource_stack_workspace: Path | None = None
        self._resource_service: Any = None

    @property
    def stream(self) -> StreamCursor:
        """UI streaming cursor"""
        return self._stream

    @property
    def file_metadata(self) -> FileMetadataScratch:
        """Scratch area for pre/post file-metadata hooks (workspace snapshot + turn context)."""
        return self._file_metadata

    @property
    def workspace_dir(self) -> Path | None:
        """当前调用的工作目录（从 env 读取，可由 update_from_state 在每次调用前刷新）。"""
        return getattr(self.env, "workspace_dir", None)

    @property
    def sandbox(self) -> Sandbox:
        """当前调用绑定的 sandbox。"""
        if self._sandbox is None:
            raise RuntimeError("runtime.sandbox is not initialized")
        return self._sandbox

    @property
    def hierarchy(self) -> str:
        """Agent 层级（MAIN / SUB），默认 MAIN。"""
        return str(getattr(self.env, "hierarchy", "MAIN") or "MAIN")

    @property
    def instructions(self) -> str:
        """场景级自定义指令，由 YAML SCENARIO.{mode}.instructions 写入，默认为空串。"""
        return str(getattr(self.env, "instructions", "") or "")

    @property
    def flex_planner_user_sync_pending(self) -> bool:
        """
        Planner 是否仍需将本轮用户 Human 与 LLM 模板对齐（见 ``dataagent.core.flex.utils.
        planner_prompt_builder.sync_flex_planner_user_human_to_state``）。
        """
        return self._flex_planner_user_sync_pending

    @property
    def runtime_environment(self) -> dict[str, Any]:
        """运行环境信息（懒加载）。

        首次访问时收集系统配置、运行环境、资源占用、模型上下文窗口信息。

        Returns:
            包含system、runtime、resources、models的字典
        """
        if self._runtime_environment is None:
            from dataagent.core.cbb.runtime_env import RuntimeEnvironmentCollector

            agent_cm = getattr(self.env, "config_manager", None)
            collector = RuntimeEnvironmentCollector(self.workspace_dir, agent_config_manager=agent_cm)
            self._runtime_environment = collector.collect_with_models(self.env.llm_configs)
        return self._runtime_environment

    @property
    def bash_tool_whitelist(self) -> list[str] | None:
        """bash 工具命令白名单，None 表示不限制。"""
        return getattr(self.env, "bash_tool_whitelist", None)

    @property
    def tool_manager(self):
        """The per-Agent ToolManager instance (if available)."""
        return getattr(self.env, "tool_manager", None)

    @property
    def job_service(self) -> Any:
        """Return the workspace-scoped :class:`~dataagent.core.jobs.service.JobService`, if available."""
        _ = self.ensure_job_services()
        return self._job_service

    @property
    def agent_service(self) -> Any:
        """Return the workspace-scoped :class:`~dataagent.core.agents.service.AgentService`, if available."""
        _ = self.ensure_job_services()
        return self._agent_service

    @property
    def resource_service(self) -> Any:
        """Return the workspace-scoped :class:`~dataagent.core.resources.service.ResourceService`, if available."""
        self.ensure_resource_services()
        return self._resource_service

    @property
    def config_manager(self) -> ConfigManager:
        """Return the per-Agent :class:`~dataagent.config.config_manager.ConfigManager`.

        Raises:
            RuntimeError: When the bound :class:`~dataagent.core.cbb.agent_env.Env` has no ``config_manager``.
        """
        cm = getattr(self.env, "config_manager", None)
        if cm is None:
            raise RuntimeError(
                "Runtime has no per-Agent config_manager bound on env. "
                "Ensure build_agent_env_from_flex_config passes config_manager."
            )
        return cm

    def ensure_job_services(self) -> Any:
        """Bind Job/Agent services to the current ``workspace_dir``.

        Returns:
            :class:`~dataagent.core.agents.service.AgentService` when ``workspace_dir`` is set,
            otherwise ``None``.
        """
        ws = self.workspace_dir
        if ws is None:
            self._job_service = None
            self._agent_service = None
            self._job_stack_workspace = None
            self._resource_service = None
            self._resource_stack_workspace = None
            return None
        resolved = Path(ws).expanduser().resolve()
        if self._job_stack_workspace == resolved and self._agent_service is not None:
            return self._agent_service
        from dataagent.core.agents.service import AgentService
        from dataagent.core.jobs.file_store import FileJobStore
        from dataagent.core.jobs.service import JobService

        store = FileJobStore(resolved, config=self.get_all_config())
        self._job_service = JobService(store)
        registry = self._ensure_agent_registry()
        self._agent_service = AgentService(registry=registry, job_service=self._job_service, runtime=self)
        self._job_stack_workspace = resolved
        return self._agent_service

    def ensure_resource_services(self) -> Any:
        """Bind :class:`~dataagent.core.resources.service.ResourceService` to the current workspace.

        Returns:
            :class:`~dataagent.core.resources.service.ResourceService` when ``workspace_dir`` is set
            and merged config contains non-empty ``RESOURCES``; otherwise ``None``.
        """
        ws = self.workspace_dir
        if ws is None:
            self._resource_service = None
            self._resource_stack_workspace = None
            return None
        config = self.get_all_config()
        resources = config.get("RESOURCES") or []
        if not resources:
            self._resource_service = None
            self._resource_stack_workspace = None
            return None
        resolved = Path(ws).expanduser().resolve()
        _ = self.ensure_job_services()
        if self._job_service is None:
            self._resource_service = None
            self._resource_stack_workspace = None
            return None
        if self._resource_stack_workspace == resolved and self._resource_service is not None:
            return self._resource_service
        from dataagent.actions.resources.bootstrap import (
            default_mcp_client_factory,
            default_resource_operation_registry,
        )
        from dataagent.core.resources.registry import ResourceRegistry
        from dataagent.core.resources.service import ResourceService

        registry = ResourceRegistry.from_config(config)
        self._resource_service = ResourceService(
            registry=registry,
            job_service=self._job_service,
            runtime=self,
            operation_registry=default_resource_operation_registry(),
            mcp_client_factory=default_mcp_client_factory(),
        )
        self._resource_stack_workspace = resolved
        return self._resource_service

    def get_config(self, key: str, default: Any = None) -> Any:
        """Read a value from the per-Agent ConfigManager.

        Args:
            key: Dot-separated configuration key.
            default: Value when the key is missing.

        Returns:
            Configuration value from the current Agent's ConfigManager.
        """
        return self.config_manager.get(key, default)

    def get_all_config(self) -> dict[str, Any]:
        """Return a deep copy of the per-Agent configuration dict."""
        return self.config_manager.get_all()

    def set_sandbox(self, sandbox: Sandbox) -> None:
        """Bind the sandbox for the current runtime invocation."""
        self._sandbox = sandbox

    def reset_flex_planner_user_sync(self) -> None:
        """新用户轮次且 ``user_query`` 非空时由 FlexAgent 调用；与 ``state`` 字典上的魔法键无关。"""
        self._flex_planner_user_sync_pending = True

    def clear_flex_planner_user_sync_pending(self) -> None:
        """在 Planner 中已追加对齐后的 ``HumanMessage`` 后调用，清除 pending。"""
        self._flex_planner_user_sync_pending = False

    def update_from_state(self, state: dict[str, Any]) -> None:
        """在每次调用前，将 state 中的 workspace 与会话标识同步到本 Runtime。

        flex 节点通过 ``aprocess(state, runtime)`` 中的 ``runtime`` 参数获取当前工作目录，
        与 galatea 的 ``_process(state, runtime)`` 一致。
        hierarchy 由 ``build_agent_env_from_flex_config``（见 flex_runtime_from_config）写入 env，不经 state。

        ``user_id`` / ``session_id`` / ``run_id`` / ``sub_id`` 与 LangGraph state 键名一致；
        缺省或空字符串时与 FlexAgent 兜底值对齐（``anonymous`` / ``default_session`` / ``0`` / ``0``）。

        Args:
            state: Workflow state dict (Flex / LangGraph).
        """
        ws = state.get("workspace")
        if ws:
            self.env.workspace_dir = Path(str(ws)).expanduser().resolve()

        uid = str(state.get("user_id") or self.user_id or "anonymous").strip()
        self.user_id = uid or "anonymous"
        sid = str(state.get("session_id") or self.session_id or "default_session").strip()
        self.session_id = sid or "default_session"
        self.run_id = int(state.get("run_id", self.run_id) or 0)
        self.sub_id = int(state.get("sub_id", self.sub_id) or 0)
        user_query = state.get("user_query")
        if user_query is not None:
            self.user_query = str(user_query)

        parent_user_query = state.get("parent_user_query")
        if parent_user_query is not None:
            self.parent_user_query = str(parent_user_query)
        elif self.sub_id == 0 and self.user_query is not None:
            self.parent_user_query = self.user_query

    def llm(self, name: str) -> Any:
        """按 ``name`` 取 ``env.llm_configs[name]``，懒加载并缓存 ``LangChainChatModelAdapter``。

        ``name`` 须为 ``build_llm_configs_from_flex_config`` 写入的键；``logical_name`` 传入工厂以填充适配器侧
        :class:`~dataagent.core.managers.llm_manager.llm_config.LLMConfig`（与 env value 中的调用参数分离）。
        """
        if name not in self._llms:
            llm_cfg = self.env.llm_configs.get(name)
            if not isinstance(llm_cfg, dict) or not llm_cfg.get("api_base"):
                raise RuntimeError(
                    f"runtime.llm({name!r}) requires env.llm_configs[{name!r}] with api_base/api_key "
                    f"(resolved at agent init). Missing or incomplete entry: {llm_cfg!r}"
                )
            from dataagent.core.managers.llm_manager.llm_client import llm_adapter_from_env_cfg

            self._llms[name] = llm_adapter_from_env_cfg(llm_cfg, name)
        return self._llms[name]

    def get_tool(self, name: str) -> BaseTool:
        """Get a registered tool instance by name."""
        tm = self.tool_manager
        if tm is None:
            raise KeyError(f"Tool {name!r} not found: no ToolManager on runtime")
        return tm.get(name)

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        tm = self.tool_manager
        return tm.list_tools() if tm is not None else []

    async def call_tool(self, name: str, **kwargs: Any) -> ToolResult:
        """Call a registered tool by name (async-first, falls back to thread for sync tools)."""
        tm = self.tool_manager
        if tm is None:
            raise KeyError(f"Tool {name!r} not found: no ToolManager on runtime")
        return await tm.acall(name, **kwargs)

    def get_tools_for_llm(self) -> list[Any]:
        """Return LangChain StructuredTool list for binding to LLM (Planner bind_tools)."""
        tm = self.tool_manager
        if tm is None:
            return []
        return [tool.to_langchain_tool() for tool in tm.get_all_tool_instances()]

    def list_builtin_skills(self) -> list[dict[str, Any]]:
        """List builtin skills from the per-Agent ToolManager."""
        tm = self.tool_manager
        return tm.list_builtin_skills() if tm is not None else []

    def list_user_skills(self) -> list[dict[str, Any]]:
        """List user skills from the per-Agent ToolManager."""
        tm = self.tool_manager
        return tm.list_user_skills() if tm is not None else []

    def is_cancelled(self) -> bool:
        """Check if the runtime has been cancelled."""
        cancel_event = getattr(self.env, "cancel_event", None)
        return bool(cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set())

    def ensure_not_cancelled(self) -> None:
        """Raise an exception if the runtime has been cancelled."""
        if self.is_cancelled():
            raise InterruptedError("用户中断会话")

    def get_cache(self, key: str, default: Any = None) -> Any:
        """从缓存中获取值。"""
        return self._cache.get(key, default)

    def set_cache(self, key: str, value: Any) -> None:
        """设置缓存值。"""
        self._cache[key] = value

    def get_runtime_env_prompt(self) -> str:
        """获取格式化后的运行环境信息，用于注入system prompt。

        Returns:
            Markdown格式的环境信息字符串，适合直接插入system prompt
        """
        from dataagent.core.cbb.runtime_env import format_runtime_environment_section

        prompt = format_runtime_environment_section(self.runtime_environment)
        whitelist = self.bash_tool_whitelist
        if whitelist:
            commands = ", ".join(f"`{cmd}`" for cmd in sorted(whitelist))
            prompt += (
                f"\n## Bash allowed whitelist\n"
                f"Only allowed to execute the following shell commands, \
                    when calling the bash tool, the first command in the command parameter must be in this list: "
                f"{commands}\n"
            )
        return prompt

    def _ensure_agent_registry(self) -> Any:
        """Build or return the cached :class:`~dataagent.core.agents.registry.AgentRegistry`."""
        if self._agent_registry is not None:
            return self._agent_registry
        from dataagent.core.agents.registry import AgentRegistry

        cm = self.config_manager
        entries = cm.get("SUBAGENT_CONFIGS") or []
        self._agent_registry = AgentRegistry.from_subagent_configs(entries)
        return self._agent_registry
