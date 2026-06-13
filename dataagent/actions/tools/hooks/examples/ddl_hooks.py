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
"""DDL validation hooks for generated SQL files."""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPostHookOutcome
from dataagent.core.managers.action_manager.base import ErrorType


async def ddl_post(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Validate generated DDL files after successful write tools.

    Args:
        inv: Per-call context with ``execution`` set; ``tool_args`` may contain a file path.

    Returns:
        Empty outcome; validation failures are written back to ``inv.execution``.
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

    path_value = inv.tool_args["path"]
    if not isinstance(path_value, str):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=invalid_path_type type={}",
            inv.tool_name,
            inv.tool_call_id,
            type(path_value).__name__,
        )
        return ToolPostHookOutcome()

    ddl_path = Path(path_value)
    ddl_file_name_re = re.compile(r"^create_.+\.sql$", re.IGNORECASE)
    if not ddl_file_name_re.fullmatch(ddl_path.name):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} path={} reason=filename_not_matched_ddl_file",
            inv.tool_name,
            inv.tool_call_id,
            ddl_path,
        )
        return ToolPostHookOutcome()

    try:
        sql_text = ddl_path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = f"读取DDL文件失败：{exc}"
        logger.exception(
            "[post_hook] ddl_post read failed. tool={} call_id={} path={}",
            inv.tool_name,
            inv.tool_call_id,
            ddl_path,
        )
        inv.execution.success = False
        inv.execution.error_text = reason
        inv.execution.error_type = ErrorType.VALIDATION_ERROR.value
        inv.execution.retry_info = {"attempt": 0, "max_retries": 0, "retriable": False}
        return ToolPostHookOutcome()

    is_valid, reason = _ddl_validator(sql_text)
    if is_valid:
        logger.debug(
            "[post_hook] ddl_post valid. tool={} call_id={} path={}",
            inv.tool_name,
            inv.tool_call_id,
            ddl_path,
        )
        return ToolPostHookOutcome()

    logger.debug(
        "[post_hook] ddl_post invalid. tool={} call_id={} path={} reason={}",
        inv.tool_name,
        inv.tool_call_id,
        ddl_path,
        reason,
    )
    inv.execution.success = False
    inv.execution.error_text = reason
    inv.execution.error_type = ErrorType.VALIDATION_ERROR.value
    inv.execution.retry_info = {"attempt": 0, "max_retries": 0, "retriable": False}
    return ToolPostHookOutcome()


def _ddl_validator(sql_text: str) -> tuple[bool, str]:
    """Validate generated DDL content.

    Args:
        sql_text: SQL text read from a candidate ``create_*.sql`` file.

    Returns:
        ``(True, "")`` when the DDL passes validation; otherwise ``(False, reason)``.
    """
    table_reasons = _validate_table_ddl(sql_text)
    field_reasons = _validate_field_ddl(sql_text)

    reasons = table_reasons + field_reasons

    if reasons:
        logger.debug("[post_hook] _ddl_validator reasons={}", reasons)
        return False, "；".join(reasons) + "；请修正和重新生成SQL DDL，并避免再出现上述问题。"

    return True, ""


def _validate_table_ddl(sql_text: str) -> list[str]:
    """Validate table name and table-level COMMENT rules."""
    reasons: list[str] = []

    # 表名规则：只能包含小写字母、数字或下划线
    table_name_valid_re = re.compile(r"^[a-z0-9_]+$")

    # 表级 COMMENT 规则：表级 COMMENT 当前明确拒绝 * 和 x，其他字符默认允许
    ddl_table_comment_re = re.compile(
        r"^\s*COMMENT\s*=?\s*'(?P<comment>[^']*)'|"
        r"^\s*\)\s*COMMENT\s*=?\s*'(?P<inline_comment>[^']*)'",
        re.MULTILINE,
    )

    # 提取表名；如果包含库名前缀，只校验最后一段真实表名
    table_name_extract_re = re.compile(
        r"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<table>[`\"]?[\w.-]+[`\"]?)",
        re.IGNORECASE,
    )

    # 提取表级 COMMENT
    table_comment_matches = [
        {
            "type": "table",
            "name": "table",
            "comment": match.group("comment") or match.group("inline_comment"),
        }
        for match in ddl_table_comment_re.finditer(sql_text)
    ]

    # 表名规则校验
    table_matches = [
        match.group("table").rsplit(".", 1)[-1].strip("`\"")
        for match in table_name_extract_re.finditer(sql_text)
    ]
    for table_name in table_matches:
        if not table_name_valid_re.fullmatch(table_name):
            reasons.append(
                f"DDL 中表名'{table_name}'不符合命名规则，只能包含小写字母、数字或下划线"
            )

    # 表级 COMMENT 规则校验
    if not table_comment_matches:
        reasons.append("DDL 中表级不能没有COMMENT关键字且内容不能为空")

    for match_info in table_comment_matches:
        reasons.extend(
            _validate_comment_text(
                "DDL 中表级COMMENT",
                match_info["comment"],
                empty_reason="DDL 中表级COMMENT关键字内容不能为空",
            )
        )

    return reasons


def _validate_field_ddl(sql_text: str) -> list[str]:
    """Validate field name and field-level COMMENT rules."""
    reasons: list[str] = []
    field_block = _extract_create_table_field_block(sql_text)
    if not field_block:
        return reasons

    # 字段名规则：只能包含小写字母、数字或下划线
    field_name_valid_re = re.compile(r"^[a-z0-9_]+$")

    # 字段级 COMMENT 规则：当前明确拒绝 * 和 x，其他字符默认允许
    field_comment_extract_re = re.compile(
        r"^\s*[`\"]?(?P<field>[^`\"\s,()]+)[`\"]?\s+"
        r".*?\bCOMMENT\s+'(?P<comment>[^']*)'",
        re.MULTILINE,
    )

    # 只在 CREATE TABLE (...) 的字段定义块内提取普通字段名
    field_extract_re = re.compile(
        r"^\s*[`\"]?(?P<field>[^`\"\s,()]+)[`\"]?\s+",
        re.MULTILINE,
    )
    field_matches = [
        match.group("field")
        for match in field_extract_re.finditer(field_block)
        if _is_field_definition_line(match.group(0))
    ]

    # 提取字段名和字段级 COMMENT
    field_comment_matches = [
        {
            "type": "field",
            "name": match.group("field"),
            "comment": match.group("comment"),
        }
        for match in field_comment_extract_re.finditer(field_block)
        if _is_field_definition_line(match.group(0))
    ]
    field_comment_names = {match_info["name"] for match_info in field_comment_matches}

    # 字段名和字段级 COMMENT 规则校验
    for field_name in field_matches:
        if field_name not in field_comment_names:
            reasons.append(f"DDL 中字段'{field_name}'不能没有COMMENT关键字且内容不能为空")

    for match_info in field_comment_matches:
        if not field_name_valid_re.fullmatch(match_info["name"]):
            reasons.append(
                f"DDL 中字段'{match_info['name']}'不符合命名规则，只能包含小写字母、数字或下划线"
            )
        reasons.extend(
            _validate_comment_text(
                f"DDL 中字段'{match_info['name']}'的COMMENT",
                match_info["comment"],
                empty_reason=f"DDL 中字段'{match_info['name']}'的COMMENT关键字内容不能为空",
            )
        )

    return reasons


def _extract_create_table_field_block(sql_text: str) -> str:
    """Extract the text inside the first ``CREATE TABLE (...)`` field block."""
    create_table_re = re.compile(
        r"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?[\w.-]+[`\"]?\s*\(",
        re.IGNORECASE,
    )
    match = create_table_re.search(sql_text)
    if not match:
        return ""

    depth = 1
    block_start = match.end()
    for idx in range(block_start, len(sql_text)):
        char = sql_text[idx]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql_text[block_start:idx]

    return ""


def _is_field_definition_line(line_text: str) -> bool:
    """Return whether the matched line fragment belongs to a normal field definition."""
    stripped_line = line_text.lstrip()
    return not stripped_line.startswith(("PARTITIONED BY", ")", "CREATE ", "STORED "))


def _validate_comment_text(
    comment_label: str,
    comment: str,
    *,
    empty_reason: str | None = None,
) -> list[str]:
    """Validate common COMMENT text rules."""
    reasons: list[str] = []
    disallowed_chars = ("*", "x", "✖", "✖️", "×")

    if not comment.strip():
        reasons.append(empty_reason or f"{comment_label}不能为空")
    for char in disallowed_chars:
        if char in comment:
            reasons.append(f"{comment_label}不能包含`{char}`这个特殊字符")

    return reasons
