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

"""Cache usage and context-dump analysis helpers for bio_lab performance tests."""

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger


def _collect_usage_metadata_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    usage_list: list[dict[str, Any]] = []
    if isinstance(messages, list):
        for msg in messages:
            msg_type = getattr(msg, "type", None) or ""
            if msg_type == "ai":
                usage = getattr(msg, "usage_metadata", None) or {}
                if usage and usage.get("input_tokens", 0) > 0:
                    usage_list.append(
                        {
                            "input_tokens": usage.get("input_tokens", 0),
                            "input_cache_read_tokens": usage.get("input_cache_read_tokens", 0),
                            "input_cache_creation_tokens": usage.get("input_cache_creation_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        }
                    )
    return usage_list


def _collect_usage_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    messages = state.get("messages", []) or []
    return _collect_usage_metadata_from_messages(messages)


def _extract_final_assistant_text(messages: list[Any]) -> str:
    """Return the answer text for one user query message window.

    If the agent used a plan, prefer the AIMessage that completes the last
    todo when it already contains user-facing text. If that message only
    carries a tool call, use the first substantive AIMessage after the matching
    ToolMessage. For non-plan flows, fall back to the last substantive
    AIMessage in the window.
    """
    if not isinstance(messages, list):
        return ""

    last_complete_idx = _find_last_ai_tool_call_index(messages, "complete_current_todo")
    if last_complete_idx is not None:
        content = _substantive_ai_content(messages[last_complete_idx])
        if content:
            return content

        complete_tool_call_ids = _tool_call_ids(messages[last_complete_idx], "complete_current_todo")
        after_complete_tool = False
        for msg in messages[last_complete_idx + 1 :]:
            if not after_complete_tool:
                after_complete_tool = _is_tool_response_for(msg, complete_tool_call_ids)
                continue
            content = _substantive_ai_content(msg)
            if content:
                return content

    last_text = ""
    for msg in messages:
        content = _substantive_ai_content(msg)
        if content:
            last_text = content
    return last_text


def _find_last_ai_tool_call_index(messages: list[Any], tool_name: str) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        if _has_tool_call(messages[idx], tool_name):
            return idx
    return None


def _substantive_ai_content(msg: Any) -> str:
    if _message_type(msg) != "ai":
        return ""
    if bool((_message_additional_kwargs(msg) or {}).get("error")):
        return ""
    content = _message_content(msg)
    if not isinstance(content, str) or not content.strip():
        return ""
    if _is_housekeeping_assistant_text(content):
        return ""
    return content


def _is_housekeeping_assistant_text(content: str) -> bool:
    normalized = re.sub(r"\s+", "", content)
    housekeeping_phrases = (
        "上一轮查询已全部完成",
        "当前没有待处理的新任务",
        "如有新的数据查询需求",
    )
    return any(phrase in normalized for phrase in housekeeping_phrases)


def _is_tool_response_for(msg: Any, tool_call_ids: set[str]) -> bool:
    if _message_type(msg) != "tool":
        return False
    tool_call_id = _message_tool_call_id(msg)
    return not tool_call_ids or tool_call_id in tool_call_ids


def _has_tool_call(msg: Any, tool_name: str) -> bool:
    return bool(_tool_call_ids(msg, tool_name))


def _tool_call_ids(msg: Any, tool_name: str) -> set[str]:
    tool_call_ids: set[str] = set()
    for tool_call in _message_tool_calls(msg):
        if not isinstance(tool_call, dict) or tool_call.get("name") != tool_name:
            continue
        tool_call_id = tool_call.get("id")
        if tool_call_id:
            tool_call_ids.add(str(tool_call_id))
    return tool_call_ids


def _message_type(msg: Any) -> str:
    msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
    msg_type = str(msg_type or "").lower()
    if msg_type in {"ai", "assistant", "aimessage"}:
        return "ai"
    if msg_type in {"tool", "toolmessage"}:
        return "tool"
    return msg_type


def _message_content(msg: Any) -> Any:
    return msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")


def _message_tool_calls(msg: Any) -> list[Any]:
    return (msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)) or []


def _message_tool_call_id(msg: Any) -> str:
    tool_call_id = msg.get("tool_call_id") if isinstance(msg, dict) else getattr(msg, "tool_call_id", "")
    return str(tool_call_id or "")


def _message_additional_kwargs(msg: Any) -> dict[str, Any]:
    additional_kwargs = msg.get("additional_kwargs") if isinstance(msg, dict) else getattr(msg, "additional_kwargs", {})
    return additional_kwargs if isinstance(additional_kwargs, dict) else {}


def _compute_cache_hit_rate(usage_records: list[dict[str, Any]]) -> dict[str, float]:
    total_input = sum(r["input_tokens"] for r in usage_records)
    total_output = sum(r.get("output_tokens", 0) for r in usage_records)
    total_cache_read = sum(r["input_cache_read_tokens"] for r in usage_records)
    total_cache_creation = sum(r["input_cache_creation_tokens"] for r in usage_records)

    if total_input == 0:
        return {
            "hit_rate": 0.0,
            "total_input": 0,
            "total_output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "num_calls": 0,
            "per_call_rates": [],
        }

    per_call_rates = []
    for r in usage_records:
        rate = r["input_cache_read_tokens"] / max(r["input_tokens"], 1)
        per_call_rates.append(round(rate * 100, 1))

    return {
        "hit_rate": round(total_cache_read / total_input * 100, 1),
        "total_input": total_input,
        "total_output": total_output,
        "cache_read": total_cache_read,
        "cache_creation": total_cache_creation,
        "num_calls": len(usage_records),
        "per_call_rates": per_call_rates,
    }


def _format_per_query_summary(per_query_usages: list[dict[str, Any]]) -> str:
    """Format per-query cache stats as a multi-line string for logs/assertions.

    For each user question, shows: number of LLM calls, end-to-end execution
    time, overall hit rate, and input/output/cache_read/cache_creation token
    totals — in addition to the per-LLM-call rates that are already printed
    elsewhere.
    """
    if not per_query_usages:
        return "Per-query breakdown: (no queries)"
    lines = [f"Per-query breakdown ({len(per_query_usages)} queries):"]
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_calls = 0
    total_elapsed = 0.0
    for i, q in enumerate(per_query_usages, 1):
        s = _compute_cache_hit_rate(q["usages"])
        elapsed = q.get("elapsed_sec", 0)
        total_input += s["total_input"]
        total_output += s["total_output"]
        total_cache_read += s["cache_read"]
        total_cache_creation += s["cache_creation"]
        total_calls += s["num_calls"]
        total_elapsed += elapsed
        lines.append(
            f"  [{i}/{len(per_query_usages)}] {q['query_key']}: "
            f"calls={s['num_calls']}, elapsed={elapsed}s, hit_rate={s['hit_rate']}%, "
            f"input={s['total_input']}, output={s['total_output']}, "
            f"cache_read={s['cache_read']}, cache_creation={s['cache_creation']}"
        )
    overall_hit = round(total_cache_read / total_input * 100, 1) if total_input else 0.0
    lines.append(
        f"  [TOTAL] calls={total_calls}, elapsed={round(total_elapsed, 2)}s, "
        f"hit_rate={overall_hit}%, "
        f"input={total_input}, output={total_output}, "
        f"cache_read={total_cache_read}, cache_creation={total_cache_creation}"
    )
    return "\n".join(lines)


def _dump_cache_analysis(
    usage_records: list[dict[str, Any]],
    output_dir: Path,
    label: str = "",
) -> Path:
    analysis = {
        "label": label,
        "cache_stats": _compute_cache_hit_rate(usage_records),
        "per_call": usage_records,
    }
    suffix = f"_{label}" if label else ""
    out_path = output_dir / f"cache_analysis_v3{suffix}.json"
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Cache analysis written to {out_path}")
    return out_path


def _extract_cache_from_messages_file(session_root: Path) -> dict[str, Any]:
    messages_file = session_root / "workspace" / ".memory" / "messages.json"
    if not messages_file.exists():
        return {"hit_rate": 0.0, "total_input": 0, "cache_read": 0, "num_calls": 0}

    from dataagent.core.context.message_history import read_messages_file

    try:
        records = read_messages_file(messages_file)
        usage_list = _collect_usage_metadata_from_messages(records)
        return _compute_cache_hit_rate(usage_list)
    except Exception as e:
        logger.warning(f"Failed to read messages.json: {e}")
        return {"hit_rate": 0.0, "total_input": 0, "cache_read": 0, "num_calls": 0}


def _analyze_context_dumps(session_root: Path) -> list[dict[str, Any]]:
    dump_base = session_root / "workspace" / ".memory" / "context_dump"
    if not dump_base.exists():
        logger.warning(f"No context_dump directory at {dump_base}")
        return []

    analysis = []
    for run_dir in sorted(dump_base.iterdir()):
        if not run_dir.is_dir():
            continue
        for round_file in sorted(run_dir.iterdir()):
            if round_file.suffix != ".txt":
                continue
            content = round_file.read_text(encoding="utf-8")
            msg_count = content.count("--- [")
            analysis.append(
                {
                    "file": str(round_file.relative_to(dump_base)),
                    "message_count": msg_count,
                    "file_size": len(content),
                }
            )
    return analysis


def _verify_system_message_stability(session_root: Path) -> dict[str, Any]:
    """Verify D6 fix: SystemMessage should NOT contain CPU:/Memory: lines.

    Across all context dumps, the SYSTEM section should be byte-identical
    (no dynamic runtime_environment values).
    """

    dump_base = session_root / "workspace" / ".memory" / "context_dump"
    if not dump_base.exists():
        return {"stable": False, "reason": "no context_dump dir", "system_samples": []}

    system_samples: list[dict[str, Any]] = []
    system_texts: list[str] = []
    for run_dir in sorted(dump_base.iterdir()):
        if not run_dir.is_dir():
            continue
        round_files = sorted(run_dir.glob("round_*.txt"))
        if not round_files:
            continue
        # Only inspect round_0 of each run (the first call after restart)
        round_file = round_files[0]
        content = round_file.read_text(encoding="utf-8")

        # Extract SYSTEM section. The header may include breakpoint annotation
        # like "--- [0] SYSTEM [bp 1 candidate] ---", so use regex to match.
        lines = content.splitlines()
        sys_lines: list[str] = []
        in_system = False
        for line in lines:
            if re.match(r"^--- \[0\] SYSTEM.*---", line):
                in_system = True
                continue
            if in_system and re.match(r"^--- \[\d+\].*---", line):
                break
            if in_system:
                sys_lines.append(line)

        system_text = "\n".join(sys_lines)
        has_cpu = any(line.strip().startswith("- CPU:") for line in sys_lines)
        has_memory = any(line.strip().startswith("- Memory:") for line in sys_lines)
        system_samples.append(
            {
                "file": str(round_file.relative_to(dump_base)),
                "system_chars": len(system_text),
                "has_cpu_line": has_cpu,
                "has_memory_line": has_memory,
                "system_hash": hash(system_text),
            }
        )
        system_texts.append(system_text)

    if not system_samples:
        return {"stable": False, "reason": "no round_0 files found", "system_samples": []}

    all_stable = all(not s["has_cpu_line"] and not s["has_memory_line"] for s in system_samples)
    # Also verify byte-stability: all system_texts should be identical
    byte_stable = len(set(system_texts)) == 1 if system_texts else False
    reason = (
        f"D6 verified: no CPU/Memory lines in SYSTEM, byte-stable across {len(system_samples)} runs"
        if all_stable and byte_stable
        else f"D6 FAILED: has_cpu_lines={[s['has_cpu_line'] for s in system_samples]}, byte_stable={byte_stable}"
    )
    return {
        "stable": all_stable and byte_stable,
        "reason": reason,
        "system_samples": system_samples,
    }


compute_cache_hit_rate = _compute_cache_hit_rate
