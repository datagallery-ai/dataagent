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
"""厂商画像包的公开入口。

构造参数、查找和登记都定义在 ``provider_profiles``。这里只把那四个名字再导出。
"""

from .provider_profiles import ProviderProfile, apply_provider_profile, get_provider_profile, register_provider_profile

__all__ = [
    "ProviderProfile", "apply_provider_profile", "get_provider_profile", "register_provider_profile",
]
