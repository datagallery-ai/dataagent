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
L1 层高码开发接口，支持用户从头开发自定义 DataAgent，提供精简的配置能力。
"""

from collections.abc import AsyncIterator
from typing import Any

from dataagent.core.cbb import BaseNode, BaseRouter, BaseState
from dataagent.core.cbb.agent_env import Env as AgentEnv
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.framework_adapters.runtime.workflow import LangGraphWorkflow
from dataagent.utils.log import logger


class BaseDataAgent:
    """L1 层 DataAgent 高码开发入口类

    支持用户从头开发自定义 DataAgent，提供最精简的配置能力。
    用户在自己的 Node 内部管理 LLM、Prompt、工具等。
    """

    def __init__(self):
        """初始化 BaseDataAgent"""
        from dataagent.config.config_manager import ConfigManager

        self._config_manager = ConfigManager()
        # ===== 架构信息（必需） =====
        self._name: str | None = None
        self._state_cls: type[BaseState] | None = None
        self._nodes: list[BaseNode] = []
        self._router: BaseRouter | None = None
        self._backend: str = "langgraph"  # 当前只支持 langgraph

        # ===== 工具/服务配置（可选） =====
        self._actions: dict[str, Any] = {}
        self._metavisor_cfg: dict[str, Any] = {}
        self._ontology_cfg: dict[str, Any] = {}
        self._database_cfg: dict[str, Any] = {}

        # ===== 运行时对象 =====
        self._workflow: LangGraphWorkflow | None = None  # LangGraphWorkflow 实例
        self._built: bool = False

    @property
    def config_manager(self):
        """Per-Agent ConfigManager for L1 nodes and tools (not the module-level singleton)."""
        return self._config_manager

    async def astream(
        self,
        init_state: dict[str, Any],
        session_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> AsyncIterator[Any]:
        """触发 Agent 对话（异步流式执行）

        Args:
            init_state: 初始状态，必须包含 user_query
            session_id: 会话 ID
            checkpoint_id: 保存点 ID

        Yields:
            Agent 对话的流式输出
        """
        # 确保已构建
        self._build()

        if self._workflow is None:
            raise RuntimeError("Workflow 未成功构建")

        # 调用 LangGraphWorkflow 的流式接口
        logger.debug(f"💬 开始流式对话: {init_state.get('user_query', 'N/A')}")
        runtime = self._build_l1_runtime()
        self._workflow.set_runtime(runtime)
        async for event in self._workflow.astream(init_state):
            yield event
        logger.debug("✅ 流式对话完成")

    async def chat(
        self,
        user_query: str,
        init_state: dict[str, Any] | None = None,
    ) -> Any:
        """触发 Agent 对话

        Args:
            user_query: 用户查询
            init_state: 初始状态配置

        Returns:
            Agent 的执行结果（最终 state）
        """
        # 确保已构建
        self._build()

        if self._workflow is None:
            raise RuntimeError("Workflow 未成功构建")

        # 构建初始状态
        if init_state:
            # 如果提供了 init_state，使用它作为基础
            state = dict(init_state)
            # 确保 user_query 被设置（如果 init_state 中没有）
            if "user_query" not in state:
                state["user_query"] = user_query
        else:
            # 没有 init_state，只设置 user_query
            state = {"user_query": user_query}

        # 调用 LangGraphWorkflow 的异步方法
        logger.debug(f"💬 开始对话: {user_query}")
        runtime = self._build_l1_runtime()
        self._workflow.set_runtime(runtime)
        result = await self._workflow.ainvoke(state)
        logger.debug("✅ 对话完成")

        return result

    def get_architecture(self) -> dict[str, Any]:
        """获取架构信息

        Returns:
            架构信息字典
        """
        return {
            "name": self._name,
            "backend": self._backend,
            "state_cls": self._state_cls.__name__ if self._state_cls else None,
            "nodes": [
                {
                    "name": node.name,
                    "type": node.__class__.__name__,
                    "chat_model_name": node.chat_model_name,
                }
                for node in self._nodes
            ],
            "router": {
                "entry_point": self._router.entry_point if self._router else None,
                "routing_rules": list(self._router.routing_rules.keys()) if self._router else [],
            },
        }

    def get_config(self) -> dict[str, Any]:
        """获取当前配置

        Returns:
            配置字典
        """
        return {
            "architecture": {
                "name": self._name,
                "backend": self._backend,
                "state_cls": self._state_cls.__name__ if self._state_cls else None,
                "nodes": [node.name for node in self._nodes],
                "entry_point": self._router.entry_point if self._router else None,
            },
            "actions": self._actions,
            "metavisor": self._metavisor_cfg,
            "ontology": self._ontology_cfg,
        }

    def set_actions(
        self,
        *,
        local_functions: list[dict[str, Any]] | None = None,
        a2a: list[dict[str, Any]] | None = None,
        mcp: list[dict[str, Any]] | None = None,
        skills: dict[str, list[str]] | None = None,
    ) -> "BaseDataAgent":
        """配置 Agent 可以使用的工具、Skills、MCP、A2A 等

        注意：L1 只存储配置，不负责注册。用户需要在自己的 Node 中使用这些工具。

        Args:
            local_functions: 本地工具配置列表
            a2a: 服务化的 A2A 接口配置列表
            mcp: MCP 服务配置列表
            skills: Skill allowlist 配置

        Returns:
            self，支持链式调用
        """
        if local_functions is not None:
            self._actions["local_functions"] = local_functions
        if a2a is not None:
            self._actions["a2a"] = a2a
        if mcp is not None:
            self._actions["mcp"] = mcp
        if skills is not None:
            self._actions["skills"] = skills

        logger.debug(f"✅ 工具配置完成: {len(self._actions)} 类工具")
        return self

    def set_architecture(
        self,
        *,
        name: str,
        state_cls: type[BaseState],
        nodes: list[BaseNode],
        router: BaseRouter,
        backend: str = "langgraph",
    ) -> "BaseDataAgent":
        """配置 Agent 的整体架构

        Args:
            name: Agent 名称
            state_cls: State 类型（BaseState 的子类）
            nodes: 节点列表，每个节点需有唯一 name
            router: 路由器实例
            backend: 底层运行时后端类型，当前只支持 "langgraph"

        Returns:
            self，支持链式调用

        Raises:
            ValueError: 如果节点名称不唯一或 backend 不支持
        """
        # 验证 backend
        if backend != "langgraph":
            raise ValueError(
                f"当前只支持 backend='langgraph'，暂不支持 '{backend}'。openjiuwen 支持将在修复后重新加入。"
            )

        # 验证节点名称唯一性
        node_names = [node.name for node in nodes]
        if len(node_names) != len(set(node_names)):
            raise ValueError("节点名称必须唯一。请检查 nodes 列表中是否有重复的 node.name。")

        # 验证 entry_point 在节点中
        if router.entry_point not in node_names:
            raise ValueError(f"router.entry_point='{router.entry_point}' 不在节点列表中。可用节点: {node_names}")

        # 保存架构信息
        self._name = name
        self._state_cls = state_cls
        self._nodes = nodes
        self._router = router
        self._backend = backend

        logger.debug(f"✅ 架构配置完成: {name}, backend={backend}, nodes={len(nodes)}")
        return self

    def set_database(
        self,
        db_id: str = "",
        engine: str = "sqlite",
        config: dict[str, Any] | None = None,
    ) -> "BaseDataAgent":
        """配置数据库

        这些配置会被注册到 config_manager 的 DATABASE 节，供节点使用。

        Args:
            db_id: 数据库 ID
            engine: 数据库引擎（sqlite, mysql, postgresql等）
            config: 数据库连接配置

        Returns:
            self，支持链式调用
        """
        self._database_cfg = {
            "db_id": db_id,
            "engine": engine,
            "config": config or {},
        }

        logger.debug(f"✅ 数据库配置完成: engine={engine}, db_id={db_id}")
        return self

    def set_metavisor(
        self,
        enable: bool,
        metavisor_url: str | None = None,
        valuematch_url: str | None = None,
        url: str | None = None,  # 保留兼容性
        scene: str | None = None,  # 保留兼容性
    ) -> "BaseDataAgent":
        """配置增强元数据服务

        这些配置会被注册到 config_manager 的 METAVISOR 节，供节点使用。

        Args:
            enable: 是否启用增强元数据服务
            metavisor_url: Metavisor 服务地址
            valuematch_url: ValueMatch 服务地址
            url: 部署地址（向后兼容）
            scene: 场景（向后兼容）

        Returns:
            self，支持链式调用
        """
        if not enable:
            self._metavisor_cfg = {}
            return self

        cfg: dict[str, Any] = {"enable": True}
        if metavisor_url:
            cfg["metavisor_url"] = metavisor_url
        if valuematch_url:
            cfg["valuematch_url"] = valuematch_url
        if url:
            cfg["url"] = url
        if scene:
            cfg["scene"] = scene
        self._metavisor_cfg = cfg

        logger.debug(f"✅ Metavisor 配置完成: enable={enable}")
        return self

    def set_ontology(
        self,
        enable: bool,
        url: str | None = None,
        scene: str | None = None,
    ) -> "BaseDataAgent":
        """配置本体服务

        这些配置会被注册到 config_manager 的 ONTOLOGY 节，供节点使用。

        Args:
            enable: 是否启用本体服务
            url: 部署地址
            scene: 场景

        Returns:
            self，支持链式调用
        """
        if not enable:
            self._ontology_cfg = {}
            return self

        cfg: dict[str, Any] = {"enable": True}
        if url:
            cfg["url"] = url
        if scene:
            cfg["scene"] = scene
        self._ontology_cfg = cfg

        logger.debug(f"✅ 本体配置完成: enable={enable}")
        return self

    def _build(self):
        """构建 Agent（延迟构建，在第一次 chat/astream 时调用）"""
        if self._built:
            return

        logger.debug(f"🚀 开始构建 BaseDataAgent: {self._name}")

        # 注册配置到 config_manager（供节点内部的 get_config 使用）
        self._register_configs()

        # 构建 Workflow（复用 core 的 LangGraphWorkflow）
        self._build_workflow()

        self._built = True
        logger.debug(f"✅ BaseDataAgent 构建完成: {self._name}")

    def _build_l1_runtime(self) -> Runtime:
        """
        Build a per-Agent :class:`~dataagent.core.cbb.runtime.Runtime` for L1 workflow execution.

        L1 nodes receive this runtime via ``aprocess(state, runtime)`` and should use
        ``runtime.get_config()`` instead of the module-level ``dataagent.config`` singleton.

        Returns:
            Runtime bound to this agent's ConfigManager.
        """
        env = AgentEnv(
            llm_configs={},
            tavily_configs={},
            modules={},
            hooks={},
            config_manager=self._config_manager,
            tool_manager=None,
        )
        return Runtime(env)

    def _build_workflow(self):
        """构建 LangGraphWorkflow（复用 core 的构图逻辑）"""
        if self._state_cls is None or not self._nodes or self._router is None:
            raise ValueError("必须先调用 set_architecture() 配置架构，才能构建 Agent。")

        logger.debug(f"🔧 开始构建 Workflow: {self._name}")

        # 直接使用 core 的 LangGraphWorkflow
        # 它会处理所有构图细节：节点包装、路由边、编译等
        self._workflow = LangGraphWorkflow(
            nodes=self._nodes,
            router=self._router,
            state_class=self._state_cls,
        )

        logger.debug(f"✅ Workflow 构建完成: {self._name}")
        logger.debug(f"  节点: {list(self._workflow.nodes.keys())}")
        logger.debug(f"  入口点: {self._workflow.router.entry_point}")

    def _register_configs(self):
        """将 L1 配置注册到本 Agent 的 ConfigManager，供节点使用。"""
        # 注册 METAVISOR 配置
        if self._metavisor_cfg:
            self._config_manager.set("METAVISOR", self._metavisor_cfg)
            logger.debug(f"📝 已注册 METAVISOR 配置: {self._metavisor_cfg}")

        # 注册 ONTOLOGY 配置
        if self._ontology_cfg:
            self._config_manager.set("ONTOLOGY", self._ontology_cfg)
            logger.debug(f"📝 已注册 ONTOLOGY 配置: {self._ontology_cfg}")

        # 注册 DATABASE 配置
        if self._database_cfg:
            self._config_manager.set("DATABASE", self._database_cfg)
            logger.debug(f"📝 已注册 DATABASE 配置: {self._database_cfg}")
