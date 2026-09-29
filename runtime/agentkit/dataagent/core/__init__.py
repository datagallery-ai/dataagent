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
"""deep agent 的包入口。

``create_deep_agent`` 和 ``DeepAgentState`` 来自已实现的 ``graph``。
版本是固定字符串，不读取安装元数据，也不在这里登记内置画像。
"""

from .graph import DeepAgentState, create_deep_agent
from .middleware.filesystem.filesystem import FilesystemMiddleware, FilesystemPermission, FsToolName
from .middleware.memory import MemoryMiddleware
from .middleware.rubric import RubricMiddleware
from .middleware.async_subagents import AsyncSubAgent, AsyncSubAgentMiddleware
from .middleware.subagents.subagents import CompiledSubAgent, SubAgent, SubAgentMiddleware
from .profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    HarnessProfileConfig,
    register_harness_profile,
)
from .profiles.provider.provider_profiles import ProviderProfile, register_provider_profile

# 与 graph 里写死的版本相同。不导入 _version，也不附加本地版本段。
__version__ = "0.1.0.dev0"

__all__ = [
    "AsyncSubAgent", "AsyncSubAgentMiddleware", "CompiledSubAgent", "DeepAgentState",
    "FilesystemMiddleware", "FilesystemPermission", "FsToolName", "GeneralPurposeSubagentProfile",
    "HarnessProfile", "HarnessProfileConfig", "MemoryMiddleware", "ProviderProfile",
    "RubricMiddleware", "SubAgent", "SubAgentMiddleware", "__version__",
    "create_deep_agent", "register_harness_profile", "register_provider_profile",
]
