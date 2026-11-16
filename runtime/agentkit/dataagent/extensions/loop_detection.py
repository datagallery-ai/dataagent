"""Loop guards for native agents, shared by all agent entry points.

Inspired by Gemini CLI's LoopDetectionService (Apache-2.0): bounded text
windows and short action cycles. Detection is local and never calls a model.
No replay, model wrapper or provider-specific runner is needed.

Action cycles are checked by ``LoopDetectionMiddleware``: a middleware hook has
an exact turn boundary, leaves model spans intact for tracing, and raises
outside the ``except Exception`` blocks that guard internal model calls. Text
repetition stays on a callback because only the streaming hook can stop a
degenerate decode before the whole response is consumed.
"""

import json
import re
from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.internal_call_transformer import INTERNAL_CALL_METADATA_KEY
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from dataagent.errors import DataAgentError

_WINDOW = 5000
_CHUNK = 50
_TEXT_REPEATS = 10
_TOOL_REPEATS = 5
_MAX_CYCLE = 5

# CommonMark: a fence opens at the start of a line after at most three spaces and
# closes with at least as many of the same character.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
# Only spaces or tabs may follow a closing fence.
_CLOSING_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
# Confirm block markers before exempting a line: '*' may start ordinary emphasis.
_STRUCTURE = re.compile(r" {0,3}(?:[|>]|#{1,6}[ \t]|[-*+][ \t]|[0-9]{1,9}[.)][ \t])")
_BREAK = re.compile(r" {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|=+[ \t]*)")

# Model calls that middleware makes on its own behalf (summarization, rubric
# grading) are not the agent's output and must not stop the agent.
_INTERNAL_METADATA = (
    INTERNAL_CALL_METADATA_KEY,
    "lc_source",
    "rubric_grader_configured_model",
)
_TEXT_BLOCKS = ("text", "text_delta")
_REASONING_BLOCKS = ("reasoning", "thinking", "reasoning_content")


class LoopDetectedError(DataAgentError, RuntimeError):
    """A model repeated content or actions without making progress."""

    error_code = "LOOP_DETECTED"

    def __init__(self, kind: str):
        self.kind = kind
        # Do not expose model text or tool arguments in errors/logs.
        super().__init__(
            source="constraint",
            component="loop_detection",
            fact=(
                f"Agent stopped because a loop was detected ({kind}). "
                "Review the completed steps and change the request before continuing."
            ),
        )


def _opening_fence(line):
    fence = _FENCE.match(line)
    if fence and fence.group(1)[0] == "`" and "`" in line[fence.end():]:
        return None  # Backticks in the info string make this inline text, not a fence.
    return fence


def _line_kind(line):
    """Return "skip" for structure or fences, "prose", or None while undecided."""
    body = line.lstrip(" ")
    if len(line) - len(body) > 3:
        return "prose"
    if not body:
        return None
    if _STRUCTURE.match(line):
        return "skip"
    if fence := _opening_fence(line):
        # A later backtick can invalidate a backtick fence. Wait for the line end.
        return "skip" if fence.group(1)[0] == "~" else None
    if len(body) < 3 and body == body[0] * len(body) and body[0] in "`~":
        return None  # A fence needs three markers.
    if body[0] in "-_*=" and all(char in (body[0], " ", "\t") for char in body):
        return None  # A thematic break or underline must occupy the whole line.
    if body == "+" or (len(body) <= 6 and not body.strip("#")):
        return None  # List markers and ATX headings need a space or line end.
    if re.fullmatch(r"[0-9]{1,9}[.)]?", body):
        return None  # Ordered lists need a delimiter followed by a space or line end.
    return "prose"


class TextLoopDetector:
    """Recognize dense repeated spans independently of transport chunk boundaries.

    Keep only the last 5,000 characters. Different suffixes after a common prefix
    do not form a loop. Fenced code and Markdown structure are exempt, including
    fences split over chunks.
    """

    def __init__(self):
        self._fence = ""
        self._fence_size = 0
        self._line = ""
        self._kind = None  # "prose", "skip", or None while the line start is open.
        self._after_cr = False
        self._reset_text()

    def _reset_text(self):
        self._buffer = ""
        self._dropped = 0  # Absolute index of self._buffer[0].
        self._start = 0  # Index in self._buffer where the live window begins.
        self._positions = defaultdict(deque)
        self._order = deque()

    def feed(self, text: str) -> bool:
        for char in text:
            if char == "\n" and self._after_cr:
                self._after_cr = False
                continue
            self._after_cr = char == "\r"
            if char in "\r\n":
                if self._end_line():
                    return True
                continue
            self._line += char
            if self._fence or self._kind == "skip":
                continue
            if self._kind == "prose":
                if self._append(char):
                    return True
                continue
            # Classify at the line start: a long table row can trip the
            # detector before its newline arrives.
            self._kind = _line_kind(self._line)
            if self._kind == "skip":
                # Structure and fences carry no evidence and break the window.
                self._reset_text()
            elif self._kind == "prose" and self._flush(self._line):
                return True
        return False

    def _flush(self, held) -> bool:
        """Check the characters held back while the line start was open."""
        return any(self._append(char) for char in held)

    def _end_line(self) -> bool:
        line, kind = self._line, self._kind
        self._line, self._kind = "", None
        if self._fence:
            closing = _CLOSING_FENCE.fullmatch(line)
            if closing:
                marker = closing.group(1)
                if marker[0] == self._fence and len(marker) >= self._fence_size:
                    self._fence, self._fence_size = "", 0
            return False
        fence = _opening_fence(line)
        if fence:
            self._fence, self._fence_size = fence.group(1)[0], len(fence.group(1))
            self._reset_text()
        elif kind is None:
            if _STRUCTURE.match(line + " ") or _BREAK.fullmatch(line):
                self._reset_text()
            else:
                # A short line such as "12" or "``" never resolved; it is prose.
                return self._flush(line)
        return False

    def _append(self, char) -> bool:
        self._buffer += char
        if len(self._buffer) - self._start > _WINDOW:
            self._start += 1
            if self._start >= _WINDOW:
                # Compact once per window so slicing costs O(1) per character.
                self._buffer = self._buffer[self._start:]
                self._dropped += self._start
                self._start = 0
        offset = self._dropped + self._start
        while self._order and self._order[0][0] < offset:
            position, key = self._order.popleft()
            positions = self._positions[key]
            if positions and positions[0] == position:
                positions.popleft()
            if not positions:
                del self._positions[key]
        if len(self._buffer) - self._start < _CHUNK:
            return False
        key = self._buffer[-_CHUNK:]
        position = self._dropped + len(self._buffer) - _CHUNK
        positions = self._positions[key]
        positions.append(position)
        self._order.append((position, key))
        if len(positions) > _TEXT_REPEATS:
            positions.popleft()
        if len(positions) < _TEXT_REPEATS or not any(c.isalnum() for c in key):
            return False
        if (positions[-1] - positions[0]) / (_TEXT_REPEATS - 1) > _CHUNK * 5:
            return False
        # Repeated JSON keys/list prefixes alone are insufficient evidence.
        periods = {
            self._buffer[start - self._dropped:end - self._dropped]
            for start, end in zip(positions, list(positions)[1:])
        }
        return len(periods) <= _TEXT_REPEATS // 2


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def check_action_loop(messages, ignored_tools=()):
    """Require repeated steps AND unchanged results; tolerate productive retries.

    ``messages`` is one turn's history; the caller owns the boundary. One
    assistant response is one step, so parallel calls do not inflate the count.
    Match results within their response, so reused call IDs and parallel results
    do not mix. Ignored tools break the cycle (e.g. deliberate polling).
    """
    records = deque(maxlen=_TOOL_REPEATS * _MAX_CYCLE)
    pending = []
    results = {}

    def flush():
        if not pending:
            return
        step = []
        for call in sorted(pending, key=lambda item: _canonical([item["name"], item["args"]])):
            result = results.get(call.get("id"))
            if call["name"] in ignored_tools or result is None:
                records.clear()
                return
            step.append([call["name"], call["args"], result.content, result.status])
        records.append(_canonical(step))

    for message in messages:
        if isinstance(message, AIMessage):
            flush()
            pending, results = message.tool_calls, {}
            if not pending:
                records.clear()
        elif isinstance(message, ToolMessage):
            results[message.tool_call_id] = message
    flush()
    history = list(records)
    for size in range(1, _MAX_CYCLE + 1):
        if len(history) >= size * _TOOL_REPEATS:
            if history[-size * _TOOL_REPEATS:] == history[-size:] * _TOOL_REPEATS:
                raise LoopDetectedError("tool_cycle")


def _channel(block):
    """Map one content block to a detection channel; never inspect tool JSON."""
    kind = block.get("type")
    if kind in _TEXT_BLOCKS:
        return "text", block.get("text") or ""
    if kind in _REASONING_BLOCKS:
        text = block.get("reasoning") or block.get("thinking") or ""
        if not text:
            # OpenAI Responses keeps reasoning in summary parts until a provider
            # translator flattens them.
            text = "".join(part.get("text") or "" for part in block.get("summary") or ())
        return "reasoning", text
    return None


def _text_parts(message):
    """Text and exposed reasoning use separate detectors.

    ``content_blocks`` merges ``additional_kwargs['reasoning_content']`` and
    normalizes providers it recognizes, which also collapses the duplicate
    reasoning representations some providers send. Shapes it cannot place arrive
    as ``non_standard``, so unwrap those rather than lose a channel.
    """
    for block in message.content_blocks:
        inner = block.get("value") if block.get("type") == "non_standard" else block
        found = _channel(inner) if isinstance(inner, dict) else None
        if found is not None:
            yield found


# Detections recorded for the current compiled runnable; see ``propagate_loops``.
_detections: ContextVar[list | None] = ContextVar("loop_detections", default=None)


def _stop(kind):
    error = LoopDetectedError(kind)
    detections = _detections.get()
    if detections is not None:
        detections.append(error)
    raise error


@contextmanager
def _recording():
    detections = []
    token = _detections.set(detections)
    try:
        yield detections
    except Exception as error:
        # A runnable may also re-raise the detection wrapped in its own error.
        if detections and detections[0] is not error:
            raise detections[0] from error
        raise
    finally:
        _detections.reset(token)


def _raise_recorded(detections, result):
    if detections:
        raise detections[0]
    return result


def propagate_loops(runnable, name):
    """Raise a detection even when a compiled runnable turns it into a reply.

    Compiled entries and subagents own their error handling: NL2SQL wraps node failures and
    returns them as text. Callbacks record each detection in this invocation's
    list; nodes run in copied contexts, so they share the list, not a rebinding.
    """

    def run(state, config):
        with _recording() as detections:
            # Keep one agent span under its public name; this is its internal workflow.
            result = runnable.invoke(state, {**config, "run_name": f"{name}.workflow"})
        return _raise_recorded(detections, result)

    async def arun(state, config):
        with _recording() as detections:
            result = await runnable.ainvoke(state, {**config, "run_name": f"{name}.workflow"})
        return _raise_recorded(detections, result)

    return RunnableLambda(run, arun, name=name)


class LoopDetectionCallback(BaseCallbackHandler):
    """Stop a degenerate decode before the whole response is consumed.

    Sync methods are deliberate: LangChain swallows async-handler exceptions on
    synchronous invocations. Inline dispatch also avoids a thread hop per token.
    Model run IDs isolate concurrent subagents and the shared model client; a
    run's detectors are only touched by that run's sequential tokens, so the
    lock guards the registry rather than the hot path.
    """

    raise_error = True
    run_inline = True

    def __init__(self):
        self._streams = {}
        self._lock = Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        # Registering the run here also records which runs to skip entirely.
        if any(key in (metadata or {}) for key in _INTERNAL_METADATA):
            return
        with self._lock:
            self._streams[run_id] = {}

    @staticmethod
    def _check(detectors, message, *, streamed=()):
        for channel, text in _text_parts(message):
            if not text or channel in streamed:
                continue
            if detectors.setdefault(channel, TextLoopDetector()).feed(text):
                _stop(f"repeated_{channel}")

    def on_llm_new_token(self, token, *, run_id, chunk=None, **kwargs):
        with self._lock:
            detectors = self._streams.get(run_id)
        if detectors is None:
            return
        try:
            if chunk is not None and hasattr(chunk, "message"):
                self._check(detectors, chunk.message)
            elif isinstance(token, str) and token:
                if detectors.setdefault("text", TextLoopDetector()).feed(token):
                    _stop("repeated_text")
        except LoopDetectedError:
            with self._lock:
                self._streams.pop(run_id, None)
            raise

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self._lock:
            detectors = self._streams.pop(run_id, None)
        if detectors is None:
            return
        # Non-streaming responses, and channels a provider only reveals in the
        # final message, are checked once here. Channels already streamed were
        # checked incrementally and must not be replayed; every block of the
        # other channels is checked.
        streamed = set(detectors)
        for generations in response.generations:
            for generation in generations:
                if hasattr(generation, "message"):
                    self._check(detectors, generation.message, streamed=streamed)

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self._lock:
            self._streams.pop(run_id, None)


class LoopDetectionState(AgentState):
    loop_turn_start: NotRequired[Annotated[int, PrivateStateAttr]]


_HINT_NAME = "loop_detection"
_TOOL_LOOP_HINT = (
    f"Your last {_TOOL_REPEATS} steps repeated the same tool calls and got the same "
    "results. Do not repeat them again. Change your approach, or answer with the "
    "information you already have. Another repetition will stop this run."
)


def _is_hint(message):
    return isinstance(message, HumanMessage) and message.name == _HINT_NAME


class LoopDetectionMiddleware(AgentMiddleware):
    """Check action cycles against this turn's own history.

    ``before_agent`` runs once per invocation, so messages that other middleware
    injects mid-turn (summarization, rubric revisions, media) cannot reset the
    count the way a trailing-user-message heuristic would.

    The first cycle in a turn adds one corrective hint and lets the model try
    again. Only steps after the hint count toward the next cycle, which stops
    the run, so a turn retries at most once.
    """

    state_schema = LoopDetectionState

    def __init__(self, ignored_tools=()):
        super().__init__()
        self.ignored_tools = tuple(ignored_tools)

    def before_agent(self, state, runtime):
        return {"loop_turn_start": len(state.get("messages", []))}

    def before_model(self, state, runtime):
        messages = state.get("messages", [])
        # A summarization rewrite can leave the recorded index overshooting the
        # rewritten history. A short slice under-reports; it never attributes
        # another turn's calls to this one.
        turn = messages[state.get("loop_turn_start", 0):]
        hints = [index for index, message in enumerate(turn) if _is_hint(message)]
        try:
            check_action_loop(turn[hints[-1] + 1:] if hints else turn, self.ignored_tools)
        except LoopDetectedError:
            if hints:
                raise
            return {"messages": [HumanMessage(content=_TOOL_LOOP_HINT, name=_HINT_NAME)]}
        return None


def loop_middleware(settings):
    """Build the action-cycle guard for one agent; text guards stay on callbacks."""
    if not settings.enabled:
        return []
    return [LoopDetectionMiddleware(settings.ignored_tools)]


def loop_callbacks(settings):
    """Build fresh callbacks for one graph without changing shared model objects."""
    if not settings.enabled:
        return []
    return [LoopDetectionCallback()]
