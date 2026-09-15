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
"""Golden-schema based metadata recall helpers and tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.semantic_tool.get_table_desc import get_table_description
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient

_DEFAULT_GOLDEN_SCHEMA_SUITE_TOOLS_RELATIVE_PATH = Path("tools.yaml")


def _load_default_golden_schema_tasks_relative_path() -> Path:
    tools_config_path = _resolve_repo_relative_file(_DEFAULT_GOLDEN_SCHEMA_SUITE_TOOLS_RELATIVE_PATH)
    config = yaml.safe_load(tools_config_path.read_text(encoding="utf-8")) or {}
    local_functions = config.get("TOOLS", {}).get("local_functions", [])
    if not isinstance(local_functions, list):
        raise ValueError(f"TOOLS.local_functions must be a list: {tools_config_path}")

    for tool_config in local_functions:
        if not isinstance(tool_config, dict) or tool_config.get("name") != "metadata_recall":
            continue
        config_section = tool_config.get("config", {})
        if not isinstance(config_section, dict):
            break
        path = config_section.get("path")
        if isinstance(path, str) and path.strip():
            return Path(path.strip())
        break
    raise ValueError(f"metadata_recall config.path not found: {tools_config_path}")


def _resolve_repo_relative_file(relative_path: Path) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = (parent / relative_path).resolve()
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"cannot find {relative_path}")


_DEFAULT_GOLDEN_SCHEMA_TASKS_RELATIVE_PATH: Path | None = None


def _get_default_golden_schema_tasks_relative_path() -> Path:
    """Load and cache the default golden-schema tasks path from the suite tool config."""
    global _DEFAULT_GOLDEN_SCHEMA_TASKS_RELATIVE_PATH
    if _DEFAULT_GOLDEN_SCHEMA_TASKS_RELATIVE_PATH is None:
        _DEFAULT_GOLDEN_SCHEMA_TASKS_RELATIVE_PATH = _load_default_golden_schema_tasks_relative_path()
    return _DEFAULT_GOLDEN_SCHEMA_TASKS_RELATIVE_PATH


def get_golden_schema_retrieve_context(
    query: str,
    *,
    client: SemanticServiceClient,
    workspace_root: Optional[Path] = None,  # noqa: UP045
    run_id: Any = None,
    sub_id: Any = None,
    source: str = "golden_schema_retrieve_context_loader",
    tasks_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the metadata context for the current query from golden-schema fixtures.

    The lookup first tries the workspace cache scoped by both query text and the
    resolved tasks file path. On a miss, it loads the matching task from
    ``tasks_path``, reads its ``golden_schema_path``, fetches table descriptions
    through the semantic client, and writes metric/cache artifacts.

    Args:
        query: Original user query. It must exactly match a task entry.
        client: Semantic service client used only for table descriptions.
        workspace_root: Workspace used for cache and metric output files.
        run_id: Optional run identifier persisted in the diagnostic payload.
        sub_id: Optional subagent identifier persisted in the diagnostic payload.
        source: Caller label persisted in the diagnostic payload.
        tasks_path: Optional tasks file path. If omitted, the de_agent suite
            tool config is used.

    Returns:
        A payload containing recalled tables, ``tables_with_columns``,
        injectable context text, and cache/diagnostic metadata.
    """
    query_text = str(query or "").strip()
    if not query_text:
        raise ValueError("original user query is required for golden schema retrieve")

    cached = read_golden_schema_retrieve_context_cache(
        query_text,
        workspace_root=workspace_root,
        tasks_path=tasks_path,
    )
    if cached is not None:
        _save_golden_schema_metric_outputs(cached, workspace_root)
        return cached

    golden_schema = get_golden_schema_tables_info(query_text, path=tasks_path)
    recalled_tables = golden_schema["tables"]
    tables_with_columns = _attach_table_descriptions({table: [] for table in recalled_tables}, client)

    context_text = f"匹配原始query的表如下：（query为 {query_text}）"
    for table in recalled_tables:
        table_desc = tables_with_columns.get(table, {}).get("table_description", "")
        context_text += "\n"
        context_text += f"{table} 描述：{table_desc}"

    raw_response = {
        "source": "golden_schema",
        "task": golden_schema["task"],
        "tasks_path": golden_schema["tasks_path"],
        "golden_schema_path": golden_schema["schema_path"],
        "tables": recalled_tables,
    }
    payload = {
        "source": source,
        "endpoint": "golden_schema/retrieve",
        "query": query_text,
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "sub_id": sub_id,
        "tables": recalled_tables,
        "tables_with_columns": tables_with_columns,
        "context_text": context_text,
        "raw_response": raw_response,
        "diagnostic": None,
        "diagnostic_path": None,
    }
    cache_path = _save_golden_schema_retrieve_cache(
        payload,
        workspace_root=workspace_root,
        tasks_path=tasks_path,
    )
    if cache_path is not None:
        payload["cache_path"] = str(cache_path)
    _save_golden_schema_metric_outputs(payload, workspace_root)
    return payload


def get_golden_schema_columns_info(
    table_name: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict:
    """Return column metadata for a table allowed by the query's golden schema.

    The function resolves the original user query from runtime context, finds the
    matching golden-schema task, and rejects any table that is not declared in
    that task's schema before calling the semantic service.

    Args:
        table_name: Fully qualified table name, for example ``db.table``.
        _tool_context: Injected tool context containing config, runtime, and
            semantic service settings.

    Returns:
        ``_fmt``-style response. Allowed tables include column details; rejected
        tables include ``allowed=False`` and the golden-schema allowlist.
    """
    if not table_name:
        return _fmt("未提供表名。", "未提供表名。", {})

    from dataagent.utils.info_utils import get_current_query

    if _tool_context.runtime is None:
        msg = "无法校验表名：未找到运行时上下文。"
        return _fmt(msg, msg, {"table": table_name, "allowed": False})
    query = get_current_query(_tool_context.runtime)
    if query is None:
        msg = "无法校验表名：未找到原始用户 query。"
        return _fmt(msg, msg, {"table": table_name, "allowed": False})

    configured_path = _tool_context.tool_config.get("path")
    try:
        golden_schema = get_golden_schema_tables_info(query, path=configured_path)
    except Exception as exc:
        msg = f"无法校验表名：{exc}"
        return _fmt(msg, msg, {"table": table_name, "allowed": False})

    allowed_tables = golden_schema["tables"]
    if table_name not in allowed_tables:
        allowed = "、".join(allowed_tables)
        msg = f"表 {table_name} 不在当前 query 对应的 golden_schema 表清单中，允许的表为：{allowed}"
        return _fmt(
            msg,
            msg,
            {
                "table": table_name,
                "allowed": False,
                "allowed_tables": allowed_tables,
                "golden_schema_path": golden_schema["schema_path"],
            },
        )

    client = SemanticServiceClient.from_config(_tool_context.config_manager)
    return _get_columns_info_with_client(table_name, client)


def read_golden_schema_retrieve_context_cache(
    query: str,
    *,
    workspace_root: Optional[Path],  # noqa: UP045
    tasks_path: str | Path | None = None,
) -> Optional[dict[str, Any]]:  # noqa: UP045
    """Read cached golden-schema retrieve context for a query and tasks path.

    Args:
        query: Original user query used as part of the cache key.
        workspace_root: Workspace containing the ``.semantic`` cache directory.
        tasks_path: Optional tasks file path used as part of the cache key.

    Returns:
        Cached payload when it exists and matches the query; otherwise ``None``.
    """
    query_text = str(query or "").strip()
    if workspace_root is None or not query_text:
        return None
    file_path = _golden_schema_retrieve_cache_path(Path(workspace_root), query_text, tasks_path=tasks_path)
    if not file_path.is_file():
        return None
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug(f"[golden_schema_retrieve] cache read failed: {exc}")
        return None
    if not isinstance(payload, dict) or payload.get("query") != query_text:
        return None
    payload["cache_path"] = str(file_path)
    return payload


def get_golden_schema_tables_info(query: str, *, path: str | Path | None = None) -> dict[str, Any]:
    """Load the matched task and table allowlist for an exact user query.

    Args:
        query: Original user query. Matching is strict after trimming whitespace.
        path: Optional tasks file path. Relative paths are resolved by walking
            parent directories from this module.

    Returns:
        A dict containing the matched task, resolved tasks path, resolved golden
        schema file path, and ordered unique table names.

    Raises:
        ValueError: If the query does not match a task or schema data is invalid.
        FileNotFoundError: If the tasks file cannot be resolved.
    """
    query_text = str(query or "").strip()
    if not query_text:
        raise ValueError("original user query is required for golden schema lookup")

    tasks_path = _resolve_golden_schema_tasks_path(path)
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        raise ValueError(f"golden schema tasks file must contain a list: {tasks_path}")

    matched_task: dict[str, Any] | None = None
    for task in tasks:
        if isinstance(task, dict) and str(task.get("query") or "").strip() == query_text:
            matched_task = task
            break
    if matched_task is None:
        raise ValueError("no sql feature eval task matched the original query exactly")

    golden_schema_path = matched_task.get("golden_schema_path")
    if not isinstance(golden_schema_path, str) or not golden_schema_path.strip():
        raise ValueError("matched sql feature eval task has no golden_schema_path")

    schema_path = (tasks_path.parent / golden_schema_path).resolve()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, list):
        raise ValueError(f"golden schema file must contain a list: {schema_path}")

    tables: list[str] = []
    for item in schema:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "").strip()
        if table and table not in tables:
            tables.append(table)
    if not tables:
        raise ValueError(f"golden schema file contains no tables: {schema_path}")

    return {
        "task": matched_task,
        "tasks_path": str(tasks_path),
        "schema_path": str(schema_path),
        "tables": tables,
    }


def _get_columns_info_with_client(table_name: str, client: SemanticServiceClient) -> dict:
    cols_raw = client.get_table_columns_info(table_name, limit=1000)

    columns: list[dict] = []
    for dtc, meta in cols_raw.items():
        _, _, column_name = dtc.split(".")
        columns.append(
            {
                "name": column_name,
                "full_name": dtc,
                "description": meta.get("column_short_description", ""),
                "value_type": meta.get("value_type", ""),
                "column_properties": meta.get("column_properties", None),
            }
        )

    summary = f"表 {table_name} 共 {len(columns)} 个字段。"
    lines = [
        f"  - {col['name']} ({col['value_type']}): {col['description']}，属性：{json.dumps(col['column_properties'])}"
        for col in columns
    ]
    detail = summary + "\n" + "\n".join(lines)

    preview_lines: list[str] = [summary]
    if columns:
        preview_lines.append("字段 (前5):" if len(columns) > 5 else "字段:")
        for col in columns[:5]:
            preview_lines.append(f"  - {col['name']} ({col['value_type']}): {col['description']}")
        if len(columns) > 5:
            preview_lines.append(f"  … 还有 {len(columns) - 5} 个字段")
    msg = "\n".join(preview_lines)

    table_description = ""
    try:
        table_description = get_table_description(f"{table_name}@hive", client)
    except Exception as e:
        logger.warning(f"[get_golden_schema_columns_info] 获取表描述失败: {e}")

    schema_data = {
        "table_name": table_name,
        "table_description": table_description,
        "columns": [
            {
                "column_name": col.get("name", ""),
                "column_description": col.get("description", ""),
                "column_type": col.get("value_type", ""),
            }
            for col in columns
        ],
    }
    logger.info(f"[get_golden_schema_columns_info] 构建保存数据完成，表: {table_name}, 包含 {len(columns)} 个字段")
    try:
        output_path, current_time = _get_workspace_path()
        logger.info(
            f"[get_golden_schema_columns_info] 获取路径成功: output_path={output_path}, current_time={current_time}"
        )
        file_path = output_path / f"output_get_table_schema_golden_{current_time}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(schema_data, f, ensure_ascii=False, indent=2)
        logger.info(f"[get_golden_schema_columns_info] 已保存文件: {file_path}, 包含 {len(columns)} 个字段")
    except Exception as e:
        logger.error(f"[get_golden_schema_columns_info] 保存文件失败: {e}")

    return _fmt(detail, msg, {"table": table_name, "columns": columns})


def _fmt(original: str, frontend: str, data: Any) -> dict:
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


def _resolve_golden_schema_tasks_path(path: str | Path | None = None) -> Path:
    """Find the sql feature eval tasks file from a configured path or the default path."""
    raw_path = Path(path).expanduser() if path else _get_default_golden_schema_tasks_relative_path()
    if raw_path.is_absolute():
        if raw_path.is_file():
            return raw_path.resolve()
        raise FileNotFoundError(f"cannot find {raw_path}")

    for parent in Path(__file__).resolve().parents:
        tasks_path = (parent / raw_path).resolve()
        if tasks_path.is_file():
            return tasks_path
    raise FileNotFoundError(f"cannot find {raw_path}")


def _attach_table_descriptions(
    tables_columns: dict,
    client: SemanticServiceClient,
    description_cache: dict[str, str] | None = None,
) -> dict:
    """Attach table descriptions and convert to the tables_with_columns structure."""
    result = {}
    cache = description_cache if description_cache is not None else {}
    for table_name, columns in tables_columns.items():
        table_qualified_name = f"{table_name}@hive"
        if table_qualified_name not in cache:
            cache[table_qualified_name] = get_table_description(table_qualified_name, client)
        result[table_name] = {
            "table_name": table_name,
            "table_description": cache[table_qualified_name],
            "columns": columns,
        }
    return result


def _get_workspace_path(workspace_root: Path | None = None) -> tuple[Path, str]:
    """Return the .metric_dir path and a timestamp."""
    if workspace_root is None:
        guard = get_current_sandbox()
        workspace_root = guard.workspace_root
    if workspace_root is None:
        raise ValueError("workspace_root is required to save metric search results")
    output_path = Path(workspace_root) / ".metric_dir"
    output_path.mkdir(parents=True, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    return output_path, current_time


def _save_tables_with_columns_to_json(
    tables_with_columns: dict,
    file_prefix: str,
    output_path: Path,
    current_time: str,
) -> None:
    """Save tables_with_columns data to a JSON file."""
    file_path = output_path / f"{file_prefix}_{current_time}.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(tables_with_columns, f, ensure_ascii=False, indent=2)


def _golden_schema_retrieve_cache_filename(query: str, *, tasks_path: str | Path | None = None) -> str:
    """Build the deterministic cache filename for a query and golden-schema tasks file."""
    resolved_tasks_path = _resolve_golden_schema_tasks_path(tasks_path)
    cache_key = json.dumps(
        {
            "query": query,
            "tasks_path": str(resolved_tasks_path),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    query_hash = sha256(cache_key.encode("utf-8")).hexdigest()
    return f"golden_schema_retrieve_context_{query_hash}.json"


def _golden_schema_retrieve_cache_path(
    workspace_root: Path,
    query: str,
    *,
    tasks_path: str | Path | None = None,
) -> Path:
    """Build the deterministic golden-schema retrieve cache path."""
    return Path(workspace_root) / ".semantic" / _golden_schema_retrieve_cache_filename(query, tasks_path=tasks_path)


def _save_golden_schema_retrieve_cache(
    payload: dict[str, Any],
    *,
    workspace_root: Optional[Path],  # noqa: UP045
    tasks_path: str | Path | None = None,
) -> Optional[Path]:  # noqa: UP045
    """Persist golden-schema retrieve context under workspace .semantic for later reuse."""
    if workspace_root is None:
        return None
    query = str(payload.get("query") or "").strip()
    if not query:
        return None
    file_path = _golden_schema_retrieve_cache_path(workspace_root, query, tasks_path=tasks_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["cache_path"] = str(file_path)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"[golden_schema_retrieve] 已保存 context cache: {file_path}")
    return file_path


def _save_golden_schema_metric_outputs(payload: dict[str, Any], workspace_root: Path | None) -> None:
    """Preserve golden-schema retrieve JSON and summary outputs in ``.metric_dir``."""
    if workspace_root is None:
        return

    tables_with_columns = payload.get("tables_with_columns")
    context_text = str(payload.get("context_text") or "")
    if not isinstance(tables_with_columns, dict) or not context_text:
        return

    output_path, current_time = _get_workspace_path(workspace_root)
    _save_tables_with_columns_to_json(
        tables_with_columns,
        "output_search_tables_with_semantic_retrieve_golden",
        output_path,
        current_time,
    )
    output_path, current_time = _get_workspace_path(workspace_root)
    summary_path = output_path / f"output_search_tables_with_retrieve_summary_golden_{current_time}.txt"
    summary_path.write_text(context_text, encoding="utf-8")
