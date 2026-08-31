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

Produces trajectory JSON files that are compatible with
:class:`evo_eval.tracing.instrumentors.dataagent.DataAgentInstrumentor`'s
``replay_events_from_file()`` method.

Design principles:
- **No opentelemetry SDK dependency** — only produces JSON files.
- **Event format** matches what ``DataAgentInstrumentor._replay_event()`` consumes:
  ``{"type": "llm_start"|"llm_end"|"tool_start"|"tool_end", ...}``
- **Controlled via ``initial_state["__otel_config"]``** — when absent or
  ``enabled=False``, the recorder is a no-op.
- **Thread-safe** — uses ``threading.Lock`` for concurrent tool calls.
- **Incremental flush per round** — events are buffered in memory and appended
  to disk after each agent round completes (via ``record_round_end()``).
  This ensures that if the process is killed (e.g. sub-agent timeout), events
  from already-completed rounds survive on disk.  The final ``flush()`` only
  writes any remaining un-flushed events.
- **No merge between main and sub-agents** — each agent (main or sub) writes
  its own trajectory file independently.  The main agent writes
  ``trajectory.json``; each sub-agent writes ``trajectory_{sub_id}_{run_id}.json``.
  Files are never merged or deleted.  Span hierarchy is maintained via
  ``span_id`` / ``parent_span_id`` / ``trace_id`` for consumers to correlate.

Output file structure (``work_dir/.otel/``)::

    trajectory.json              ← main agent (one group per round)
    trajectory_{sub_id}_{run_id}.json  ← sub-agents (one group per round)

Each file is a JSON array of event groups, where each group represents one
agent round::

    [
      {
        "role": "main",
        "session_id": "20260807_143000_xxx",
        "run_id": 0,
        "span_id": "a1b2c3d4e5f6a7b8",
        "parent_span_id": "4567...",
        "trace_id": "0123...",
        "round_index": 0,
        "parent_tool_call_id": "call_abc123",  // only for sub-agents
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
      },
      {
        "role": "main",
        ...
        "round_index": 1,
        "events": [...]
      }
    ]
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from itertools import islice
from pathlib import Path
from typing import Any

from loguru import logger

# Glob pattern for sub-agent trajectory files: trajectory_1_0.json, trajectory_1_1.json, …
_SUB_TRAJECTORY_GLOB = "trajectory_*.json"


def _generate_span_id() -> str:
    """Generate a 16-char hex span ID (OTel spec: 8 bytes, 16 hex chars)."""
    return secrets.token_hex(8)


def _generate_trace_id() -> str:
    """Generate a 32-char hex trace ID (OTel spec: 16 bytes, 32 hex chars)."""
    return secrets.token_hex(16)


class OtelEventRecorder:
    """Lightweight OTel event recorder that produces trajectory JSON files.

    Usage::

        # From initial_state (created by DataAgentAdapter)
        recorder = OtelEventRecorder.from_state(initial_state)
        if recorder:
            recorder.record_llm_start(model="qwen-plus")
            # ... LLM call ...
            recorder.record_llm_end(usage={"input_tokens": 100, "output_tokens": 50})
            recorder.record_round_end()  # persist this round's events to disk
            # ... more rounds ...
            recorder.flush()  # write any remaining un-flushed events
    """

    def __init__(
        self,
        *,
        output_dir: str | Path,
        session_id: str = "",
        otel_config: dict[str, Any] | None = None,
        role: str = "main",
        sub_id: int = 0,
        run_id: int = 0,
    ) -> None:
        """Initialize the event recorder.

        Args:
            output_dir: Directory to write trajectory files (typically ``work_dir/.otel``).
            session_id: Session identifier for this agent run.
            otel_config: The ``__otel_config`` dict from initial_state, containing
                ``parent_trace_id``, ``parent_span_id``, ``provider_name``.
            role: Agent role (``"main"`` or ``"sub-agent"``).
            sub_id: Numeric sub-agent id.  When ``> 0`` the recorder writes to
                ``trajectory_{sub_id}_{run_id}.json``; the main agent (``sub_id == 0``)
                writes to ``trajectory.json``.
            run_id: Invocation counter for this sub-agent.  ``run_id > 0`` implies
                the sub-agent resumed from a previous workspace state.
        """
        self._output_dir = Path(output_dir)
        self._session_id = session_id
        self._otel_config = otel_config or {}
        self._role = role
        self._sub_id = int(sub_id)
        self._run_id = int(run_id)
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

        # Track how many events have already been flushed to disk.
        # Events[:_flushed_count] are already on disk; events[_flushed_count:] are new.
        self._flushed_count: int = 0

        # Monotonic round counter — each call to record_round_end() increments this.
        self._round_index: int = 0

        # Derive span hierarchy from otel_config.
        # For the main agent, ``parent_span_id`` comes from the user-supplied config
        # (or stays empty).  For sub-agents, the parent's ``otel_config`` property
        # already includes its ``span_id``, so it becomes the sub-agent's
        # ``parent_span_id`` automatically.
        raw_config = otel_config or {}
        self._trace_id = raw_config.get("trace_id", "") or raw_config.get("parent_trace_id", "") or _generate_trace_id()
        self._parent_span_id = raw_config.get("span_id", "") or raw_config.get("parent_span_id", "")
        self._span_id = _generate_span_id()

        # Per-round span_id: generated here for Round 0 so that executor can
        # read ``round_otel_config`` before any record_round_end() call.
        # After each round is persisted, a new span_id is generated for the next round.
        self._current_round_span_id: str = _generate_span_id()

        # The tool_call_id of the parent agent's submit_subagent / sub_agent_tool
        # call that launched this sub-agent.  Empty for main agents.  Enables
        # consumers to distinguish which specific tool call in the parent round
        # spawned each sub-agent when multiple sub-agents are launched in the
        # same round.
        self._parent_tool_call_id: str = raw_config.get("parent_tool_call_id", "")

    # ── Utilities ────────────────────────────────────────────────────────────

    @property
    def event_count(self) -> int:
        """Number of events recorded so far (including already flushed)."""
        with self._lock:
            return len(self._events)

    @property
    def unflushed_event_count(self) -> int:
        """Number of events recorded but not yet flushed to disk."""
        with self._lock:
            return len(self._events) - self._flushed_count

    @property
    def output_dir(self) -> Path:
        """The output directory for trajectory files."""
        return self._output_dir

    @property
    def parent_tool_call_id(self) -> str:
        """The tool_call_id of the parent's submit tool call that launched this sub-agent.

        Empty for main agents.  For sub-agents, this is the ``call_xxx`` ID from
        the parent agent's ``submit_subagent`` or ``sub_agent_tool`` invocation,
        enabling consumers to trace which specific tool call spawned this sub-agent
        when multiple sub-agents are launched in the same round.
        """
        return self._parent_tool_call_id

    @property
    def otel_config(self) -> dict[str, Any]:
        """The ``__otel_config`` dict, suitable for propagating to sub-agents.

        The returned dict includes this recorder's own ``span_id`` so that
        sub-agents can use it as their ``parent_span_id``, establishing a
        proper span hierarchy (e.g. B5 → C1).  It also includes
        ``output_dir`` so that sub-agents write their trajectory files to
        the same directory as the main agent, making it easy to find all
        trajectory files in one place.
        """
        config = dict(self._otel_config)
        config["trace_id"] = self._trace_id
        config["span_id"] = self._span_id
        config["parent_span_id"] = self._parent_span_id
        config["output_dir"] = str(self._output_dir)
        if self._parent_tool_call_id:
            config["parent_tool_call_id"] = self._parent_tool_call_id
        return config

    @property
    def round_otel_config(self) -> dict[str, Any]:
        """OTel config for the **current round**, suitable for propagating to sub-agents.

        Unlike :attr:`otel_config` which carries the agent-run-level ``span_id``,
        this property carries the current round's ``span_id``
        (``_current_round_span_id``).  When a sub-agent is launched from this
        round, its ``parent_span_id`` will point to the specific round that
        invoked it — not the entire agent run — making it possible to trace
        which tool-call round triggered a sub-agent.

        The span_id is generated at init (for Round 0) and after each
        :meth:`record_round_end` call (for subsequent rounds), so it is
        always available when the executor reads it via the ContextVar.

        Like :attr:`otel_config`, this also includes ``output_dir`` so that
        sub-agents write to the same directory as the main agent.
        """
        config = dict(self._otel_config)
        config["trace_id"] = self._trace_id
        config["span_id"] = self._current_round_span_id
        config["parent_span_id"] = self._effective_parent_span_id()
        config["output_dir"] = str(self._output_dir)
        if self._parent_tool_call_id:
            config["parent_tool_call_id"] = self._parent_tool_call_id
        return config

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
            # Fallback: derive from state["workspace"] when not explicitly configured
            workspace = state.get("workspace")
            if isinstance(workspace, (str, Path)) and str(workspace).strip():
                output_dir = str(Path(workspace) / ".otel")
                logger.debug(
                    "OtelEventRecorder: output_dir not set, defaulting to %s",
                    output_dir,
                )
            else:
                logger.debug("OtelEventRecorder: enabled but no output_dir and no workspace, skipping")
                return None

        session_id = str(state.get("session_id", ""))

        # Infer role and sub_id from state
        sub_id = state.get("sub_id", 0)
        if isinstance(sub_id, int) and sub_id > 0:
            role = "sub-agent"
        else:
            role = "main"
            sub_id = 0

        run_id = state.get("run_id", 0)
        if not isinstance(run_id, int):
            run_id = 0

        return cls(
            output_dir=output_dir,
            session_id=session_id,
            otel_config=otel_config,
            role=role,
            sub_id=sub_id,
            run_id=run_id,
        )

    # ── LLM Events ──────────────────────────────────────────────────────────

    def record_llm_start(self, model: str, messages: Any = None) -> None:
        """Record an LLM call start event.

        Args:
            model: The model name (e.g. ``"qwen-plus"``, ``"deepseek-chat"``).
            messages: The messages list sent to the LLM.  When provided, it is
                stored as a list of dicts (OpenAI message format).  The caller
                is responsible for serialising LangChain Message objects before
                passing them in; the recorder stores the value verbatim.
        """
        event: dict[str, Any] = {
            "type": "llm_start",
            "model": model,
            "timestamp": time.time(),
        }
        if messages is not None:
            event["input"] = messages
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

    # ── Incremental round flush ─────────────────────────────────────────────

    def record_round_end(self) -> None:
        """Persist events from the current round to disk and start a new round.

        Called after each agent round completes (i.e. after all tool results
        from the current LLM call have been recorded).  The events accumulated
        since the last ``record_round_end()`` (or since recorder creation) are
        written as a new group appended to the trajectory file.

        This ensures that if the process is killed (e.g. sub-agent timeout),
        events from already-completed rounds survive on disk.  Only the
        currently-executing round (whose events are still in memory) would be
        lost.

        Uses atomic write (read existing → append → temp file + ``os.replace``)
        so the trajectory file is never left in a corrupted state.
        """
        with self._lock:
            new_events = self._unflushed_events()
            if not new_events:
                logger.debug("OtelEventRecorder: no new events to persist for round %d", self._round_index)
                return

            group = self._build_group(new_events)

            output_path = self._resolve_output_path()
            self._append_groups_to_file(output_path, [group])

            self._flushed_count = len(self._events)
            self._round_index += 1
            # Generate a fresh span_id for the next round so that
            # ``round_otel_config`` returns a new value when executor reads it.
            self._current_round_span_id = _generate_span_id()

            logger.debug(
                "OtelEventRecorder: persisted round %d (%d events, span_id=%s) to %s",
                group["round_index"],
                len(new_events),
                group["span_id"][:8],
                output_path.name,
            )

    # ── Flush ────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Write any remaining un-flushed events to disk.

        If ``record_round_end()`` has been called after every round, this
        method is typically a no-op.  It exists as a safety net to ensure
        all events are persisted, especially when the agent exits before
        a round completes (e.g. exception during workflow).

        Unlike the old implementation, this method does **not** merge
        sub-agent trajectory files into the main agent's file.  Each agent
        writes its own file independently.
        """
        with self._lock:
            remaining = self._unflushed_events()
            if not remaining:
                logger.debug("OtelEventRecorder: no remaining events to flush")
                return

            group = self._build_group(remaining)

            output_path = self._resolve_output_path()
            self._append_groups_to_file(output_path, [group])

            self._flushed_count = len(self._events)
            self._round_index += 1
            # Generate a fresh span_id for the next round (if any).
            self._current_round_span_id = _generate_span_id()

            logger.debug(
                "OtelEventRecorder: flushed remaining %d events (round_index=%d, span_id=%s, sub_id=%s)",
                len(remaining),
                group["round_index"],
                group["span_id"][:8],
                self._sub_id,
            )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _unflushed_events(self) -> list[dict[str, Any]]:
        """Return a copy of events that have not yet been persisted to disk."""
        return list(islice(self._events, self._flushed_count, None))

    def _effective_parent_span_id(self) -> str:
        """Return the ``parent_span_id`` that groups should use.

        - **Main agent with external parent** (``_parent_span_id`` non-empty):
          returns ``_parent_span_id`` — the externally-provided parent span
          (e.g. injected via YAML ``OTEL_CONFIG.parent_span_id``).

        - **Main agent without external parent** (``_parent_span_id`` empty):
          returns ``""`` — this is a top-level agent; rounds have no parent
          span in an external system.  Using the agent's own randomly-generated
          ``_span_id`` would create a phantom span that no consumer can resolve.

        - **Sub-agent**: returns ``_parent_span_id`` — the parent agent's
          round span that launched this sub-agent.  This makes the hierarchy
          directly traceable: consumers can find the parent round in the main
          agent's trajectory file.
        """
        if self._parent_span_id:
            return self._parent_span_id
        return ""

    def _build_group(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a single group dict for persistence.

        Uses :meth:`_effective_parent_span_id` to compute the correct
        ``parent_span_id`` depending on whether this is a main or sub-agent.
        """
        parent_span_id = self._effective_parent_span_id()
        group: dict[str, Any] = {
            "role": self._role,
            "session_id": self._session_id,
            "run_id": self._run_id,
            "span_id": self._current_round_span_id,
            "parent_span_id": parent_span_id,
            "trace_id": self._trace_id,
            "round_index": self._round_index,
            "otel_config": {
                **self._otel_config,
                "trace_id": self._trace_id,
                "span_id": self._current_round_span_id,
                "parent_span_id": parent_span_id,
                "output_dir": str(self._output_dir),
            },
            "events": list(events),
        }
        if self._parent_tool_call_id:
            group["parent_tool_call_id"] = self._parent_tool_call_id
            group["otel_config"]["parent_tool_call_id"] = self._parent_tool_call_id
        return group

    def _resolve_output_path(self) -> Path:
        """Return the trajectory output path for this recorder.

        - Main agent (``sub_id == 0``): ``trajectory.json``
        - Sub-agent (``sub_id > 0``): ``trajectory_{sub_id}_{run_id}.json``
        """
        if self._sub_id > 0:
            return self._output_dir / f"trajectory_{self._sub_id}_{self._run_id}.json"
        return self._output_dir / "trajectory.json"

    def _append_groups_to_file(self, output_path: Path, new_groups: list[dict[str, Any]]) -> None:
        """Append new event groups to an existing trajectory file.

        Reads the existing groups (if any), appends the new ones, and writes
        back atomically.  On the first write the file is created from scratch.
        """
        existing = self._read_groups(output_path) if output_path.is_file() else []
        existing.extend(new_groups)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(output_path, existing)
