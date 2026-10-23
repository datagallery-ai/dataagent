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
"""Data-engineering Suite checks for the paired DataTaskIR tool hooks."""

from collections.abc import Mapping
from typing import Any

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState


def validate_data_task_ir_config(state: FlexState, runtime: Runtime) -> FlexState:
    """Reject an incomplete IR switch before the Suite checks metadata or plans.

    Runs at the beginning of each Agent turn, rather than during config loading.
    Raising stops this turn; the terminal chat loop may still accept new input.
    """
    validate_data_task_ir_hooks(runtime.get_all_config())
    return state


def validate_data_task_ir_hooks(config: Mapping[str, Any]) -> None:
    """Check effective tool registrations after default/Suite/user merging.

    Builtin entries are registered first and local_functions may replace them,
    as in ToolManager. Consumer hooks alone do not enable DataTaskIR.
    """
    tools = config.get("TOOLS")
    if not isinstance(tools, Mapping):
        return

    effective_tools: dict[str, Mapping[str, Any]] = {}
    replaces = tools.get("_replaces") or []
    for section in ("builtin", "local_functions"):
        entries = tools.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping) or not entry.get("module"):
                continue
            name = entry.get("function") or entry.get("name")
            if not name or (section == "builtin" and name in replaces):
                continue
            effective_tools[str(name)] = entry

    prefix = "dataagent.actions.tools.hooks.examples.ir_hooks."
    required = {prefix + "create_ir", prefix + "update_ir"}
    configured: set[str] = set()
    for entry in effective_tools.values():
        hooks = entry.get("hooks")
        if not isinstance(hooks, Mapping):
            continue
        for phase in ("pre", "post"):
            items = hooks.get(phase)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, str) or item.strip() not in required:
                    continue
                if phase != "post":
                    raise ValueError("DataTaskIR 配置错误：create_ir 和 update_ir 必须配置在工具 hooks.post 中。")
                configured.add(item.strip())
    if configured and configured != required:
        missing = ", ".join(sorted(spec.rsplit(".", 1)[-1] for spec in required - configured))
        raise ValueError(
            "DataTaskIR 配置错误：create_ir 和 update_ir 必须同时配置或同时关闭；"
            f"当前缺少 {missing}。请检查合并后的 TOOLS.builtin / TOOLS.local_functions 工具 hooks.post 配置。"
        )
