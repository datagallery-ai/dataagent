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
from typing import Literal


class LLMConfig:
    """维护管理LLM的配置"""

    def __init__(
        self,
        name: str,
        provider: str,
        model_type: Literal["chat", "embedding"],
        section: str | None = None,
        **client_kwargs,
    ):
        """
        初始化LLM配置

        Args:
            name: config唯一标识名称，用于注册、缓存llm实例
            provider: 提供商名称
            model_type: 模型类型，支持"chat"或"embedding"
            section: 对应 YAML 中 MODEL 下的 key，由 LLMManager 在 init_from_config 时通过 section 字段注入。
            **client_kwargs: 实例化LLM实例时，传入给官方API的参数（如model、api_key、base_url等）
        """
        self.name = name
        self.provider = provider
        self.model_type = model_type
        if self.model_type not in ["chat", "embedding"]:
            raise ValueError(f"不支持的模型类型: {type}，支持的类型为: chat, embedding")
        self.section = section or name

        # 所有其他参数都存储在 extra_params 字典中
        self.client_kwargs = client_kwargs.copy()

    def __repr__(self) -> str:
        params_dict = self.to_dict()
        params_str = ", ".join(f"{k}={repr(v)}" for k, v in params_dict.items())
        return f"LLMConfig({params_str})"

    def __str__(self) -> str:
        return self.__repr__()

    @classmethod
    def from_dict(cls, config_dict: dict) -> "LLMConfig":
        """从字典创建LLMConfig实例"""
        config = config_dict.copy()
        if not all(k in config for k in ["name", "provider", "model_type"]):
            raise ValueError("name, provider, model_type参数是必需的")
        return cls(**config)

    def to_dict(self) -> dict:
        """获取所有参数（必需参数+额外参数）"""
        result = {
            "name": self.name,
            "provider": self.provider,
            "model_type": self.model_type,
        }
        result.update(self.client_params())
        return result

    def client_params(self) -> dict:
        """获取实例化LLM的参数"""

        return self.client_kwargs["params"]

    def create_llm(self):
        """创建LLM实例 - 兼容旧接口"""
        from dataagent.core.managers.llm_manager.llm_manager import LLMManager

        return LLMManager().create_llm(self)
