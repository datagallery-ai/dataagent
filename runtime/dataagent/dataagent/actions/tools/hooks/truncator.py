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
"""Compatibility hook superseded by native Deep Agents result eviction."""

from __future__ import annotations

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


async def truncator(request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command | None:
    """Retain the old YAML hook name while native filesystem middleware evicts output.

    Args:
        request: Native LangChain request for the tool call.
        result: Native tool result or state command returned by LangChain.

    Returns:
        ``None`` so Deep Agents' built-in filesystem result eviction remains the
        single source of truth.
    """
    del request, result
    return None
