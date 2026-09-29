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
"""画像包的公开入口。

厂商画像管模型怎么构造，运行时画像管装配之后的提示、工具和中间件。
两边的类型和登记函数从各自的模块再导出。导入本包不会去加载内置画像。
"""

from .harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    HarnessProfileConfig,
    register_harness_profile,
)
from .provider.provider_profiles import ProviderProfile, register_provider_profile

__all__ = [
    "GeneralPurposeSubagentProfile", "HarnessProfile", "HarnessProfileConfig", "ProviderProfile",
    "register_harness_profile", "register_provider_profile",
]
