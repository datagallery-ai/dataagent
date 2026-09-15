# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""SQL syntax validation hooks for edit_file on .sql files."""

from __future__ import annotations

from pathlib import Path

import sqlglot
from loguru import logger

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPostHookOutcome
from dataagent.core.managers.action_manager.base import ErrorType

_SQLGLOT_DIALECT = "spark"


async def sql_edit_post(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Validate SQL syntax after edit_file modifies a .sql file.

    Reads the file from disk, parses it with sqlglot, and marks the tool
    execution as failed if any statement cannot be parsed.  The file is
    NOT rolled back so the LLM can read the current content and fix it.

    Args:
        inv: Per-call context with ``execution`` set; ``tool_args`` may
            contain a file ``path``.

    Returns:
        Empty outcome; validation failures are written back to
        ``inv.execution``.
    """
    if inv.execution is None:
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=no_execution",
            inv.tool_name,
            inv.tool_call_id,
        )
        return ToolPostHookOutcome()

    if not inv.execution.success:
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=tool_failed error={}",
            inv.tool_name,
            inv.tool_call_id,
            inv.execution.error_text,
        )
        return ToolPostHookOutcome()

    if "path" not in inv.tool_args:
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=no_path",
            inv.tool_name,
            inv.tool_call_id,
        )
        return ToolPostHookOutcome()

    path_value = inv.tool_args.get("path")
    if not isinstance(path_value, str):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=invalid_path_type type={}",
            inv.tool_name,
            inv.tool_call_id,
            type(path_value).__name__,
        )
        return ToolPostHookOutcome()

    if not Path(path_value).suffix.lower() == ".sql":
        logger.debug("[post_hook] skip non sql file: {}", path_value)
        return ToolPostHookOutcome()

    # If edit_file reported no actual changes, nothing to validate.
    raw = inv.execution.raw_result
    if isinstance(raw, dict) and raw.get("data", {}).get("changed") is False:
        logger.debug("[post_hook] edit_file makes no change, skip. path={}", path_value)
        return ToolPostHookOutcome()

    # --- parse the file ---
    file_path = Path(path_value)
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = f"SQL语法校验：读取文件失败 — {exc}"
        logger.warning("[post_hook] sql_edit_post read failed: {}", exc)
        _mark_failed(inv, reason)
        return ToolPostHookOutcome()

    if not content.strip():
        return ToolPostHookOutcome()

    parse_errors = _parse_sql(content)
    if not parse_errors:
        logger.debug("[post_hook] sql_edit_post valid. path={}", file_path)
        return ToolPostHookOutcome()

    reason = "SQL语法校验失败：edit_file 修改后 SQL 无法解析。错误详情：\n" + "\n".join(
        f"  - {e}" for e in parse_errors
    )
    logger.debug("[post_hook] sql_edit_post invalid. path={} errors={}", file_path, parse_errors)
    _mark_failed(inv, reason)
    return ToolPostHookOutcome()


def _parse_sql(content: str) -> list[str]:
    """Parse SQL content with sqlglot and return a list of error messages.

    Uses ``sqlglot.parse`` with ``ErrorLevel.RAISE`` so that any syntax
    error immediately raises ``ParseError``.  Returns an empty list when
    all statements parse successfully.
    """
    try:
        sqlglot.parse(content, read=_SQLGLOT_DIALECT, error_level=sqlglot.errors.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as exc:
        return [str(exc)]

    return []


def _mark_failed(inv: ToolHookInvocation, reason: str) -> None:
    """Mark the tool execution as failed with a validation error."""
    inv.execution.success = False
    inv.execution.error_text = reason
    inv.execution.error_type = ErrorType.VALIDATION_ERROR.value
    inv.execution.retry_info = {"attempt": 0, "max_retries": 0, "retriable": False}
