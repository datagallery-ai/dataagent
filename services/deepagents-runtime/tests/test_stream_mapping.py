from deepagents_runtime.stream import StreamState, map_stream_event


class _Chunk:
    def __init__(self, content="", tool_call_chunks=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []


class _Message:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls or []


def test_map_chat_model_stream_emits_text_events():
    state = StreamState(thread_id="t", run_id="r")
    events = map_stream_event(
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("你好")}},
        state,
        timestamp=1,
    )
    assert [event["type"] for event in events] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT"]
    assert events[1]["delta"] == "你好"


def test_map_chat_model_end_emits_full_text_when_not_streamed():
    state = StreamState(thread_id="t", run_id="r")
    events = map_stream_event(
        {"event": "on_chat_model_end", "data": {"output": _MessageWithContent("完整回复")}},
        state,
        timestamp=4,
    )
    assert [event["type"] for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]
    assert events[1]["delta"] == "完整回复"


class _MessageWithContent:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _replay(raw_events: list[tuple[dict, int]]):
    from deepagents_runtime.stream import unfinished_tool_ids

    state = StreamState(thread_id="t", run_id="r")
    mapped = []
    for event, timestamp in raw_events:
        mapped.extend(map_stream_event(event, state, timestamp=timestamp))
    return state, mapped, unfinished_tool_ids(state)


def test_map_tool_call_chunks_and_end():
    state, mapped, unfinished = _replay([
        (
            {
                "event": "on_chat_model_stream",
                "data": {
                    "chunk": _Chunk(
                        tool_call_chunks=[{"id": "call_1", "name": "write_todos", "args": '{"todos":[]}'}]
                    )
                },
            },
            2,
        ),
        (
            {"event": "on_tool_end", "name": "write_todos", "run_id": "lg-1", "data": {"output": {"ok": True}}},
            3,
        ),
    ])
    starts = [event for event in mapped if event["type"] == "TOOL_CALL_START"]
    assert len(starts) == 1
    assert starts[0]["toolCallId"] == "call_1"
    ends = [event for event in mapped if event["type"] == "TOOL_CALL_END"]
    assert ends[0]["toolCallId"] == "call_1"
    assert unfinished == []


def test_langgraph_tool_start_does_not_open_a_second_call():
    """One model tool_call.id is one AG-UI toolCallId. on_tool_start is execution, not a new call."""
    _state, mapped, unfinished = _replay([
        (
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": _MessageWithContent(
                        "",
                        tool_calls=[{"id": "call_todo_1", "name": "write_todos", "args": {"todos": []}}],
                    )
                },
            },
            1,
        ),
        (
            {
                "event": "on_tool_start",
                "name": "write_todos",
                "run_id": "01a05d8c-db63-7bf1-8552-ac6d4447e00a",
                "data": {"input": {"todos": [{"content": "show tool UI"}]}},
            },
            2,
        ),
        (
            {
                "event": "on_tool_end",
                "name": "write_todos",
                "run_id": "01a05d8c-db63-7bf1-8552-ac6d4447e00a",
                "data": {"output": "Updated todo list"},
            },
            3,
        ),
    ])
    starts = [event for event in mapped if event["type"] == "TOOL_CALL_START"]
    assert [event["toolCallId"] for event in starts] == ["call_todo_1"]
    assert [event["toolCallId"] for event in mapped if event["type"] == "TOOL_CALL_END"] == ["call_todo_1"]
    results = [event for event in mapped if event["type"] == "TOOL_CALL_RESULT"]
    assert [event["toolCallId"] for event in results] == ["call_todo_1"]
    assert results[0]["role"] == "tool"
    assert results[0]["messageId"]
    assert unfinished == []


def test_langgraph_tool_start_does_not_replay_model_args():
    _state, mapped, _unfinished = _replay([
        (
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": _MessageWithContent(
                        "",
                        tool_calls=[{"id": "call_todo_1", "name": "write_todos", "args": {"todos": []}}],
                    )
                },
            },
            1,
        ),
        (
            {
                "event": "on_tool_start",
                "name": "write_todos",
                "run_id": "01a05d8c-db63-7bf1-8552-ac6d4447e00a",
                "data": {"input": {"todos": [{"content": "show tool UI"}]}},
            },
            2,
        ),
    ])
    args_events = [event for event in mapped if event["type"] == "TOOL_CALL_ARGS"]
    assert [event["toolCallId"] for event in args_events] == ["call_todo_1"]
    assert args_events[0]["delta"] == '{"todos": []}'


def test_two_same_name_tools_keep_model_ids_in_order():
    _state, mapped, unfinished = _replay([
        (
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": _MessageWithContent(
                        "",
                        tool_calls=[
                            {"id": "call_a", "name": "write_todos", "args": {"todos": [{"content": "a"}]}},
                            {"id": "call_b", "name": "write_todos", "args": {"todos": [{"content": "b"}]}},
                        ],
                    )
                },
            },
            1,
        ),
        ({"event": "on_tool_start", "name": "write_todos", "run_id": "lg-a", "data": {"input": {}}}, 2),
        ({"event": "on_tool_end", "name": "write_todos", "run_id": "lg-a", "data": {"output": "a"}}, 3),
        ({"event": "on_tool_start", "name": "write_todos", "run_id": "lg-b", "data": {"input": {}}}, 4),
        ({"event": "on_tool_end", "name": "write_todos", "run_id": "lg-b", "data": {"output": "b"}}, 5),
    ])
    assert [event["toolCallId"] for event in mapped if event["type"] == "TOOL_CALL_START"] == ["call_a", "call_b"]
    results = [event for event in mapped if event["type"] == "TOOL_CALL_RESULT"]
    assert [event["toolCallId"] for event in results] == ["call_a", "call_b"]
    assert [event["content"] for event in results] == ["a", "b"]
    assert unfinished == []


def test_unfinished_tool_ids_stay_open_when_end_is_missing():
    _state, mapped, unfinished = _replay([
        (
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": _MessageWithContent(
                        "",
                        tool_calls=[{"id": "call_open", "name": "ask_user", "args": {"question": "?"}}],
                    )
                },
            },
            1,
        ),
        ({"event": "on_tool_start", "name": "ask_user", "run_id": "lg-ask", "data": {"input": {}}}, 2),
    ])
    assert [event["toolCallId"] for event in mapped if event["type"] == "TOOL_CALL_START"] == ["call_open"]
    assert unfinished == ["call_open"]
