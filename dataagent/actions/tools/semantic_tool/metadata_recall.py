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
import copy
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.tools import _resolve_bound_llm_model_name, sub_agent_tool
from dataagent.utils.constants import DEFAULT_SUBAGENT_TOOL_TIMEOUT
from dataagent.utils.info_utils import get_current_query
from dataagent.utils.runtime_paths import dataagent_package_root


async def metadata_recall(
    query: str,
    add_user_query: bool = False,
    timeout: int = DEFAULT_SUBAGENT_TOOL_TIMEOUT,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """从元数据系统检索与查询相关的表、列字段、指标、UDF、Join 关系等信息。

    适用场景：
    - 需要从元数据系统获取表、列字段、指标等业务语义信息
    - NL2SQL 任务需要表列 Schema 信息作为背景知识
    - 需要检索 UDF 函数的功能和使用方式
    - 需要检索表之间的可连接列的 Join 关系

    注意：
    - 不可以频繁调用这个工具，需要全面理解用户意图并整理好所有的检索需求，再一次性调用这个工具，太多次调用这个工具会导致召回精度差，但是漏召回相关信息会导致召回准确率差
    - add_user_query 参数最多只能有一次调用的时候传入 True，在需要广泛搜索与会话用户原始 query 直接相关的信息时才可以填 True
    - 如果只是局部检索/召回确定名称的表/列/UDF/检索 Join 关系时，只需要在 query 参数中描述检索需求，add_user_query 不需要填写

    工作方式：
    1. 启动专用 subagent（带 search_tables_with_typename、get_table_schema、
       search_udf_function_by_name_keyword、search_udf_function_by_dsl、
       get_join_relations 工具）
    2. Subagent 根据 query 语义调用合适的元数据 API
    3. 返回结构化的召回结果

    Args:
        query: 自然语言表达需要查询的元数据表、列字段、指标、UDF、Join 关系等信息，可以额外加入一些检索逻辑的指示
        add_user_query: 是否需要将原始的完整用户 query 信息一起告诉 subagent，默认为 False
        timeout (int): 超时秒数，默认 3600

    Returns:
        {"original_msg": 召回结果摘要, "frontend_msg": 前端展示摘要, "data": 结构化数据}
    """
    source_config_path = dataagent_package_root() / "agents" / "metadata_recall" / "metadata_recall_agent.yaml"
    with source_config_path.open(encoding="utf-8") as f:
        source_config = yaml.safe_load(f) or {}

    guard = get_current_sandbox()
    workspace_path = guard.workspace_root or Path.cwd().resolve()

    temp_config = _build_metadata_recall_sub_agent_config(
        source_config,
        config_manager=_tool_context.config_manager,
        tool_config=_tool_context.tool_config,
        workspace_root=workspace_path,
    )

    # 读取主 Agent 保存的 query
    user_query_str = get_current_query(_tool_context.runtime) if _tool_context.runtime else ""
    enhanced_query = f"用户本轮的检索需求：{query}."
    if add_user_query:
        enhanced_query += (
            f"\n需要同时检索和原始任务相关的元数据、UDF和Join 信息，以下是原始任务的描述：{user_query_str}"
        )

    temp_root = workspace_path
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="metadata_recall_sub_agent_",
        dir=temp_root,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        yaml.safe_dump(temp_config, temp_file, allow_unicode=False, sort_keys=False)
        temp_config_path = temp_file.name

    try:
        res = await sub_agent_tool(query=enhanced_query, config_path=temp_config_path, timeout=timeout)
    finally:
        Path(temp_config_path).unlink(missing_ok=True)

    # sub_agent_tool 返回 {"original_msg": <worker_result>, "state": <flex state dict>, ...}
    # state 包含完整的 messages 列表，需提取最后一条 AIMessage 的 content 作为干净结果
    return _extract_recall_result_from_state(res)


def _build_metadata_recall_sub_agent_config(
    source_config: dict[str, Any],
    *,
    config_manager: Any,
    tool_config: dict[str, Any] | None = None,
    workspace_root: Path,
) -> dict[str, Any]:
    """构建 metadata_recall subagent 的临时配置，注入主 Agent 的模型配置和 workspace。"""
    temp_config = copy.deepcopy(source_config)

    bound_llm_model_name = _resolve_bound_llm_model_name(tool_config=tool_config)
    if bound_llm_model_name:
        runtime_model = config_manager.get(f"MODEL.{bound_llm_model_name}")
        if isinstance(runtime_model, dict) and runtime_model:
            runtime_model_copy = copy.deepcopy(runtime_model)
            runtime_params = runtime_model_copy.get("params")
            if isinstance(runtime_params, dict):
                runtime_params["temperature"] = 0.0
            temp_config["MODEL"] = {
                bound_llm_model_name: runtime_model_copy,
            }
            # 同步更新 ACTOR_LOOP 中 planner 的 chat_model.name
            actor_loop = temp_config.get("ACTOR_LOOP")
            if isinstance(actor_loop, list):
                for node_cfg in actor_loop:
                    if not isinstance(node_cfg, dict):
                        continue
                    chat_model = node_cfg.get("chat_model")
                    if isinstance(chat_model, dict):
                        chat_model["name"] = bound_llm_model_name

    temp_config["WORKSPACE"] = {"path": str(workspace_root.resolve())}

    semantic_layer = config_manager.get("SEMANTIC_LAYER")
    database_id = config_manager.get("DATABASE.db_id")
    if semantic_layer:
        temp_config["SEMANTIC_LAYER"] = semantic_layer
    temp_config["DATABASE"] = {"db_id": database_id} if database_id else {}

    return temp_config


def _extract_recall_result_from_state(res: dict[str, Any]) -> dict[str, Any]:
    """从 sub_agent_tool 返回的结果中提取召回结果。

    sub_agent_tool 返回 {
        "original_msg": <worker_result dict>,
        "frontend_msg": ...,
        "state": <flex state dict>,
        "sub_id": ...,
    }，其中 state 是完整的 Flex graph 最终状态（包含 messages 列表），
    original_msg 是 worker_result 结构体。

    返回格式：
    {
        "original_msg": str,   # 最终摘要文本
        "frontend_msg": str,
        "data": {
            "recall_result_path": str,  # 召回结果文件路径
            "num_entries": int,
        }
    }
    """
    frontend_msg = res.get("frontend_msg", "")

    # 从 worker_result 检查子 Agent 执行错误
    original_msg = res.get("original_msg")
    if isinstance(original_msg, dict) and original_msg.get("error"):
        err_msg = f"元数据召回 Agent 执行失败：{original_msg['error']}"
        return {"original_msg": err_msg, "frontend_msg": err_msg}

    # 从 state 字段获取完整的 Flex graph 最终状态（包含 messages）
    state = res.get("state")
    if not isinstance(state, dict):
        logger.warning("metadata_recall_tool: state 不可用，回退为文本化输出")
        text = json.dumps(original_msg, ensure_ascii=False)
        return {"original_msg": text, "frontend_msg": frontend_msg or text}

    messages_raw = state.get("messages")
    if not isinstance(messages_raw, list) or not messages_raw:
        logger.warning("metadata_recall_tool: state dict 中未找到 messages，回退为文本化输出")
        text = json.dumps(state, ensure_ascii=False)
        return {"original_msg": text, "frontend_msg": frontend_msg or text}

    # 检查消息是 dict 还是 str（取决于 _to_jsonable 是否正确序列化了 Pydantic 模型）
    sample = messages_raw[0]
    if isinstance(sample, dict):
        messages = messages_raw  # Pydantic model_dump 路径 → dict 消息
    elif isinstance(sample, str):
        messages = messages_raw  # _to_jsonable str() 回退路径 → 字符串消息
    else:
        # LangChain 原生对象（不应该出现，以防万一）
        messages = [str(m) for m in messages_raw]

    # 提取最后一条 AIMessage 的 content 作为摘要
    ai_content: str | None = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("type") == "AIMessage":
            c = msg.get("content", "")
            if c:
                ai_content = str(c)
                break
    if ai_content is None:
        ai_content = _find_ai_msg_in_strings(messages)

    # 如果没有 AI content，回退
    if not ai_content:
        logger.warning("metadata_recall_tool: 未找到 AIMessage，回退为文本化输出")
        text = json.dumps(state, ensure_ascii=False)
        return {"original_msg": text, "frontend_msg": frontend_msg or text}

    # 组装返回结果
    result_msg = ai_content.strip()

    return {
        "original_msg": result_msg,
        "frontend_msg": result_msg,
        "data": None,
    }


def _find_ai_msg_in_strings(messages: list[Any]) -> str | None:
    """在（可能为字符串的）消息列表中查找最后一条 AI 消息，返回其 content。"""
    for msg in reversed(messages):
        msg_str = str(msg) if not isinstance(msg, str) else msg
        # AI message: has content= and additional_kwargs=, no name= or tool_call_id=
        if "additional_kwargs=" in msg_str and "name=" not in msg_str and "tool_call_id=" not in msg_str:
            content = _parse_str_message_content(msg_str)
            if content:
                return content
    return None


def _parse_str_message_content(msg_str: str) -> str | None:
    """从 str() 序列化的 LangChain 消息字符串中提取 content 字段值。

    ``_to_jsonable`` 对 Pydantic 模型回退到 ``str(obj)``，产出形如
    ``content='...' name='tool' tool_call_id='x'`` 的字符串。
    本函数用正则提取 content 值（兼容单/双引号包裹），返回 content 原始文本。"""
    m = re.search(r"content=([\"'])(.*?)\1\s+(?:name=|additional_kwargs=|tool_call_id=)", msg_str, re.DOTALL)
    return m.group(2) if m else None
