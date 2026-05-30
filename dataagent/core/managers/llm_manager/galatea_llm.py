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
"""Galatea-style LLM wrapper.

Wraps a LangChain chat model and exposes the minimal interface that galatea
nodes and Runtime rely on: ``bind_tools()`` and ``invoke()``.

Currently defaults to ``ChatTongyi`` (DashScope/Qwen).  To switch providers,
replace the model construction in ``__init__`` with a call to
``dataagent.core.managers.llm_manager.LLMManager`` — the ``bind_tools`` /
``invoke`` surface stays identical, so no callers need to change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import BaseMessage


class LLM:
    """Minimal LLM wrapper used by galatea-style nodes via ``Runtime.llm()``."""

    def __init__(self, llm_config: dict[str, Any]) -> None:
        self._llm = ChatTongyi(**llm_config)

    def bind_tools(self, tools: list[Callable]) -> LLM:
        """Bind tools to the underlying model and return self for chaining."""
        self._llm = self._llm.bind_tools(tools)
        return self

    def invoke(self, messages: list[BaseMessage]) -> BaseMessage:
        """Run a single inference pass and return the assistant message."""
        return self._llm.invoke(messages)
