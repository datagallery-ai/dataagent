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
"""Flex 内置 plan_enforcer hook（planner 节点 pre-hook）。

统一管控两类"强制建 plan"触发，均由 HOOKS YAML 配置驱动、按需加载：

1. **skill 触发**：当前轮次内读过 ``require_plan_skills`` 中列出的 skill 的 ``SKILL.md``
   且当前无 plan → 置 ``skill_md_read_without_plan=True``。
2. **tool-call 触发**：当前轮次内已执行 ToolMessage 数 ≥ ``tool_call_threshold``
   → 写入 ``tool_call_count`` 与 ``plan_required_threshold`` 供 todo 模板升级。

**多轮边界**：两条触发均**只统计当前 user query 轮次内**的消息，避免 q2 启动时把
q1 历史中的工具调用 / SKILL.md 读取计入而误触发。轮次边界由
``runtime.flex_planner_user_sync_pending`` 信号判定（见 :func:`_current_turn_messages`）。

默认 ``flex_default_configs.yaml`` 不含此 hook。配置示例::

    HOOKS:
      nodes:
        planner:
          pre:
            - name: plan_enforcer
              require_plan_skills:
                - create-neutralization-experiment
              tool_call_threshold: 4
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.workflow.state import FlexState

# state 标志键：hook 写入，planner_prompt_builder 读取
SKILL_MD_READ_WITHOUT_PLAN_KEY = "skill_md_read_without_plan"
TOOL_CALL_COUNT_KEY = "tool_call_count"
PLAN_REQUIRED_THRESHOLD_KEY = "plan_required_threshold"

# 匹配 skill/<name>/SKILL.md 路径
_SKILL_MD_PATH_PATTERN = re.compile(r"skill/([^/]+)/SKILL\.md", re.IGNORECASE)


def plan_enforcer(
    state: FlexState,
    runtime: Runtime,
    *,
    require_plan_skills: list[str] | None = None,
    tool_call_threshold: int | None = None,
) -> FlexState:
    """Planner 节点 pre-hook：按配置强制建 plan（skill 读取 / tool-call 阈值）。

    Args:
        state: Flex workflow state。
        runtime: Per-invocation Runtime。``runtime.flex_planner_user_sync_pending`` 用于
            判定当前轮次边界（首轮 True，user 消息 sync 后 False）。
        require_plan_skills: 需要 ``create_plan`` 的 skill 名列表。为 ``None`` / 空时关闭 skill 触发。
        tool_call_threshold: 当前轮次内 ToolMessage 数达到此值即触发。为 ``None`` 或 ``0``
            时关闭 tool-call 触发（避免"任意 ≥1 调用即触发"的过激行为）。

    两条触发均未配置时，hook 清除上一轮残留标志后直接返回（无操作）。
    所有统计仅限**当前 user query 轮次内**的消息（见 :func:`_current_turn_messages`）。
    """
    require_set = {str(n).strip() for n in (require_plan_skills or []) if str(n).strip()}
    # threshold 为 None 或 0 均视为关闭（避免"任意 ≥1 调用即触发"的过激行为）
    threshold_enabled = bool(tool_call_threshold)
    if not require_set and not threshold_enabled:
        _clear_enforcement_flags(state)
        return state

    context = get_context_for_flex_state(state, runtime, swallow_errors=True)
    if context is None:
        _clear_enforcement_flags(state)
        return state
    if context.todolist_manager.todolist is not None:
        _clear_enforcement_flags(state)
        return state

    # 仅统计当前轮次内的消息，避免跨 query 误触发
    current_turn_msgs = _current_turn_messages(state, runtime)

    # ── skill 触发 ──
    if require_set:
        read_skill_names = _extract_read_skill_names(current_turn_msgs)
        matched = [n for n in read_skill_names if n in require_set]
        if matched:
            logger.debug(
                "[plan_enforcer] SKILL.md read without plan in current turn "
                f"for configured skill(s): {matched} → enforcing"
            )
        state[SKILL_MD_READ_WITHOUT_PLAN_KEY] = bool(matched)
    else:
        state[SKILL_MD_READ_WITHOUT_PLAN_KEY] = False

    # ── tool-call 触发 ──
    if threshold_enabled:
        threshold = int(tool_call_threshold)  # type: ignore[arg-type]
        count = _count_tool_messages(current_turn_msgs)
        state[TOOL_CALL_COUNT_KEY] = count
        state[PLAN_REQUIRED_THRESHOLD_KEY] = threshold
        if count and count >= threshold:
            logger.debug(
                f"[plan_enforcer] {count} tool call(s) in current turn without plan "
                f"≥ threshold {tool_call_threshold} → enforcing"
            )
    else:
        state[TOOL_CALL_COUNT_KEY] = 0
        state[PLAN_REQUIRED_THRESHOLD_KEY] = 0

    return state


def _current_turn_messages(state: Any, runtime: Any) -> list[Any]:
    """返回当前 user query 轮次内的消息切片。

    多轮会话中 ``state['messages']`` 累积全部历史（含 prior query 的工具调用与
    SKILL.md 读取）。若直接全量统计，q2 首轮迭代就会因 q1 的工具调用数 ≥ 阈值而误触发
    强制建 plan。

    轮次边界判定：

    - **首轮迭代**（``runtime.flex_planner_user_sync_pending`` 为 True）：当前 user 消息
      尚未 sync 进 ``state['messages']``（由 ``prepare_flex_planner_prompt`` 内的
      ``sync_flex_planner_user_human_to_state`` 在本 hook 之后完成），当前 query 尚无任何
      处理消息 → 返回空列表（count=0、无 SKILL.md 读取）。
    - **后续迭代**（pending 为 False）：当前 user 消息已入 state，返回**最后一条
      HumanMessage 之后**的消息（即当前 query 的处理消息：AI tool_call + ToolMessage）。

    ``runtime`` 缺少该属性时（如单测 mock）视为 False，走"后续迭代"分支；若此时
    state 内无 HumanMessage（边缘/测试场景），退化为全量统计以保持计数语义。
    """
    messages = list(state.get("messages") or [])
    if getattr(runtime, "flex_planner_user_sync_pending", False):
        return []
    last_human_idx = -1
    for idx, msg in enumerate(messages):
        if (getattr(msg, "type", "") or "").lower() == "human":
            last_human_idx = idx
    if last_human_idx < 0:
        return messages
    return messages[last_human_idx + 1 :]


def _clear_enforcement_flags(state: Any) -> None:
    """清除上一轮残留的 enforcement 标志。"""
    state[SKILL_MD_READ_WITHOUT_PLAN_KEY] = False
    state[TOOL_CALL_COUNT_KEY] = 0
    state[PLAN_REQUIRED_THRESHOLD_KEY] = 0


def _count_tool_messages(messages: list[Any]) -> int:
    """统计 ToolMessage 数（用于 tool-call 阈值触发）。"""
    count = 0
    for m in messages:
        if (getattr(m, "type", "") or "").lower() == "tool":
            count += 1
    return count


def _extract_read_skill_names(messages: list[Any]) -> list[str]:
    """从 history 的 AIMessage.tool_calls 中提取 read_file(skill/<n>/SKILL.md) 的 skill 名。"""
    names: list[str] = []
    seen: set[str] = set()
    for message in messages:
        tool_calls = getattr(message, "tool_calls", None) or []
        for tc in tool_calls:
            if not isinstance(tc, dict) or tc.get("name") != "read_file":
                continue
            raw_args = tc.get("args") or {}
            if not isinstance(raw_args, dict):
                continue
            path = str(raw_args.get("path") or raw_args.get("file_path") or "")
            match = _SKILL_MD_PATH_PATTERN.search(path)
            if not match:
                continue
            skill_name = match.group(1)
            if skill_name and skill_name not in seen:
                seen.add(skill_name)
                names.append(skill_name)
    return names
