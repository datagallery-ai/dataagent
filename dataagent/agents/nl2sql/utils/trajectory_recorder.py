from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from dataagent.core.context.message_history import _write_raw
from dataagent.utils.constants import _TZ_CN

_PROMPT_SUMMARY_MAX = 2000
_RESULT_SUMMARY_MAX = 8000


def _now_cn() -> datetime:
    return datetime.now(tz=_TZ_CN)


def _ts_cn() -> str:
    return _now_cn().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class _NullRecorder:
    def record_node_start(self, **kw): pass
    def record_tool_call(self, **kw): return "call_null"
    def record_tool_result(self, **kw): pass
    def record_llm_call(self, **kw): pass


class NL2SQLTrajectoryRecorder:
    """Records NL2SQL sub-agent internal steps as LangChain-compatible message records."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._call_counter: int = 0

    def _next_tool_call_id(self) -> str:
        self._call_counter += 1
        return f"call_nl2sql_{self._call_counter}"

    def record_tool_call(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        purpose: str,
        tool_call_id: str | None = None,
    ) -> str:
        tid = tool_call_id or self._next_tool_call_id()
        self._records.append({
            "type": "AIMessage",
            "content": "",
            "name": "",
            "additional_kwargs": {"reasoning_content": purpose},
            "response_metadata": {"timestamp": _ts_cn()},
            "tool_calls": [
                {
                    "name": tool_name,
                    "args": args,
                    "id": tid,
                    "type": "tool_call",
                }
            ],
            "invalid_tool_calls": [],
            "usage_metadata": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "input_cache_read_tokens": 0,
                "input_cache_creation_tokens": 0,
                "output_reasoning_tokens": 0,
            },
        })
        return tid

    def record_tool_result(self, *, content: str, tool_call_id: str) -> None:
        self._records.append({
            "type": "ToolMessage",
            "content": content,
            "name": "",
            "additional_kwargs": {},
            "response_metadata": {"timestamp": _ts_cn()},
            "tool_call_id": tool_call_id,
        })

    def record_llm_call(
        self,
        *,
        node_name: str,
        action: str,
        purpose: str,
        prompt_summary: str,
        result_summary: str,
        usage_metadata: dict[str, int] | None = None,
    ) -> None:
        tid = self._next_tool_call_id()
        um = usage_metadata or {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_cache_read_tokens": 0,
            "input_cache_creation_tokens": 0,
            "output_reasoning_tokens": 0,
        }
        full_purpose = f"{node_name}: {purpose}"
        tool_name = f"llm_invoke_{node_name}"
        if action:
            tool_name += f"_{action}"
        args = {"prompt_summary": prompt_summary[:_PROMPT_SUMMARY_MAX] if len(prompt_summary) > _PROMPT_SUMMARY_MAX else prompt_summary}
        self._records.append({
            "type": "AIMessage",
            "content": "",
            "name": "",
            "additional_kwargs": {"reasoning_content": full_purpose},
            "response_metadata": {"timestamp": _ts_cn()},
            "tool_calls": [
                {
                    "name": tool_name,
                    "args": args,
                    "id": tid,
                    "type": "tool_call",
                }
            ],
            "invalid_tool_calls": [],
            "usage_metadata": um,
        })
        result_content = result_summary[:_RESULT_SUMMARY_MAX] if len(result_summary) > _RESULT_SUMMARY_MAX else result_summary
        self._records.append({
            "type": "ToolMessage",
            "content": result_content,
            "name": "",
            "additional_kwargs": {},
            "response_metadata": {"timestamp": _ts_cn()},
            "tool_call_id": tid,
        })

    def record_node_start(self, *, node_name: str, purpose: str) -> None:
        self._records.append({
            "type": "HumanMessage",
            "content": f"=== {node_name} ===\n{purpose}",
            "name": "",
            "additional_kwargs": {},
            "response_metadata": {"timestamp": _ts_cn()},
        })

    def write_trajectory(self, path: Path) -> None:
        _write_raw(path, self._records)

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self._records)
