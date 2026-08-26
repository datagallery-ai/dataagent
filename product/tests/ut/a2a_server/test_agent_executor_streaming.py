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
import asyncio
from types import SimpleNamespace

import pytest
from a2a.types.a2a_pb2 import TaskState

from dataagent.a2a_server.agent_executor import DataAgentExecutor


class FakeAgent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def astream(self, **kwargs):
        for chunk in self._chunks:
            yield chunk


class RecordingAgent:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.kwargs = None

    async def astream(self, **kwargs):
        self.kwargs = kwargs
        for chunk in self._chunks:
            yield chunk


class RecordingDualAgent:
    def __init__(self, *, chunks=None, chat_result=None):
        self._chunks = list(chunks or [])
        self.chat_result = chat_result if chat_result is not None else {"final_answer": "chat ok"}
        self.astream_kwargs = None
        self.chat_kwargs = None
        self.chat_message = None

    async def chat(self, message, **kwargs):
        self.chat_message = message
        self.chat_kwargs = kwargs
        return self.chat_result

    async def astream(self, **kwargs):
        self.astream_kwargs = kwargs
        for chunk in self._chunks:
            yield chunk


class FakeEventQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class FakeRequestContext:
    def __init__(self, *, metadata):
        self.task_id = "t1"
        self.context_id = "c1"
        self.current_task = None
        self.message = None
        self.metadata = metadata

    def get_user_input(self):
        return "show tables"


@pytest.mark.asyncio
async def test_execute_agent_astream_accumulates_text_and_completes():
    executor = DataAgentExecutor(agent=FakeAgent(chunks=["hello ", "world"]))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="hi",
        session_id="s1",
        task_id="t1",
        context_id="c1",
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    artifact_events = [e for e in queue.events if e.__class__.__name__ == "TaskArtifactUpdateEvent"]

    assert len(status_events) >= 2
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED)
    assert status_events[-1].status.message.parts[0].text == "hello world"
    assert len(artifact_events) == 1
    assert artifact_events[0].__class__.__name__ == "TaskArtifactUpdateEvent"


@pytest.mark.asyncio
async def test_execute_agent_astream_uses_dataagent_initial_state_contract():
    """A2A should call DataAgent.astream through the SDK initial_state path."""
    agent = RecordingAgent(chunks=[("values", {"messages": [SimpleNamespace(content="ok")]})])
    executor = DataAgentExecutor(agent=agent)

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="show tables",
        session_id="s1",
        task_id="t1",
        context_id="c1",
        event_queue=queue,
        cancel_event=cancel_event,
    )

    kwargs = agent.kwargs
    assert isinstance(kwargs, dict)
    assert "input" not in kwargs
    assert kwargs.get("session_id") == "s1"
    assert kwargs.get("stream_mode") == ["updates", "custom", "values"]

    initial_state = kwargs.get("initial_state")
    assert isinstance(initial_state, dict)
    assert initial_state.get("session_id") == "s1"
    assert initial_state.get("run_id") == 0
    assert initial_state.get("user_query") == "show tables"


@pytest.mark.asyncio
async def test_execute_routes_streaming_request_to_astream():
    """Streaming A2A requests should use DataAgent.astream."""
    agent = RecordingDualAgent(chunks=[("values", {"messages": [SimpleNamespace(content="stream ok")]})])
    executor = DataAgentExecutor(agent=agent)
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"dataagent_streaming": True})

    await executor.execute(context, queue)

    assert agent.chat_kwargs is None
    assert isinstance(agent.astream_kwargs, dict)
    assert agent.astream_kwargs.get("stream_mode") == ["updates", "custom", "values"]

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED)
    assert status_events[-1].status.message.parts[0].text == "stream ok"


@pytest.mark.asyncio
async def test_execute_routes_non_streaming_request_to_chat():
    """Non-streaming A2A requests should use DataAgent.chat."""
    agent = RecordingDualAgent(chat_result={"final_answer": "chat ok"})
    executor = DataAgentExecutor(agent=agent)
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"dataagent_streaming": False})

    await executor.execute(context, queue)

    assert agent.astream_kwargs is None
    assert agent.chat_message == "show tables"
    assert isinstance(agent.chat_kwargs, dict)
    assert agent.chat_kwargs.get("session_id") == "c1"

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED)
    assert status_events[-1].status.message.parts[0].text == "chat ok"


@pytest.mark.asyncio
async def test_execute_agent_astream_handles_tuple_chunk_formats():
    """Test that custom deltas are accumulated but values/updates use assignment."""
    chunks = [
        ("values", {"messages": [SimpleNamespace(content="snap1")]}),
        ("updates", {"content": "snap2"}),
        ("custom", {"token": "delta"}),
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED)
    final_text = status_events[-1].status.message.parts[0].text
    # snap2 replaces snap1, then delta is appended
    assert final_text == "snap2delta", f"Expected 'snap2delta', got '{final_text}'"


@pytest.mark.asyncio
async def test_execute_agent_astream_values_snapshots_do_not_duplicate():
    """Test that consecutive values snapshots produce correct final text.

    This is the core bug fix: two values snapshots with "hello" then "hello world"
    should produce "hello world", not "hellohello world".
    """
    chunks = [
        ("values", {"messages": [SimpleNamespace(content="hello")]}),
        ("values", {"messages": [SimpleNamespace(content="hello world")]}),
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED)
    final_text = status_events[-1].status.message.parts[0].text
    # Final text must be "hello world", NOT "hellohello world"
    assert final_text == "hello world", f"Expected 'hello world', got '{final_text}'"


@pytest.mark.asyncio
async def test_execute_agent_astream_natural_language_error_words_do_not_fail():
    """Test that natural language containing 'error' or 'failed' is NOT treated as failure."""
    chunks = [
        ("values", {"messages": [SimpleNamespace(content="No errors found in the dataset.")]}),
        ("values", {"messages": [SimpleNamespace(content="The failed_count column is 0.")]}),
        ("values", {"messages": [SimpleNamespace(content="The error rate decreased this week.")]}),
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    # Should complete successfully, NOT fail
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED), (
        f"Expected COMPLETED but got {status_events[-1].status.state}"
    )


@pytest.mark.asyncio
async def test_execute_agent_astream_structured_error_marks_failed():
    """Test that structured error signals in state cause failure."""
    chunks = [
        ("values", {"error": "Something went wrong", "messages": []}),
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_FAILED)


@pytest.mark.asyncio
async def test_execute_agent_astream_structured_errors_list_marks_failed():
    """Test that state['errors'] (list) causes failure."""
    chunks = [
        ("values", {"errors": ["Validation error", "Timeout error"], "messages": []}),
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_FAILED)


@pytest.mark.asyncio
async def test_execute_agent_astream_status_error_marks_failed():
    """Test that state['status'] = 'error' causes failure."""
    chunks = [("values", {"status": "error", "messages": [SimpleNamespace(content="partial result")]})]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_FAILED)


@pytest.mark.asyncio
async def test_execute_agent_astream_exception_in_state_marks_failed():
    """Test that state['exception'] causes failure."""
    chunks = [("values", {"exception": "ValueError: invalid value", "messages": []})]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_FAILED)


@pytest.mark.asyncio
async def test_execute_agent_astream_message_metadata_error_marks_failed():
    """Test that last message metadata error causes failure."""
    chunks = [
        (
            "values",
            {"messages": [SimpleNamespace(content="Result", additional_kwargs={"error": True}, response_metadata={})]},
        )
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_FAILED)


@pytest.mark.asyncio
async def test_execute_agent_astream_mixed_deltas_and_snapshots():
    """Test mixing custom deltas with values snapshots."""
    chunks = [
        ("custom", {"token": "start "}),
        ("values", {"messages": [SimpleNamespace(content="snapshot")]}),
        ("custom", {"token": " end"}),
    ]
    executor = DataAgentExecutor(agent=FakeAgent(chunks=chunks))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id=None,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_COMPLETED)
    final_text = status_events[-1].status.message.parts[0].text
    # snapshot replaces previous text, then " end" is appended
    assert final_text == "snapshot end", f"Expected 'snapshot end', got '{final_text}'"


@pytest.mark.asyncio
async def test_execute_agent_astream_cancel_stops_streaming():
    """Test that cancellation stops streaming immediately."""
    executor = DataAgentExecutor(agent=FakeAgent(chunks=["should", "not", "finish"]))

    queue = FakeEventQueue()
    cancel_event = asyncio.Event()
    cancel_event.set()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id="c1",
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_CANCELED)
    assert status_events[-1].status.message.parts[0].text == "Task was canceled."


@pytest.mark.asyncio
async def test_execute_agent_astream_unexpected_exception_marks_failed():
    """Test that unexpected exceptions cause failure."""

    class BadAgent:
        async def astream(self, **kwargs):
            raise RuntimeError("stream broke")
            yield  # pragma: no cover

    executor = DataAgentExecutor(agent=BadAgent())
    queue = FakeEventQueue()
    cancel_event = asyncio.Event()

    await executor._execute_agent_astream(
        user_text="q",
        session_id="s1",
        task_id="t1",
        context_id="c1",
        event_queue=queue,
        cancel_event=cancel_event,
    )

    status_events = [e for e in queue.events if e.__class__.__name__ == "TaskStatusUpdateEvent"]
    assert int(status_events[-1].status.state) == int(TaskState.TASK_STATE_FAILED)
    assert "stream broke" in status_events[-1].status.message.parts[0].text


def test_has_error_final_state_with_error_field():
    """Test _has_error_final_state detects top-level error field."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    assert executor._has_error_final_state({"error": "Something went wrong"}) is True


def test_has_error_final_state_with_errors_list():
    """Test _has_error_final_state detects errors list."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    assert executor._has_error_final_state({"errors": ["error1", "error2"]}) is True


def test_has_error_final_state_with_exception():
    """Test _has_error_final_state detects exception field."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    assert executor._has_error_final_state({"exception": "ValueError"}) is True


def test_has_error_final_state_with_status_error():
    """Test _has_error_final_state detects status='error'."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    assert executor._has_error_final_state({"status": "error"}) is True


def test_has_error_final_state_with_status_failed():
    """Test _has_error_final_state detects status='failed'."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    assert executor._has_error_final_state({"status": "failed"}) is True


def test_has_error_final_state_with_message_metadata_error():
    """Test _has_error_final_state detects error in message metadata."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    state = {"messages": [{"content": "result", "additional_kwargs": {"error": True}}]}
    assert executor._has_error_final_state(state) is True


def test_has_error_final_state_with_response_metadata_error():
    """Test _has_error_final_state detects error in response_metadata."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    state = {"messages": [{"content": "result", "response_metadata": {"error": "some error"}}]}
    assert executor._has_error_final_state(state) is True


def test_has_error_final_state_no_error():
    """Test _has_error_final_state returns False for normal responses."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    state = {"messages": [{"content": "No errors found in the dataset."}]}
    assert executor._has_error_final_state(state) is False


def test_has_error_final_state_natural_language_failed():
    """Test that natural language containing 'failed' is NOT detected as error."""
    executor = DataAgentExecutor(agent=SimpleNamespace())
    state = {"messages": [{"content": "The failed_count column is 0."}]}
    assert executor._has_error_final_state(state) is False


def test_unpack_stream_chunk_formats():
    """Test _unpack_stream_chunk handles various formats."""
    executor = DataAgentExecutor(agent=SimpleNamespace())

    # (mode, data)
    mode, data = executor._unpack_stream_chunk(("custom", {"token": "t"}))
    assert mode == "custom"
    assert data == {"token": "t"}

    # (_, mode, data)
    mode, data = executor._unpack_stream_chunk((0, "values", {"x": 1}))
    assert mode == "values"
    assert data == {"x": 1}

    # dict with type
    mode, data = executor._unpack_stream_chunk({"type": "custom", "token": "t"})
    assert mode == "custom"
    assert data == {"type": "custom", "token": "t"}


def test_extract_custom_delta_text():
    """Test _extract_custom_delta_text extracts from custom events."""
    executor = DataAgentExecutor(agent=SimpleNamespace())

    assert executor._extract_custom_delta_text({"token": "abc"}) == "abc"
    assert executor._extract_custom_delta_text({"content": "def"}) == "def"
    assert executor._extract_custom_delta_text({"text": "ghi"}) == "ghi"
    assert executor._extract_custom_delta_text({"delta": "jkl"}) == "jkl"
    assert executor._extract_custom_delta_text({"other": "xxx"}) == ""


def test_extract_state_answer_text():
    """Test _extract_state_answer_text extracts from state snapshots."""
    executor = DataAgentExecutor(agent=SimpleNamespace())

    # Direct keys
    assert executor._extract_state_answer_text({"content": "hello"}) == "hello"
    assert executor._extract_state_answer_text({"answer": "world"}) == "world"

    # From messages
    state = {"messages": [SimpleNamespace(content="from message")]}
    assert executor._extract_state_answer_text(state) == "from message"

    # Dict messages
    state = {"messages": [{"content": "from dict message"}]}
    assert executor._extract_state_answer_text(state) == "from dict message"


def test_extract_chunk_text_handles_common_formats():
    executor = DataAgentExecutor(agent=SimpleNamespace())

    assert executor._extract_chunk_text("plain") == "plain"
    assert executor._extract_chunk_text({"content": "dict"}) == "dict"
    assert executor._extract_chunk_text(SimpleNamespace(content="obj")) == "obj"
    assert executor._extract_chunk_text(None) == ""
    assert executor._extract_chunk_text(("values", {"text": "tuple"})) == "tuple"
    assert executor._extract_chunk_text((0, "custom", {"token": "t"})) == "t"


def test_has_error_response_string_machine_formatted():
    """Test _has_error_response_string only matches machine-formatted prefixes."""
    executor = DataAgentExecutor(agent=SimpleNamespace())

    # Machine-formatted errors (should match)
    assert executor._has_error_response_string("Error: something went wrong") is True
    assert executor._has_error_response_string("Agent execution failed: timeout") is True

    # Natural language with error/failed words (should NOT match)
    assert executor._has_error_response_string("No errors found in the data") is False
    assert executor._has_error_response_string("The failed_count column is 0") is False
    assert executor._has_error_response_string("This analysis error rate is low") is False
    assert executor._has_error_response_string("The hypothesis failed to be rejected") is False

    # Empty and edge cases
    assert executor._has_error_response_string("") is False
    assert executor._has_error_response_string(None) is False
