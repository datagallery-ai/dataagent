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

import asyncio
from typing import Any

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import NL2SQL_PROMPT_PREFIX


class BaseNL2SQLNode(BaseNode):
    def __init__(self, name: str, config_manager: Any | None = None, **kwargs: Any) -> None:
        """Initialize NL2SQL node with optional per-Agent ConfigManager.

        Args:
            name: Node name for prompts and routing.
            config_manager: Per-Agent configuration; required for DATABASE/SEMANTIC_LAYER reads.
            **kwargs: Remaining node-specific options (passed to :class:`BaseNode`).
        """
        super().__init__(name=name, **kwargs)
        self._config_manager = config_manager

    @property
    def db(self):
        return self._get_agent_config("DATABASE.db_id", "")

    @property
    def engine(self):
        return self._get_agent_config("DATABASE.engine", "sqlite")

    @property
    def sql_service_engine(self) -> str:
        svc = self._get_agent_config("DATABASE.sql_service_engine")
        return svc if svc else self.engine

    def execute_with_llm(self, context: dict[str, str], action: str = "") -> str:
        llm = llm_manager.get_default_llm()
        system_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}system"
        ).content
        user_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}user"
        ).apply_prompt_template(**context)
        prompts = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        return llm.invoke(prompts).content

    def _get_agent_config(self, key: str, default: Any = None) -> Any:
        """Read configuration from the bound per-Agent ConfigManager."""
        if self._config_manager is None:
            raise RuntimeError(
                f"NL2SQL node {self.name!r} has no config_manager; pass config_manager when constructing the node."
            )
        return self._config_manager.get(key, default)

    async def _aprocess(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        return await asyncio.to_thread(self._process, state, runtime)
