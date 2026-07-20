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
"""Shared ``messages.json`` wire format and file I/O for LangChain message histories.

Provides serialize/deserialize, replay-safe sanitization, and explicit-path read/write
helpers. Flex session hooks and swarm worker persistence both reuse this module so
message artifacts stay format-compatible across session and worker directories.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def _serialize(msg: BaseMessage) -> dict[str, Any]:
    """Convert one LangChain message to the ``messages.json`` record wire format."""
    # _ts is stamped at message creation time (build_human_message /
    # Planner._to_ai_message) to capture the actual chat time. This fallback
    # only stamps _ts for messages created outside those paths (e.g. langchain
    # internals), ensuring all records have a timestamp for
    # _compute_round_summaries to derive per-round elapsed time.
    akw_live = getattr(msg, "additional_kwargs", None)
    if not isinstance(akw_live, dict):
        akw_live = {}
        with contextlib.suppress(Exception):
            msg.additional_kwargs = akw_live
    if isinstance(akw_live, dict) and "_ts" not in akw_live:
        akw_live["_ts"] = time.time()
    payload: dict[str, Any] = {
        "type": msg.__class__.__name__,
        "content": getattr(msg, "content", ""),
        "name": getattr(msg, "name", "") or "",
        "additional_kwargs": dict(getattr(msg, "additional_kwargs", {}) or {}),
        "response_metadata": dict(getattr(msg, "response_metadata", {}) or {}),
    }
    if isinstance(msg, AIMessage):
        payload["tool_calls"] = list(getattr(msg, "tool_calls", []) or [])
        payload["invalid_tool_calls"] = list(getattr(msg, "invalid_tool_calls", []) or [])
        usage = getattr(msg, "usage_metadata", None)
        if isinstance(usage, dict) and usage:
            payload["usage_metadata"] = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "input_cache_read_tokens": int(usage.get("input_cache_read_tokens") or 0),
                "input_cache_creation_tokens": int(usage.get("input_cache_creation_tokens") or 0),
                "output_reasoning_tokens": int(usage.get("output_reasoning_tokens") or 0),
            }
    if isinstance(msg, ToolMessage):
        tool_call_id = getattr(msg, "tool_call_id", "")
        payload["tool_call_id"] = str(tool_call_id) if tool_call_id is not None else ""
    return payload


def _deserialize(payload: dict[str, Any]) -> BaseMessage | None:
    """Parse one wire-format record into a LangChain message, or ``None`` when invalid."""
    t = str(payload.get("type", ""))
    content = payload.get("content", "")
    akw = payload.get("additional_kwargs") or {}
    rmeta = payload.get("response_metadata") or {}
    if not isinstance(akw, dict):
        akw = {}
    if not isinstance(rmeta, dict):
        rmeta = {}

    if t == "HumanMessage":
        return HumanMessage(content=content, additional_kwargs=akw, response_metadata=rmeta)
    if t == "AIMessage":
        tool_calls = payload.get("tool_calls") or []
        if not tool_calls and isinstance(akw.get("tool_calls"), list):
            tool_calls = akw.get("tool_calls") or []
        raw_usage = payload.get("usage_metadata") or {}
        usage_metadata = {
            "input_tokens": int(raw_usage.get("input_tokens") or 0),
            "output_tokens": int(raw_usage.get("output_tokens") or 0),
            "total_tokens": int(raw_usage.get("total_tokens") or 0),
            "input_cache_read_tokens": int(raw_usage.get("input_cache_read_tokens") or 0),
            "input_cache_creation_tokens": int(raw_usage.get("input_cache_creation_tokens") or 0),
            "output_reasoning_tokens": int(raw_usage.get("output_reasoning_tokens") or 0),
        }
        return AIMessage(
            content=content,
            additional_kwargs=akw,
            response_metadata=rmeta,
            tool_calls=tool_calls,
            invalid_tool_calls=payload.get("invalid_tool_calls") or [],
            usage_metadata=usage_metadata,
        )
    if t == "ToolMessage":
        tid = payload.get("tool_call_id", "")
        tid = tid if isinstance(tid, str) else ""
        if not tid.strip():
            return None
        return ToolMessage(
            content=content,
            tool_call_id=tid,
            additional_kwargs=akw,
            response_metadata=rmeta,
        )
    return None


def _valid_tool_call_ids(msg: AIMessage) -> set[str]:
    """Collect non-empty tool-call ids from an ``AIMessage`` payload."""
    calls = list(getattr(msg, "tool_calls", []) or [])
    if not calls:
        raw = getattr(msg, "additional_kwargs", {}).get("tool_calls", [])
        if isinstance(raw, list):
            calls = raw
    ids: set[str] = set()
    for c in calls:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid and str(cid).strip():
            ids.add(str(cid).strip())
    return ids


def _sanitize(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Drop orphan AIMessage→ToolMessage pairs (aligned with galatea replay sanitization)."""
    sanitized: list[BaseMessage] = []
    pending_ai: AIMessage | None = None
    pending_tools: list[ToolMessage] = []
    pending_ids: set[str] = set()

    def _flush(*, keep: bool) -> None:
        nonlocal pending_ai, pending_tools, pending_ids
        if keep and pending_ai is not None:
            sanitized.append(pending_ai)
            sanitized.extend(pending_tools)
        pending_ai = None
        pending_tools = []
        pending_ids = set()

    for msg in messages:
        if isinstance(msg, AIMessage):
            _flush(keep=not pending_ids)
            pending_ids = _valid_tool_call_ids(msg)
            if pending_ids:
                pending_ai = msg
                pending_tools = []
            else:
                sanitized.append(msg)
            continue
        if isinstance(msg, ToolMessage):
            tid = str(getattr(msg, "tool_call_id", "") or "").strip()
            if not pending_ids or tid not in pending_ids:
                continue
            pending_tools.append(msg)
            pending_ids.discard(tid)
            if not pending_ids:
                _flush(keep=True)
            continue
        _flush(keep=not pending_ids)
        sanitized.append(msg)

    _flush(keep=not pending_ids)
    return sanitized


def _read_raw(path: Path) -> list[dict[str, Any]]:
    """Load raw message records from a ``messages.json`` file."""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("messages", [])
        return records if isinstance(records, list) else []
    except (OSError, ValueError):
        return []


def _compute_round_summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从序列化后的 messages records 中按 HumanMessage 分轮次，累加每轮的 token 统计。

    连续的 HumanMessage（如 user query + SESSION INTENT 注入）合并为同一轮，
    不会因为注入的上下文消息而拆分出空轮次。

    每轮 summary 额外包含：
    - ``elapsed_sec``：该轮首末消息的 ``_ts`` 时间戳之差（秒）。``_ts`` 由
      ``_serialize`` 在消息首次序列化时盖戳。旧记录无 ``_ts`` 时为 ``0.0``。
    - ``cache_hit_rate``：``input_cache_read_tokens / input_tokens * 100``（%）。
    """
    summaries: list[dict[str, Any]] = []
    round_idx = 0
    round_usage: dict[str, int] | None = None
    round_has_ai: bool = False
    round_start_ts: float | None = None
    round_end_ts: float | None = None

    def _extract_ts(rec: dict[str, Any]) -> float | None:
        """Extract ``_ts`` from a record's additional_kwargs, skipping folded summaries."""
        akw = rec.get("additional_kwargs") or {}
        if not isinstance(akw, dict):
            return None
        # 折叠摘要（``direct_fold`` 产出）的 ``_ts`` 反映的是首次序列化时刻，
        # 可能远晚于该轮真实消息，不能用作轮次计时。跳过它可避免
        # round_start_ts > round_end_ts 导致负数 elapsed_sec。
        if akw.get("_folded"):
            return None
        ts = akw.get("_ts")
        return float(ts) if ts is not None else None

    def _new_usage() -> dict[str, int]:
        """Return a fresh zeroed token-usage accumulator dict."""
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_cache_read_tokens": 0,
            "input_cache_creation_tokens": 0,
            "output_reasoning_tokens": 0,
        }

    def _finalize(idx: int, usage: dict[str, int], start_ts: float | None, end_ts: float | None) -> dict[str, Any]:
        """Build the summary dict for one round from its accumulated usage and ts range."""
        input_tokens = usage.get("input_tokens", 0)
        cache_read = usage.get("input_cache_read_tokens", 0)
        cache_hit_rate = round(cache_read / input_tokens * 100, 1) if input_tokens > 0 else 0.0
        elapsed_sec = round(end_ts - start_ts, 2) if start_ts is not None and end_ts is not None else 0.0
        return {
            "round": idx,
            **usage,
            "elapsed_sec": elapsed_sec,
            "cache_hit_rate": cache_hit_rate,
        }

    def _update_ts(start_ts: float | None, end_ts: float | None, ts: float | None) -> tuple[float | None, float | None]:
        """Track earliest and latest timestamps, robust to out-of-order _ts."""
        if ts is None:
            return start_ts, end_ts
        if start_ts is None or ts < start_ts:
            start_ts = ts
        if end_ts is None or ts > end_ts:
            end_ts = ts
        return start_ts, end_ts

    for rec in records:
        t = str(rec.get("type", ""))
        ts = _extract_ts(rec)
        if t == "HumanMessage":
            if round_usage is not None and round_has_ai:
                summaries.append(_finalize(round_idx, round_usage, round_start_ts, round_end_ts))
                round_idx += 1
                round_usage = _new_usage()
                round_has_ai = False
                round_start_ts = ts
                round_end_ts = ts
            if round_usage is None:
                round_usage = _new_usage()
                round_has_ai = False
                round_start_ts = ts
                round_end_ts = ts
            else:
                round_start_ts, round_end_ts = _update_ts(round_start_ts, round_end_ts, ts)
        if t == "AIMessage" and round_usage is not None:
            round_has_ai = True
            um = rec.get("usage_metadata") or {}
            for k in round_usage:
                round_usage[k] += int(um.get(k) or 0)
            round_start_ts, round_end_ts = _update_ts(round_start_ts, round_end_ts, ts)
    if round_usage is not None:
        summaries.append(_finalize(round_idx, round_usage, round_start_ts, round_end_ts))
    return summaries


def _write_raw(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomically write raw message records and round_summaries to a ``messages.json`` file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    round_summaries = _compute_round_summaries(records)
    payload = {"messages": records, "round_summaries": round_summaries}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def serialize_message(msg: BaseMessage) -> dict[str, Any]:
    """Serialize one LangChain message using the shared history wire format."""
    return _serialize(msg)


def deserialize_message(payload: dict[str, Any]) -> BaseMessage | None:
    """Deserialize one record from the shared history wire format."""
    return _deserialize(payload)


def sanitize_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return messages safe for replay, filtering SystemMessage and orphan tool calls."""
    return _sanitize([m for m in messages if not isinstance(m, SystemMessage)])


def read_messages_file(path: Path | str) -> list[BaseMessage]:
    """Read and sanitize messages from an explicit ``messages.json`` path."""
    records = _read_raw(Path(path))
    messages: list[BaseMessage] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        msg = deserialize_message(record)
        if msg is not None:
            messages.append(msg)
    return sanitize_messages(messages)


def write_messages_file(
    path: Path | str,
    messages: list[BaseMessage],
    *,
    sanitize: bool = True,
) -> None:
    """Write messages to an explicit ``messages.json`` path.

    Args:
        path: Target ``messages.json`` path.
        messages: LangChain messages to persist.
        sanitize: When ``True`` (default), apply ``sanitize_messages`` (drop
            orphan AIMessage→ToolMessage pairs, strip SystemMessage) before
            writing — suitable for replay-safe snapshots. When ``False``, only
            strip SystemMessage and keep orphan AIMessages intact; the reader
            (:func:`read_messages_file`) still sanitizes at load time, so
            replay safety is preserved while the on-disk file retains the full
            state (including HITL requests whose ToolMessage hasn't arrived
            yet) for archival/debugging.
    """
    filtered = sanitize_messages(messages) if sanitize else [m for m in messages if not isinstance(m, SystemMessage)]
    records = [serialize_message(m) for m in filtered]
    _write_raw(Path(path), records)


def write_message_records_file(path: Path | str, records: list[dict[str, Any]]) -> None:
    """Write already-serialized message records after deserialize/sanitize validation."""
    messages: list[BaseMessage] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        msg = deserialize_message(record)
        if msg is not None:
            messages.append(msg)
    write_messages_file(path, messages)
