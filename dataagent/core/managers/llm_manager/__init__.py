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
__all__ = [
    "create_llm",
    "get_llm",
    "create_llm_for_node",
    "LLMManager",
    "LLMConfig",
    "llm_manager",
    "ChatModel",
    "LLMResponse",
]


from dataagent.core.managers.llm_manager.adapters import ChatModel, LLMResponse
from dataagent.core.managers.llm_manager.llm_config import LLMConfig
from dataagent.core.managers.llm_manager.llm_manager import LLMManager

llm_manager = LLMManager()


def create_llm(config):
    """创建LLM实例"""
    return llm_manager.create_llm(config)


def get_llm(name: str):
    """获取已注册的LLM实例"""
    return llm_manager.get_llm(name)


def get_llm_config(name: str) -> LLMConfig | None:
    """获取LLM配置"""
    return llm_manager.get_llm_config(name)


def create_llm_for_node(node_config: dict):
    """为节点创建LLM实例"""
    llm_config = LLMConfig.from_dict(node_config.get("chat_model", {}))
    return create_llm(llm_config)
