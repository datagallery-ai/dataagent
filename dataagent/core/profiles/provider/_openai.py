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
"""给 OpenAI 这个 provider 登记默认的模型构造参数。

键是 ``openai``。唯一的参数是打开 Responses API。
这里没有给模型读的提示词。导入时不登记。
"""

from dataagent.core.profiles.provider.provider_profiles import ProviderProfile as ModelInitProfile
from dataagent.core.profiles.provider.provider_profiles import (
    _register_provider_profile_impl as publish,
)


def register() -> None:
    """让 OpenAI 模型默认走 Responses API。"""
    settings = {"use_responses_api": True}
    profile = ModelInitProfile(init_kwargs=settings)
    publish("openai", profile)
