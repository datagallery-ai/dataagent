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
"""Flex Planner 业务 prompt 组装。

通用 prompt 加载/构造仍走 ``dataagent.core.managers.prompt_manager``；本模块只承载与
Planner 业务直接耦合的逻辑（依赖 Context / state / runtime / tool_manager）。
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import Context
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.utils.messages_utils import (
    build_human_message,
    build_messages,
    build_system_message,
)

# state 标志键：由可选 planner pre-hook 写入，本模块只读取。
TOOL_CALL_COUNT_KEY = "tool_call_count"
PLAN_REQUIRED_THRESHOLD_KEY = "plan_required_threshold"


def _runtime_agent_config(runtime: Any) -> dict[str, Any]:
    """Return per-Agent config dict from runtime.

    Raises:
        RuntimeError: When ``runtime`` is missing or has no bound per-Agent ConfigManager.
    """
    if runtime is None:
        raise RuntimeError("Planner prompt building requires a Runtime with per-Agent config.")
    get_all = getattr(runtime, "get_all_config", None)
    if not callable(get_all):
        raise RuntimeError("Runtime must provide get_all_config() for planner prompt building.")
    return get_all() or {}


def prepare_flex_planner_prompt(
    context: Context,
    state: Any,
    *,
    system_prompt: PromptTemplate,
    user_prompt: PromptTemplate,
    runtime: Any,
    workspace: Any = None,
    **kwargs: Any,
) -> list[BaseMessage]:
    """Flex Planner 专用：从 session messages 组装 planner prompt。

    ``system_prompt`` / ``user_prompt`` 必须由调用方传入（通常来自 ``Planner.__init__``
    持有的内置模板实例；yaml ``prompt_template`` 只会作为 partial 追加到模板插槽）。
    Subagent Worker metadata 以模板变量 ``worker_metadata_prompt`` 注入内置 ``planner/system.md``
    （与设计文档一致），不写入 ``state["messages"]``。
    默认 ``planner/system.md`` 已内嵌 matplotlib 中文字体说明；其他节点模板（如 ``nl2sql_react``）不含该段。
    """
    runtime_env_prompt = ""
    # 注入运行环境信息到 prompt
    if hasattr(runtime, "get_runtime_env_prompt"):
        runtime_env_prompt = runtime.get_runtime_env_prompt()

    database_environment = str(getattr(runtime.env, "environment_description", "") or "").strip()

    agent_cfg = _runtime_agent_config(runtime)
    worker_metadata_prompt = ""
    prompt_kwargs = {
        "runtime_environment": runtime_env_prompt,
        "worker_metadata_prompt": worker_metadata_prompt,
        "database_environment": database_environment,
        **kwargs,
    }
    merged = {**state, "workspace": workspace}

    system_message, user_message = _build_planner_system_and_user_messages(
        context,
        merged,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        instruction=str(runtime.instructions).strip(),
        agent_config=agent_cfg,
        **prompt_kwargs,
    )
    sync_flex_planner_user_human_to_state(runtime, state, user_message)
    _max_tr_len = getattr(getattr(runtime, "env", None), "max_tool_result_length", None)
    history_messages = build_messages(
        list(state.get("messages") or []),
        max_tool_result_length=_max_tr_len,
    )
    has_current_user_message = any(
        getattr(message, "type", None) == getattr(user_message, "type", None)
        and str(getattr(message, "content", "") or "") == str(user_message.content or "")
        for message in history_messages
    )
    messages = [system_message] + ([] if has_current_user_message else [user_message]) + history_messages

    # L3: planner pre-hook 置位后，注入硬性提醒（system-voiced，tool-call 阈值）。
    todo_plan_vars = _build_plan_prompt_variables(context=context, state=state)
    enforcement_message = _build_plan_enforcement_message(todo_plan_vars)
    if enforcement_message is not None:
        messages.append(enforcement_message)

    todo_message = build_todo_message(context=context, state=state)
    if todo_message:
        messages.append(todo_message)

    return messages


def _save_human_message_to_full(state: Any, user_message: HumanMessage, runtime: Runtime) -> None:
    """将用户 HumanMessage 增量追加到 messages_full.json。"""
    try:
        from dataagent.core.flex.hooks.history_writer import save_messages_full_for_state

        save_messages_full_for_state(state, [user_message], runtime=runtime)
    except Exception:
        logger.warning(f"写入用户 HumanMessage 到 messages_full.json 失败: {traceback.format_exc()}")


def sync_flex_planner_user_human_to_state(
    runtime: Runtime,
    state: Any,
    user_message: HumanMessage,
) -> None:
    """本 user 轮首次进入 Planner 时，将模板化 ``user_message`` 追加到 ``state["messages"]``。

    ``FlexAgent`` 在 ``chat()``/``astream()`` 入口调用 ``runtime.reset_flex_planner_user_sync()``；
    此处仅在 ``runtime.flex_planner_user_sync_pending`` 为 True 时追加一次，随后清除。

    openjiuwen 下 ``state`` 常为 ``GlobalStateProxy``：修改 ``messages`` 后须显式
    ``state["messages"] = msgs`` 触发 ``update_global_state``，不能只依赖 list 原地 append。
    """
    messages_to_append = [user_message]

    if runtime.flex_planner_user_sync_pending:
        raw_msgs = state.get("messages")
        msgs = [] if raw_msgs is None else list(raw_msgs)
        msgs.extend(messages_to_append)
        state["messages"] = msgs
        runtime.clear_flex_planner_user_sync_pending()
        for msg in messages_to_append:
            _save_human_message_to_full(state, msg, runtime)
        return
    # openjiuwen：漏置 pending 或 messages 尚未初始化时，避免 Planner 仅有 SystemMessage
    if not state.get("messages"):
        state["messages"] = messages_to_append
        for msg in messages_to_append:
            _save_human_message_to_full(state, msg, runtime)


def build_todo_message(context: Context, *, state: Any = None) -> HumanMessage | None:
    """构建包含待办指令的 HumanMessage。"""
    todo_template = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/todo")
    return build_human_message(
        prompt_template=todo_template, prompt_str="", **_build_plan_prompt_variables(context=context, state=state)
    )


def _build_plan_enforcement_message(todo_plan_vars: dict[str, Any]) -> HumanMessage | None:
    """根据 plan 状态标志构建 ``[SYSTEM POLICY]`` 强制建 plan 提醒消息。

    由 planner pre-hook 写入的 ``todo_plan_vars`` 标志决定是否触发：

    - **tool-call 触发**（``tool_call_count >= plan_required_threshold``）：当前轮次
      ToolMessage 数达阈值且无 plan → 返回 tool-call 措辞提醒。

    有 plan、无触发或标志缺失时返回 ``None``。
    """
    if todo_plan_vars.get("has_plan"):
        return None
    tool_call_count = int(todo_plan_vars.get("tool_call_count", 0) or 0)
    plan_required_threshold = int(todo_plan_vars.get("plan_required_threshold", 0) or 0)
    if tool_call_count and tool_call_count >= plan_required_threshold:
        return HumanMessage(
            content=(
                f"[SYSTEM POLICY] {tool_call_count} tool call(s) have been "
                "made without an active `create_plan`. Per the Work Plan policy, complex "
                "multi-step tasks MUST be registered as a plan before further substantive "
                "execution. Call `create_plan` now with an `introduction`, `approach`, "
                "and ordered `todos`, then proceed with the first todo."
            )
        )
    return None


def _build_planner_system_and_user_messages(
    context: Context,
    state: Any,
    *,
    system_prompt: PromptTemplate,
    user_prompt: PromptTemplate,
    instruction: str = "",
    agent_config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[BaseMessage, HumanMessage]:
    """构建 Planner 的 system + user。

    ``system_prompt`` / ``user_prompt`` 由调用方传入，不再内部 ``from_package_relative``。
    """
    enable_human_feedback = state.get("enable_human_feedback", False) is True
    system_prompt_variables = {
        "enable_human_feedback": enable_human_feedback,
        **kwargs,
    }
    system_message = build_system_message(
        system_prompt,
        **system_prompt_variables,
    )

    trajectory_graph = context.get_trajectory(trimmed=False)
    query_node = trajectory_graph.nodes[context.initial_pt]
    user_query = query_node.get("query")

    workspace = state.get("workspace")
    working_directory = str(Path(str(workspace)).expanduser().resolve())

    full_cfg = agent_config or {}

    user_prompt_variables = {
        "user_query": user_query,
        "database_context": _build_database_context_prompt(full_cfg),
        "planning_instructions": instruction,
        "working_directory": working_directory,
        "allow_path_lines": _allow_path_bullet_lines(full_cfg),
    }
    user_prompt_variables.update(kwargs)
    user_message = build_human_message(user_prompt, **user_prompt_variables)
    return system_message, user_message


def _build_database_context_prompt(config: dict[str, Any]) -> str:
    """Build a natural-language database context for planner user prompt when NL2SQL tool is absent."""
    database = config.get("DATABASE") or {}
    if not database:
        return ""

    db_id = database.get("db_id")
    engine = database.get("engine")
    db_config = database.get("config") or {}

    lines = [
        f"- DB ID: `{db_id}`" if db_id else "",
        f"- DB Engine: `{engine}`" if engine else "",
    ]
    for key, value in db_config.items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(line for line in lines if line)


def _allow_path_bullet_lines(config: dict[str, Any]) -> str:
    """Format ``WORKSPACE.allow_path`` as Markdown bullet lines for planner templates (empty if unset)."""
    paths = ToolManager.workspace_allow_path_list(config)
    if not paths:
        return ""
    return "\n".join(f"- `{p}`" for p in paths)


def _build_plan_prompt_variables(context: Context, *, state: Any = None) -> dict[str, Any]:
    """从进程内全局 Plan 快照构建 planner 模板变量。

    ``tool_call_count`` / ``plan_required_threshold`` 由 planner pre-hook 写入 state；
    本模块只读取。缺省 ``None`` 时退化为只读 plan 快照（向后兼容，无 enforcement）。
    """
    plan = context.todolist_manager.todolist
    if state is not None and plan is None:
        tool_call_count = int(state.get(TOOL_CALL_COUNT_KEY, 0) or 0)
        plan_required_threshold = int(state.get(PLAN_REQUIRED_THRESHOLD_KEY, 0) or 0)
    else:
        tool_call_count = 0
        plan_required_threshold = 0
    if plan is None:
        return {
            "has_plan": False,
            "plan_all_todos_done": False,
            "plan_introduction": "",
            "plan_approach": "",
            "plan_current_todo": "",
            "plan_todos_overview": "",
            "tool_call_count": tool_call_count,
            "plan_required_threshold": plan_required_threshold,
        }

    incomplete = [t for t in plan.todos if not t.completed]
    all_done = len(plan.todos) == 0 or not incomplete
    current_todo = incomplete[0].title if incomplete else ""

    overview_lines: list[str] = []
    for item in plan.todos:
        mark = "x" if item.completed else " "
        overview_lines.append(f"- [{mark}] {item.title}")

    return {
        "has_plan": True,
        "plan_all_todos_done": all_done,
        "plan_introduction": plan.introduction,
        "plan_approach": plan.approach,
        "plan_current_todo": current_todo,
        "plan_todos_overview": "\n".join(overview_lines),
        "tool_call_count": tool_call_count,
        "plan_required_threshold": plan_required_threshold,
    }
