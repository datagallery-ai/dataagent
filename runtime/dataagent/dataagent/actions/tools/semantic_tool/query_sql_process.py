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
"""Query the DML (INSERT SQL) expression of an existing table via semantic service."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient


def query_sql_process(target_table: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Query the DML (INSERT SQL) expression for an existing table.

    Retrieves the SQL process (DML expression) associated with the target table
    from the semantic metadata service. This is used when adding new columns to
    an existing table and you need to modify the current INSERT SQL.

    Args:
        target_table (str): The fully qualified target table name (e.g. 'db.table_name').

    Returns:
        dict with keys ``original_msg``, ``frontend_msg``, ``data``.
        ``data`` contains ``expression`` (the INSERT SQL string) and ``target_table``.
    """
    if not target_table:
        return _fmt("未提供目标表名。", "未提供目标表名。", {})

    client = SemanticServiceClient.from_config(_tool_context.config_manager)

    try:
        result = client.get_entity_by_unique_attribute(
            type_name="sql_process",
            attr_name="target_table",
            attr_value=target_table,
        )
    except Exception as e:
        logger.error(f"[query_sql_process] 查询失败: {e}")
        return _fmt(f"查询 sql_process 失败: {e}", f"查询失败: {e}", {"expression": "", "target_table": target_table})

    entity = result.get("entity", {})
    if not entity:
        msg = f"未找到 target_table='{target_table}' 对应的 sql_process"
        return _fmt(msg, msg, {"expression": "", "target_table": target_table})

    attributes = entity.get("attributes", {})
    expression = attributes.get("expression", "")

    if not expression:
        msg = f"sql_process 实体已找到 ('{target_table}')，但 expression 为空"
        return _fmt(msg, msg, {"expression": "", "target_table": target_table})

    # 保存到 .metric_dir
    _save_sql_process_output(target_table, expression)

    detail = f"目标表: {target_table}\nDML 表达式:\n{expression}"
    preview = f"已获取 '{target_table}' 的 DML 表达式"

    return _fmt(detail, preview, {"expression": expression, "target_table": target_table})


def _fmt(original: str, frontend: str, data: Any) -> dict[str, Any]:
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


def _save_sql_process_output(target_table: str, expression: str) -> None:
    """将查询结果保存到 .metric_dir 目录。"""
    try:
        guard = get_current_sandbox()
        workspace_root = guard.workspace_root
        if workspace_root is None:
            return
        output_path = Path(workspace_root) / ".metric_dir"
        output_path.mkdir(parents=True, exist_ok=True)
        current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        file_path = output_path / f"output_query_sql_process_{current_time}.json"
        payload = {
            "target_table": target_table,
            "expression": expression,
            "queried_at": datetime.now(UTC).isoformat(),
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"[query_sql_process] 已保存文件: {file_path}")
    except Exception as e:
        logger.error(f"[query_sql_process] 保存文件失败: {e}")
