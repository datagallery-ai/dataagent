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
"""Semantic-service basic retrieval tools."""

from __future__ import annotations

import json
from typing import Any

import requests
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient


def list_semantic_layer_tables(*, _tool_context: ToolExecutionContext) -> dict:
    """List semantic-layer tables exposed by semantic-service basic retrieval API.

    Use this tool when you need to inspect which semantic-layer metadata tables
    can be queried through semantic-service. This tool only calls
    ``GET /api/semantic/v1/retrieval/tables`` and does not execute SQL.

    Returns:
        dict with keys ``original_msg``, ``frontend_msg`` and ``data``.
        ``data`` is the raw semantic-service response, normally containing
        ``tables`` and ``count``.
    """
    try:
        client = SemanticServiceClient.from_config(_tool_context.config_manager)
        raw = client.list_retrieval_tables()
    except (requests.RequestException, ValueError) as err:
        logger.error(f"查询语义层表清单失败：{err}")
        return _fmt(f"请求失败：{err}", "查询语义层表清单失败。", {"tables": [], "count": 0})

    tables = raw.get("tables", []) if isinstance(raw, dict) else []
    count = raw.get("count", len(tables)) if isinstance(raw, dict) else len(tables)
    preview_tables = ", ".join(str(table) for table in tables[:10])
    summary = f"语义层可查询表共 {count} 张。"
    if preview_tables:
        summary += f" 前10张：{preview_tables}"
    return _fmt(_json(raw), summary, raw)


def get_semantic_layer_table_schema(table: str, *, _tool_context: ToolExecutionContext) -> dict:
    """Get schema for a semantic-layer metadata table.

    Use this tool after ``list_semantic_layer_tables`` when you need to inspect
    the columns of one semantic-layer metadata table. This tool only calls
    ``GET /api/semantic/v1/retrieval/tables/{table}/schema`` and does not
    execute SQL.

    Args:
        table: Semantic-layer table name returned by ``list_semantic_layer_tables``.

    Returns:
        dict with keys ``original_msg``, ``frontend_msg`` and ``data``.
        ``data.raw`` is the raw semantic-service response. ``data.schema`` is a
        parsed schema dict when the response contains a JSON schema string.
    """
    normalized_table = table.strip() if isinstance(table, str) else ""
    if not normalized_table:
        return _fmt("未提供语义层表名。", "未提供语义层表名。", {"table": table})

    try:
        client = SemanticServiceClient.from_config(_tool_context.config_manager)
        raw = client.get_retrieval_table_schema(normalized_table)
    except (requests.RequestException, ValueError) as err:
        logger.error(f"查询语义层表 schema 失败：{err}")
        return _fmt(
            f"请求失败：{err}",
            f"查询语义层表 {normalized_table} 的 schema 失败。",
            {"table": normalized_table},
        )

    parsed_schema = _parse_schema(raw.get("schema") if isinstance(raw, dict) else None)
    columns = parsed_schema.get("columns", []) if isinstance(parsed_schema, dict) else []
    summary = f"语义层表 {normalized_table} 共 {len(columns)} 个字段。"
    if columns:
        preview = ", ".join(str(col.get("name", "")) for col in columns[:10] if isinstance(col, dict))
        if preview:
            summary += f" 前10个字段：{preview}"

    data = {"raw": raw, "schema": parsed_schema}
    return _fmt(_json(data), summary, data)


def _fmt(original: str, frontend: str, data: Any) -> dict:
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _parse_schema(schema: Any) -> dict[str, Any]:
    if isinstance(schema, dict):
        return schema
    if not isinstance(schema, str) or not schema.strip():
        return {}
    try:
        parsed = json.loads(schema)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
