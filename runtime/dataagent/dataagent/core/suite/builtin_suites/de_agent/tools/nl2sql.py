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
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.tools import (
    _build_nl2sql_sub_agent_config,
    _resolve_and_authorize,
    sub_agent_tool,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import render_context
from dataagent.actions.tools.semantic_tool.get_join_relations import get_join_relations
from dataagent.actions.tools.semantic_tool.get_table_desc import get_table_description
from dataagent.actions.tools.semantic_tool.search_tables_with_schema import get_table_schema
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.utils.runtime_paths import dataagent_package_root


async def nl2sql_sub_agent_tool(
    query: str,
    sql_filename: str,
    csv_filename: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str]:
    """Convert natural language query to SQL. One SQL query at a time.

    This function is intended to perform a thorough sql file generation from scratch.
    If you only need small edits to existing sql files, use `write_file` or `edit_file` tools.

    A good query should explicitly describe:
    - business goal and statistical intent
    - entity definitions and metric formulas
    - required joins and matching keys
    - filters, grouping granularity, aggregations, and sorting
    - intermediate computation logic
    - output fields and final result format

    Args:
        - query (str): Natural language query.
        - sql_filename (str): Filename for the generated SQL (with .sql extension).
        - csv_filename (str): Filename for query results (with .csv extension).

    Returns:
        dict[str, str], original and frontend message to the agent
    """
    runtime = _tool_context.runtime
    if runtime is None or runtime.workspace_dir is None:
        raise RuntimeError(
            "nl2sql_sub_agent_tool: session workspace is unavailable; "
            "set initial_state.workspace (or chat(workspace=...)) before calling this tool."
        )
    source_config_path = (
        dataagent_package_root() / "core" / "suite" / "builtin_suites" / "de_agent" / "documents" / "zdy.yaml"
    )
    user_prompt_path = dataagent_package_root() / "agents" / "nl2sql" / "prompts" / "user"
    with source_config_path.open(encoding="utf-8") as f:
        source_config = yaml.safe_load(f) or {}
    guard = get_current_sandbox()
    workspace = str(guard.workspace_root)
    ws_config = source_config.setdefault("WORKSPACE", {})
    shutil.copytree(user_prompt_path, workspace, dirs_exist_ok=True)
    # 将suite路径下的 sql_rules.md 复制到 workspace下
    tool_cfg = _tool_context.tool_config or {}
    suite_name = str(tool_cfg.get("suite_name") or "example_suite").strip()
    config_manager = _tool_context.config_manager
    suite_root = config_manager.get_activated_suite_root(suite_name)
    sql_rules_path = suite_root / "documents" / "sql_rules.md"
    shutil.copy2(sql_rules_path, workspace)
    ws_config["path"] = workspace
    # 若启用 metadata_recall 或 search_udf，确保 schema_udf_basic.md 存在（可空），供 nl2sql 配置 user_evidence 使用
    config_manager = _tool_context.config_manager
    local_functions = (
        {} if config_manager is None else (config_manager.get("TOOLS", {}) or {}).get("local_functions", {})
    )
    agent_tools = [i.get("function", "") for i in local_functions if isinstance(i, dict)]
    if "search_udf_function_by_name_keyword" in agent_tools or "metadata_recall" in agent_tools:
        schema_udf_basic_path = os.path.join(workspace, "schema_udf_basic.md")
        if not os.path.exists(schema_udf_basic_path):
            with open(schema_udf_basic_path, "w", encoding="utf-8") as f:
                f.write("")
    temp_config = _build_nl2sql_sub_agent_config(
        source_config,
        config_manager=config_manager,
        tool_config=_tool_context.tool_config,
        user_id=str(getattr(runtime, "user_id", None) or ""),
        session_id=str(getattr(runtime, "session_id", None) or ""),
    )
    temp_root = guard.workspace_root or Path.cwd().resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="nl2sql_sub_agent_",
        dir=temp_root,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        yaml.safe_dump(temp_config, temp_file, allow_unicode=False, sort_keys=False)
        temp_config_path = temp_file.name
    try:
        runtime.set_cache("nl2sql_detail", query)
        data_task_ir = runtime.get_cache("ir_field_values", {})
        if data_task_ir:
            rendered_ir = render_context(data_task_ir)
            ir_constraint_prompt = (
                "\n\n"
                + "【DataTaskIR 强制约束说明】\n"
                + "以下DataTaskIR记录了此任务的**已确认口径约束**，你生成的SQL**必须严格遵循**：\n\n"
                + rendered_ir
                + "\n\n"
                + "【约束遵循规则 - 必须遵守】\n"
                + "1. **已确认值是强制约束**：IR中明确记录的字段名、操作符、常量值、过滤条件等是已确认口径，必须在SQL中完整实现，不得自行更改或忽略\n"
                + "2. **未记录的操作默认为被禁止**：IR中未明确记录的操作（如去重、过滤、JOIN类型变更等）Agent不得自行添加（包括调用方 query 中主Agent附加、但IR未记录的操作），必须先在IR中记录才能执行\n"
                + "3. **禁止操作是强制约束**：以下通用操作默认被禁止，除非IR明确记录允许——在聚合前使用窗口函数去重（如ROW_NUMBER()）、过滤LEFT JOIN的NULL侧使其退化为INNER JOIN、用户未明确要求时使用DISTINCT或COUNT(DISTINCT)\n"
                + "4. **必须保留的内容**：LEFT JOIN的左表所有记录不得因去重或过滤而丢失；用户未明确要求去重时，所有满足过滤条件的原始记录都必须参与计算\n"
                + "5. **事实记录身份不等于去重**：fact_deduplication中的同一事实字段仅定义记录身份；IR未定义选择函数时不产生去重，不得仅因该字段存在而执行去重\n"
                + "6. **聚合指标约束**：计数类指标（count_metrics）必须按IR中定义的count_type和expression_template执行\n"
                + "7. **比例指标约束**：ratio_metrics中的分子分母必须严格按定义执行，注意分子不应包含分母的所有记录\n"
                + "8. **窗口分区键约束**：window_partitioning定义的分区键用于窗口函数（ROW_NUMBER() OVER(PARTITION BY ...)），必须与IR一致\n"
                + "9. **最终序列分区键约束**：final_sequence_partitioning定义的分区键用于最终输出分组，与窗口分区键可能是不同概念\n"
                + "10. **输出字段定义是权威输出列清单**：IR中的'输出字段定义'（output_fields）列出的字段是最终输出列的唯一权威清单。INSERT SELECT的输出列必须与其完全一致——字段个数、顺序、字段名均不得增减或改动；IR未列出的字段禁止出现在最终输出（即使调用方query中提到了该字段也不能输出）；IR已列出的字段禁止遗漏。当调用方query描述的输出字段与IR输出字段定义冲突时，一律以IR为准，必须按IR的输出字段生成SQL\n"
                + "11. **违反约束=错误**：如果生成的SQL违反了IR中的任何已确认约束（包括禁止操作和必须保留的内容），结果将被视为错误\n"
                + "12. **IR优先于系统通用规则**：系统通用工程规则（如默认添加设备ID合法性过滤、默认判空过滤、默认去重、默认加时间窗口边界等）与本IR已确认口径冲突时，以IR为准；IR未记录的操作默认不执行，除非用户原始问题明确要求\n"
                + "13. **业务口径冲突以IR为准**：本查询中出现的其他业务口径描述（包括主Agent附加的“业务口径”段落、中间推导、示例口径）若与DataTaskIR记录冲突，一律以DataTaskIR为准；若与IR同时出现冲突口径，SQL Agent应报告冲突而不得自行取舍\n"
                + "14. **query 中的“已确认口径”段落不构成约束来源**：调用方 query 顶部的“已确认口径（最高优先级）”“业务口径”等段落只是主Agent的意图描述，不是权威约束。其中出现的去重、过滤、聚合、JOIN、排序、TopN 等操作若未在本 DataTaskIR 中记录，一律视为未确认口径，禁止实现；只有 DataTaskIR 记录的内容才能写入SQL。若 query 段落与 IR 矛盾（例如 query 要求按某键去重、取唯一，而 IR 未记录任何去重要求），以 IR 为准，不得执行去重\n"
            )
            query += ir_constraint_prompt
        res = await sub_agent_tool(query=query, config_path=temp_config_path)
    finally:
        Path(temp_config_path).unlink(missing_ok=True)
    worker_payload = res.get("original_msg")
    if isinstance(worker_payload, dict) and worker_payload.get("error"):
        err = worker_payload.get("error")
        return {
            "original_msg": f"nl2sql_sub_agent_tool 工具执行失败：{err}",
            "frontend_msg": f"nl2sql_sub_agent_tool 工具执行失败：{err}",
        }
    sub_state = res.get("state")

    if not isinstance(sub_state, dict):
        logger.warning(
            f"nl2sql_sub_agent_tool: expected dict state from sub_agent_tool, got {type(sub_state).__name__}"
        )
        return res
    if sub_state.get("error"):
        return {
            "original_msg": f"nl2sql_sub_agent_tool 工具执行失败：{sub_state['error']}",
            "frontend_msg": f"nl2sql_sub_agent_tool 工具执行失败：{sub_state['error']}",
        }
    sql = sub_state.get("sql", "")
    try:
        import sqlglot

        dialect = source_config["DATABASE"]["engine"]
        sql = sqlglot.parse_one(sql, read=dialect).sql(pretty=True)
    except Exception:
        try:
            import sqlparse

            sql = sqlparse.format(sql, reindent=True, keyword_case="upper")
        except Exception:
            logger.warning("SQL cannot be reformatted.")

    columns = sub_state.get("columns") or []
    rows = sub_state.get("rows") or []
    sql_save_path = os.path.join(workspace, sql_filename)
    csv_save_path = os.path.join(workspace, csv_filename)
    sql_path = _resolve_and_authorize(sql_save_path, "sql_save_path", operation="nl2sql_sub_agent", mode="write")
    csv_path = _resolve_and_authorize(csv_save_path, "csv_save_path", operation="nl2sql_sub_agent", mode="write")
    sql_path.write_text(f"{sql}\n", encoding="utf-8")
    pd.DataFrame(rows, columns=columns if columns else None).to_csv(csv_path, index=False, encoding="utf-8-sig")
    frontend_msg_md = (
        f"\n\n nl2sql_sub_agent_tool 工具执行完成\n\n"
        f"SQL 文件已保存到：`{str(sql_path)}`\n\n"
        f"CSV 结果已保存到：`{str(csv_path)}`\n\n"
        f"生成的SQL语句如下:\n```sql\n{sql}\n```"
    )
    return {
        "original_msg": f"SQL 执行完成，SQL 文件已保存到：{str(sql_path)}，查询结果已保存到：{str(csv_path)}",
        "frontend_msg": frontend_msg_md,
    }


def _validate_nl2sql_metadata_grounding(nl_request: str) -> dict[str, Any]:
    """
    Validate whether the metadata entities referenced in the NL2SQL request
    are grounded in the retrieved metadata context before invoking
    `nl2sql_sub_agent_tool`.

    This validator checks whether referenced tables, columns, partition keys,
    metrics, join keys, and other schema entities actually exist in the
    retrieved metadata documents.

    The validator is designed to reduce:
    - schema hallucination
    - invalid table/column references
    - incorrect join conditions
    - downstream SQL generation failures

    Validation behavior:
    - exact_match:
        The entity exists exactly in metadata.
    - possible_match:
        A semantically similar entity exists.
    - not_found:
        No relevant entity exists in metadata.

    Args:
        query (str):
            Natural language request that will be sent to
            `nl2sql_sub_agent_tool`.

    Returns:
        dict[str, Any]:
            Structured validation result.

            Example:
            {
                "valid": True,
                "summary": "Most metadata entities matched successfully.",
                "details": [
                    {
                        "entity": "user_id",
                        "entity_type": "column",
                        "status": "exact_match",
                        "matched_name": "user_id"
                    },
                    {
                        "entity": "install_time",
                        "entity_type": "column",
                        "status": "possible_match",
                        "matched_name": "download_time",
                        "reason": "similar semantic meaning"
                    }
                ],
            }
    """
    # =========================
    # 1. Get Workspace
    # =========================
    guard = get_current_sandbox()
    workspace_root = guard.workspace_root
    if workspace_root is None:
        raise ValueError("workspace_root is required")

    workspace_root = Path(workspace_root)

    # =========================
    # 2. Metadata Files
    # =========================
    schemair_file = workspace_root / "schema_schemair.md"
    udf_basic_file = workspace_root / "schema_udf_basic.md"
    metadata_files = [schemair_file, udf_basic_file]
    missing_files = [str(file.name) for file in metadata_files if not file.exists()]
    if missing_files:
        return {
            "original_msg": f"metadata files not found: {missing_files}",
            "frontend_msg": f"\n\n❌ 校验失败\n\n缺少 metadata 文件:\n{chr(10).join(missing_files)}",
            "data": {
                "valid": False,
                "summary": "metadata files missing",
                "details": [],
                "tokens_used": 0,
                "metadata_files": [str(file.name) for file in metadata_files],
            },
        }

    # =========================
    # 3. Read Metadata Content
    # =========================
    try:
        schemair_content = schemair_file.read_text(encoding="utf-8")
        udf_basic_content = udf_basic_file.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "original_msg": str(e),
            "frontend_msg": f"\n\n❌ 校验失败\n\nmetadata 文件读取异常:\n{str(e)}",
            "data": {
                "valid": False,
                "summary": str(e),
                "details": [],
                "tokens_used": 0,
                "metadata_files": [str(file.name) for file in metadata_files],
            },
        }

    # =========================
    # 4. Limit Metadata Length
    # =========================
    metadata_content = f"""
# Schema Metadata

{schemair_content}

# UDF Metadata

{udf_basic_content}
"""

    # =========================
    # 5. Prompt
    # =========================
    system_prompt = """
You are a senior data warehouse metadata grounding validator.

Your task is to validate whether the metadata entities
mentioned in the user request truly exist in the provided metadata.

Validation scope:
1. table names
2. column names
3. partition fields
4. join keys
5. metric fields
6. udf functions

Rules:
1. Do NOT hallucinate metadata.
2. If an entity does not exist, mark it as not_found.
3. Distinguish:
   - exact_match
   - possible_match
   - not_found
4. Output MUST be valid JSON only.
5. Be conservative and precise. If at least one entity is marked as possible_match or not_found, output valid = False.
6. Output valid as true if the only problem is that the target table does not exist in metadata.

Output format:
{
  "valid": true,
  "summary": "Most metadata matched",
  "details": [
    {
      "entity": "table_name",
      "entity_type": "table",
      "status": "exact_match",
      "matched_name": "xxx"
    },
    {
      "entity": "explode_json",
      "entity_type": "udf",
      "status": "possible_match",
      "matched_name": "parse_json"
    }
  ]
}
"""

    user_prompt = f"""
<user_request>
{nl_request}
</user_request>

<metadata>
{metadata_content}
</metadata>
"""

    # =========================
    # 6. Invoke LLM
    # =========================
    llm = llm_manager.get_default_llm()
    try:
        response = llm.invoke([{"role": "user", "content": system_prompt}, {"role": "user", "content": user_prompt}])
        raw_result = response.content.split("</think>")[-1].strip()
        result_json = json.loads(raw_result)
        tokens_used = getattr(response, "usage_metadata", {}).get("total_tokens", 0)
    except json.JSONDecodeError:
        logger.warning("\n\n❌ 校验失败\n\nLLM LLM返回结果不是合法 JSON")
        return {
            "original_msg": raw_result,
            "frontend_msg": "\n\n❌ 校验失败\n\nLLM LLM返回结果不是合法 JSON",
            "data": {
                "valid": True,
                "summary": "invalid json output",
                "details": [],
                "tokens_used": 0,
                "metadata_files": [str(file.name) for file in metadata_files],
            },
        }
    except Exception as e:
        logger.warning(f"\n\n❌ 校验器执行异常\n\n{str(e)}")
        return {
            "original_msg": str(e),
            "frontend_msg": f"\n\n❌ 校验器执行异常\n\n{str(e)}",
            "data": {
                "valid": True,
                "summary": str(e),
                "details": [],
                "tokens_used": 0,
                "metadata_files": [str(file.name) for file in metadata_files],
            },
        }

    # =========================
    # 7. Parse Result
    # =========================
    valid = result_json.get("valid", False)
    summary = result_json.get("summary", "")
    details = result_json.get("details", [])
    matched_count = sum(1 for item in details if item.get("status") in ["exact_match", "possible_match"])
    not_found_count = sum(1 for item in details if item.get("status") == "not_found")

    # =========================
    # 8. Build Messages
    # =========================
    msg = (
        "\n\n✅ 校验完成\n\n"
        f"校验摘要: {summary}\n"
        f"匹配实体数: {matched_count}\n"
        f"未匹配实体数: {not_found_count}\n"
        f"元数据文件数: {len(metadata_files)}"
    )

    # =========================
    # 9. Return
    # =========================
    return {
        "original_msg": msg,
        "frontend_msg": msg,
        "data": {
            "valid": valid,
            "summary": summary,
            "details": details,
            "tokens_used": tokens_used,
            "metadata_files": [str(file.name) for file in metadata_files],
        },
    }


async def wrapped_nl2sql_sub_agent_tool(
    query: str,
    source_table: list[str],
    target_table: str,
    sql_filename: str,
    csv_filename: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Call nl2sql (natural language to sql) subagent to write sql scripts. One SQL query at a time.

    This function is intended to perform a thorough sql file generation from scratch.
    If you only need small edits to existing sql files, use `write_file` or `edit_file` tools.

    A good query should explicitly describe:
    - business goal and statistical intent
    - entity definitions and metric formulas
    - required joins and matching keys
    - filters, grouping granularity, aggregations, and sorting
    - intermediate computation logic
    - output fields and final result format

    Args:
        - query (str): Natural language query.
        - source_table (list[str]): list of table names that this nl2sql task may require as source tables.
        - target_table (str): name of output table in nl2sql task.
        - sql_filename (str): Filename for the generated SQL (with .sql extension).
        - csv_filename (str): Filename for query results (with .csv extension).

    Returns:
        dict[str, str], original and fronted message to be read by agent
    """
    # 语义感知增强-元数据增强模块 基础URL和认证
    client = SemanticServiceClient.from_config(_tool_context.config_manager)

    # 使用 get_table_schema 获取每个表的列信息和 original_msg
    tables_with_columns = {}
    for table_name in source_table:
        schema_result = get_table_schema(table_name=table_name, _tool_context=_tool_context)
        table_data = schema_result.get("data", {})
        columns = table_data.get("columns", [])

        # 调用 get_table_description 获取表描述
        try:
            table_qualified_name = f"{table_name}@hive"
            table_description = get_table_description(table_qualified_name, client)
        except Exception:
            table_description = ""

        tables_with_columns[table_name] = {
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

    # 构建 schema_ir 并保存到 schema_schemair.md
    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path:
        _build_schema_ir_for_nl2sql_v2(
            tables_with_columns, str(workspace_path), table_names=source_table, _tool_context=_tool_context
        )

    result = await nl2sql_sub_agent_tool(
        query=query, sql_filename=sql_filename, csv_filename=csv_filename, _tool_context=_tool_context
    )
    return result


def _build_schema_ir_for_nl2sql_v2(
    tables_with_columns: dict,
    workspace_path: str,
    save_file: bool = True,
    *,
    table_names: list[str] | None = None,
    _tool_context: ToolExecutionContext | None = None,
) -> dict[str, Any]:
    """
    基于表列数据构建 nl2sql 使用的 schema 中间表示。

    Args:
        tables_with_columns: 表列信息字典，格式为：
            {
                "table_name": {
                    "table_description": "表描述",
                    "columns": [
                        {"column_name": "列名", "column_description": "列描述", "column_type": "类型"},
                        ...
                    ]
                },
                ...
            }
        workspace_path: 工作空间路径
        save_file: 是否保存 schema_ir 文件
        table_names: 表名列表，用于获取 join 关系
        _tool_context: 工具执行上下文

    Returns:
        schema_ir 字典
    """
    schema_ir: dict[str, Any] = {}

    for table_name, table_info in tables_with_columns.items():
        schema_ir[table_name] = {
            "description": table_info.get("table_description", ""),
            "columns": {},
        }

        columns_list = table_info.get("columns", [])
        for col_info in columns_list:
            column_name = col_info.get("column_name", "")
            if column_name:
                schema_ir[table_name]["columns"][column_name] = {
                    "value_type": col_info.get("column_type"),
                    "description": col_info.get("column_description"),
                    "example_values": None,
                }

    # 获取表的 join 关系
    join_relations: list[dict[str, Any]] = []
    if table_names and _tool_context:
        join_result = get_join_relations(table_names=table_names, _tool_context=_tool_context)
        join_relations = join_result.get("data", {}).get("joins", [])

    # 保存 schema_ir 和 join_relations 中间表示
    if save_file and schema_ir:
        save_path = os.path.join(workspace_path, "schema_schemair.md")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("schema_ir = ")
            f.write(json.dumps(schema_ir, ensure_ascii=False, indent=2))
            f.write("\n\n")
            f.write("join_relations = ")
            f.write(json.dumps(join_relations, ensure_ascii=False, indent=2))

    return schema_ir
