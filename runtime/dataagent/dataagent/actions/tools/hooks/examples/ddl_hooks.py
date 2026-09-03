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

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from loguru import logger


async def ddl_post(request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command | None:
    """Validate direct ``write_file`` DDL content after a successful tool call.

    The native file backend owns file I/O, so this compatibility hook validates
    the content supplied to ``write_file`` rather than reaching into a separate
    agent runtime workspace.

    Args:
        request: Native LangChain request for the completed tool call.
        result: Native tool result or state command returned by LangChain.

    Returns:
        An error ``ToolMessage`` for invalid DDL, otherwise ``None``.
    """
    if not isinstance(result, ToolMessage):
        return None

    tool_call = request.tool_call
    tool_name = str(tool_call.get("name", "")).strip()
    tool_call_id = str(tool_call.get("id") or result.tool_call_id)
    if result.status != "success":
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=tool_failed",
            tool_name,
            tool_call_id,
        )
        return None

    tool_args = tool_call.get("args", {})
    if not isinstance(tool_args, dict):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=invalid_tool_args",
            tool_name,
            tool_call_id,
        )
        return None

    path_value = tool_args.get("file_path")
    if not isinstance(path_value, str):
        path_value = tool_args.get("path")
    if not isinstance(path_value, str):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=invalid_path_type type={}",
            tool_name,
            tool_call_id,
            type(path_value).__name__,
        )
        return None

    ddl_file_name_re = re.compile(r"^create_.+\.sql$", re.IGNORECASE)
    file_name = path_value.rsplit("/", 1)[-1]
    if not ddl_file_name_re.fullmatch(file_name):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} path={} reason=filename_not_matched_ddl_file",
            tool_name,
            tool_call_id,
            path_value,
        )
        return None

    sql_text = tool_args.get("content")
    if not isinstance(sql_text, str):
        logger.debug(
            "[post_hook] ddl_post skip. tool={} call_id={} reason=no_write_content",
            tool_name,
            tool_call_id,
        )
        return None

    is_valid, reason = _ddl_validator(sql_text)
    if is_valid:
        logger.debug(
            "[post_hook] ddl_post valid. tool={} call_id={} path={}",
            tool_name,
            tool_call_id,
            path_value,
        )
        return None

    logger.debug(
        "[post_hook] ddl_post invalid. tool={} call_id={} path={} reason={}",
        tool_name,
        tool_call_id,
        path_value,
        reason,
    )
    return ToolMessage(
        content=reason,
        name=result.name or tool_name or None,
        tool_call_id=result.tool_call_id,
        status="error",
    )


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

    # 表名规则：只能包含小写字母、数字或下划线，且必须以ods/dwd/dws/dim/ads开头
    table_name_valid_re = re.compile(r"^(ods|dwd|dws|dim|ads)[a-z0-9_]+$")

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
        match.group("table").rsplit(".", 1)[-1].strip('`"') for match in table_name_extract_re.finditer(sql_text)
    ]
    for table_name in table_matches:
        if not table_name_valid_re.fullmatch(table_name):
            reasons.append(
                f"DDL 中表名'{table_name}'不符合命名规则，只能包含小写字母、数字或下划线，且必须以ods/dwd/dws/dim/ads开头"
            )

    # 表级 COMMENT 规则校验
    if not table_comment_matches:
        reasons.append("DDL 中表级不能没有COMMENT关键字且内容不能为空")

    if not _validate_target_table_with_database(sql_text):
        reasons.append("DDL 中目标表缺少库名，请使用 `库名.表名` 格式")

    for match_info in table_comment_matches:
        reasons.extend(
            _validate_comment_text(
                "DDL 中表级COMMENT",
                match_info["comment"],
                empty_reason="DDL 中表级COMMENT关键字内容不能为空",
            )
        )

    return reasons


def _validate_target_table_with_database(sql_text: str) -> tuple[bool, str]:
    """
    校验 DDL/DML 中目标表是否缺少库名（必须使用 ``库名.表名``）。

    覆盖范围：

    * DDL: ``CREATE TABLE [IF NOT EXISTS] <目标>``
    * DML: ``INSERT OVERWRITE TABLE <目标>`` / ``INSERT INTO TABLE <目标>``

    当检测到目标表没有带库名（不含 ``.``）时返回错误。
    """
    # 去掉 SQL 注释行（行内 -- 之后的内容），避免误判注释里的 CREATE/INSERT
    lines = sql_text.split("\n")
    stripped_lines = []
    for line in lines:
        idx = line.find("--")
        if idx >= 0:
            stripped_lines.append(line[:idx])
        else:
            stripped_lines.append(line)
    content_no_comment = "\n".join(stripped_lines)
    content_upper = content_no_comment.upper()

    # 标识符允许：字母/数字/下划线，可整体被反引号/双引号/中括号包裹
    ident = r"`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[A-Za-z_]\w*"
    schema_group = rf"(?:({ident})\.)?"
    table_group = rf"({ident})"
    target_group = schema_group + table_group

    # DDL: CREATE [EXTERNAL] TABLE [IF NOT EXISTS] <target>
    ddl_pattern = re.compile(
        rf"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\b(?:\s+IF\s+NOT\s+EXISTS)?\s+{target_group}",
        re.IGNORECASE,
    )

    # DML: INSERT OVERWRITE|INTO [TABLE] <target>
    dml_pattern = re.compile(
        rf"\bINSERT\s+(?:OVERWRITE|INTO)\b(?:\s+TABLE)?\s+{target_group}",
        re.IGNORECASE,
    )

    for pattern, kind in (
        (ddl_pattern, "DDL"),
        (dml_pattern, "DML"),
    ):
        for match in pattern.finditer(content_upper):
            schema = match.group(1)
            # 从原始 content 中按匹配偏移切片回小写原文（避免大写转换影响错误信息）
            start_in_original = match.start(2)
            end_in_original = match.end(2)
            table_original = sql_text[start_in_original:end_in_original]
            if schema:
                # 已带库名
                continue
            return (
                False,
                f"{kind}校验失败:  目标表 `{table_original}` 缺少库名，请使用 `库名.{table_original}` 格式",
            )

    return True, ""


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
            reasons.append(f"DDL 中字段'{match_info['name']}'不符合命名规则，只能包含小写字母、数字或下划线")
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
    disallowed_chars = ("*", "x", "✖", "✖️", "×", "@")

    if not comment.strip():
        reasons.append(empty_reason or f"{comment_label}不能为空")
    for char in disallowed_chars:
        if char in comment:
            reasons.append(f"{comment_label}不能包含`{char}`这个特殊字符")

    return reasons
