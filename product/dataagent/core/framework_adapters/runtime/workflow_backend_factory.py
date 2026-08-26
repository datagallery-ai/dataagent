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
from __future__ import annotations

from typing import Any

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_router import BaseRouter
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.framework_adapters.runtime.workflow import LangGraphWorkflow
from dataagent.core.framework_adapters.runtime.workflow_backend import (
    LangGraphWorkflowBackend,
    OpenJiuWenWorkflowBackend,
    WorkflowBackend,
)


def create_workflow_backend(
    *,
    backend: str,
    nodes: list[BaseNode],
    router: BaseRouter,
    state_class: type[BaseState] | None = None,
    config: Any | None = None,
) -> WorkflowBackend:
    """
    Core 层统一 backend 工厂。

    - Flow/Flex 只通过该工厂选择 backend，避免在 domain 层散落框架差异。
    - `config` 用于 openjiuwen 的 checkpoint DSN 等配置读取。
    """
    b = (backend or "langgraph").lower()
    if b == "langgraph":
        wf = LangGraphWorkflow(nodes=nodes, router=router, state_class=state_class or BaseState)
        return LangGraphWorkflowBackend(wf)

    if b == "openjiuwen":
        from dataagent.core.framework_adapters.runtime.workflow_openjiuwen import OpenJiuWenWorkflow

        wf = OpenJiuWenWorkflow(nodes=nodes, router=router, state_class=state_class, config=config)
        return OpenJiuWenWorkflowBackend(wf)

    raise ValueError(f"Unsupported backend: {backend}")
