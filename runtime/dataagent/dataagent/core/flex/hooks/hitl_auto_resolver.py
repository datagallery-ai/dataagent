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
"""HIL Auto Resolver Hook.

当 Agent 触发 HIL (request_human_feedback) 时，自动调用 LLM 生成回复：
- 从 messages[0] 获取原始 query
- 通过 JSON 文件匹配获取 golden_sql_path
- 调用 LLM 生成符合人类风格的回复
- 注入 ToolMessage 继续执行

匹配模式：
1. 直接模式（默认）：配置 hitl_task_index_path
   - 根据 query 在 JSON 中匹配获取 task
   - 直接读取 task.golden_sql_path 指定的文件
2. 目录匹配模式（兜底）：仅配置 hitl_golden_sql_dir
   - 在目录中匹配文件名包含 query 关键词的 .sql 文件
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState

HUMAN_FEEDBACK_TOOL_NAME = "request_human_feedback"
_HITL_AUTO_RESOLVER_PROMPT = """你是 SQL 领域的专家。用户在等待对以下问题的回复。

## 原始问题
{original_query}

## 标准答案 SQL
{golden_sql}

## Agent 提出的 HIL 问题
{agent_hil_question}

请结合原始问题和标准答案，用简洁、自然的语言回答 Agent 的问题。
回答应该帮助 Agent 理解用户意图和标准答案，从而顺利完成任务。
直接给出回答即可，不需要额外的格式。"""


def hitl_auto_resolver(
    state: FlexState,
    runtime: Runtime | None,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    """HIL 自动回复 Hook.

    触发条件：
    1. 配置了 hitl_auto_resolve: true
    2. 配置了 hitl_golden_sql_dir
    3. messages 中存在 request_human_feedback 工具调用

    匹配模式：
    - 直接模式（默认）：配置 hitl_task_index_path
      根据 query 在 JSON 中匹配 task，直接读取 task.golden_sql_path
    - 目录匹配模式（兜底）：仅配置 hitl_golden_sql_dir
      在目录中匹配文件名包含 query 关键词的 .sql 文件

    Args:
        state: FlexState
        runtime: Runtime（含 llm 工厂与 config）
        original_state: 原始 state（优先从此处读取配置）

    Returns:
        修改后的 state（含注入的 ToolMessage）
    """
    if not _is_auto_resolve_enabled(state, original_state, runtime):
        return state

    messages = state.get("messages", [])
    if not messages:
        return state

    # 1. 检测 HIL 调用
    feedback_call = _find_human_feedback_call(messages)
    if feedback_call is None:
        logger.debug("[hitl_auto_resolver] 未找到 request_human_feedback 工具调用，跳过")
        return state

    # 2. 获取原始 query
    original_query = _extract_original_query(messages)
    if not original_query:
        logger.warning("[hitl_auto_resolver] 无法获取原始 query，跳过")
        return state

    logger.debug(f"[hitl_auto_resolver] 原始 query: {original_query[:80]!r}...")

    # 3. 匹配 golden SQL
    matched_task = _try_match_golden_sql(original_query, state, original_state, runtime)
    if not matched_task:
        return state

    # 4. 提取 HIL 问题并生成回复
    result = _generate_and_inject_reply(feedback_call, messages, original_query, matched_task, state, runtime)
    if result is None:
        return state

    updated_messages, resolved_info = result

    # 5. 更新 ActionNode 并构建返回值
    return _build_hook_return(state, updated_messages, resolved_info)


def _try_match_golden_sql(
    original_query: str,
    state: Any,
    original_state: Any,
    runtime: Any,
) -> dict | None:
    """通过 JSON 索引匹配 golden SQL。"""
    task_index_path = _get_task_index_path(state, original_state, runtime)
    if not task_index_path:
        logger.debug("[hitl_auto_resolver] 未配置 hitl_task_index_path，跳过")
        return None

    golden_sql, matched_task = _match_golden_sql_by_path(original_query, task_index_path)
    if not golden_sql:
        logger.debug(f"[hitl_auto_resolver] JSON 索引未匹配到 golden SQL，query={original_query[:50]!r}")
        return None

    logger.debug(
        f"[hitl_auto_resolver] 匹配成功，task={matched_task.get('name')}, score={matched_task.get('score', 'N/A')}"
    )
    return matched_task


def _generate_and_inject_reply(
    feedback_call: dict,
    messages: list,
    original_query: str,
    matched_task: dict,
    state: Any,
    runtime: Any,
) -> tuple[list, dict] | None:
    """提取 HIL 问题、生成回复并注入 ToolMessage。"""
    hil_question = _extract_hil_question(feedback_call)
    if not hil_question:
        logger.debug("[hitl_auto_resolver] 无法提取 HIL 问题内容，跳过")
        return None, None

    # 从 matched_task 获取 golden_sql
    golden_sql_path = matched_task.get("golden_sql_path", "")
    try:
        sql_file = Path(golden_sql_path)
        if not sql_file.is_absolute():
            index_path = state.get("_task_index_path", "")
            if index_path:
                sql_file = Path(index_path).parent / sql_file
        golden_sql = sql_file.read_text(encoding="utf-8").strip()
    except Exception:
        golden_sql = matched_task.get("golden_sql", "")

    # 调用 LLM 生成回复
    answer = _generate_answer(original_query, golden_sql, hil_question, runtime)
    if not answer:
        logger.warning("[hitl_auto_resolver] LLM 生成回复失败，跳过")
        return None, None

    # 注入 ToolMessage
    tool_call_id = feedback_call.get("id", "")
    updated_messages = _inject_tool_message(messages, tool_call_id, answer)
    logger.debug("[hitl_auto_resolver] 自动回复已注入")

    # 更新 ActionNode
    _update_action_node(state, tool_call_id, answer)

    # 构建 resolved_info
    resolved_info = {
        "question": hil_question,
        "answer": answer,
        "task_name": matched_task.get("name"),
        "task_id": matched_task.get("task_id"),
        "golden_sql_path": golden_sql_path,
    }

    return updated_messages, resolved_info


def _update_action_node(state: Any, tool_call_id: str, answer: str) -> None:
    """更新 context 中的 ActionNode，使 output/success 不再是 Pending/False。"""
    from dataagent.core.context.context import ContextFactory

    try:
        uid = str(state.get("user_id", "anonymous"))
        sid = str(state.get("session_id", "default_session"))
        rid = int(state.get("run_id", 0))
        subid = int(state.get("sub_id", 0))
        ctx = ContextFactory.get_context(uid, sid, rid, subid)
        ctx.modify_node(
            graph_node_label=f"Action({tool_call_id})",
            changes={"output": answer, "success": True},
        )
        logger.debug(f"[hitl_auto_resolver] 更新 ActionNode({tool_call_id}): output 已更新")
    except Exception:
        logger.debug(f"[hitl_auto_resolver] 更新 ActionNode({tool_call_id}) 失败（context 可能不可用）")


def _build_hook_return(state: Any, updated_messages: list, resolved_info: dict) -> dict:
    """构建 hook 返回值，处理 GlobalStateProxy 的未提交本地更新问题。"""
    from dataagent.core.framework_adapters.runtime.context import GlobalStateProxy as _GSP

    if isinstance(state, _GSP):
        base = dict(state)
        base.update(state.local_updates())
        return {
            **base,
            "messages": updated_messages,
            "hitl_auto_resolved": True,
            "hitl_resolved_info": resolved_info,
            "need_human_feedback": False,
        }
    return {
        **state,
        "messages": updated_messages,
        "hitl_auto_resolved": True,
        "hitl_resolved_info": resolved_info,
        "need_human_feedback": False,
    }


def _is_auto_resolve_enabled(
    state: Any,
    original_state: Any,
    runtime: Any,
) -> bool:
    """检查是否启用 HIL 自动回复。"""
    source = original_state if original_state is not None else state
    if source.get("hitl_auto_resolve"):
        logger.debug("[hitl_auto_resolver] 从 state 中读取 hitl_auto_resolve=True")
        return True

    result = _runtime_auto_resolve_enabled(runtime)
    if result:
        logger.debug("[hitl_auto_resolver] 从 runtime 配置中读取 hitl_auto_resolve=True")
    else:
        logger.warning("[hitl_auto_resolver] hitl_auto_resolve 未启用")
    return result


def _runtime_auto_resolve_enabled(runtime: Any) -> bool:
    """从 runtime 配置中检查是否启用。"""
    if runtime is None:
        logger.warning("[hitl_auto_resolver] runtime 为 None，无法读取配置")
        return False
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        logger.warning("[hitl_auto_resolver] runtime.get_all_config 不可调用")
        return False
    try:
        config = get_all_config()
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] get_all_config() 调用失败: {e}")
        return False

    agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, dict) else {}
    hitl_auto_resolve = agent_config.get("hitl_auto_resolve", False)
    logger.debug(
        f"[hitl_auto_resolver] runtime 配置检查: AGENT_CONFIG.hitl_auto_resolve={hitl_auto_resolve}, "
        f"config_keys={list(config.keys()) if isinstance(config, dict) else type(config)}"
    )
    return bool(hitl_auto_resolve)


def _find_human_feedback_call(messages: list) -> dict | None:
    """查找最后一个 request_human_feedback 工具调用。"""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        for call in tool_calls:
            if isinstance(call, dict):
                name = call.get("name", "")
            elif hasattr(call, "name"):
                name = call.name
            else:
                continue
            if name == HUMAN_FEEDBACK_TOOL_NAME:
                return call
    return None


def _extract_original_query(messages: list) -> str:
    """从 messages[0] 提取原始 query。

    只支持从 <user_query>...</user_query> 标签中提取内容。
    """
    if not messages:
        return ""
    first_msg = messages[0]
    content = ""

    if isinstance(first_msg, HumanMessage):
        content = getattr(first_msg, "content", "")
    elif isinstance(first_msg, dict):
        content = str(first_msg.get("content", ""))
    else:
        content = str(first_msg)

    if not content:
        return ""

    # 如果 content 是对象字符串表示（而不是实际文本），返回空
    if content.startswith("content=") or not any(c.isalpha() for c in content):
        return ""

    # 提取 <user_query>...</user_query> 标签中的内容

    tag_pattern = r"<user_query>(.*?)</user_query>"
    match = re.search(tag_pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()

    # 没有找到 <user_query> 标签，返回空
    return ""


def _get_task_index_path(
    state: Any,
    original_state: Any,
    runtime: Any,
) -> str | None:
    """获取 JSON 索引文件路径。"""
    source = original_state if original_state is not None else state
    if source.get("hitl_task_index_path"):
        path = str(source.get("hitl_task_index_path"))
        logger.debug(f"[hitl_auto_resolver] 从 state 读取 hitl_task_index_path: {path}")
        return path

    if runtime is None:
        logger.warning("[hitl_auto_resolver] runtime 为 None，无法读取配置")
        return None
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        logger.warning("[hitl_auto_resolver] runtime.get_all_config 不可调用")
        return None
    try:
        config = get_all_config()
        agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, dict) else {}
        path = agent_config.get("hitl_task_index_path")
        if path:
            logger.debug(f"[hitl_auto_resolver] 从 runtime 配置读取 hitl_task_index_path: {path}")
        else:
            logger.warning(f"[hitl_auto_resolver] runtime 配置中没有 hitl_task_index_path，config={config}")
        return path
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 runtime 配置失败: {e}")
        return None


def _match_golden_sql_by_path(query: str, index_path: str) -> tuple[str | None, dict | None]:
    """通过 JSON 文件匹配 golden SQL（直接读取 golden_sql_path）。

    流程：
    1. 读取 JSON 索引文件（包含 query 和 golden_sql_path 字段）
    2. 根据 query 匹配获取对应的 task
    3. 直接读取 task.golden_sql_path 指定的文件

    匹配策略：综合考虑包含匹配和关键词匹配

    返回：(golden_sql, matched_task)
    """
    index_file = _resolve_index_file(index_path)
    if index_file is None:
        return None, None

    tasks = _load_tasks_from_index(index_file)
    if tasks is None:
        return None, None

    best_match, best_score = _find_best_task_match(query, tasks)
    if not best_match or best_score == 0:
        logger.warning(f"[hitl_auto_resolver] 未找到匹配的 task，query={query[:80]!r}")
        return None, None

    logger.debug(f"[hitl_auto_resolver] 匹配到 task: name={best_match.get('name')}, score={best_score}")
    return _read_golden_sql(best_match, index_file)


def _resolve_index_file(index_path: str) -> Path | None:
    """解析并验证 JSON 索引文件路径。"""
    index_file = Path(index_path)
    logger.debug(f"[hitl_auto_resolver] 检查 JSON 文件: {index_file} (绝对路径: {index_file.resolve()})")
    if index_file.is_file():
        return index_file

    logger.warning(f"[hitl_auto_resolver] JSON 文件不存在: {index_file}，尝试相对于 cwd: {Path.cwd()}")
    alt_path = Path.cwd() / index_path
    if alt_path.is_file():
        logger.debug(f"[hitl_auto_resolver] 使用相对路径文件: {alt_path}")
        return alt_path

    logger.warning(f"[hitl_auto_resolver] 相对路径文件也不存在: {alt_path}")
    return None


def _load_tasks_from_index(index_file: Path) -> list[dict] | None:
    """从 JSON 索引文件加载 tasks。"""
    try:
        import json as _json

        with open(index_file, encoding="utf-8") as f:
            tasks = _json.load(f)
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 JSON 文件失败: {e}")
        return None

    if not isinstance(tasks, list):
        logger.warning(f"[hitl_auto_resolver] JSON 格式错误，期望 list，实际 {type(tasks).__name__}")
        return None

    logger.debug(f"[hitl_auto_resolver] JSON 文件加载成功，共 {len(tasks)} 个 task")
    return tasks


def _calculate_match_score(query_lower: str, task_query: str) -> float:
    """计算 query 与 task_query 的匹配得分。"""
    if task_query == query_lower:
        return 100000  # 精确匹配

    if query_lower in task_query:
        # query 完全包含在 task_query 中
        score = 80 + len(query_lower) / len(task_query) * 10
        logger.debug(
            f"[hitl_auto_resolver] query-in-task 子串匹配, query_len={len(query_lower)}, task_len={len(task_query)}"
        )
        return score

    if task_query in query_lower:
        # task_query 完全包含在 query 中
        score = 70 + len(task_query) / len(query_lower) * 10
        logger.debug(
            f"[hitl_auto_resolver] task-in-query 子串匹配, query_len={len(query_lower)}, task_len={len(task_query)}"
        )
        return score

    # 关键词片段匹配
    query_segments = re.findall(r"[\u4e00-\u9fff]{4,}|[a-z0-9]{4,}", query_lower)
    score = 0
    for seg in query_segments:
        if seg in task_query:
            score += 10
        else:
            for j in range(len(seg) - 3):
                if seg[j : j + 4] in task_query:
                    score += 5  # 部分匹配给较低分数
                    break
    return score


def _find_best_task_match(query: str, tasks: list[dict]) -> tuple[dict | None, float]:
    """在 tasks 中查找与 query 最佳匹配的任务。返回 (best_match, best_score)。"""
    import time

    start_time = time.time()
    query_lower = query.lower()
    logger.debug(f"[hitl_auto_resolver] 原始 query (repr): {repr(query)}, (lower repr): {repr(query_lower)}")

    best_match: dict | None = None
    best_score = 0

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue

        task_query = str(task.get("query", "")).lower()
        task_name = task.get("name", "N/A")
        golden_sql_path = task.get("golden_sql_path")

        if i < 3:
            logger.debug(f"[hitl_auto_resolver] task[{i}] ({task_name}) query (lower repr): {repr(task_query[:80])}")

        if not task_query or not golden_sql_path:
            continue

        score = _calculate_match_score(query_lower, task_query)

        if score > 0:
            logger.debug(f"[hitl_auto_resolver] task[{i}] ({task_name}) 得分: score={score}")

        if score > best_score:
            best_score = score
            best_match = task

        if score == 100000:  # 精确匹配：找到后直接结束
            logger.debug(f"[hitl_auto_resolver] task[{i}] 精确匹配: name={task.get('name')}, 立即结束匹配")
            break

        if time.time() - start_time > 5:  # 超时保护
            logger.warning(f"[hitl_auto_resolver] 匹配超时，已处理 {i + 1}/{len(tasks)} 个 task")
            break

    logger.debug(f"[hitl_auto_resolver] 匹配完成，耗时: {time.time() - start_time:.2f}s，最佳分数: {best_score}")
    return best_match, best_score


def _read_golden_sql(task: dict, index_file: Path) -> tuple[str, dict]:
    """读取 task 中 golden_sql_path 指定的 SQL 文件内容。"""
    golden_sql_path = task.get("golden_sql_path", "")
    logger.debug(f"[hitl_auto_resolver] 获取到 golden_sql_path: {golden_sql_path}")

    if not golden_sql_path:
        logger.warning("[hitl_auto_resolver] golden_sql_path 为空")
        return None, None  # type: ignore[return-value]

    sql_file = Path(golden_sql_path)
    logger.debug(f"[hitl_auto_resolver] 解析 golden_sql_path: {sql_file}, 是否绝对路径: {sql_file.is_absolute()}")

    if not sql_file.is_absolute():
        sql_file = index_file.parent / sql_file  # 相对于 JSON 文件所在目录
        logger.debug(f"[hitl_auto_resolver] 转换为绝对路径: {sql_file}")

    if not sql_file.is_file():
        logger.warning(f"[hitl_auto_resolver] golden SQL 文件不存在: {sql_file}")
        return None, None  # type: ignore[return-value]

    try:
        golden_sql = sql_file.read_text(encoding="utf-8").strip()
        logger.debug(f"[hitl_auto_resolver] 成功读取 golden SQL，长度: {len(golden_sql)} 字符")
        return golden_sql, task
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 golden SQL 失败: {e}")
        return None, None  # type: ignore[return-value]


def _find_common_substrings(s1: str, s2: str, min_len: int = 4) -> list[str]:
    """找出两个字符串中共同的有意义子串（用于中文匹配）。"""
    common = []
    # 简单滑动窗口匹配
    for length in range(min_len, min(len(s1), len(s2)) + 1):
        for i in range(len(s1) - length + 1):
            substr = s1[i : i + length]
            if substr in s2 and substr not in common:
                common.append(substr)
    return common[:5]  # 最多返回5个


def _extract_hil_question(call: Any) -> str:
    """从工具调用中提取 HIL 问题内容。"""
    # 从 pending_action 或 reason 中提取
    if isinstance(call, dict):
        args = call.get("args", {})
        pending_action = args.get("pending_action", "")
        reason = args.get("reason", "")
        return (pending_action or reason or "").strip()
    if hasattr(call, "args"):
        args = call.args or {}
        pending_action = getattr(args, "pending_action", "") or args.get("pending_action", "")
        reason = getattr(args, "reason", "") or args.get("reason", "")
        return (pending_action or reason or "").strip()
    return ""


def _generate_answer(
    original_query: str,
    golden_sql: str,
    hil_question: str,
    runtime: Any,
) -> str | None:
    """调用 LLM 生成回复。"""
    if runtime is None:
        logger.warning("[hitl_auto_resolver] runtime 为 None，无法生成回复")
        return None

    llm_getter = getattr(runtime, "llm", None)
    if not callable(llm_getter):
        logger.warning("[hitl_auto_resolver] runtime.llm 不可调用")
        return None

    try:
        llm = llm_getter("planner")
        logger.debug("[hitl_auto_resolver] LLM 获取成功，准备生成回复")
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 获取 planner LLM 失败: {e}")
        return None

    prompt = _HITL_AUTO_RESOLVER_PROMPT.format(
        original_query=original_query,
        golden_sql=golden_sql,
        agent_hil_question=hil_question,
    )

    try:
        logger.debug(f"[hitl_auto_resolver] 调用 LLM，hil_question={hil_question[:100]!r}...")
        response = llm.invoke([SystemMessage(content=prompt), HumanMessage(content="请回答。")])
        content = getattr(response, "content", None)
        if content:
            answer_str = str(content).strip()
            logger.debug(f"[hitl_auto_resolver] LLM 生成回复成功，长度={len(answer_str)}")
            logger.debug(f"[hitl_auto_resolver] LLM 回复内容: {answer_str!r}")
            return answer_str
        return None
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] LLM 调用失败: {e}")
        return None


def _inject_tool_message(messages: list, tool_call_id: str, answer: str) -> list:
    """注入 ToolMessage 到 messages。"""
    tool_message = ToolMessage(
        content=answer,
        tool_call_id=tool_call_id,
        name=HUMAN_FEEDBACK_TOOL_NAME,
    )
    return [*messages, tool_message]
