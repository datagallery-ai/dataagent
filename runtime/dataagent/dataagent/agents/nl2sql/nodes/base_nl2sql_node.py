"""Shared native node base for the NL2SQL LangGraph."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.core.errors import DataAgentError
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import NL2SQL_PROMPT_PREFIX, TZ_CN
from dataagent.utils.env_utils import get_env_bool
from dataagent.utils.log import logger


class BaseNL2SQLNode:
    """Run an NL2SQL stage with an injected LangChain chat model and effective config."""

    def __init__(
        self,
        name: str,
        model: BaseChatModel | None = None,
        agent_config: Mapping[str, Any] | None = None,
        config_manager: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self._model = model
        self.agent_config = agent_config or {}
        self._compat_config_manager = config_manager
        self.config = kwargs
        self._nl2sql_context_dump_dir: Path | None = None
        self._context_dump_seq: list[int] = [0]
        self._context_dump_enabled = get_env_bool("DATAAGENT_CONTEXT_DUMP")

    @property
    def db(self) -> str:
        """Return the configured ``DATABASE.db_id``."""
        return str(self._get_agent_config("DATABASE.db_id", "") or "")

    @property
    def dialect(self) -> str:
        """Return the configured ``DATABASE.dialect``."""
        return str(self._get_agent_config("DATABASE.dialect", "sqlite") or "sqlite")

    @property
    def engine(self) -> str:
        """Return ``DATABASE.engine``, falling back to the SQL dialect."""
        return str(self._get_agent_config("DATABASE.engine") or self.dialect)

    @property
    def model(self) -> BaseChatModel:
        """Return the injected model or fail when a model-backed operation is attempted."""
        if self._model is None:
            raise RuntimeError(f"NL2SQL node {self.name!r} requires an injected BaseChatModel for model calls.")
        return self._model

    def get(self, key: str, default: Any = None) -> Any:
        """Expose ConfigManager-compatible dotted lookup to dependent clients."""
        return self._get_agent_config(key, default)

    def set_context_dump_dir(self, dump_dir: Any | None) -> None:
        """Set or clear this node's optional prompt-dump directory."""
        self._nl2sql_context_dump_dir = Path(dump_dir) if dump_dir is not None else None

    async def aprocess(self, state: NL2SQLState, runtime: Any = None) -> dict[str, Any]:
        """Execute the native asynchronous node and omit reducer-owned public fields from its delta."""
        result = await self._aprocess(state, runtime)
        return {key: value for key, value in result.items() if key not in {"messages", "files", "structured_response"}}

    async def execute_with_llm(self, context: dict[str, Any], action: str = "") -> str:
        """Render this node's prompts and invoke the injected LangChain chat model."""
        system_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}system"
        ).content
        user_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}user"
        ).apply_prompt_template(**context)
        response = await self.model.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
        content = response.content
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        self._dump_llm_context(system_prompt, user_prompt, text, self.name, action)
        return text

    async def execute_with_llm_json(self, context: dict[str, Any], action: str = "") -> Any:
        """Invoke the model and parse the JSON content required by the NL2SQL algorithm."""
        for attempt in range(3):
            try:
                return json.loads(json_parser(await self.execute_with_llm(context, action)))
            except (DataAgentError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    if isinstance(exc, DataAgentError):
                        raise
                    raise DataAgentError(
                        source="internal",
                        fact=f"Model output format error: JSON parsing failed after 3 attempts: {exc}",
                        component="nl2sql",
                    ) from exc
        return None

    async def _aprocess(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        raise NotImplementedError

    def _dump_llm_context(
        self,
        system_prompt: str,
        user_prompt: str,
        result: str,
        node_name: str,
        action: str,
    ) -> None:
        if not self._context_dump_enabled or self._nl2sql_context_dump_dir is None:
            return
        try:
            self._context_dump_seq[0] += 1
            sequence = self._context_dump_seq[0]
            label = f"{node_name}_{action}" if action else node_name
            dump_file = self._nl2sql_context_dump_dir / f"{sequence:02d}_round_{label}.txt"
            separator = "=" * 80
            timestamp = datetime.now(tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
            dump_file.parent.mkdir(parents=True, exist_ok=True)
            dump_file.write_text(
                f"{separator}\n  NL2SQL Prompt Dump  |  {timestamp}  |  node: {label}\n{separator}\n\n"
                f"--- [0] SYSTEM ---\n{system_prompt}\n\n--- [1] HUMAN ---\n{user_prompt}\n\n"
                f"--- [2] AI ---\n{result}\n\n{separator}\n  END OF DUMP\n{separator}\n",
                encoding="utf-8",
            )
            logger.info("NL2SQL context dump saved: {:02d}_round_{}.txt", sequence, label)
        except Exception as exc:
            self._context_dump_seq[0] -= 1
            logger.warning("Failed to dump NL2SQL context: {}", exc)

    def _get_agent_config(self, key: str, default: Any = None) -> Any:
        value: Any = self.agent_config
        missing = object()
        for segment in key.split("."):
            if not isinstance(value, Mapping):
                return default
            value = value.get(segment, missing)
            if value is missing:
                manager_get = getattr(self._compat_config_manager, "get", None)
                return manager_get(key, default) if callable(manager_get) else default
        return value
