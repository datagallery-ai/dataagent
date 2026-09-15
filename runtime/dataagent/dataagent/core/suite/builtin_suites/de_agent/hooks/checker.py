# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Example hooks for the ``example_suite`` reference Suite.

Hook modules are loaded via single-file import (``import_callable_from_suite_root``).
Do **not** use package-relative imports such as ``from .common import ...`` or
``from hooks.common import ...``; keep helpers in this file or use absolute imports
(e.g. ``dataagent.*``).
"""

import requests
from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState


def check_metadata(state: FlexState, runtime: Runtime) -> FlexState:
    """
    Check metadata connection at the beginning of agent.
    """
    try:
        client = SemanticServiceClient.from_config(runtime.config_manager)
        _ = client.list_retrieval_tables()
    except (requests.RequestException, ValueError) as err:
        logger.error(f"语义层连接失败：{err}")
        state["messages"].append(AIMessage("语义层连接失败，Agent退出。"))
        state["complete"] = True

    return state
