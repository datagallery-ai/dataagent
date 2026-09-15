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
"""Table name validation and existence check tools for the add_columns skill."""

import re
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.search_tables_with_schema import get_table_schema

_TABLE_NAME_PATTERN = re.compile(r"^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")


def validate_table_name(table_name: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Validate table name format and database name.

    Only use this tool in add-column scenario, when user inputs the target table.
    table_name MUST come from the user's explicit input; never fabricate or guess a table name.

    Args:
        table_name (str): table name provided by the user.

    Returns:
        dict with keys ``original_msg``, ``frontend_msg``, ``data``.
        ``data`` contains:
        - ``valid`` (bool): Whether the table name is valid.
        - ``error`` (str): Error description if validation fails, empty string if valid.
    """
    # Format validation
    format_error = _validate_table_name_format(table_name)
    if format_error:
        return _fmt(
            f"表名格式校验失败: {format_error}",
            f"表名格式校验失败: {format_error}",
            {"valid": False, "error": format_error},
        )

    # Database validation
    db_name = table_name.strip().split(".")[0]
    allowed_dbs = _get_allowed_databases(_tool_context)
    if allowed_dbs and db_name not in allowed_dbs:
        error = f"数据库名 '{db_name}' 不在允许的数据库列表中，允许的数据库: {', '.join(sorted(allowed_dbs))}"
        return _fmt(
            f"表名校验失败: {error}",
            f"表名校验失败: {error}",
            {"valid": False, "error": error},
        )

    return _fmt("表名校验通过", "表名校验通过", {"valid": True, "error": ""})


def check_table_existence(table_name: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Check if a table exists in the metadata service and return its schema.

    Only use this tool in add-column scenario.

    Args:
        table_name (str): Fully-qualified table name, in ``{db}.{table}`` format.

    Returns:
        dict with keys ``original_msg``, ``frontend_msg``, ``data``.
        ``data`` contains:
        - ``exists`` (bool): Whether the table exists in metadata.
        - ``columns`` (list): Column list from get_table_schema (empty if not exists).
        - ``error`` (str): Error description if table not found.
    """
    schema_result = get_table_schema(table_name, _tool_context=_tool_context)
    data = schema_result.get("data", {})
    columns = data.get("columns", [])

    exists = bool(columns)

    if exists:
        msg = f"表 '{table_name}' 存在。"
        return _fmt(
            msg + "\n" + schema_result.get("original_msg", ""),
            msg,
            {"exists": True, "columns": columns, "error": ""},
        )
    else:
        msg = f"表 '{table_name}' 在元数据中不存在。"
        return _fmt(
            msg + " 无法执行加列操作，请跳转降级流程。",
            msg,
            {"exists": False, "columns": [], "error": "表不存在"},
        )


# ============================================================
# Internal helpers
# ============================================================


def _get_allowed_databases(_tool_context: ToolExecutionContext) -> set[str]:
    """Read configured DATABASE.db_id list from tool context.

    Returns:
        Set of allowed database names; empty set when not configured (skips db validation).
    """
    raw = _tool_context.config_manager.get("DATABASE.db_id", "")
    if isinstance(raw, list):
        return {s for s in raw if s and str(s).strip()}
    if isinstance(raw, str) and raw.strip():
        return {raw.strip()}
    return set()


def _validate_table_name_format(table_name: str) -> str:
    """Validate table name format. Returns empty string if valid, error message if invalid."""
    if not table_name or not table_name.strip():
        return "表名不能为空"

    table_name = table_name.strip()
    match = _TABLE_NAME_PATTERN.match(table_name)

    if not match:
        if "." not in table_name:
            return (
                f"表名 '{table_name}' 缺少数据库名前缀，正确格式为 {{db_name}}.{{table_name}}，例如 agapads.ads_xxx_ds"
            )
        parts = table_name.split(".")
        if len(parts) > 2:
            return f"表名 '{table_name}' 包含多个点号，正确格式为 {{db_name}}.{{table_name}}"
        if not parts[0]:
            return f"表名 '{table_name}' 数据库名部分为空"
        if not parts[1]:
            return f"表名 '{table_name}' 表名部分为空"
        return f"表名 '{table_name}' 格式不正确，只能包含小写字母、数字和下划线"

    return ""


def _fmt(original: str, frontend: str, data: Any) -> dict[str, Any]:
    return {"original_msg": original, "frontend_msg": frontend, "data": data}
