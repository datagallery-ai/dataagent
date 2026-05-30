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
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate


def execute_with_llm(suffix: str, context: dict[str, str]) -> str:
    """execute_with_llm"""
    llm = llm_manager.get_default_llm()
    system_prompt = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/perceptor/{suffix}_system"
    ).apply_prompt_template(**context)
    user_prompt = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/perceptor/{suffix}_user"
    ).apply_prompt_template(**context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    res = llm.invoke(messages).content.strip()
    return res if res != "N/A" else ""
