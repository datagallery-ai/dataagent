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
"""Flex Planner 业务 prompt 组装 + skill selection。

通用 prompt 加载/构造仍走 ``dataagent.core.managers.prompt_manager``；本模块只承载与
Planner / Skill Selector 业务直接耦合的逻辑（依赖 Context / state / runtime / tool_manager）。
"""

from __future__ import annotations

import json
import re
import traceback
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import Context
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.core.swarm.swarm_config import swarm_enabled
from dataagent.utils.messages_utils import (
    build_human_message,
    build_messages,
    build_system_message,
)
from dataagent.utils.parsing_utils import extract_json_block, normalize_newlines

SKILL_SELECTION_CACHE_KEY = "planner_skill_selection"
SKILL_SELECTOR_PROMPT_NAMESPACE = "skill_selector"

# state 标志键：由 planner pre-hook（如 plan_enforcer）写入，
# 本模块只读取，不再自算 SKILL.md 读取检测与 tool-call 阈值。
SKILL_MD_READ_WITHOUT_PLAN_KEY = "skill_md_read_without_plan"
TOOL_CALL_COUNT_KEY = "tool_call_count"
PLAN_REQUIRED_THRESHOLD_KEY = "plan_required_threshold"


def _runtime_agent_config(runtime: Any) -> dict[str, Any]:
    """
    Return per-Agent config dict from runtime.

    Raises:
        RuntimeError: When ``runtime`` is missing or has no bound per-Agent ConfigManager.
    """
    if runtime is None:
        raise RuntimeError("Planner prompt building requires a Runtime with per-Agent config.")
    get_all = getattr(runtime, "get_all_config", None)
    if not callable(get_all):
        raise RuntimeError("Runtime must provide get_all_config() for planner prompt building.")
    return get_all() or {}


def build_builtin_skills_prompt(skills: list[dict[str, Any]]) -> str:
    """构建 builtin skills prompt 段落。"""
    return _build_skill_entries_prompt(skills, section_title="Builtin Skills")


def build_user_skills_prompt(skills: list[dict[str, Any]]) -> str:
    """构建 user skills prompt 段落。"""
    return _build_skill_entries_prompt(skills, section_title="User Skills")


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
    if swarm_enabled(agent_cfg):
        worker_metadata_prompt = _build_subagent_worker_metadata_prompt_fragment(state, runtime=runtime)
    prompt_kwargs = {
        **_build_flex_skill_prompt_variables(
            state=state,
            runtime=runtime,
        ),
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

    # L3: planner pre-hook（plan_enforcer）置位后，注入硬性提醒（system-voiced）。
    # 两条触发独立：skill-md-read（更具体）优先；否则 tool-call 阈值。
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

    由 planner pre-hook（``plan_enforcer``）写入的 ``todo_plan_vars`` 标志决定触发哪条
    提醒（两条触发独立、互斥）：

    - **skill 触发**（``skill_md_read_without_plan``）：读过声明需要 plan 的 skill 的
      ``SKILL.md`` 且无 plan → 返回 skill 措辞提醒（注册 SKILL.md ``## Workflow`` 为 todos）。
    - **tool-call 触发**（``tool_call_count >= plan_required_threshold``）：当前轮次
      ToolMessage 数达阈值且无 plan → 返回 tool-call 措辞提醒。

    有 plan、无触发或标志缺失时返回 ``None``。skill 触发优先于 tool-call（更具体）。
    """
    if todo_plan_vars.get("has_plan"):
        return None
    if todo_plan_vars.get("skill_md_read_without_plan"):
        return HumanMessage(
            content=(
                "[SYSTEM POLICY] A skill's SKILL.md has been read but no `create_plan` "
                "has been called. Per the Work Plan policy, multi-step skill workflows "
                "MUST be registered as a plan before substantive execution. "
                "Call `create_plan` now with the SKILL.md `## Workflow` steps as `todos`, "
                "then proceed with the first todo."
            )
        )
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


def _build_skill_entries_prompt(skills: list[dict[str, Any]], *, section_title: str) -> str:
    """将一类 skills 格式化为独立段落，供 planner prompt 模板注入。"""
    if not skills:
        return ""

    lines: list[str] = [f"## {section_title}", ""]
    for skill in skills:
        name = skill["name"]
        description = skill.get("description", "")
        skill_md_path = f"skill/{name}/SKILL.md"
        lines.append(
            f"### Skill: `{name}`\n"
            f"  description: {description}\n"
            f"  skill root: `skill/{name}`\n"
            f"  skill entry alias: `{skill_md_path}`\n"
        )
    return "\n".join(lines)


def _build_subagent_worker_metadata_prompt_fragment(
    state: Any,
    *,
    runtime: Any = None,
) -> str:
    """Build the fenced JSON fragment for ``planner/system.md``'s ``worker_metadata_prompt`` slot.

    Injected during ``_build_planner_system_and_user_messages`` (not via a later
    prepend) so context dumps and the rendered system template stay aligned with
    the design doc: each record exposes only ``sub_id``, ``last_query``,
    ``last_answer``, ``artifacts``, and ``error``.
    """
    user_id = str(state.get("user_id") or "").strip()
    parent_session_id = str(state.get("session_id") or "").strip()
    if not user_id or not parent_session_id:
        return ""
    config = None
    if runtime is not None and hasattr(runtime, "get_all_config"):
        config = runtime.get_all_config()
    parent_workspace = state.get("workspace")
    if parent_workspace is None and config:
        from dataagent.utils.runtime_paths import resolve_effective_workspace_root

        parent_workspace = resolve_effective_workspace_root(
            config=config,
            session_id=parent_session_id,
            user_id=user_id,
        )
    try:
        from dataagent.core.swarm.worker_metadata import build_worker_metadata_context

        assets = build_worker_metadata_context(
            user_id=user_id,
            parent_session_id=parent_session_id,
            limit=10,
            parent_workspace=parent_workspace,
            config=config,
        )
    except Exception as exc:
        logger.debug("skip worker_metadata_prompt fragment: {}", exc)
        return ""
    if not assets:
        return ""
    return "```json\n" + json.dumps(assets, ensure_ascii=False, indent=2) + "\n```"


def _build_skill_prompt_variables(
    *,
    builtin_skills: list[dict[str, Any]] | None = None,
    user_skills: list[dict[str, Any]] | None = None,
    runtime: Any = None,
) -> dict[str, str]:
    """返回 planner system 模板使用的 skill 变量。"""
    if builtin_skills is None:
        builtin_skills = runtime.list_builtin_skills() if runtime is not None else []
    if user_skills is None:
        user_skills = runtime.list_user_skills() if runtime is not None else []
    builtin_skills = builtin_skills or []
    user_skills = user_skills or []
    return {
        "builtin_skills_prompt": build_builtin_skills_prompt(builtin_skills),
        "user_skills_prompt": build_user_skills_prompt(user_skills),
    }


def _build_flex_skill_prompt_variables(
    *,
    state: Any,
    runtime: Any,
) -> dict[str, str]:
    """Return planner skill prompt variables for flex mode."""
    # 1、获取 builtin/user skills 列表 (prefer runtime, no global singleton)
    builtin_skills = runtime.list_builtin_skills() if runtime is not None else []

    user_id = str(state.get("user_id") or "").strip() or None
    # 拉 user skills 前刷新缓存（当前 Agent 的 ToolManager，非全局单例）
    tm = getattr(runtime, "tool_manager", None) if runtime is not None else None
    if tm is not None:
        tm.refresh_user_skills(user_id=user_id)
    else:
        reason = "runtime is None" if runtime is None else "runtime.env.tool_manager is None"
        logger.debug("skip refresh_user_skills: {} (user_id={})", reason, user_id)
    user_skills = runtime.list_user_skills() if runtime is not None else []
    config = _runtime_agent_config(runtime)
    # 2、获取相关 skills 的上限
    relevant_skills_limit = _get_relevant_skills_limit(config)
    latest_user_query = str(state.get("user_query") or "")
    # 3、根据 user_query 筛选 skills
    selected = _select_relevant_skills_for_prompt(
        latest_user_query=latest_user_query,
        history_user_messages=_build_history_user_messages(
            state.get("messages") or [],
            latest_user_query=latest_user_query,
        ),
        builtin_skills=builtin_skills,
        user_skills=user_skills,
        runtime=runtime,
        relevant_skills_limit=relevant_skills_limit,
        cache_key=_build_skill_selection_cache_key(state),
    )
    # 4、返回筛选过的 skills
    return _build_skill_prompt_variables(
        builtin_skills=selected["selected_builtin_skills"],
        user_skills=selected["selected_user_skills"],
    )


def _get_relevant_skills_limit(config: dict[str, Any]) -> int | None:
    """Read the optional relevant skills limit from AGENT_CONFIG."""
    agent_config = config.get("AGENT_CONFIG", {}) or {}
    raw_value = agent_config.get("relevant_skills_limit")
    return _normalize_relevant_skills_limit(raw_value)


def _warn_relevant_skills_limit_invalid(raw_value: Any) -> None:
    logger.warning(
        f"AGENT_CONFIG.relevant_skills_limit is invalid ({raw_value!r}): expected a non-negative integer; "
        "skill selection filtering is disabled.",
    )


def _normalize_relevant_skills_limit(raw_value: Any) -> int | None:
    """Normalize relevant_skills_limit to a non-negative integer, otherwise disable filtering.

    仅接受：未配置（``None`` / 空白字符串）、非负 ``int``（``bool`` 不算）、
    或可解析为非负整数的数字字符串（YAML 常见）。其它取值 ``warning`` 并关闭 skill 筛选。
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if stripped == "":
            return None
        try:
            limit = int(stripped)
        except ValueError:
            _warn_relevant_skills_limit_invalid(raw_value)
            return None
        if limit < 0:
            _warn_relevant_skills_limit_invalid(raw_value)
            return None
        return limit
    if type(raw_value) is int:
        if raw_value < 0:
            _warn_relevant_skills_limit_invalid(raw_value)
            return None
        return raw_value
    _warn_relevant_skills_limit_invalid(raw_value)
    return None


def _run_planner_skill_llm_selection(
    *,
    latest_user_query: str,
    history_user_messages: str,
    skill_candidates: list[dict[str, Any]],
    skill_snapshot: str,
    relevant_skills_limit: int,
    cand_str: str,
) -> dict[str, Any]:
    """Call skill-selector LLM, parse result, or fall back to full candidate list."""
    try:
        raw_response = _invoke_skill_selection_model(
            latest_user_query=latest_user_query,
            history_user_messages=history_user_messages,
            skill_candidates=skill_candidates,
            relevant_skills_limit=relevant_skills_limit,
        )
        parsed_response = _parse_skill_selection_response(raw_response)
        selected_names = _resolve_selected_skill_names(
            parsed_response,
            skill_candidates=skill_candidates,
            relevant_skills_limit=relevant_skills_limit,
        )
        selected_builtin_skills, selected_user_skills = _split_selected_skills(
            selected_names=selected_names,
            skill_candidates=skill_candidates,
        )
        selection_result: dict[str, Any] = {
            "query": latest_user_query,
            "history_user_messages": history_user_messages,
            "skill_snapshot": skill_snapshot,
            "selected_builtin_skills": selected_builtin_skills,
            "selected_user_skills": selected_user_skills,
            "relevant_skills_limit": relevant_skills_limit,
            "selection_debug_info": {
                "mode": "model",
                "selected_names": selected_names,
                "model_response": parsed_response,
            },
        }
        logger.debug(
            f"Planner skill selection done: selected {len(selected_names)}/{len(skill_candidates)} — {selected_names!r}"
        )
        return selection_result
    except Exception as exc:
        fallback_names = [skill["name"] for skill in skill_candidates]
        selected_builtin_skills, selected_user_skills = _split_selected_skills(
            selected_names=fallback_names,
            skill_candidates=skill_candidates,
        )
        logger.warning(
            f"Planner skill selection failed, fallback to all skills ({len(skill_candidates)}). "
            f"Candidates: [{cand_str}]. Error: {exc!r}"
        )
        return {
            "query": latest_user_query,
            "history_user_messages": history_user_messages,
            "skill_snapshot": skill_snapshot,
            "selected_builtin_skills": selected_builtin_skills,
            "selected_user_skills": selected_user_skills,
            "relevant_skills_limit": relevant_skills_limit,
            "selection_debug_info": {
                "mode": "fallback",
                "selected_names": fallback_names,
                "error": str(exc),
            },
        }


def _select_relevant_skills_for_prompt(
    *,
    latest_user_query: str,
    history_user_messages: str = "",
    builtin_skills: list[dict[str, Any]],
    user_skills: list[dict[str, Any]],
    runtime: Any,
    relevant_skills_limit: int | None,
    cache_key: str = SKILL_SELECTION_CACHE_KEY,
) -> dict[str, Any]:
    """Select the skill subset that should be exposed to the planner prompt.

    ``relevant_skills_limit`` 须已为 ``_normalize_relevant_skills_limit`` 的结果（``int`` 非负或 ``None``）；
    生产路径由 ``_get_relevant_skills_limit`` 提供。单测通过 ``tests/prompts/test_planner_prompt_builder`` 内包装函数再规范化入参。
    """
    # 1.1 未启用 limit：直接全量返回（mode=all）
    if relevant_skills_limit is None:
        logger.debug("Planner skill selection: limit unset, inject all builtin/user skills (no filtering)")
        return {
            "selected_builtin_skills": builtin_skills,
            "selected_user_skills": user_skills,
            "selection_debug_info": {"mode": "all"},
        }
    # 1.2 limit 为 0：显式去掉全部 skills
    if relevant_skills_limit == 0:
        logger.debug("Planner skill selection: relevant_skills_limit=0, no skills in planner prompt")
        return {
            "selected_builtin_skills": [],
            "selected_user_skills": [],
            "selection_debug_info": {"mode": "none"},
        }
    # 2、构建候选集合（把 builtin/user 合成一个有序列表）
    skill_candidates = _build_skill_candidates(builtin_skills, user_skills)
    if not str(latest_user_query or "").strip():
        logger.debug("Planner skill selection: empty user_query, skip LLM filter, inject all skills")
        return {
            "selected_builtin_skills": builtin_skills,
            "selected_user_skills": user_skills,
            "selection_debug_info": {"mode": "all"},
        }
    # 3、构建 snapshot 快照（用于缓存失效判断）
    skill_snapshot = _build_skill_snapshot(skill_candidates)
    cand_str = ", ".join(f"{c.get('name')}({c.get('source')})" for c in skill_candidates)
    cached = runtime.get_cache(cache_key)
    # 4、命中 runtime cache：直接复用（不重复调模型）
    # 当 user_query、skill_snapshot、relevant_skills_limit 发生变化时缓存失效
    if _is_valid_skill_selection_cache(
        cached,
        user_query=latest_user_query,
        skill_snapshot=skill_snapshot,
        relevant_skills_limit=relevant_skills_limit,
    ):
        if isinstance(cached, dict):
            logger.debug(
                f"Planner skill selection cache hit: limit={relevant_skills_limit}, candidates=[{cand_str}], "
                f"selected={(cached.get('selection_debug_info') or {}).get('selected_names')!r}"
            )
        return cached

    logger.debug(
        f"Planner skill selection: limit={relevant_skills_limit}, {len(skill_candidates)} candidate(s): [{cand_str}]"
    )
    selection_result = _run_planner_skill_llm_selection(
        latest_user_query=latest_user_query,
        history_user_messages=history_user_messages,
        skill_candidates=skill_candidates,
        skill_snapshot=skill_snapshot,
        relevant_skills_limit=relevant_skills_limit,
        cand_str=cand_str,
    )

    runtime.set_cache(cache_key, selection_result)
    return selection_result


def _build_skill_selection_cache_key(state: Any) -> str:
    """Build a per-session-run cache key for skill selection."""
    session_id = str(state.get("session_id") or "unknown_session")
    run_id = str(state.get("run_id") or "unknown_run")
    return f"{SKILL_SELECTION_CACHE_KEY}:{session_id}:{run_id}"


def _build_history_user_messages(messages: list[Any], *, latest_user_query: str) -> str:
    """Format existing HumanMessage history for the skill selector."""
    latest_query = str(latest_user_query or "").strip()
    user_messages: list[str] = []
    seen_messages: set[str] = set()
    for message in build_messages(list(messages), context=None):
        if not isinstance(message, HumanMessage):
            continue
        content = _extract_user_query_from_human_message(str(message.content or ""))
        if not content:
            continue
        if latest_query and content == latest_query:
            continue
        if content in seen_messages:
            continue
        seen_messages.add(content)
        user_messages.append(content)
    if not user_messages:
        return ""
    return "\n".join(f"{index}. {content}" for index, content in enumerate(user_messages, start=1))


def _extract_user_query_from_human_message(content: str) -> str:
    """Extract the original user query from a planner-rendered HumanMessage."""
    text = str(content or "").strip()
    if not text:
        return ""
    match = re.search(r"<user_query>(.*?)</user_query>", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _build_skill_candidates(
    builtin_skills: list[dict[str, Any]],
    user_skills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a unified ordered candidate list for skill selection."""
    candidates: list[dict[str, Any]] = []
    for skill in builtin_skills:
        candidates.append(
            {
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or ""),
                "source": "builtin",
                "skill": skill,
            }
        )
    for skill in user_skills:
        candidates.append(
            {
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or ""),
                "source": "user",
                "skill": skill,
            }
        )
    return candidates


def _build_skill_snapshot(skill_candidates: list[dict[str, Any]]) -> str:
    """Build a lightweight stable snapshot for cache invalidation."""
    snapshot_payload = [
        {
            "name": candidate["name"],
            "source": candidate["source"],
            "description": candidate["description"],
        }
        for candidate in skill_candidates
    ]
    return json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True)


def _is_valid_skill_selection_cache(
    cached: Any,
    *,
    user_query: str,
    skill_snapshot: str,
    relevant_skills_limit: int,
) -> bool:
    """Check whether the cached selection can be reused for the current turn."""
    if not isinstance(cached, dict):
        return False
    return (
        cached.get("query") == user_query
        and cached.get("skill_snapshot") == skill_snapshot
        and cached.get("relevant_skills_limit") == relevant_skills_limit
    )


def _invoke_skill_selection_model(
    *,
    latest_user_query: str,
    history_user_messages: str = "",
    skill_candidates: list[dict[str, Any]],
    relevant_skills_limit: int,
) -> str:
    """Call the planner model once to rank and filter skill candidates."""
    # skill_selector：直接 ``PromptTemplate.from_package_relative`` 加载（等价于旧 PromptManager.get_prompt + pre_scan）
    skills_payload = [
        {
            "name": candidate["name"],
            "description": candidate["description"],
            "source": candidate["source"],
        }
        for candidate in skill_candidates
    ]
    messages = [
        build_system_message(
            PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/{SKILL_SELECTOR_PROMPT_NAMESPACE}/system"),
            relevant_skills_limit=relevant_skills_limit,
        ),
        build_human_message(
            PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/{SKILL_SELECTOR_PROMPT_NAMESPACE}/user"),
            history_user_messages=history_user_messages,
            user_query=latest_user_query,
            relevant_skills_limit=relevant_skills_limit,
            skills_json=json.dumps(skills_payload, ensure_ascii=False, indent=2),
        ),
    ]
    llm = llm_manager.get_default_llm()
    if llm is None:
        raise RuntimeError("llm_manager.get_default_llm() is not initialized for skill selection")
    response = llm.invoke(messages)
    return str(response.content or "")


def _parse_skill_selection_response(raw_response: str) -> dict[str, Any]:
    """Parse and normalize the skill selection model response."""
    parsed = extract_json_block(raw_response)
    parsed = normalize_newlines(parsed)
    if not isinstance(parsed, dict):
        raise ValueError("skill selection response must be a JSON object")
    _validate_skill_selection_response(parsed)
    return parsed


def _validate_skill_selection_response(parsed_response: dict[str, Any]) -> None:
    """Validate the strict JSON schema required by the skill selector."""
    expected_keys = {"include", "exclude", "ranked_candidates", "selected"}
    if set(parsed_response) != expected_keys:
        raise ValueError(f"skill selection response keys must be exactly {sorted(expected_keys)}")

    _validate_named_reason_entries(parsed_response.get("include"), field_name="include")
    _validate_named_reason_entries(parsed_response.get("exclude"), field_name="exclude")
    _validate_ranked_candidates(parsed_response.get("ranked_candidates"))
    _validate_selected_entries(parsed_response.get("selected"))


def _validate_named_reason_entries(entries: Any, *, field_name: str) -> None:
    """Validate include/exclude arrays."""
    if not isinstance(entries, list):
        raise ValueError(f"{field_name} must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{field_name} entries must be objects")
        name = entry.get("name")
        reason = entry.get("reason")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{field_name}.name must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{field_name}.reason must be a non-empty string")


def _validate_ranked_candidates(entries: Any) -> None:
    """Validate ranked_candidates array."""
    if not isinstance(entries, list):
        raise ValueError("ranked_candidates must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("ranked_candidates entries must be objects")
        name = entry.get("name")
        score = entry.get("score")
        reason = entry.get("reason")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("ranked_candidates.name must be a non-empty string")
        if not isinstance(score, (int, float)):
            raise ValueError("ranked_candidates.score must be numeric")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("ranked_candidates.reason must be a non-empty string")


def _validate_selected_entries(entries: Any) -> None:
    """Validate selected array."""
    if not isinstance(entries, list):
        raise ValueError("selected must be a list")
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("selected entries must be non-empty strings")


def _resolve_selected_skill_names(
    parsed_response: dict[str, Any],
    *,
    skill_candidates: list[dict[str, Any]],
    relevant_skills_limit: int,
) -> list[str]:
    """Resolve the final selected skill names from model output."""
    valid_names = {candidate["name"] for candidate in skill_candidates}
    excluded_names = _extract_dict_name_entries(parsed_response.get("exclude"), valid_names=valid_names)
    include_names = [
        name
        for name in _extract_dict_name_entries(parsed_response.get("include"), valid_names=valid_names)
        if name not in excluded_names
    ]
    ranked_names = [
        name
        for name in _extract_dict_name_entries(parsed_response.get("ranked_candidates"), valid_names=valid_names)
        if name not in excluded_names
    ]
    fallback_selected = [
        name
        for name in _extract_selected_names(parsed_response.get("selected"), valid_names=valid_names)
        if name not in excluded_names
    ]
    if not include_names and not ranked_names and not fallback_selected:
        return []
    ordered_names = include_names + _dedupe_names(ranked_names or fallback_selected)
    return _dedupe_names(ordered_names)[:relevant_skills_limit]


def _extract_dict_name_entries(entries: Any, *, valid_names: set[str]) -> list[str]:
    """Extract ordered valid skill names from dict entries with a ``name`` field."""
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name and name in valid_names:
                names.append(name)
    return _dedupe_names(names)


def _extract_selected_names(entries: Any, *, valid_names: set[str]) -> list[str]:
    """Extract ordered valid names from the selected list."""
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        name = str(entry or "").strip()
        if name and name in valid_names:
            names.append(name)
    return _dedupe_names(names)


def _dedupe_names(names: list[str]) -> list[str]:
    """Deduplicate names while preserving order."""
    ordered_names: list[str] = []
    seen_names: set[str] = set()
    for name in names:
        if name in seen_names:
            continue
        seen_names.add(name)
        ordered_names.append(name)
    return ordered_names


def _split_selected_skills(
    *,
    selected_names: list[str],
    skill_candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split selected names back into builtin and user skill lists."""
    candidate_by_name = {candidate["name"]: candidate for candidate in skill_candidates}
    selected_builtin_skills: list[dict[str, Any]] = []
    selected_user_skills: list[dict[str, Any]] = []
    for name in selected_names:
        candidate = candidate_by_name.get(name)
        if not isinstance(candidate, dict):
            continue
        skill = candidate.get("skill")
        if not isinstance(skill, dict):
            continue
        if candidate.get("source") == "builtin":
            selected_builtin_skills.append(skill)
            continue
        selected_user_skills.append(skill)
    return selected_builtin_skills, selected_user_skills


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
    tools_config = config.get("TOOLS") or {}
    local_functions = tools_config.get("local_functions") or []
    if any((item or {}).get("function") == "nl2sql_sub_agent_tool" for item in local_functions):
        return ""

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

    ``skill_md_read_without_plan`` / ``tool_call_count`` / ``plan_required_threshold``
    均由 planner pre-hook（``plan_enforcer``）写入 state；本模块只读取，不再自算
    SKILL.md 读取检测与 tool-call 阈值。缺省 ``None`` 时退化为只读 plan 快照
    （向后兼容，无 enforcement）。
    """
    plan = context.todolist_manager.todolist
    if state is not None and plan is None:
        tool_call_count = int(state.get(TOOL_CALL_COUNT_KEY, 0) or 0)
        skill_md_read_without_plan = bool(state.get(SKILL_MD_READ_WITHOUT_PLAN_KEY, False))
        plan_required_threshold = int(state.get(PLAN_REQUIRED_THRESHOLD_KEY, 0) or 0)
    else:
        tool_call_count = 0
        skill_md_read_without_plan = False
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
            "skill_md_read_without_plan": skill_md_read_without_plan,
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
        "skill_md_read_without_plan": skill_md_read_without_plan,
        "plan_required_threshold": plan_required_threshold,
    }
