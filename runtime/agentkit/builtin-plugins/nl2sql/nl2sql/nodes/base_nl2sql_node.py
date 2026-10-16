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
"""Base class for nl2sql graph nodes (langgraph-native, no legacy BaseNode).

The chat model is bound by the graph factory (resolved from the host
configuration), never created here: nodes stay free of model construction and
global state. The template-method hook machinery of the legacy ``dataagent``
BaseNode is replaced by :meth:`BaseNL2SQLNode.run`, which is registered
directly as a langgraph graph node.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from nl2sql.config_manager import NL2SQLConfig
from nl2sql.constants import NL2SQL_PROMPT_PREFIX
from nl2sql.errors import DataAgentError
from nl2sql.prompt_manager import PromptTemplate
from nl2sql.utils.nl2sql_utils import json_parser


class BaseNL2SQLNode:
    """One node of the nl2sql workflow; subclass and override ``_aprocess``."""

    def __init__(self, name: str, config: NL2SQLConfig, **kwargs: Any) -> None:
        """Initialize an nl2sql node.

        Args:
            name: Node name for prompts and routing.
            config: Validated plugin parameters (``plugins.config.nl2sql``).
            **kwargs: Remaining node-specific options (exposed as ``self.config``).
        """
        self.name = name
        self.config = kwargs
        self._config = config
        self._model: BaseChatModel | None = None  # noqa: UP045

    def bind_model(self, model: BaseChatModel) -> None:
        """Bind the chat model resolved by the host factory."""
        self._model = model

    @property
    def model(self) -> BaseChatModel:
        """The bound chat model; the graph factory must have bound one."""
        if self._model is None:
            raise RuntimeError(
                f"NL2SQL node {self.name!r} has no model; the graph factory must bind one."
            )
        return self._model

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """Execute the node and return the state update for langgraph.

        ``_aprocess`` returns the full (mutated) state; ``messages`` is stripped
        because nl2sql nodes never produce messages and the ``add_messages``
        reducer must not re-append them.
        """
        result = await self._aprocess(state)
        return {key: value for key, value in dict(result).items() if key != "messages"}

    @property
    def db(self):
        """Return the configured database.db_id."""
        return self._config.get("database.db_id", "")

    @property
    def dialect(self):
        """Return the configured database.dialect (default sqlite)."""
        return self._config.get("database.dialect", "sqlite")

    @property
    def engine(self):
        """Return database.engine, falling back to dialect."""
        return self._config.get("database.engine") or self.dialect

    async def execute_with_llm(self, context: dict[str, str], action: str = "") -> str:
        """Render the node prompts and asynchronously invoke the bound model."""
        system_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}system"
        ).content
        user_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/{self.name}/{action}user"
        ).apply_prompt_template(**context)
        response = await self.model.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
        return response.content

    async def execute_with_llm_json(self, context: dict[str, str], action: str = "") -> Any:
        """Execute the model with the given context and action, returning parsed JSON output."""
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

    async def _aprocess(self, state: dict[str, Any]) -> dict[str, Any]:
        """Override this in subclasses to implement the node's work."""
        raise NotImplementedError("The async process is not implemented")
