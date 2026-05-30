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
"""dataagent 包入口。

注意：这里保持轻量，避免在 import dataagent 时就触发大量依赖初始化/循环导入。
需要 DataAgent 等对象时，通过属性懒加载获取。
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "DataAgent Team"

from typing import Any


def __getattr__(name: str) -> Any:
    if name in ("DataAgent", "AgentBuilder", "load_agent_from_config", "BaseDataAgent"):
        from dataagent.interface.sdk import AgentBuilder, BaseDataAgent, DataAgent, load_agent_from_config

        return {
            "DataAgent": DataAgent,
            "AgentBuilder": AgentBuilder,
            "BaseDataAgent": BaseDataAgent,
            "load_agent_from_config": load_agent_from_config,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
