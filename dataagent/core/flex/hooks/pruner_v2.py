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
"""Flex 内置 pruner hook（planner 节点 pre-hook）。

从 8577 ``core.state.pruner`` 移植：字符阈值 + 固定尾部窗口 + LLM 摘要。
适配本仓 Runtime / FlexState / ``add_messages`` reducer。

可选 YAML（任选其一 dict）::

    CONTEXT:
      pruner:
        enabled: true
        strategy: basic
        threshold_chars: 262144
        min_start: 1
        tail_keep: 6
        llm_role: planner
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState

MIN_START = 1
TAIL_KEEP = 6
COMPRESSION_THRESHOLD = 262144
PRUNER_DIR_NAME = "pruner"
PRUNER_SNAPSHOT_FILE_NAME = "snapshots.json"
_BEIJING_TZ = timezone(timedelta(hours=8))


def pruner(state: FlexState, runtime: Runtime) -> FlexState:
    """Planner 节点 pre-hook：按字符阈值压缩中间历史消息。"""
    config = _pruner_config(runtime)
    if not bool(config.get("enabled", True)):
        return state
    strategy = str(config.get("strategy") or "basic").strip().lower()
    if strategy not in {"basic", "default"}:
        return state

    updated_state = cast(FlexState, deepcopy(dict(state)))
    messages = list(state.get("messages") or [])
    start, end = _compression_window(messages, config)
    if start >= end:
        return updated_state

    messages_to_summarize = messages[start : end + 1]
    content = _messages_to_summary_input(messages_to_summarize)
    threshold = _positive_int(config.get("threshold_chars"), COMPRESSION_THRESHOLD)
    if len(content) < threshold:
        return updated_state

    try:
        summary = _summarize_messages(content, runtime, config)
    except Exception as exc:
        logger.exception(f"[pruner] compression failed, skip pruning: {type(exc).__name__}: {exc}")
        _record_pruning_error(runtime=runtime, start=start, end=end, error=str(exc))
        return updated_state
    if not summary:
        return updated_state

    # Flex messages reducer 是 add_messages：必须先 RemoveMessage 清空再追加。
    compressed = messages[:start] + [AIMessage(content=summary)] + messages[end + 1 :]
    updated_state["messages"] = cast(
        list[AnyMessage],
        [RemoveMessage(id="__remove_all__"), *compressed],
    )
    try:
        _record_pruning_snapshot(
            runtime=runtime,
            start=start,
            end=end,
            original_message_count=len(messages),
            pruned_message_count=len(messages_to_summarize),
            summary=summary,
        )
    except OSError:
        logger.debug("[pruner] snapshot write skipped", exc_info=True)
    return updated_state


def _pruner_config(runtime: Runtime) -> dict[str, Any]:
    """读取 pruner 配置：CONTEXT.pruner / AGENT_CONFIG.pruner / PRUNER，兼容 env.state.pruner。"""
    get_config = getattr(runtime, "get_config", None)
    if callable(get_config):
        for key in ("CONTEXT.pruner", "AGENT_CONFIG.pruner", "PRUNER"):
            raw = get_config(key)
            if isinstance(raw, dict):
                return raw

    env = getattr(runtime, "env", None)
    state = getattr(env, "state", None) if env is not None else None
    if isinstance(state, dict):
        raw = state.get("pruner")
        if isinstance(raw, dict):
            return raw
    return {}


def _compression_window(messages: list[Any], config: dict[str, Any]) -> tuple[int, int]:
    start = _positive_int(config.get("min_start"), MIN_START)
    tail_keep = _positive_int(config.get("tail_keep"), TAIL_KEEP)
    end = len(messages) - tail_keep

    while start < end:
        if isinstance(messages[start], AIMessage):
            break
        start += 1
    while end > start:
        if isinstance(messages[end], ToolMessage):
            break
        end -= 1

    # Swallow leading orphan ToolMessages into the pruned range so the kept
    # suffix never starts with a tool reply whose AI(tool_calls) was removed.
    end = _leading_orphan_tool_end(messages, end)
    if end >= len(messages):
        end = len(messages) - 1
    if end <= start:
        return start, start

    # Compression must start at an AIMessage, not mid tool-batch.
    start = _align_start_to_tool_batch(messages, start, end)
    return start, end


def _tool_call_ids(message: Any) -> set[str]:
    if not isinstance(message, AIMessage):
        return set()
    ids: set[str] = set()
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict):
            call_id = str(call.get("id") or "").strip()
        else:
            call_id = str(getattr(call, "id", "") or "").strip()
        if call_id:
            ids.add(call_id)
    return ids


def _leading_orphan_tool_end(messages: list[Any], end: int) -> int:
    """Extend end so kept suffix messages[end+1:] has no orphan ToolMessages."""
    if end < -1 or end >= len(messages) - 1:
        return end

    while True:
        suffix_start = end + 1
        if suffix_start >= len(messages):
            return end

        open_ids: set[str] = set()
        last_orphan = -1
        for index in range(suffix_start, len(messages)):
            message = messages[index]
            if isinstance(message, AIMessage):
                open_ids = _tool_call_ids(message)
                continue
            if isinstance(message, ToolMessage):
                tool_call_id = str(getattr(message, "tool_call_id", "") or "").strip()
                if tool_call_id and tool_call_id in open_ids:
                    open_ids.discard(tool_call_id)
                    continue
                # Tool without a matching AI(tool_calls) still in the suffix.
                last_orphan = index
                continue
            # Human / other roles reset the open tool_calls context.
            open_ids = set()

        if last_orphan < 0:
            return end

        # Fold orphans (and everything through last_orphan) into the prune window.
        end = last_orphan
        if end >= len(messages) - 1:
            return end


def _align_start_to_tool_batch(messages: list[Any], start: int, end: int) -> int:
    """If start lands on a ToolMessage, rewind to its parent AIMessage."""
    if start <= 0 or start >= len(messages) or start >= end:
        return start
    if not isinstance(messages[start], ToolMessage):
        return start

    tool_call_id = str(getattr(messages[start], "tool_call_id", "") or "").strip()
    for index in range(start - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage):
            if not tool_call_id or tool_call_id in _tool_call_ids(message):
                return index
            # Nearest prior AI without matching id — still a safe batch boundary.
            return index
        if isinstance(message, ToolMessage):
            continue
        break
    return start


def _messages_to_summary_input(messages: list[Any]) -> str:
    content = ""
    for message in messages:
        if isinstance(message, AIMessage):
            content += f"Assistant: {message}\n"
        elif isinstance(message, ToolMessage):
            content += f"Tool: {message}\n"
        else:
            content += f"Unknown: {message}\n"
    return content


def _summarize_messages(content: str, runtime: Runtime, config: dict[str, Any]) -> str:
    prompt = f"""You are a context compression model for an agent runtime.

Input: a chronological list of messages (assistant/tool).

Task: produce a compact, faithful summary that preserves:
- Key actions taken by the assistant.
- Key results obtained from tool executions.
- References to external artifacts by ID/path if present.
- Open issues, errors, and TODOs.

Rule:
- Do NOT include verbose logs, code, or large outputs.
- Prefer structured YAML.
- Output only the summary (no preamble).

Please summarize the following content:
{content}
"""
    role = str(config.get("llm_role") or "pruner").strip()
    llm = _select_pruner_llm(runtime, role)
    if llm is None:
        logger.warning("[pruner] no LLM available (tried llm_role/planner/chat_model), skip")
        return ""
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content if hasattr(response, "content") else str(response)


def _select_pruner_llm(runtime: Runtime, role: str) -> Any:
    """按 env.llm_configs 键选择 LLM；本仓无 runtime.llms，需先探测再 llm()。"""
    configs = getattr(getattr(runtime, "env", None), "llm_configs", None)
    if not isinstance(configs, dict):
        configs = {}
    for candidate in (role, "planner", "chat_model"):
        if not candidate:
            continue
        if configs and candidate not in configs:
            continue
        try:
            return runtime.llm(candidate)
        except Exception:
            logger.debug(f"[pruner] failed to init LLM candidate {candidate!r}", exc_info=True)
            continue
    return None


def _snapshot_path(runtime: Runtime) -> Path | None:
    workspace = getattr(runtime, "workspace_dir", None)
    if workspace is None:
        return None
    return Path(workspace).expanduser().resolve() / ".memory" / PRUNER_DIR_NAME / PRUNER_SNAPSHOT_FILE_NAME


def _now_beijing_iso() -> str:
    return datetime.now(_BEIJING_TZ).isoformat(timespec="seconds")


def _read_json_object(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _record_pruning_snapshot(
    *,
    runtime: Runtime,
    start: int,
    end: int,
    original_message_count: int,
    pruned_message_count: int,
    summary: str,
) -> None:
    path = _snapshot_path(runtime)
    if path is None:
        return
    payload = _read_json_object(path, {"version": 1, "snapshots": []})
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots.append(
        {
            "saved_at": _now_beijing_iso(),
            "user_id": getattr(runtime, "user_id", ""),
            "session_id": getattr(runtime, "session_id", ""),
            "range": {"start": start, "end": end},
            "original_message_count": original_message_count,
            "pruned_message_count": pruned_message_count,
            "summary": summary,
        }
    )
    payload["version"] = 1
    payload["snapshots"] = snapshots
    _write_json_object(path, payload)


def _record_pruning_error(*, runtime: Runtime, start: int, end: int, error: str) -> None:
    try:
        path = _snapshot_path(runtime)
        if path is None:
            return
        payload = _read_json_object(path, {"version": 1, "snapshots": []})
        snapshots = payload.get("snapshots")
        if not isinstance(snapshots, list):
            snapshots = []
        snapshots.append(
            {
                "saved_at": _now_beijing_iso(),
                "user_id": getattr(runtime, "user_id", ""),
                "session_id": getattr(runtime, "session_id", ""),
                "range": {"start": start, "end": end},
                "status": "failed",
                "error": error,
            }
        )
        payload["version"] = 1
        payload["snapshots"] = snapshots
        _write_json_object(path, payload)
    except Exception:
        logger.debug("[pruner] failed to persist pruning error snapshot", exc_info=True)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
