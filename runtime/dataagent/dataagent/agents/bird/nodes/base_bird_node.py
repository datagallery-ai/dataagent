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
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from dataagent.agents.bird.constants import BIRD_PROMPT_PREFIX
from dataagent.agents.bird.context_dump import current_context_dump
from dataagent.agents.bird.errors import LLMOutputParseError
from dataagent.agents.bird.utils.bird_utils import json_parser
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.errors import DataAgentError
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import TZ_CN
from dataagent.utils.env_utils import get_env_bool
from dataagent.utils.log import logger

_TYPE_LABELS = {
    SystemMessage: "SYSTEM",
    HumanMessage: "HUMAN",
    AIMessage: "AI",
    ToolMessage: "TOOL",
}


class BaseBirdNode(BaseNode):
    def __init__(self, name: str, config_manager: Any | None = None, **kwargs: Any) -> None:
        """Initialize BIRD node with optional per-Agent ConfigManager.

        Args:
            name: Node name for prompts and routing.
            config_manager: Per-Agent configuration; required for DATABASE/SEMANTIC_LAYER reads.
            **kwargs: Remaining node-specific options (passed to :class:`BaseNode`).
        """
        self._allow_unfenced_json = bool(kwargs.pop("allow_unfenced_json", False))
        super().__init__(name=name, **kwargs)
        self._config_manager = config_manager
        self._llm_limiter: asyncio.Semaphore | None = None
        self._context_dump_enabled: bool = get_env_bool("DATAAGENT_CONTEXT_DUMP")

    @property
    def db(self):
        """Return the configured DATABASE.db_id."""
        return self._get_agent_config("DATABASE.db_id", "")

    @property
    def dialect(self):
        """Return the configured DATABASE.dialect (default sqlite)."""
        return self._get_agent_config("DATABASE.dialect", "sqlite")

    @property
    def engine(self):
        """Return DATABASE.engine, falling back to dialect."""
        return self._get_agent_config("DATABASE.engine") or self.dialect

    async def _invoke_llm(self, prompts: list[dict[str, str]], **kwargs: Any) -> Any:
        """Share the owning BirdAgent's optional limit, including HTTP retries."""
        llm = llm_manager.get_default_llm()
        if self._llm_limiter is None:
            return await llm.ainvoke(prompts, **kwargs)
        async with self._llm_limiter:
            return await llm.ainvoke(prompts, **kwargs)

    async def execute_with_llm(self, context: dict[str, str], action: str = "", **llm_kwargs: Any) -> str:
        """Render the node prompts and asynchronously invoke the configured LLM."""
        system_prompt = self._get_prompt_template(f"{BIRD_PROMPT_PREFIX}/{self.name}/{action}system").content
        user_prompt = self._get_prompt_template(f"{BIRD_PROMPT_PREFIX}/{self.name}/{action}user").apply_prompt_template(
            **context
        )
        prompts = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        response = await self._invoke_llm(prompts, **llm_kwargs)
        content = response.content
        self._dump_llm_context(system_prompt, user_prompt, content, self.name, action)
        return content

    async def execute_with_llm_json(
        self,
        context: dict[str, str],
        action: str = "",
        *,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str | None = None,
        response_format_type: str = "json_schema",
        json_loader: Callable[[str], Any] = json.loads,
        json_validator: Callable[[Any], Any] | None = None,
    ) -> Any:
        """Execute the LLM with the given context and action, returning parsed JSON output."""
        llm_kwargs: dict[str, Any] = {}
        if response_schema is not None:
            if not response_schema_name:
                raise ValueError("response_schema_name is required when response_schema is set")
            if response_format_type == "json_object":
                llm_kwargs["response_format"] = {"type": "json_object"}
            elif response_format_type == "json_schema":
                llm_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_schema_name,
                        "strict": True,
                        "schema": response_schema,
                    },
                }
            else:
                raise ValueError(f"Unsupported response_format_type: {response_format_type}")
        for attempt in range(3):
            try:
                if llm_kwargs:
                    content = await self.execute_with_llm(context, action, **llm_kwargs)
                else:
                    content = await self.execute_with_llm(context, action)
                parsed = json_loader(json_parser(content, allow_unfenced=self._allow_unfenced_json))
                return json_validator(parsed) if json_validator else parsed
            except (LLMOutputParseError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise DataAgentError(
                        source="internal",
                        fact=f"Model output format error: JSON parsing failed after 3 attempts: {exc}",
                        component="bird",
                    ) from exc
        return None

    @staticmethod
    def _get_prompt_template(reference: str) -> PromptTemplate:
        """Resolve a package-owned BIRD prompt."""
        return PromptTemplate.from_package_relative(reference)

    def _dump_llm_context(self, system_prompt: str, user_prompt: str, result: str, node_name: str, action: str) -> None:
        """Persist the (system, user, AI) prompt triple to the node's context-dump file."""
        if not self._context_dump_enabled:
            return
        dump = current_context_dump()
        if dump is None or not dump.active:
            return
        try:
            dump.sequence += 1
            seq = dump.sequence
            label = f"{node_name}_{action}" if action else node_name
            dump_file = (dump.directory / f"{seq:02d}_round_{label}.txt").resolve()
            if not dump_file.is_relative_to(dump.directory):
                raise ValueError("Bird context dump file must remain inside its request directory.")
            separator = "=" * 80
            ts = datetime.now(tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
            with dump_file.open("w", encoding="utf-8") as f:
                f.write(f"{separator}\n")
                f.write(f"  BIRD Prompt Dump  |  {ts}  |  node: {label}\n")
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
            logger.info(f"BIRD context dump saved: {seq:02d}_round_{label}.txt")
        except Exception as exc:
            dump.sequence -= 1
            logger.warning(f"Failed to dump BIRD context: {exc}")

    def _get_agent_config(self, key: str, default: Any = None) -> Any:
        """Read configuration from the bound per-Agent ConfigManager."""
        if self._config_manager is None:
            raise RuntimeError(
                f"BIRD node {self.name!r} has no config_manager; pass config_manager when constructing the node."
            )
        return self._config_manager.get(key, default)
