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
"""prompt_manager 包：仅暴露通用 prompt 装配能力。

公共 API：

- :class:`PromptTemplate` —— Jinja2 模板载体（仅 ``content``）；包内 md 见
  :meth:`PromptTemplate.from_package_relative`
- :data:`PROMPT_MD_PREFIX` —— 内置 ``*.md`` 相对 ``dataagent`` 包根的路径前缀

yaml ``prompt_template`` 配置解析见 :func:`dataagent.config.config_manager.build_prompt`；
planner 业务相关的 skill 选择 / planner system+user 组装见
``dataagent.core.flex.utils.planner_prompt_builder``。
"""

from dataagent.core.managers.prompt_manager.template import PROMPT_MD_PREFIX, PromptTemplate

__all__ = [
    "PROMPT_MD_PREFIX",
    "PromptTemplate",
]
