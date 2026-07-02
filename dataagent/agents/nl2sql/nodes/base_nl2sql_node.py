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
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import NL2SQL_PROMPT_PREFIX, _TZ_CN
from dataagent.utils.env_utils import get_env_bool
from dataagent.utils.log import logger

_TYPE_LABELS = {
    SystemMessage: "SYSTEM",
    HumanMessage: "HUMAN",
    AIMessage: "AI",
    ToolMessage: "TOOL",
}

_SCHEMA_NODES = {"generator", "validator", "selector"}

_NL2SQL_COMMON_SYSTEM_KEY = f"{NL2SQL_PROMPT_PREFIX}/nl2sql_common_system"





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
        self._nl2sql_context_dump_dir: Path | None = None
        self._context_dump_seq: list[int] = [0]
        self._context_dump_enabled: bool = get_env_bool("DATAAGENT_CONTEXT_DUMP")
        self._common_system_cache: str | None = None

    def set_context_dump_dir(self, dump_dir: Any | None) -> None:
        if dump_dir is not None:
            self._nl2sql_context_dump_dir = Path(dump_dir)
        else:
            self._nl2sql_context_dump_dir = None

    def _dump_llm_context(self, system_prompt: str, user_prompt: str, result: str, node_name: str, action: str) -> None:
        if not self._context_dump_enabled:
            return
        if self._nl2sql_context_dump_dir is None:
            return
        try:
            self._context_dump_seq[0] += 1
            seq = self._context_dump_seq[0]
            label = f"{node_name}_{action}" if action else node_name
            dump_file = self._nl2sql_context_dump_dir / f"{seq:02d}_round_{label}.txt"
            separator = "=" * 80
            ts = datetime.now(tz=_TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
            with dump_file.open("w", encoding="utf-8") as f:
                f.write(f"{separator}\n")
                f.write(f"  NL2SQL Prompt Dump  |  {ts}  |  node: {label}\n")
                f.write(f"{separator}\n\n")
                f.write("--- [0] SYSTEM ---\n")
                f.write(f"{system_prompt}\n\n")
                f.write("--- [1] HUMAN ---\n")
                f.write(f"{user_prompt}\n\n")
                f.write("--- [2] AI ---\n")
                f.write(f"{result}\n\n")
                f.write(f"{separator}\n")
                f.write("  END OF DUMP\n")
                f.write(f"{separator}\n")
            logger.info(f"NL2SQL context dump saved: {seq:02d}_round_{label}.txt")
        except Exception as exc:
            self._context_dump_seq[0] -= 1
            logger.warning(f"Failed to dump NL2SQL context: {exc}")

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

    def _get_common_system(self) -> str:
        if self._common_system_cache is None:
            self._common_system_cache = PromptTemplate.from_package_relative(
                _NL2SQL_COMMON_SYSTEM_KEY
            ).content
        return self._common_system_cache

    def execute_with_llm(self, context: dict[str, str], action: str = "") -> str:
        llm = llm_manager.get_default_llm()
        node_system = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}system"
        ).content
        user_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}user"
        ).apply_prompt_template(**context)

        schema_str = context.get("schema", "")

        prompts = [
            {"role": "system", "content": node_system},
            {"role": "user", "content": user_prompt},
        ]
        system_prompt = node_system
        full_user = user_prompt

        response = llm.invoke(prompts)
        content = response.content
        self._dump_llm_context(system_prompt, full_user, content, self.name, action)
        return content

    def _get_agent_config(self, key: str, default: Any = None) -> Any:
        if self._config_manager is None:
            raise RuntimeError(
                f"NL2SQL node {self.name!r} has no config_manager; pass config_manager when constructing the node."
            )
        return self._config_manager.get(key, default)

    async def _aprocess(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        return await asyncio.to_thread(self._process, state, runtime)
