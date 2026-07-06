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
"""Flex-specific portraiter hook.

产物路径：

- user_profile（跨 session，用户级，固定在 home）：
  ``~/.dataagent/{user_id}/.memory/profile.json``

- user_snapshot / messages（session 级，随 ``WORKSPACE.path`` + layout）：
  ``{workspace}/<session_memory_dir>/snapshot.json``
  ``{workspace}/<session_memory_dir>/messages.json``
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.hooks.agent_turn import is_job_workspace_subagent, is_subagent
from dataagent.core.flex.hooks.history_writer import save_messages
from dataagent.core.flex.workflow.state import FlexState
from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir, resolve_user_root

# ── 路径 ────────────────────────────────────────────────────────────────────


def _user_memory_dir(user_id: str) -> Path:
    """跨 session 的用户级 memory 目录。"""
    path = resolve_user_root(user_id=user_id) / ".memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_memory_dir(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve and ensure the session memory directory for writes."""
    path = _resolve_session_memory_dir(
        user_id,
        session_id,
        workspace=workspace,
        config=config,
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_session_memory_dir(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve the session memory directory without creating it."""
    return resolve_flex_session_memory_dir(
        user_id=user_id,
        session_id=session_id,
        workspace=workspace,
        config=config,
    )


# ── JSON I/O ─────────────────────────────────────────────────────────────────


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    return payload if isinstance(payload, dict) else default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── 默认值 ────────────────────────────────────────────────────────────────────


def _default_snapshot() -> dict[str, Any]:
    return {
        "session_summary": "",
        "goals": [],
        "constraints": [],
        "decisions": [],
        "important_findings": [],
        "artifacts": [],
    }


def _default_profile() -> dict[str, Any]:
    return {"identity": "", "technical_level": "", "preferences": "", "recurring_topics": []}


# ── 加载 / 保存 ────────────────────────────────────────────────────────────────


def _load_snapshot(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = (
        _resolve_session_memory_dir(
            user_id,
            session_id,
            workspace=workspace,
            config=config,
        )
        / "snapshot.json"
    )
    payload = _read_json(path, {})
    # 兼容旧格式（外层有 user_snapshot）和新格式（直接是 snapshot 内容）
    snap = payload.get("user_snapshot") if "user_snapshot" in payload else payload
    return snap if isinstance(snap, dict) else _default_snapshot()


def _save_snapshot(
    user_id: str,
    session_id: str,
    snapshot: dict[str, Any],
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    _write_json(
        _session_memory_dir(user_id, session_id, workspace=workspace, config=config) / "snapshot.json",
        snapshot,
    )


def _load_profile(user_id: str) -> dict[str, Any]:
    path = _user_memory_dir(user_id) / "profile.json"
    payload = _read_json(path, {})
    profile = payload.get("user_profile")
    return profile if isinstance(profile, dict) else _default_profile()


def _save_profile(user_id: str, profile: dict[str, Any]) -> None:
    _write_json(_user_memory_dir(user_id) / "profile.json", {"user_profile": profile})


# ── LLM 更新 ──────────────────────────────────────────────────────────────────


def _messages_to_conversation(messages: list[BaseMessage]) -> str:
    parts = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage):
            parts.append(f"Human: {m.content}")
        elif isinstance(m, AIMessage):
            parts.append(f"Assistant: {m.content}")
        elif isinstance(m, ToolMessage):
            parts.append(f"Tool: {m.content}")
        else:
            parts.append(f"Unknown: {m}")
    return "\n".join(parts)


def _normalize_memory(memory: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(memory, dict):
        return {"user_snapshot": _default_snapshot(), "user_profile": _default_profile()}
    snap = memory.get("user_snapshot")
    profile = memory.get("user_profile")
    return {
        "user_snapshot": snap if isinstance(snap, dict) else _default_snapshot(),
        "user_profile": profile if isinstance(profile, dict) else _default_profile(),
    }


def _update_snapshot(current_snapshot: dict[str, Any], conversation: str, runtime: Runtime) -> dict[str, Any]:
    """更新 session 级别的 snapshot，包含 session_summary 字段。"""
    prompt = f"""You are a session summarizer for an agent runtime.

Input:
- Current session snapshot JSON
- Recent interactions between user and agent

Task:
Update the session snapshot based on the conversation.

Rules:
- Keep everything factual and grounded in the conversation.
- Do not invent details.
- session_summary: One concise sentence (under 50 chars) summarizing what happened.
- goals: Update with current session goals. Remove completed ones.
- constraints: Note any constraints mentioned.
- decisions: Record key decisions made.
- important_findings: Note any significant findings or results.
- artifacts: List any files/data produced.
- Keep the snapshot concise.

Output a valid JSON object only:
{{
  "session_summary": "...",
  "goals": [...],
  "constraints": [...],
  "decisions": [...],
  "important_findings": [...],
  "artifacts": [...]
}}

<current_snapshot>{json.dumps(current_snapshot, ensure_ascii=False)}</current_snapshot>

<conversation>{conversation}</conversation>
"""
    llm = runtime.llm("planner")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content if hasattr(response, "content") else str(response)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return current_snapshot
    if not isinstance(parsed, dict):
        return current_snapshot
    return parsed


def _update_profile(current_profile: dict[str, Any], conversation: str, runtime: Runtime) -> dict[str, Any]:
    """更新用户级别的 profile。"""
    prompt = f"""You are a user profile analyst for an agent runtime.

## Your Task
Analyze the conversation and update the user profile.

## Update Rules
- **identity**, **technical_level**, **preferences**: Overwrite with latest information.
- **recurring_topics**: Incrementally add new topics. Deduplicate.
- **task_summary**: INHERIT existing entries. Only merge if the new task is related to an existing one. Never truncate history.
- Keep the profile concise (under 500 characters total).

## Output Format
Return ONLY a valid JSON object:
{{
  "identity": "User's background and role",
  "technical_level": "e.g., beginner/intermediate/expert",
  "preferences": "Communication style and tool preferences",
  "recurring_topics": ["topic1", "topic2"],
  "task_summary": "Chronological summary of user tasks (append only)"
}}

## Input
<conversation>
{conversation}
</conversation>

<old_profile>
{json.dumps(current_profile, ensure_ascii=False)}
</old_profile>
"""
    llm = runtime.llm("planner")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content if hasattr(response, "content") else str(response)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return current_profile
    if not isinstance(parsed, dict):
        return current_profile
    return parsed


def _normalize_memory(memory: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(memory, dict):
        return {"user_snapshot": _default_snapshot(), "user_profile": _default_profile()}
    snap = memory.get("user_snapshot")
    profile = memory.get("user_profile")
    return {
        "user_snapshot": snap if isinstance(snap, dict) else _default_snapshot(),
        "user_profile": profile if isinstance(profile, dict) else _default_profile(),
    }


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return _default_snapshot()
    return {
        "session_summary": str(snapshot.get("session_summary", "")),
        "goals": snapshot.get("goals", []) if isinstance(snapshot.get("goals"), list) else [],
        "constraints": snapshot.get("constraints", []) if isinstance(snapshot.get("constraints"), list) else [],
        "decisions": snapshot.get("decisions", []) if isinstance(snapshot.get("decisions"), list) else [],
        "important_findings": snapshot.get("important_findings", [])
        if isinstance(snapshot.get("important_findings"), list)
        else [],
        "artifacts": snapshot.get("artifacts", []) if isinstance(snapshot.get("artifacts"), list) else [],
    }


def _normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return _default_profile()
    return {
        "identity": str(profile.get("identity", "")),
        "technical_level": str(profile.get("technical_level", "")),
        "preferences": str(profile.get("preferences", "")),
        "recurring_topics": profile.get("recurring_topics", [])
        if isinstance(profile.get("recurring_topics"), list)
        else [],
        "task_summary": str(profile.get("task_summary", "")),
    }


# ── 公开 hook ─────────────────────────────────────────────────────────────────


def portraiter(state: FlexState, runtime: Runtime) -> FlexState:
    """Agent 级别 post-hook：默认落盘 ``messages.json``；仅在 ``enable_portrait`` 时用 LLM 更新画像文件。

    产物路径（session 级随 ``WORKSPACE.path`` + layout；用户级固定在 home）：
    - ``~/.dataagent/{user_id}/.memory/profile.json``
    - ``{workspace}/<session_memory_dir>/snapshot.json``
    - ``{workspace}/<session_memory_dir>/messages.json``
    """
    logger.debug("[portraiter] hook called")
    user_id = str(state.get("user_id") or "")
    session_id = str(state.get("session_id") or "")
    messages: list[BaseMessage] = list(state.get("messages") or [])

    logger.debug(f"[portraiter] user_id={user_id}, session_id={session_id}, messages_count={len(messages)}")

    if not user_id or not session_id:
        logger.debug("[portraiter] skipped: no user_id or session_id")
        return state

    if is_subagent(state) and not is_job_workspace_subagent(state):
        logger.debug("[portraiter] skipped: is subagent")
        return state

    workspace = state.get("workspace")
    config = runtime.get_all_config() if hasattr(runtime, "get_all_config") else None

    # 如果 state 中没有 messages，尝试从历史文件加载
    if not messages:
        from dataagent.core.flex.hooks.history_writer import load_messages

        messages = load_messages(user_id, session_id, workspace=workspace, config=config)
        logger.debug(f"[portraiter] loaded {len(messages)} messages from history file")

    save_messages(user_id, session_id, messages, workspace=workspace, config=config)

    if is_subagent(state):
        logger.debug("[portraiter] job subagent: messages persisted, skipping portrait update")
        return state

    if not state.get("enable_portrait"):
        logger.debug("[portraiter] skipped: enable_portrait is False")
        return state

    logger.debug("[portraiter] proceeding with portrait update")

    snapshot = _load_snapshot(user_id, session_id, workspace=workspace, config=config)
    profile = _load_profile(user_id)
    conversation = _messages_to_conversation(messages)

    # 独立更新 snapshot（session 级）
    updated_snapshot = _normalize_snapshot(_update_snapshot(snapshot, conversation, runtime))
    _save_snapshot(user_id, session_id, updated_snapshot, workspace=workspace, config=config)

    # 独立更新 profile（用户级）
    updated_profile = _normalize_profile(_update_profile(profile, conversation, runtime))
    _save_profile(user_id, updated_profile)

    # 更新 MEMORY.md 索引（在 snapshot/profile 更新之后调用）
    try:
        from dataagent.core.flex.hooks.memory_indexer import update_memory_index

        update_memory_index(user_id, session_id, workspace=workspace, config=config)
    except Exception as e:
        logger.warning(f"[portraiter] failed to update memory index: {e}")

    return state
