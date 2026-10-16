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
"""Compiled-subagent entrypoint: plugin parameters plus host services → nl2sql Runnable.

The host compiles ``subagents/nl2sql.json`` (``kind="compiled"``), calls
``build_subagent(params, services)`` with the Agent configuration's
``plugins.config.nl2sql`` mapping and host services (``services.get_model``
resolves model aliases; ``services.semantic_client`` is the host-owned
semantic-service client), and passes the returned Runnable to the core as a
native sub-agent. The Runnable adapts the SubAgent task protocol:

- Input: the isolated task state whose ``messages`` carries the task
  description as a single human message; it becomes the workflow ``question``.
- Output: a single ``AIMessage`` holding the final state as JSON. Only the
  ``messages`` key is returned — anything else would leak workflow channels
  into the parent agent state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from nl2sql.config_manager import NL2SQLConfig
from nl2sql.constants import DEFAULT_WORKFLOW_RECURSION_LIMIT
from nl2sql.errors import DataAgentError
from nl2sql.graph import build_nl2sql_graph, state_defaults
from nl2sql.state import get_default_state
from nl2sql.utils.sql_service import check_engine_driver


class _CallCounter:
    """Mutable per-task budget: langgraph runs nodes in copied task contexts,
    so only mutation (not ContextVar rebinding) is visible across node boundaries."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.budget:
            raise DataAgentError(
                source="llm",
                component="nl2sql",
                fact=f"模型调用预算已用尽：单次任务最多 {self.budget} 次",
            )
        self.used += 1


_current_counter: ContextVar[_CallCounter | None] = ContextVar("nl2sql_call_counter", default=None)
"""The current task's counter; reset by each run_task invocation."""


class _BudgetedModel:
    """Delegate to the host model, spending the current task's call budget."""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def ainvoke(self, messages, config=None, **kwargs):
        counter = _current_counter.get()
        if counter is not None:
            counter.spend()
        return await self._model.ainvoke(messages, config, **kwargs)


def _message_text(message: Any) -> str:
    """Extract the text of one chat message (string or content-block content)."""
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return str(content).strip()


def _question_from_task(state: Mapping[str, Any]) -> str:
    """The task question: the last non-empty human message of the task state."""
    texts = [
        text
        for message in state.get("messages") or []
        if isinstance(message, HumanMessage) and (text := _message_text(message))
    ]
    if not texts:
        raise DataAgentError(
            source="config",
            component="nl2sql",
            fact="任务输入缺少自然语言问题（messages 中没有用户消息）",
        )
    return texts[-1]


def _task_reply(final: Mapping[str, Any]) -> dict[str, Any]:
    """The SubAgent result: only ``messages`` with one JSON AIMessage."""
    payload = {
        "sql": final.get("sql", ""),
        "confidence": final.get("confidence", 0.0),
        "columns": final.get("columns"),
        "rows_preview": [list(row) for row in (final.get("rows_preview") or [])],
        "row_count": final.get("row_count", 0),
        "total_known": final.get("total_known", False),
        "truncated": final.get("truncated", False),
        "issues": final.get("issues") or [],
        "error": final.get("error"),
    }
    return {"messages": [AIMessage(content=json.dumps(payload, ensure_ascii=False, default=str))]}


def _error_reply(exc: Exception) -> dict[str, Any]:
    """Report a failed task as a JSON AIMessage instead of raising past the parent."""
    if isinstance(exc, DataAgentError):
        payload = {"error": exc.fact, "source": exc.source, "component": exc.component}
    else:
        payload = {"error": f"nl2sql task failed: {exc}"}
    return {"messages": [AIMessage(content=json.dumps(payload, ensure_ascii=False))]}


def _run_config(config: RunnableConfig | None) -> dict[str, Any]:
    """Pass the invocation config through; the workflow needs its own recursion limit."""
    merged = dict(config or {})
    merged["recursion_limit"] = DEFAULT_WORKFLOW_RECURSION_LIMIT
    return merged


def build_subagent(params: dict[str, Any], services) -> Runnable:
    """Build the nl2sql sub-agent Runnable from ``plugins.config.nl2sql`` parameters.

    The semantic-service client is host-owned: ``services.semantic_client`` comes
    from the root ``semantic`` settings (``None`` when unconfigured) and is shared,
    not built from plugin parameters.
    """
    nl2sql_config = NL2SQLConfig(params)
    engine = str(nl2sql_config.get("database.engine") or nl2sql_config.get("database.dialect"))
    check_engine_driver(engine)
    model = _BudgetedModel(services.get_model(str(nl2sql_config.get("model"))))
    budget = nl2sql_config.model_call_budget
    semantic_client = services.semantic_client
    workflow = build_nl2sql_graph(nl2sql_config, model=model, semantic_client=semantic_client)
    defaults = state_defaults(nl2sql_config)

    # The second parameter must be named ``config``: RunnableLambda injects the
    # invocation config only into a parameter with that name. The task function
    # is async, so the Runnable supports ainvoke (the host executes agents
    # asynchronously); langchain-core refuses a synchronous invoke of a
    # coroutine lambda.
    async def run_task(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        token = _current_counter.set(_CallCounter(budget))
        try:
            question = _question_from_task(state)
            final = await workflow.ainvoke(
                get_default_state(question=question, **defaults),
                config=_run_config(config),
            )
            return _task_reply(final)
        except Exception as exc:
            return _error_reply(exc)
        finally:
            _current_counter.reset(token)

    return RunnableLambda(run_task, name="nl2sql_subagent")
