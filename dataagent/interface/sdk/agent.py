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
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dataagent.actions.tools.local_tool.sandbox import create_sandbox
from dataagent.config import ConfigManager
from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.suite.debug_dump import dump_merged_config
from dataagent.utils.log import logger, setup_session_log
from dataagent.utils.runtime_paths import dataagent_package_path, resolve_effective_workspace_root

if TYPE_CHECKING:
    from dataagent.interface.sdk.builder import AgentBuilder


class DataAgent:
    """DataAgent主类"""

    def __init__(self, config: Any):
        self.config = config.copy()
        self.backend = config.get("AGENT_CONFIG.backend", "langgraph")
        # 默认采用 react（即 flex 模式）
        self.type = config.get("AGENT_CONFIG.type", "react")
        self.agent_type = config.get("AGENT_CONFIG.agent_type", None)

        # 工具模块初始化
        self.global_init(self.config)

        self._chat_agent_instance = None
        self.session_id = None

        logger.trace(f"DataAgent initialized with {self.backend} backend")

    def __repr__(self) -> str:
        return f"DataAgent(backend={self.backend}, config_loaded={bool(self.config)})"

    @property
    def _chat_agent(self):
        """Lazy initialization of chat agent"""
        if self._chat_agent_instance is None:
            self._chat_agent_instance = self.select_engine(self.config)
        return self._chat_agent_instance

    @staticmethod
    def _validate_workspace(workspace: Path | str | None) -> Path | None:
        """校验并规范化代码级 workspace 覆盖路径。"""
        if workspace is None:
            return None

        if isinstance(workspace, Path):
            normalized_path = workspace.expanduser()
        elif isinstance(workspace, str):
            stripped_path = workspace.strip()
            if not stripped_path:
                raise ValueError("`workspace` 不能为空字符串")
            try:
                normalized_path = Path(stripped_path).expanduser()
            except (TypeError, ValueError) as exc:
                raise ValueError(f"`workspace` 不是合法路径: {workspace!r}") from exc
        else:
            raise TypeError("`workspace` 必须是 `str`、`Path` 或 `None`")

        if not normalized_path.is_absolute():
            normalized_path = normalized_path.resolve(strict=False)
            logger.trace(f"检测到传入路径为相对路径，实际将被使用的路径为：{normalized_path}")

        if normalized_path.exists() and not normalized_path.is_dir():
            raise ValueError(f"`workspace` 必须是目录路径，当前为文件: {normalized_path}")

        return normalized_path

    @staticmethod
    def _ensure_workspace(state: Mapping[str, Any]) -> None:
        """Authorize and materialize the workspace directory before the run."""
        workspace_dir = Path(state["workspace"]).expanduser().resolve()
        create_sandbox(enabled=False, workspace_root=workspace_dir).authorize_write(
            workspace_dir,
            source_kind="system_injected",
            operation="prompt_working_directory",
        )
        workspace_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Default workspace set to {workspace_dir}")

    @staticmethod
    def _touch_workspace_catalog(state: Mapping[str, Any]) -> None:
        """Refresh workspace catalog session metadata after workspace materialize."""
        from dataagent.core.workspace.catalog import safe_touch_catalog

        workspace = str(state.get("workspace") or "").strip()
        session_id = str(state.get("session_id") or "").strip()
        if not workspace or not session_id:
            return
        safe_touch_catalog(workspace, session_id)

    @classmethod
    def from_config(cls, config: str | Path) -> "DataAgent":
        """从YAML配置文件创建Agent

        加载优先级（后者覆盖前者）：
        1. 默认配置：core/flex/flex_default_configs.yaml
        2. 用户指定的 YAML 配置文件
        3. .env 环境变量配置（最高优先级）
        """

        # 定位默认配置文件（若不存在则退化为仅加载用户配置）
        default_config_path: str | None = None
        candidate = dataagent_package_path("core", "flex", "flex_default_configs.yaml")
        if candidate.exists():
            default_config_path = str(candidate)

        # Per-Agent ConfigManager: do not reload into module-level dataagent.config.config_manager.
        agent_config_manager = ConfigManager()
        agent_config_manager.reload(str(config), default_config_path=default_config_path)

        # 确保最终配置中 AGENT_CONFIG.type 至少有一个有效默认值，
        # 与上面的粗读逻辑保持一致（react）
        if agent_config_manager.get("AGENT_CONFIG.type") is None:
            agent_config_manager.set("AGENT_CONFIG.type", "react")

        # 出站 mTLS：把 certificate: 段（插值后）下发为进程环境变量，供深层出站点统一读取。
        import os

        from dataagent.common_utils.outbound_tls import ENV_PRESERVE_ON_MISSING, apply_certificate_config

        apply_certificate_config(
            agent_config_manager.get("certificate"),
            preserve_existing_on_missing=os.getenv(ENV_PRESERVE_ON_MISSING) == "1",
        )

        return cls(config=agent_config_manager)

    def astream(self, *args, **kwargs):
        """流式对话"""
        input_val = kwargs.get("input")
        # 优先级：显式 kwargs.workspace > input.workspace（input 可能是 LangGraph Command，无 .get）
        in_ws = input_val.get("workspace") if isinstance(input_val, Mapping) else None
        workspace = self._validate_workspace(kwargs.get("workspace") or in_ws)
        initial_state = self._initialize_state(
            kwargs.get("initial_state"),
            kwargs.get("session_id"),
            workspace,
        )
        self._ensure_workspace(initial_state)
        self._touch_workspace_catalog(initial_state)
        self._dump_runtime_config(initial_state)
        setup_session_log(
            user_id=str(initial_state.get("user_id", "anonymous")),
            session_id=str(initial_state.get("session_id", kwargs.get("session_id"))),
        )
        kwargs["initial_state"] = initial_state
        if "workspace" in kwargs:
            kwargs["workspace"] = workspace
        return self._chat_agent.astream(*args, **kwargs)

    def select_engine(self, config: Any):
        """根据后端类型创建具体实现（固定使用 SCENARIO.chat）。"""
        engine_config = config.get_all() if hasattr(config, "get_all") else dict(config)
        engine_config["mode"] = "chat"
        if self.type == "react":
            from dataagent.core.flex.agent import FlexAgent

            return FlexAgent.from_config(config=engine_config, config_manager=self.config)
        if self.type == "nl2sql":
            from dataagent.agents.nl2sql.agent import NL2SQLAgent

            return NL2SQLAgent.from_config(config=engine_config, config_manager=self.config)
        raise ValueError(f"Unsupported agent type: {self.type}")

    def global_init(self, config: dict[str, Any] | None):
        """初始化管理器"""
        # self.config = config.copy() 仍是 ConfigManager
        # 下文入参 safe_cfg 期望是 dict，但 DataAgent 实际传的是 ConfigManager（即 self.config）
        # 各个 *Manager.init_from_config() 都要求 dict，所以在这里做一次“规范化”最小改动
        safe_cfg: dict[str, Any] = {}
        try:
            if config is None:
                safe_cfg = {}
            elif isinstance(config, Mapping):
                safe_cfg = dict(config)
            elif hasattr(config, "get_all") and callable(config.get_all):
                safe_cfg = config.get_all() or {}
            elif hasattr(config, "settings") and isinstance(getattr(config, "settings", None), dict):
                # 浅拷贝足够；只用于初始化读取
                safe_cfg = dict(config.settings)
            else:
                safe_cfg = {}
        except Exception as e:
            logger.warning(f"global_init(): failed to normalize config to dict, fallback to empty dict: {e}")
            safe_cfg = {}
        llm_manager.init_from_config(safe_cfg)
        # Tool initialization is now per-Agent, done inside build_agent_env_from_flex_config

    async def chat(
        self,
        user_query: str,
        session_id: str | None = None,
        workspace: Path | str | None = None,
        initial_state: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """单轮对话"""
        # 显式参数 session_id > initial_state 中的 session_id（如 CLI 只传 initial_state）> 已有 self.session_id > 新生成
        if not session_id and isinstance(initial_state, dict):
            sid = initial_state.get("session_id")
            if sid is not None and str(sid).strip():
                session_id = str(sid).strip()
        if not session_id:
            # 仅在 self.session_id 为空时生成新 id（不回写外部传入值，避免并发覆盖）
            if not self.session_id:
                self.session_id = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_") + str(
                    uuid.uuid4()
                )
            session_id = self.session_id
        workspace = self._validate_workspace(workspace)
        initial_state = self._initialize_state(initial_state, session_id, workspace)
        logger.debug(f"当前 workspace：{initial_state['workspace']}")
        setup_session_log(
            user_id=str(initial_state.get("user_id", "anonymous")),
            session_id=str(initial_state.get("session_id", session_id)),
        )
        try:
            self._ensure_workspace(initial_state)
            self._touch_workspace_catalog(initial_state)
            self._dump_runtime_config(initial_state)
            extra: dict[str, Any] = {}
            if checkpoint_id:
                extra["checkpoint_id"] = checkpoint_id
            response = await self._chat_agent.chat(
                user_query,
                session_id=initial_state["session_id"],
                initial_state=initial_state,
                **extra,
            )
            return response
        except Exception as e:
            logger.error(f"Chat failed: {e}")
            return {"error": str(e), "final_answer": f"抱歉，处理您的请求时出现错误：{str(e)}"}

    def build_agent_graph(self, mode: str = "chat") -> BaseAgent:
        """Pre-build the agent workflow graph (only ``chat`` is supported)."""
        if mode != "chat":
            raise ValueError(f"Unsupported agent graph mode: {mode!r}; only 'chat' is supported")
        return self._chat_agent

    def get_agent_info(self) -> dict[str, Any]:
        """获取Agent信息"""
        info = {}

        # 从配置中获取基本信息
        agent_config = self.config.get("AGENT_CONFIG", {})
        info.update(
            {
                "name": agent_config.get("name", "DataPilot Agent"),
                "version": agent_config.get("version", "1.0"),
                "description": agent_config.get("description", "数据分析Agent"),
                "backend": self.backend,
                "has_config": bool(self.config),
            }
        )
        return info

    def name(self) -> str:
        """获取Agent名称"""
        return str(self.get_agent_info().get("name", ""))

    def description(self) -> str:
        """获取Agent描述"""
        return self.get_agent_info().get("description", "数据分析Agent")

    def version(self) -> str:
        """获取Agent版本"""
        return self.get_agent_info().get("version", "1.0")

    def update_config(self, new_config: dict[str, Any]):
        """更新配置"""
        self.config.update(new_config)
        if self._chat_agent_instance:
            fn = getattr(self._chat_agent, "update_config", None)
            if callable(fn):
                fn(new_config)

        logger.debug("DataAgent configuration updated")

    def get_node(self, node_name: str):
        """获取指定节点实例"""
        if hasattr(self._chat_agent, node_name):
            return getattr(self._chat_agent, node_name)
        else:
            raise ValueError(f"Node '{node_name}' not found")

    def _initialize_state(
        self,
        initial_state: dict[str, Any] | None = None,
        session_id: str | None = None,
        workspace: Path | str | None = None,
    ) -> dict[str, Any]:
        """初始化initial_state"""
        if initial_state is None:
            initial_state = {}
        else:
            state_workspace = initial_state.get("workspace")
            normalized_state_workspace = self._validate_workspace(state_workspace)
            if normalized_state_workspace is not None:
                initial_state["workspace"] = normalized_state_workspace
            else:
                initial_state.pop("workspace", None)
        if session_id is None:
            session_id = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())
        defaults = {
            "messages": [],
            "complete": False,
            "user_id": self.config.get("USER_ID", "anonymous"),
            "session_id": self.config.get("SESSION_ID", session_id),
            "run_id": self.config.get("RUN_ID", 0),
            "sub_id": self.config.get("SUB_ID", 0),
        }
        for key, default_val in defaults.items():
            if key not in initial_state:
                initial_state[key] = default_val

        resolved_workspace = resolve_effective_workspace_root(
            config=self.config.get_all(),
            user_id=str(initial_state.get("user_id")),
            session_id=str(initial_state.get("session_id")),
            workspace_override=workspace,
        )
        if workspace is not None or "workspace" not in initial_state:
            initial_state["workspace"] = resolved_workspace

        return initial_state

    def _dump_runtime_config(self, initial_state: Mapping[str, Any]) -> None:
        """
        Persist merged Agent settings under the resolved workspace ``.runtime/`` directory.

        Invoked on each ``chat()`` / ``astream()`` turn after workspace is materialized.
        Skipped when only ``from_config`` / ``reload()`` runs without a chat entrypoint.
        """
        workspace = initial_state.get("workspace")
        if workspace is None:
            return
        if hasattr(self.config, "get_all") and callable(self.config.get_all):
            settings = self.config.get_all()
        elif hasattr(self.config, "settings") and isinstance(getattr(self.config, "settings", None), dict):
            settings = dict(self.config.settings)
        else:
            settings = dict(self.config) if isinstance(self.config, Mapping) else {}
        dump_merged_config(settings, workspace=workspace)
