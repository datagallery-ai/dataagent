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
"""Reference implementations for native per-tool-call pre/post hooks.

Wire in YAML under ``TOOLS.local_functions[].hooks``, ``TOOLS.mcp_servers[].hooks``,
or ``TOOLS.A2A[].<agent_id>.hooks`` using dotted ``module.path.callable`` specs, for example::

    hooks:
      pre:
        - dataagent.actions.tools.hooks.examples.example_hooks.audit_pre
      post:
        - dataagent.actions.tools.hooks.examples.example_hooks.audit_post

Hooks receive LangChain's ``ToolCallRequest`` and, for post-hooks, the native
``ToolMessage`` result. Return ``None`` to preserve the current request/result.
"""

from __future__ import annotations

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from loguru import logger


async def audit_pre(request: ToolCallRequest) -> ToolCallRequest | None:
    """Example pre-hook that logs the native tool call.

    Args:
        request: Native LangChain request for the pending tool call.

    Returns:
        ``None`` to execute the original request unchanged.
    """
    tool_call = request.tool_call
    tool_args = tool_call.get("args", {})
    logger.debug(
        "[example_hooks] pre tool={} call_id={} arg_keys={}",
        tool_call.get("name", ""),
        tool_call.get("id", ""),
        list(tool_args.keys()) if isinstance(tool_args, dict) else [],
    )
    return None


async def audit_post(request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command | None:
    """Example post-hook that logs the native tool result.

    Args:
        request: Native LangChain request that produced the result.
        result: Native tool result or state command after LangChain has executed the tool.

    Returns:
        ``None`` to preserve the original result.
    """
    tool_call = request.tool_call
    logger.debug(
        "[example_hooks] post tool={} call_id={} success={}",
        tool_call.get("name", ""),
        tool_call.get("id", ""),
        result.status == "success" if isinstance(result, ToolMessage) else None,
    )
    return None


def noop_probe_tool(label: str = "") -> dict[str, str]:
    """Minimal local tool for YAML hook wiring smoke tests (not for production agents).

    Args:
        label: Optional string echoed in ``original_msg``.

    Returns:
        Structured tool payload compatible with Executor normalization.
    """
    text = label or "ok"
    return {"original_msg": text, "frontend_msg": text}
