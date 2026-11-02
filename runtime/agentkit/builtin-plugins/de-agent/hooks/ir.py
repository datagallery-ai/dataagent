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
"""Connect native todo/file/tool events to the legacy DataTaskIR lifecycle."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace

from langchain_core.messages import ToolMessage
from langgraph.types import Command
from loguru import logger
from utils.data_task_ir import core
from utils.data_task_ir.inputs import tool_succeeded
from utils.data_task_ir.runtime import ir_enabled, ir_state


def _failure(request, exc, result=None):
    logger.exception("DataTaskIR hook failed: {}", type(exc).__name__)
    message = ToolMessage(
        content=f"DataTaskIR: {exc}. Fix the input and retry the IR update before continuing.",
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )
    if isinstance(result, Command):
        update = dict(result.update or {})
        update["messages"] = [
            item
            for item in update.get("messages", [])
            if not isinstance(item, ToolMessage) or item.tool_call_id != message.tool_call_id
        ] + [message]
        return replace(result, update=update)
    return message


def _completed(todos):
    return Counter(todo.get("content", "") for todo in todos if todo.get("status") == "completed")


async def update_ir(request, result, *, params):
    """Create IR on the first successful write_todos; update on newly completed todos."""
    context = request.runtime.context
    if not ir_enabled(context) or not tool_succeeded(request, result):
        return None
    try:
        await core.create_ir(context)
        cache = ir_state(context)
        old = cache.get("todos", request.state.get("todos", []))
        new = (result.update or {}).get("todos") if isinstance(result, Command) else None
        if new is None:
            new = request.tool_call.get("args", {}).get("todos", [])
        cache["todos"] = deepcopy(new)
        if _completed(new) - _completed(old):
            await core.update_ir(context, request.state)
        return None
    except Exception as exc:
        return _failure(request, exc, result)


async def ensure_ir_ready(request, *, params):
    """Use parent messages before crossing the compiled NL2SQL run-hook boundary."""
    if not ir_enabled(request.runtime.context):
        return None
    name = request.tool_call.get("name", "")
    if name == "task" and request.tool_call.get("args", {}).get("subagent_type") != "nl2sql":
        return None
    try:
        await core.ensure_ir_ready(request.runtime.context, request.state)
    except Exception as exc:
        return _failure(request, exc)
    return None
