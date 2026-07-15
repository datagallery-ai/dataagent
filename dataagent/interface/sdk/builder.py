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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from dataagent.utils.builder_utils import (
    get_agent_type,
    get_final_yaml,
    normalize_skill_allowlists,
)

if TYPE_CHECKING:
    from dataagent.interface.sdk.agent import DataAgent


class AgentBuilder:
    """Agent构建器（L0 北向接口）"""

    def __init__(self):
        self._global_config: dict = {}
        self.config_path: Path | None = None
        self.name: str | None = None

    def set_name(self, name: str) -> "AgentBuilder":
        """设置 Agent 名称。"""
        agent_config = self._global_config.get("AGENT_CONFIG")
        if not isinstance(agent_config, dict):
            agent_config = {}
        agent_config["name"] = name
        self._global_config["AGENT_CONFIG"] = agent_config
        self.name = name
        return self

    def set_base_config(
        self,
        *,
        name: str,
        description: str,
        agent_type: str = "deep_analyze",
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置 Agent 基本配置。

        必填字段:
            - name：Agent名称
            - description：Agent描述
            - agent_type：Agent类型（据此创建对应预制 Agent，可选值：deep_analyze）
        """
        # 1. 构建 agent_config
        agent_config = {
            "name": name,
            "description": description,
            "agent_type": agent_type,
        }
        for key, value in kwargs.items():
            agent_config[key] = value

        # 3. 更新 AgentBuilder 全局配置属性 "AGENT_CONFIG"
        self._global_config["AGENT_CONFIG"] = agent_config
        self.name = name
        return self

    def set_models(
        self,
        *,
        default_chat_model: dict,
        default_embedding_model: dict | None = None,
        model_config: dict | None = None,
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置 Agent 模型配置。

        必填字段:
            - default_chat_model：Agent 对话模型配置

        可选字段:
            - default_embedding_model：嵌入模型配置
            - model_config：其它模型配置（会并入 MODEL）
        """
        # 1. 构建 model
        model = {"chat_model": default_chat_model}
        if default_embedding_model is not None:
            model["default_embedding_model"] = default_embedding_model
        if model_config is not None:
            model.update(model_config)
        for key, value in kwargs.items():
            model[key] = value

        # 2. 更新AgentBuilder 全局配置属性 "MODEL"
        self._global_config["MODEL"] = model
        return self

    def set_raw_models(self, *, model: dict[str, Any], **kwargs: Any) -> "AgentBuilder":
        """
        直接透传原始 MODEL 配置。

        必填字段:
            - model：写入 YAML 的 MODEL；若未显式提供 ``chat_model``，则会把第一个
              ``model_type == "chat"`` 或未声明 ``model_type`` 的槽位归一化为 ``MODEL.chat_model``
        """
        raw_model = dict(model)
        if "chat_model" not in raw_model:
            normalized_model: dict[str, Any] = {}
            chat_model_renamed = False
            for key, value in raw_model.items():
                target_key = key
                if not chat_model_renamed and isinstance(value, dict):
                    model_type = value.get("model_type")
                    if model_type in (None, "chat"):
                        target_key = "chat_model"
                        chat_model_renamed = True
                normalized_model[target_key] = value
            raw_model = normalized_model

        for key, value in kwargs.items():
            raw_model[key] = value

        self._global_config["MODEL"] = raw_model
        return self

    def set_scenario(
        self,
        *,
        instructions: str = "",
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置注入 Agent 的场景约束说明。

        可选字段:
            - instructions：注入先验知识
        """
        scenario = self._global_config.get("SCENARIO")
        if not isinstance(scenario, dict):
            scenario = {}

        chat = scenario.get("chat")
        if not isinstance(chat, dict):
            chat = {}

        if instructions:
            chat["instructions"] = instructions
        for key, value in kwargs.items():
            chat[key] = value

        if chat:
            scenario["chat"] = chat
            self._global_config["SCENARIO"] = scenario

        return self

    def set_actions(
        self,
        *,
        skills: list[str] | None = None,
    ) -> "AgentBuilder":
        """
        设置 Agent 可额外使用的工具能力配置。

        可选字段:
            - skills：Skill allowlist 配置
        """
        normalized_skills = normalize_skill_allowlists(skills)

        tools = self._global_config.get("TOOLS")
        if not isinstance(tools, dict):
            tools = {}

        if normalized_skills is not None:
            tools["skills"] = normalized_skills

        if tools:
            self._global_config["TOOLS"] = tools

        return self

    def set_history(
        self,
        *,
        backend: str = "",
        url: str = "",
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置历史上下文存储配置。

        可选字段:
            - backend：history 后端类型（如 postgresql）
            - url：history 后端连接地址
        """
        history = self._global_config.get("CONTEXT")
        if not isinstance(history, dict):
            history = {}

        if backend:
            history["backend"] = backend
        if url:
            history["url"] = url
        for key, value in kwargs.items():
            history[key] = value

        if history:
            self._global_config["CONTEXT"] = history

        return self

    def set_knowledge_base(
        self,
        *,
        backend: str,
        index: str,
        url: str,
        embedding_model: dict[str, Any],
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置知识库存储配置。

        必填字段:
            - backend：knowledge_base 后端类型（如 elasticsearch）
            - index：数据库查询前缀（映射为 KNOWLEDGE_BASE.scene）
            - url：knowledge_base 后端连接地址
            - embedding_model：嵌入模型配置（映射为 KNOWLEDGE_BASE.model）
        """
        if not isinstance(embedding_model, dict):
            raise ValueError("`embedding_model` is required and must be a dict.")

        knowledge_base = {
            "backend": backend,
            "scene": index,
            "url": url,
            "model": dict(embedding_model),
        }
        for key, value in kwargs.items():
            knowledge_base[key] = value
        self._global_config["KNOWLEDGE_BASE"] = knowledge_base
        return self

    def set_metavisor(
        self,
        *,
        url: str = "",
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置增强元数据配置。

        可选字段:
            - url：元数据服务地址（格式 x.x.x.x:port）
        """
        metavisor = self._global_config.get("METAVISOR")
        if not isinstance(metavisor, dict):
            metavisor = {}

        if url:
            metavisor["url"] = url
        for key, value in kwargs.items():
            metavisor[key] = value

        if metavisor:
            self._global_config["METAVISOR"] = metavisor

        return self

    def set_database(
        self,
        *,
        db_id: str,
        engine: str = "",
        config: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置数据库配置。
        """
        database = self._global_config.get("DATABASE")
        if not isinstance(database, dict):
            database = {}

        if db_id:
            database["db_id"] = db_id
        if engine:
            database["engine"] = engine
        if config:
            database["config"] = config
        for key, value in kwargs.items():
            database[key] = value

        if database:
            self._global_config["DATABASE"] = database

        return self

    def set_ontology(
        self,
        *,
        url: str = "",
        scene: str = "",
        **kwargs: Any,
    ) -> "AgentBuilder":
        """
        设置本体配置。

        可选字段:
            - url：本体服务地址（格式 x.x.x.x:port）
            - scene：本体场景
        """
        ontology = self._global_config.get("ONTOLOGY")
        if not isinstance(ontology, dict):
            ontology = {}

        if url:
            ontology["url"] = url
        if scene:
            ontology["scene"] = scene
        for key, value in kwargs.items():
            ontology[key] = value

        if ontology:
            self._global_config["ONTOLOGY"] = ontology

        return self

    def from_config(self, *, config: str | Path | dict) -> None:
        """从配置加载Agent。"""
        # 根据配置类型选择加载方式
        if isinstance(config, (str, Path)):
            self._load_from_path(config_path=config)
        elif isinstance(config, dict):
            self._load_from_dict(config_dict=config)
        else:
            raise TypeError("`config` must be str, Path, or dict.")

    async def build(self) -> "DataAgent":
        """异步构建Agent。"""
        from dataagent.interface.sdk.agent import DataAgent

        if self.config_path is None:
            self._load_from_dict(config_dict=self._global_config)
            if self.config_path is None:
                raise ValueError("No config was set.")
        agent = DataAgent.from_config(config=str(self.config_path))
        return agent

    def _load_from_dict(self, *, config_dict: dict) -> None:
        """根据 dict 类型的配置字典加载预制 Agent。"""
        # 1. 解析 agent_type
        agent_type = get_agent_type(
            config_dict,
            source="config_dict",
        )

        # 2. 将 config_dict 与预制示例合并，在 ~/.dataagent/.builder/output 下生成 merged_xxx_config.yaml
        yaml_path = get_final_yaml(
            agent_type=agent_type,
            config_dict=config_dict,
        )

        # 3. 复用 _load_from_path 加载生成的新 YAML 文件
        self._load_from_path(config_path=yaml_path, agent_type=agent_type)

    def _load_from_path(
        self,
        *,
        config_path: str | Path,
        agent_type: str | None = None,
    ) -> None:
        """根据 str | Path 类型的配置文件加载预制 Agent。"""
        if agent_type is None:
            # 因为 build 和 from_config 都可作为 L0 接口提供给外部，
            # 当外部未通过 dict 类型显式传入 agent_type 时，尝试从 YAML 配置文件中自动解析 agent_type
            path = Path(config_path)
            try:
                with path.open("r", encoding="utf-8") as f:
                    loaded_config = yaml.safe_load(f)
            except OSError as exc:
                raise ValueError(
                    f"Failed to load config file: {config_path}. Original error: {exc.__class__.__name__}: {exc}"
                ) from exc
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"Failed to parse YAML config file: {config_path}. Original error: {exc.__class__.__name__}: {exc}"
                ) from exc
            agent_type = get_agent_type(
                loaded_config,
                source=config_path,
            )
        self.config_path = Path(config_path)
