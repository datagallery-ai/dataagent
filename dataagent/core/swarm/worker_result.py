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
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.messages import AIMessage


@dataclass
class WorkerResult:
    """Structured subagent result returned from child process to parent agent.

    The parent process stores this in worker metadata and surfaces it as the
    ``original_msg`` payload on ``sub_agent_tool`` for the planner ToolMessage.

    ``iteration_count`` reflects Planner completion steps (Flex ``curr_iter`` when
    present), not swarm ``run_id``.
    """

    sub_id: int
    parent_session_id: str
    worker_session_id: str
    status: str
    final_answer: str
    artifacts: list[str]
    tool_calls_count: int
    iteration_count: int
    error: str | None
    resumed: bool
    perf_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert the dataclass to a JSON-serializable dictionary."""
        return asdict(self)


def worker_session_id(parent_session_id: str, sub_id: int) -> str:
    """Build the runtime session id used by one worker subprocess."""
    return f"subagent_{parent_session_id}_{int(sub_id)}"


def worker_result_from_payload(payload: dict[str, Any]) -> WorkerResult:
    """Parse a child-process ``worker_result`` payload into ``WorkerResult``."""
    raw_perf_summary = payload.get("perf_summary")
    return WorkerResult(
        sub_id=int(payload.get("sub_id", 0) or 0),
        parent_session_id=str(payload.get("parent_session_id") or ""),
        worker_session_id=str(payload.get("worker_session_id") or ""),
        status=str(payload.get("status") or "failed"),
        final_answer=str(payload.get("final_answer") or ""),
        artifacts=[str(item) for item in payload.get("artifacts") or []],
        tool_calls_count=int(payload.get("tool_calls_count", 0) or 0),
        iteration_count=int(payload.get("iteration_count", 0) or 0),
        error=None if payload.get("error") is None else str(payload.get("error")),
        resumed=bool(payload.get("resumed", False)),
        perf_summary=raw_perf_summary if isinstance(raw_perf_summary, dict) else None,
    )


def synthesize_worker_result(
    *,
    final_state: Any,
    sub_id: int,
    parent_session_id: str,
    status: str = "success",
    error: str | None = None,
    resumed: bool = False,
    perf_summary: dict[str, Any] | None = None,
) -> WorkerResult:
    """Synthesize ``WorkerResult`` from a subagent ``final_state``.

    This is used inside the child process before stdout emission. It extracts a
    concise final answer, artifacts, and simple counters while keeping missing
    fields harmless for existing agent states.

    When the workflow state does not set explicit ``final_answer`` / ``answer``
    fields, the last non-empty assistant message body in ``messages`` is used
    instead of stringifying the entire state (which would bloat planner metadata).

    The optional state key ``summary`` is treated only as a fallback text source
    for ``final_answer`` when no higher-priority answer fields are present.

    ``iteration_count`` is planner completion steps: Flex ``curr_iter`` when that
    key is present, otherwise legacy ``iteration_count`` / ``iterations``. Swarm
    ``run_id`` is never used (it is the worker run ordinal, not planner depth).

    ``perf_summary`` carries the child process's slim LLM usage summary
    (``schema_version=1``) for parent-side idempotent aggregation; ``None``
    when performance collection is disabled or unavailable.
    """
    state = final_state if isinstance(final_state, dict) else {}
    explicit_answer = _pick_text(state, "final_answer", "answer", "output", "result", "summary")
    from_messages = _last_assistant_visible_text(state)
    final_answer = explicit_answer or from_messages
    artifacts = _extract_artifacts(state)
    return WorkerResult(
        sub_id=int(sub_id),
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id(parent_session_id, int(sub_id)),
        status=status,
        final_answer=final_answer,
        artifacts=artifacts,
        tool_calls_count=_count_tool_calls(state),
        iteration_count=_count_iterations(state),
        error=error,
        resumed=resumed,
        perf_summary=perf_summary,
    )


def build_busy_result(*, sub_id: int, parent_session_id: str) -> WorkerResult:
    """Return the protocol-level result for a locked worker.

    Busy is a business-level refusal for this call, not a tool-system error; the
    parent therefore returns it as normal tool data and does not update metadata.
    """
    msg = f"subagent {sub_id} is already running; create a new subagent instead of reusing it"
    return WorkerResult(
        sub_id=int(sub_id),
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id(parent_session_id, int(sub_id)),
        status="failed",
        final_answer="",
        artifacts=[],
        tool_calls_count=0,
        iteration_count=0,
        error=msg,
        resumed=False,
    )


def build_timeout_result(*, sub_id: int, parent_session_id: str, timeout: int) -> WorkerResult:
    """Return the result written when the parent kills a timed-out worker."""
    msg = f"subagent {sub_id} timed out after {timeout} seconds"
    return WorkerResult(
        sub_id=int(sub_id),
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id(parent_session_id, int(sub_id)),
        status="timeout",
        final_answer="",
        artifacts=[],
        tool_calls_count=0,
        iteration_count=0,
        error=msg,
        resumed=False,
    )


def _pick_text(state: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty text value from ``state`` for the given keys."""
    for key in keys:
        value = state.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _normalize_message_content(content: Any) -> str:
    """Convert LangChain ``BaseMessage.content`` (str or block list) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _last_assistant_visible_text(state: dict[str, Any]) -> str:
    """Return the latest assistant message with non-empty visible text in ``messages``.

    Tool-only rounds (empty ``content`` but tool calls) are skipped so the value
    reflects the subagent's last user-facing reply when possible.
    """
    messages = state.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = _normalize_message_content(msg.content).strip()
            if text:
                return text
            continue
        if isinstance(msg, dict) and msg.get("type") == "AIMessage":
            text = _normalize_message_content(msg.get("content", "")).strip()
            if text:
                return text
    return ""


def _extract_artifacts(state: dict[str, Any]) -> list[str]:
    """Extract artifact paths from common final-state fields."""
    raw = state.get("artifacts") or state.get("files") or []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _count_tool_calls(state: dict[str, Any]) -> int:
    """Count tool calls from explicit counters or message objects."""
    raw = state.get("tool_calls_count")
    if isinstance(raw, int):
        return raw
    messages = state.get("messages")
    if isinstance(messages, list):
        count = 0
        for msg in messages:
            calls = getattr(msg, "tool_calls", None)
            if calls:
                count += len(calls)
        return count
    return 0


def _count_iterations(state: dict[str, Any]) -> int:
    """Return planner completion steps from subagent final graph state.

    Prefer Flex/React ``curr_iter`` (incremented once per Planner node completion)
    when that key exists on ``state``. Otherwise fall back to explicit
    ``iteration_count`` or ``iterations`` for non-Flex backends.

    Never uses ``run_id``: that field records swarm worker invocation ordinal,
    not how many Planner rounds ran inside one subprocess.

    Args:
        state: Final workflow state dict from the subagent process.

    Returns:
        Non-negative integer planner-step count, or 0 if unset or invalid.
    """
    if not isinstance(state, dict):
        return 0
    if "curr_iter" in state:
        try:
            return max(0, int(state["curr_iter"]))
        except (TypeError, ValueError):
            pass
    for key in ("iteration_count", "iterations"):
        raw = state.get(key)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0
