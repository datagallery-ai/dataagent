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
from typing import Any, cast

from loguru import logger

from dataagent.core.managers.llm_manager.adapters import ChatModel, LangChainChatModelAdapter
from dataagent.core.managers.llm_manager.llm_client import LLMClient
from dataagent.core.managers.llm_manager.llm_config import LLMConfig


class LLMManager:
    _instance = None

    def __new__(cls):
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.llm_cache: dict[str, dict] = {}
            self._initialized = True

    def init_from_config(self, config: dict[str, Any]):
        """初始化管理器"""
        logger.trace("=== Initializing LLM Manager 🛠️ ===")
        # 遍历 MODEL 配置时，同时保留 section 名称
        for section_name, llm_dict in (config.get("MODEL", {}) or {}).items():
            if not isinstance(llm_dict, dict):
                continue
            cfg = dict(llm_dict or {})
            cfg.setdefault("section", section_name)
            # 如果 name 不存在，则设置为 section_name，向后兼容
            cfg.setdefault("name", section_name)

            llm_cfg = LLMConfig.from_dict(cfg)
            self.create_llm(llm_cfg)

    def create_llm(self, config: LLMConfig | dict[str, Any]) -> ChatModel | None:
        """根据配置创建 LLM 实例并缓存；embedding 仅注册配置，不构造 LLMClient。"""
        if isinstance(config, dict):
            config = LLMConfig.from_dict(config)
        if config.name in self.llm_cache:
            logger.warning(f"⚠️ '{config.name}' model configuration already exists, will be overwritten.")
        if config.model_type == "embedding":
            self.llm_cache[config.name] = {"llm_config": config, "llm_instance": None}
            logger.trace(f"✅ Embedding config for {config.name} (section={config.section}) registered.")
            return None
        try:
            raw_instance = LLMClient.from_llm_config(config)
            llm_instance: ChatModel = LangChainChatModelAdapter(raw_instance, config)
            self.llm_cache[config.name] = {"llm_config": config, "llm_instance": llm_instance}
            logger.trace(f"✅ LLM instance for {config.name} (section={config.section}) created.")
            return llm_instance
        except Exception as e:
            raise ValueError(f"❌ Creating {config.model_type} model instance failed.") from e

    def get_llm_config(self, name: str) -> LLMConfig | None:
        """根据名称获取缓存的LLM配置"""
        rec = self.llm_cache.get(name, {})
        return rec.get("llm_config", None)

    def get_llm(self, name: str) -> ChatModel | None:
        """根据名称获取缓存的LLM实例"""
        rec = self.llm_cache.get(name, {})
        return rec.get("llm_instance", None)

    def delete_llm(self, name: str) -> bool:
        """移除指定名称和类型的LLM实例"""
        if name in self.llm_cache:
            del self.llm_cache[name]
            return True
        return False

    def list_llms(self) -> list[str]:
        """获取所有缓存的LLM实例名称列表"""
        return list(self.llm_cache.keys())

    def register_llm(self, name: str, llm: ChatModel):
        """注册LLM实例"""
        if name in self.llm_cache:
            logger.warning(f"⚠️ '{name}' model already exists, will be overwritten.")
        self.llm_cache[name] = {"llm_config": {}, "llm_instance": llm}
        logger.debug(f"✅ '{name}' model registered.")

    def get_default_llm(self) -> ChatModel:
        """获取默认 Chat LLM"""
        from dataagent.core.framework_adapters.runtime.context import get_current_runtime

        rt = get_current_runtime()
        if rt is not None:
            llm_getter = getattr(rt, "llm", None)
            if callable(llm_getter):
                try:
                    return cast(ChatModel, llm_getter("planner"))
                except RuntimeError:
                    logger.debug("runtime.llm('planner') unavailable, fallback to cached chat llm")
        for rec in self.llm_cache.values():
            llm_config, llm_instance = rec["llm_config"], rec["llm_instance"]
            if llm_config.model_type == "chat" and llm_instance is not None:
                return llm_instance
        raise RuntimeError("Default LLM not found.")
