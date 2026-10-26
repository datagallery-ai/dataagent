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
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.sub_agent_config import temporary_sub_agent_config
from dataagent.actions.tools.local_tool.tools import _resolve_bound_llm_model_name, sub_agent_tool
from dataagent.actions.tools.semantic_tool.search_tables_with_schema import inject_enum_values_config
from dataagent.utils.constants import DEFAULT_SUBAGENT_TOOL_TIMEOUT
from dataagent.utils.info_utils import get_current_query
from dataagent.utils.runtime_paths import dataagent_package_root, resolve_history_sql_corpus_path

HISTORY_SQL_MARKER = "历史sql"
HISTORY_SQL_DISABLED_MSG = (
    "历史SQL检索功能未启用（HISTORY_SQL.enabled=false）。当前部署未开启该能力--"
    "这不是⌈已检索但无匹配⌋，请勿据此推断不存在相关历史SQL，直接跳过历史SQL引用即可。"
)
_HISTORY_SQL_TOOL_NAMES = frozenset({"grep", "glob"})
_HISTORY_SQL_SKILL_DIR_SUFFIX = "metadata_recall/skill"
_HISTORY_SQL_ROUTE_PREFIXES = ("历史SQL路由", "- 历史SQL路由")


def _history_sql_enabled(config_manager: Any) -> bool:
    """Return whether HISTORY_SQL.enabled is on; defaults to false when unset."""
    value = config_manager.get("HISTORY_SQL.enabled", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _is_history_sql_query(query: str) -> bool:
    """"Return whether the recall query asks for history-SQL workflow."""
    return isinstance(query, str) and HISTORY_SQL_MARKER in query.lower()


def _instructions_have_history_sql_route(instructions: str) -> bool:
    """Return whether any instruction line routes to the history-SQL workflow."""
    return any(line.lstrip().startswith(_HISTORY_SQL_ROUTE_PREFIXES) for line in instructions.splitlines())


def _has_history_sql_bits(config: dict[str, Any]) -> bool:
    """Return whether the sub-agent config still carries any history-SQL bits.

    Detection is permissive on purpose (any of the four bits counts) so that
    the OFF-state strip cannot silently miss a drifted anchor; the golden
    schema variant yaml carries none of these bits and stays untouched.
    """
    agent_config = config.get("AGENT_CONFIG")
    if isinstance(agent_config, dict):
        description = agent_config.get("description")
        if isinstance(description, str) and HISTORY_SQL_MARKER in description.lower():
            return True
    scenario = config.get("SCENARIO")
    if isinstance(scenario, dict):
        chat = scenario.get("chat")
        if isinstance(chat, dict):
            instructions = chat.get("instructions")
            if isinstance(instructions, str) and _instructions_have_history_sql_route(instructions):
                return True
    tools = config.get("TOOLS")
    if not isinstance(tools, dict):
        return False
    builtin = tools.get("builtin")
    if isinstance(builtin, list) and any(
        isinstance(item, dict) and item.get("name") in _HISTORY_SQL_TOOL_NAMES for item in builtin
    ):
        return True
    skills = tools.get("skills")
    if isinstance(skills, dict):
        custom_dirs = skills.get("custom_dirs")
        if isinstance(custom_dirs, list) and any(
            isinstance(item, str) and item.rstrip("/").endswith(_HISTORY_SQL_SKILL_DIR_SUFFIX)
            for item in custom_dirs
        ):
            return True
    return False


def _strip_history_sql_bits(config: dict[str, Any]) -> None:
    """Remove history-SQL bits from the sub-agent config in place (enabled=false).

    Anchors tolerate upstream text drift: description is rebuilt from
    ``；``-separeted segments, the routing instruction line is dropped by its
    line prefix, builtin grep/glob are filtered structurally by name, and the
    skill dir is dropped by its suffix.
    """
    agent_config = config.get("AGENT_CONFIG")
    if isinstance(agent_config, dict):
        description = agent_config.get("description")
        if isinstance(description, str) and HISTORY_SQL_MARKER in description.lower():
            kept_segments = [seg for seg in description.split("；") if HISTORY_SQL_MARKER not in seg.lower()]
            agent_config["description"] = "；".join(kept_segments)
    scenario = config.get("SCENARIO")
    if isinstance(scenario, dict):
        chat = scenario.get("chat")
        if isinstance(chat, dict):
            instructions = chat.get("instructions")
            if isinstance(instructions, str) and _instructions_have_history_sql_route(instructions):
                chat["instructions"] = _strip_history_sql_route_lines(instructions)
    tools = config.get("TOOLS")
    if not isinstance(tools, dict):
        return
    builtin = tools.get("builtin")
    if isinstance(builtin, list):
        tools["builtin"] = [
            item
            for item in builtin
            if not (isinstance(item, dict) and item.get("name") in _HISTORY_SQL_TOOL_NAMES)
        ]
    skills = tools.get("skills")
    if isinstance(skills, dict):
        custom_dirs = skills.get("custom_dirs")
        if isinstance(custom_dirs, list):
            remaining = [
                item
                for item in custom_dirs
                if not (isinstance(item, str) and item.rstrip("/").endswith(_HISTORY_SQL_SKILL_DIR_SUFFIX))
            ]
            if remaining:
                skills["custom_dirs"] = remaining
            else:
                skills.pop("custom_dirs", None)
            if not skills:
                tools.pop("skills", None)


def _strip_history_sql_route_lines(instructions: str) -> str:
    """Drop history-SQL routing instruction lines, preserving the trailing newline."""
    kept_lines = [
        line for line in instructions.split("\n") if not line.lstrip().startswith(_HISTORY_SQL_ROUTE_PREFIXES)
    ]
    result = "\n".join(kept_lines)
    if instructions.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


async def metadata_recall(
    query: str,
    add_user_query: bool = False,
    timeout: int = DEFAULT_SUBAGENT_TOOL_TIMEOUT,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """从元数据系统检索与查询相关的表、列字段、指标、UDF、Join 关系等信息；或按"用户行为+输出主键"标准从本地历史SQL文件库检索相似历史SQL作为参考。

    适用场景：
    - 需要从元数据系统获取表、列字段、指标等业务语义信息
    - NL2SQL 任务需要表列 Schema 信息作为背景知识
    - 需要检索 UDF 函数的功能和使用方式
    - 需要检索表之间的可连接列的 Join 关系
    - 需要检索相似历史SQL作为新特征开发任务的参考（继承过滤口径、模仿列用法与 UDF 用法、辅助选表甄别）。检索标准：必须满足"用户行为相同 + 历史SQL输出主键与本次输出主键相同"，源表已确认时可要求"历史SQL源表必须包含该表"；特征类型/时间周期/排序维度相同为加分项。query 写法示例：
      "检索历史SQL作为参考。要求：输出特征1：exposure_cnt_23d（用户行为：曝光）；本次输出主键：imei + app_id；加分项：统计特征、23填周期；源表必须包含：biads.ads_noah_common_user_exposure_cnt_one_day_dm"

    注意：
    - 不可以频繁调用这个工具，需要全面理解用户意图并整理好所有的检索需求，再一次性调用这个工具，太多次调用这个工具会导致召回精度差，但是漏召回相关信息会导致召回准确率差
    - add_user_query 参数最多只能有一次调用的时候传入 True，在需要广泛搜索与会话用户原始 query 直接相关的信息时才可以填 True
    - 如果只是局部检索/召回确定名称的表/列/UDF/检索 Join 关系时，只需要在 query 参数中描述检索需求，add_user_query 不需要填写

    工作方式：
    1. 启动专用 subagent（带 get_table_schema、search_udf_function_by_name_keyword、
       search_udf_function_by_dsl、get_join_relations 工具）
    2. Subagent 根据 query 语义调用合适的元数据 API
    3. 返回结构化的召回结果

    Args:
        query: 自然语言表达需要查询的元数据表、列字段、指标、UDF、Join 关系等信息，或历史SQL检索需求（含特征+行为标注、本次输出主键、可选源表要求、加分项），可以额外加入一些检索逻辑的指示
        add_user_query: 是否需要将原始的完整用户 query 信息一起告诉 subagent，默认为 False
        timeout (int): 超时秒数，默认 3600

    Returns:
        {"original_msg": 召回结果摘要, "frontend_msg": 前端展示摘要, "data": 结构化数据}
    """
    if not _history_sql_enabled(_tool_context.config_manager) and _is_history_sql_query(query):
        return {"original_msg": HISTORY_SQL_DISABLED_MSG, "frontend_msg": HISTORY_SQL_DISABLED_MSG, "data": {}}

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

    with temporary_sub_agent_config(
        temp_config, prefix="metadata_recall_sub_agent_", workspace_root=workspace_path
    ) as temp_config_path:
        res = await sub_agent_tool(query=enhanced_query, config_path=temp_config_path, timeout=timeout)

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
    """构建 metadata_recall subagent 的临时配置，注入主 Agent 的模型配置、workspace 及列枚举值配置。"""
    temp_config = copy.deepcopy(source_config)

    bound_llm_model_name = _resolve_bound_llm_model_name(tool_config=tool_config)
    if bound_llm_model_name:
        runtime_model = config_manager.get(f"MODEL.{bound_llm_model_name}")
        if isinstance(runtime_model, dict) and runtime_model:
            runtime_model_copy = copy.deepcopy(runtime_model)
            runtime_params = runtime_model_copy.get("params")
            if isinstance(runtime_params, dict):
                runtime_params["temperature"] = 0.1
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

    # 保留主配置的 WORKSPACE.allow_path：历史SQL检索等本地文件工具依赖这些只读根目录，
    # 丢失会导致子 Agent 既无读权限、prompt 中也看不到路径，只能盲搜 workspace 而漏召回
    workspace_config: dict[str, Any] = {"path": str(workspace_root.resolve())}
    main_allow_paths = config_manager.get("WORKSPACE.allow_path")
    if isinstance(main_allow_paths, (list, tuple)):
        workspace_config["allow_path"] = list(main_allow_paths)
    elif main_allow_paths:
        workspace_config["allow_path"] = [str(main_allow_paths)]
    temp_config["WORKSPACE"] = workspace_config

    # 历史SQL三层门控：子代理 yaml 带历史SQL bits时，ON = 注入语料只读根；
    # OFF = 原地摘除全部 bits（含残留检测告警）。golden 变体 yaml 无 bits，整块跳过
    if _has_history_sql_bits(temp_config):
        if _history_sql_enabled(config_manager):
            corpus_path = resolve_history_sql_corpus_path()
            allow_paths = workspace_config.setdefault("allow_path", [])
            if str(corpus_path) not in allow_paths:
                allow_paths.append(str(corpus_path))
        else:
            _strip_history_sql_bits(temp_config)
            if _has_history_sql_bits(temp_config):
                logger.warning(
                    "metadata_recall_tool: HISTORY_SQL.enabled=false 但摘除后仍检测到历史SQL残留，"
                    "strip 锚点可能随 metadata_recall_agent.yaml 上游漂移，请核对"
                )

    semantic_layer = config_manager.get("SEMANTIC_LAYER")
    database_id = config_manager.get("DATABASE.db_id")
    if semantic_layer:
        temp_config["SEMANTIC_LAYER"] = semantic_layer
    temp_config["DATABASE"] = {"db_id": database_id} if database_id else {}

    inject_enum_values_config(temp_config, tool_config)

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
