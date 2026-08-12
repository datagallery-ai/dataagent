# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""OtelEventRecorder — lightweight OTel event recorder for DataAgent.

Produces a ``trajectory.json`` file that is fully compatible with
:class:`evo_eval.tracing.instrumentors.dataagent.DataAgentInstrumentor`'s
``replay_events_from_file()`` method.

Design principles:
- **No opentelemetry SDK dependency** — only produces JSON files.
- **Event format** matches what ``DataAgentInstrumentor._replay_event()`` consumes:
  ``{"type": "llm_start"|"llm_end"|"tool_start"|"tool_end", ...}``
- **Controlled via ``initial_state["__otel_config"]``** — when absent or
  ``enabled=False``, the recorder is a no-op.
- **Thread-safe** — uses ``threading.Lock`` for concurrent tool calls.
- **Flush on demand** — events are buffered in memory and written to disk
  when ``flush()`` is called (typically at the end of ``FlexAgent.chat()``).
- **Sub-agent isolation** — each sub-agent writes its own
  ``trajectory_{sub_id}.json``; the main agent merges all sub-agent files
  into ``trajectory.json`` when it flushes, eliminating concurrent-write
  conflicts.

Output file structure (``work_dir/.otel/trajectory.json``)::

    [
      {
        "role": "main",
        "session_id": "20260807_143000_xxx",
        "otel_config": {
          "parent_trace_id": "0123...",
          "parent_span_id": "4567...",
          "provider_name": "dataagent"
        },
        "events": [
          {"type": "llm_start", "model": "qwen-plus", "timestamp": ...},
          {"type": "tool_start", "tool_name": "bash", "tool_call_id": "call_xxx", ...},
          {"type": "tool_end", "tool_call_id": "call_xxx", "result": "...", ...},
          {"type": "llm_end", "usage": {...}, "finish_reason": "stop", ...}
        ]
      }
    ]
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

# Glob pattern for sub-agent trajectory files: trajectory_1.json, trajectory_2.json, …
_SUB_TRAJECTORY_GLOB = "trajectory_*.json"


class OtelEventRecorder:
    """Lightweight OTel event recorder that produces trajectory.json for evo-eval.

    Usage::

        # From initial_state (created by DataAgentAdapter)
        recorder = OtelEventRecorder.from_state(initial_state)
        if recorder:
            recorder.record_llm_start(model="qwen-plus")
            # ... LLM call ...
            recorder.record_llm_end(usage={"input_tokens": 100, "output_tokens": 50})
            recorder.flush()  # writes to work_dir/.otel/trajectory.json
    """

    def __init__(
        self,
        *,
        output_dir: str | Path,
        session_id: str = "",
        otel_config: dict[str, Any] | None = None,
        role: str = "main",
        sub_id: int = 0,
    ) -> None:
        """Initialize the event recorder.

        Args:
            output_dir: Directory to write trajectory files (typically ``work_dir/.otel``).
            session_id: Session identifier for this agent run.
            otel_config: The ``__otel_config`` dict from initial_state, containing
                ``parent_trace_id``, ``parent_span_id``, ``provider_name``.
            role: Agent role (``"main"`` or ``"sub-agent"``).
            sub_id: Numeric sub-agent id.  When ``> 0`` the recorder writes to
                ``trajectory_{sub_id}.json``; the main agent (``sub_id == 0``)
                merges all sub-agent files into ``trajectory.json`` on flush.
        """
        self._output_dir = Path(output_dir)
        self._session_id = session_id
        self._otel_config = otel_config or {}
        self._role = role
        self._sub_id = int(sub_id)
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # ── Utilities ────────────────────────────────────────────────────────────

    @property
    def event_count(self) -> int:
        """Number of events recorded so far."""
        with self._lock:
            return len(self._events)

    @property
    def output_dir(self) -> Path:
        """The output directory for trajectory files."""
        return self._output_dir

    @property
    def otel_config(self) -> dict[str, Any]:
        """The ``__otel_config`` dict, suitable for propagating to sub-agents."""
        return self._otel_config

    # ── I/O helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _read_groups(path: Path) -> list[dict[str, Any]]:
        """Read event groups from a JSON file, returning an empty list on failure."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("OtelEventRecorder: failed to read %s, skipping: %s", path, e)
        return []

    @staticmethod
    def _atomic_write_json(output_path: Path, data: list[dict[str, Any]]) -> None:
        """Write *data* as JSON to *output_path* via temp file + atomic replace."""
        tmp_path = output_path.with_suffix(f".tmp.{os.getpid()}_{threading.get_ident()}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, output_path)
        except BaseException:
            # Clean up the temp file on any failure (including KeyboardInterrupt)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> OtelEventRecorder | None:
        """Create an OtelEventRecorder from ``initial_state["__otel_config"]``.

        Returns ``None`` when OTel tracing is not enabled (no ``__otel_config``
        or ``enabled=False``), making it safe to call unconditionally.

        Args:
            state: The agent's initial_state dict, which should contain
                ``__otel_config`` injected by ``DataAgentAdapter``.

        Returns:
            An ``OtelEventRecorder`` instance, or ``None`` if tracing is disabled.
        """
        otel_config = state.get("__otel_config")
        if not isinstance(otel_config, dict) or not otel_config.get("enabled"):
            return None

        output_dir = otel_config.get("output_dir", "")
        if not output_dir:
            logger.debug("OtelEventRecorder: __otel_config.enabled but no output_dir, skipping")
            return None

        session_id = str(state.get("session_id", ""))

        # Infer role and sub_id from state
        sub_id = state.get("sub_id", 0)
        if isinstance(sub_id, int) and sub_id > 0:
            role = "sub-agent"
        else:
            role = "main"
            sub_id = 0

        return cls(
            output_dir=output_dir,
            session_id=session_id,
            otel_config=otel_config,
            role=role,
            sub_id=sub_id,
        )

    # ── LLM Events ──────────────────────────────────────────────────────────

    def record_llm_start(self, model: str) -> None:
        """Record an LLM call start event.

        Args:
            model: The model name (e.g. ``"qwen-plus"``, ``"deepseek-chat"``).
        """
        event: dict[str, Any] = {
            "type": "llm_start",
            "model": model,
            "timestamp": time.time(),
        }
        with self._lock:
            self._events.append(event)

    def record_llm_end(
        self,
        usage: dict[str, Any] | None = None,
        finish_reason: str = "",
        content: str = "",
        reasoning_content: str = "",
    ) -> None:
        """Record an LLM call end event.

        Args:
            usage: Token usage dict with ``input_tokens`` and ``output_tokens``.
            finish_reason: The stop reason (e.g. ``"stop"``, ``"tool_calls"``).
            content: The model's text output (assistant message content).
            reasoning_content: The model's reasoning/thinking content, if any.
        """
        # Normalize usage to the format DataAgentInstrumentor expects
        usage_dict: dict[str, Any] | None = None
        if isinstance(usage, dict):
            usage_dict = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
        event: dict[str, Any] = {
            "type": "llm_end",
            "timestamp": time.time(),
        }
        if usage_dict is not None:
            event["usage"] = usage_dict
        if finish_reason:
            event["finish_reason"] = finish_reason
        if content:
            event["content"] = content
        if reasoning_content:
            event["reasoning_content"] = reasoning_content
        with self._lock:
            self._events.append(event)

    # ── Tool Events ──────────────────────────────────────────────────────────

    def record_tool_start(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: str = "",
    ) -> None:
        """Record a tool call start event.

        Args:
            tool_name: Name of the tool being called.
            tool_call_id: Unique ID for this tool call.
            arguments: Tool call arguments as a string (typically JSON).
        """
        event: dict[str, Any] = {
            "type": "tool_start",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "timestamp": time.time(),
        }
        if arguments:
            event["arguments"] = arguments
        with self._lock:
            self._events.append(event)

    def record_tool_end(
        self,
        tool_call_id: str,
        result: str = "",
        is_error: bool = False,
    ) -> None:
        """Record a tool call end event.

        Args:
            tool_call_id: The ID of the tool call this result is for.
            result: Tool execution result as a string.
            is_error: Whether the tool execution resulted in an error.
        """
        event: dict[str, Any] = {
            "type": "tool_end",
            "tool_call_id": tool_call_id,
            "timestamp": time.time(),
        }
        if result:
            event["result"] = result
        if is_error:
            event["is_error"] = True
        with self._lock:
            self._events.append(event)

    # ── Flush ────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Write buffered events to disk.

        - **Sub-agents** (``sub_id > 0``) write to ``trajectory_{sub_id}.json``.
          No merge is needed — the main agent will collect the file later.
        - **Main agent** (``sub_id == 0``) writes to ``trajectory.json`` and
          then merges all ``trajectory_*.json`` files from the same directory
          into the output, removing the sub-agent files afterwards.

        Both paths use atomic replace (write to temp file + ``os.replace``)
        to avoid producing a corrupted JSON file.
        """
        if not self._events:
            logger.debug("OtelEventRecorder: no events to flush")
            return

        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Build the event group for this recorder
        group: dict[str, Any] = {
            "role": self._role,
            "session_id": self._session_id,
            "otel_config": self._otel_config,
            "events": list(self._events),
        }

        if self._sub_id > 0:
            self._flush_sub_agent(group)
        else:
            self._flush_main_agent(group)

        logger.debug(
            "OtelEventRecorder: flushed %d events (sub_id=%s)",
            len(self._events),
            self._sub_id,
        )

    def _flush_sub_agent(self, group: dict[str, Any]) -> None:
        """Write a sub-agent's event group to its own file.

        Args:
            group: The event group dict to write.
        """
        output_path = self._output_dir / f"trajectory_{self._sub_id}.json"
        self._atomic_write_json(output_path, [group])

    def _flush_main_agent(self, group: dict[str, Any]) -> None:
        """Write the main agent's event group and merge sub-agent files.

        Reads all ``trajectory_*.json`` files, appends their groups to the
        main agent's groups, writes the combined result to ``trajectory.json``,
        and then deletes the sub-agent files.

        Args:
            group: The main agent's event group dict.
        """
        output_path = self._output_dir / "trajectory.json"

        # Collect existing groups from trajectory.json (if present)
        existing_groups: list[dict[str, Any]] = []
        if output_path.is_file():
            existing_groups = self._read_groups(output_path)

        # Collect groups from sub-agent trajectory files
        sub_groups: list[dict[str, Any]] = []
        sub_files: list[Path] = []
        for sub_path in sorted(self._output_dir.glob(_SUB_TRAJECTORY_GLOB)):
            sub_files.append(sub_path)
            sub_groups.extend(self._read_groups(sub_path))

        existing_groups.append(group)
        existing_groups.extend(sub_groups)

        self._atomic_write_json(output_path, existing_groups)

        # Clean up sub-agent files after successful merge
        for sub_path in sub_files:
            with contextlib.suppress(OSError):
                sub_path.unlink()
