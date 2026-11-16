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
"""DataTaskIR configuration and private state in the native AgentContext."""

from typing import Any

from dataagent import AgentContext


def ir_options(context: AgentContext) -> dict:
    return context.get_plugin_config("de-agent").get("data_task_ir", {})


def ir_enabled(context: AgentContext) -> bool:
    return ir_options(context).get("enabled", False) is True


def ir_state(context: AgentContext) -> dict[str, Any]:
    return context.get_plugin_state("de-agent").setdefault("data_task_ir", {})
