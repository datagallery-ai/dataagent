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
"""通用 HITL（human-in-the-loop）无人值守基座。

为 auto-resolver 类 hook（场景实现见 de_agent suite 的 hitl_auto_resolver）
提供的通用机制，与具体场景（评测 golden、问题类别、回答模板）解耦：

- 启用门控：``is_auto_resolve_enabled``（state/runtime 的 ``hitl_auto_resolve``）
- 交互预算：``DEFAULT_HITL_BUDGET`` / ``get_hitl_budget`` / ``get_hitl_turn_count``
  （budget 是硬兜底；``hitl_count`` 为 FlexState 真实累加器）
- 问题去重：``normalize_question`` / ``get_hitl_question_history``
  （从 messages 派生同题已答问题史，软保护）
- HITL 调用检测与载荷提取：``find_human_feedback_call`` /
  ``extract_original_query`` / ``extract_hil_question``
- 消息注入与状态契约：``inject_tool_message`` / ``inject_simple_reply`` /
  ``inject_fallback_reply`` / ``build_hook_return`` / ``update_action_node``
  （写 ``hitl_auto_resolved`` / ``hitl_resolved_info`` / ``need_human_feedback=False``，
  由 HumanFeedbackNode._handle_auto_resolved 消费）

问题分类的通用机制在 :mod:`dataagent.core.flex.hooks.hitl_classifier`
（可注册分类器框架），具体类别与关键词表由场景侧注册。
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from loguru import logger

HUMAN_FEEDBACK_TOOL_NAME = "request_human_feedback"

DEFAULT_HITL_BUDGET: int = 4
"""HITL 每题总交互预算缺省；planner 预算提示与场景 auto-resolver hook 共用。"""


def is_auto_resolve_enabled(
    state: Any,
    original_state: Any = None,
    runtime: Any = None,
) -> bool:
    """检查是否启用 HITL 自动回复（state 优先，其次 runtime 配置）。"""
    source = original_state if original_state is not None else state
    if source.get("hitl_auto_resolve"):
        logger.debug("[hitl_common] 从 state 中读取 hitl_auto_resolve=True")
        return True

    result = runtime_auto_resolve_enabled(runtime)
    if result:
        logger.debug("[hitl_common] 从 runtime 配置中读取 hitl_auto_resolve=True")
    else:
        logger.warning("[hitl_common] hitl_auto_resolve 未启用")
    return result


def runtime_auto_resolve_enabled(runtime: Any) -> bool:
    """从 runtime 配置中检查是否启用。"""
    if runtime is None:
        logger.warning("[hitl_common] runtime 为 None，无法读取配置")
        return False
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        logger.warning("[hitl_common] runtime.get_all_config 不可调用")
        return False
    try:
        config = get_all_config()
    except Exception as e:
        logger.warning(f"[hitl_common] get_all_config() 调用失败: {e}")
        return False

    agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, dict) else {}
    hitl_auto_resolve = agent_config.get("hitl_auto_resolve", False)
    logger.debug(
        f"[hitl_common] runtime 配置检查: AGENT_CONFIG.hitl_auto_resolve={hitl_auto_resolve}, "
        f"config_keys={list(config.keys()) if isinstance(config, dict) else type(config)}"
    )
    return bool(hitl_auto_resolve)


def get_hitl_budget(state: Any, original_state: Any = None) -> int:
    """从 state 读取 hitl_budget，缺省 DEFAULT_HITL_BUDGET。"""
    source = original_state if original_state is not None else state
    budget = source.get("hitl_budget", DEFAULT_HITL_BUDGET)
    try:
        return int(budget) if budget else DEFAULT_HITL_BUDGET
    except (TypeError, ValueError):
        return DEFAULT_HITL_BUDGET


def get_hitl_turn_count(state: Any, original_state: Any = None) -> int:
    """读取当前题已完成的 HITL 轮数。

    原 state["hitl_turn_count"] 键不在 FlexState 里，LangGraph
    会静默丢弃，永远是 0。改读 hitl_count —— 真实的 FlexState 累加器，由
    human_feedback 节点每完成一轮 HITL（含自动解析路径）累加 1。
    pre-hook 时读到的是"本轮之前已完成轮数"，语义与 turn_count 一致。
    """
    source = original_state if original_state is not None else state
    count = source.get("hitl_count", 0)
    try:
        return int(count) if count else 0
    except (TypeError, ValueError):
        return 0


def normalize_question(question: str) -> str:
    """规范化问题文本用于去重比对（去空白/标点/大小写）。"""
    if not question:
        return ""
    # 去除标点和空白，只保留字母数字中文
    return re.sub(r"[\s　，。？！,.?!:：;；()（）\"'“”‘’《》<>\-]+", "", question).lower()


def get_hitl_question_history(state: Any, original_state: Any = None, messages: list | None = None) -> list[dict]:
    """获取同题内已回答过的 HITL 问题列表（用于去重）。

    原 state["hitl_question_history"] 键不在 FlexState 里，
    LangGraph 会静默丢弃节点返回，历史永远为空。改从 messages 派生：
    AIMessage 的 request_human_feedback tool_call（问题）+ 对应 ToolMessage（答复）
    天然随 messages 通道流动。注意 pruner 压缩可能移除早期问答，去重是软保护，
    budget 才是硬兜底。
    """
    # 优先用传入 messages；缺失时从 state 读（FlexState 真实通道）
    msgs = messages if messages is not None else (state.get("messages") or [])
    questions: dict[str, str] = {}
    history: list[dict] = []
    for msg in msgs:
        if isinstance(msg, AIMessage):
            for tc in (getattr(msg, "tool_calls", None) or []):
                if isinstance(tc, dict) and tc.get("name") == HUMAN_FEEDBACK_TOOL_NAME:
                    q = extract_hil_question(tc)
                    if q:
                        questions[str(tc.get("id") or "")] = q
        elif isinstance(msg, ToolMessage) and getattr(msg, "name", "") == HUMAN_FEEDBACK_TOOL_NAME:
            q = questions.get(str(getattr(msg, "tool_call_id", "") or ""), "")
            if q:
                history.append(
                    {
                        "question": q,
                        "question_norm": normalize_question(q),
                        "answer": str(msg.content),
                    }
                )
    return history


def find_human_feedback_call(messages: list) -> dict | None:
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


def extract_original_query(messages: list) -> str:
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


def extract_hil_question(call: Any) -> str:
    """从工具调用中提取 HITL 问题内容（reason 与 pending_action 拼接）。

    agent 常把实质内容（表名/候选清单/口径细节）写在 reason、pending_action
    只写一个短标签（反之亦然），只取单一字段会丢失提取信号导致程序化分支
    误判"没给选项"，故两个字段全部纳入，reason 在前与 HITL 提示渲染顺序一致。
    """
    if isinstance(call, dict):
        args = call.get("args", {})
        pending_action = args.get("pending_action", "") or ""
        reason = args.get("reason", "") or ""
    elif hasattr(call, "args"):
        args = call.args or {}
        pending_action = getattr(args, "pending_action", "") or args.get("pending_action", "") or ""
        reason = getattr(args, "reason", "") or args.get("reason", "") or ""
    else:
        return ""
    parts = [p.strip() for p in (reason, pending_action) if p and p.strip()]
    return "\n".join(parts)


def inject_tool_message(messages: list, tool_call_id: str, answer: str) -> list:
    """注入 ToolMessage 到 messages。"""
    tool_message = ToolMessage(
        content=answer,
        tool_call_id=tool_call_id,
        name=HUMAN_FEEDBACK_TOOL_NAME,
    )
    return [*messages, tool_message]


def build_hook_return(state: Any, updated_messages: list, resolved_info: dict) -> dict:
    """构建 auto-resolver hook 返回值，处理 GlobalStateProxy 的未提交本地更新问题。

    ``resolved_info`` 对本函数是不透明 dict（内容由场景 hook 构造）。
    """
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


def update_action_node(state: Any, tool_call_id: str, answer: str) -> None:
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
        logger.debug(f"[hitl_common] 更新 ActionNode({tool_call_id}): output 已更新")
    except Exception:
        logger.debug(f"[hitl_common] 更新 ActionNode({tool_call_id}) 失败（context 可能不可用）")


def inject_simple_reply(
    state: Any,
    messages: list,
    feedback_call: dict,
    answer: str,
    *,
    action: str = "SIMPLE",
    reason: str = "",
) -> dict:
    """注入简单回复（用于 budget 收敛/去重命中/UNA 拒绝），不走 LLM。"""
    tool_call_id = feedback_call.get("id", "") if feedback_call else ""
    tool_msg = ToolMessage(
        content=answer,
        tool_call_id=tool_call_id,
        name=HUMAN_FEEDBACK_TOOL_NAME,
    )
    updated_messages = [*messages, tool_msg]
    logger.info(
        f"[hitl_common] 注入简单回复（action={action}, reason={reason or 'N/A'}）: {answer[:80]!r}"
    )
    return build_hook_return(
        state,
        updated_messages,
        {
            "question": "(simple)",
            "answer": answer,
            "task_name": None,
            "task_id": None,
            "golden_sql_path": "",
            "action": action,
            "reason": reason,
        },
    )


def inject_fallback_reply(
    state: Any,
    messages: list,
    feedback_call: dict,
    reason: str = "",
) -> dict:
    """匹配/生成失败时的兜底回复，避免 HumanFeedbackNode 走 input() 阻塞。

    在 agent 已触发 request_human_feedback 但 auto_resolver 无法生成有效回复时
    （如 query 提取失败、任务匹配失败、LLM 调用失败），注入一条"请基于
    已有信息自主判断继续"的 ToolMessage，并置 hitl_auto_resolved=True，让
    HumanFeedbackNode 走 _handle_auto_resolved 透明路径而非阻塞等待输入。
    """
    answer = "请基于已有元数据和题意自主判断并继续，无需再就此事询问。"
    tool_call_id = feedback_call.get("id", "") if feedback_call else ""
    tool_msg = ToolMessage(
        content=answer,
        tool_call_id=tool_call_id,
        name=HUMAN_FEEDBACK_TOOL_NAME,
    )
    updated_messages = [*messages, tool_msg]
    logger.warning(
        f"[hitl_common] 使用兜底回复（reason={reason or 'unknown'}），注入 ToolMessage 避免阻塞"
    )
    return build_hook_return(
        state,
        updated_messages,
        {
            "question": "(fallback)",
            "answer": answer,
            "task_name": None,
            "task_id": None,
            "golden_sql_path": "",
            "fallback_reason": reason,
        },
    )
