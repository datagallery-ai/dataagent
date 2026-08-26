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

from dataagent.interface.sdk.agent import DataAgent


def load_agent_from_config(config_source: str | Path | dict) -> DataAgent:
    """从配置加载Agent

    Args:
        config_source: 配置文件路径或配置字典

    Returns:
        DataAgent实例
    """
    if isinstance(config_source, (str, Path)):
        return DataAgent.from_config(config_source)
    if isinstance(config_source, dict):
        # 为保持行为一致，优先走 from_config_dict（如果你后续需要支持 dict 直传，再补齐）
        raise NotImplementedError("当前仅支持从 YAML 路径加载，请使用 load_agent_from_config(path)。")
    raise ValueError("config_source must be a file path or dictionary")
