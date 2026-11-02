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
"""Read-only access to the shared DataTaskIR contract."""

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from loguru import logger
from utils.data_task_ir import core
from utils.data_task_ir.runtime import ir_enabled

from dataagent.models import ModelContext


async def read_ir(runtime: ToolRuntime[ModelContext]) -> str | ToolMessage:
    """Read current DataTaskIR constraints before drafting DDL or fixing SQL.

    Initialize the plan with write_todos first. This tool never accepts model-written
    field values as evidence; it uses user messages and successful source evidence.
    """
    if not ir_enabled(runtime.context):
        return "DataTaskIR is disabled; use user requirements and source evidence directly."
    try:
        return core.read_ir(runtime.context)
    except Exception as exc:
        logger.exception("DataTaskIR read failed: {}", type(exc).__name__)
        return ToolMessage(
            content=f"DataTaskIR: {exc}. Fix inputs and retry the IR update before generating SQL.",
            tool_call_id=runtime.tool_call_id or "",
            status="error",
        )
