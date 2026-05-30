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
"""Internal tool execution context (not exposed in LLM tool schema)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dataagent.config.config_manager import ConfigManager
    from dataagent.core.cbb.runtime import Runtime


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Internal execution context injected by :class:`~dataagent.actions.tools.local.LocalToolWrapper`.

    This object is not part of the LLM-visible tool schema. Tools that need Agent YAML
    configuration should declare a keyword-only ``_tool_context`` parameter.

    ``tool_config`` carries the current tool instance's YAML ``config`` slice (e.g.
    ``llm_model`` / ``embedding_model``), merged at call time from
    :class:`~dataagent.actions.tools.local.LocalToolWrapper`.

    ``runtime`` is resolved at **call time** via
    :func:`~dataagent.core.framework_adapters.runtime.context.get_current_runtime` when the tool
    runs inside a Flex / workflow node. It is ``None`` for direct ``ToolManager.acall``,
    RAG service calls, or unit tests that construct ``ToolExecutionContext`` manually.
    When present, prefer ``runtime.config_manager`` and session fields
    (``user_id`` / ``session_id`` / ``run_id`` / ``sub_id``) over ad-hoc globals.
    """

    # 当前 agent 的完整 config_manager
    config_manager: ConfigManager | None = None
    # 获取当前工具在 yaml 中的 config 配置
    tool_config: dict[str, Any] = field(default_factory=dict)
    # 当前调用的 per-invocation Runtime（workflow 内由 LocalToolWrapper 注入）
    runtime: Runtime | None = None
