from deepagents_runtime.events import (
    AGENT_INTERRUPT_TYPE,
    INTERRUPT_EVENT_NAME,
    RUNTIME_BOUND_EVENT,
    build_interrupt_event,
    encode_sse,
    run_error,
    run_finished,
    run_started,
    runtime_bound,
    text_reply_events,
    tool_call_result,
)


def test_run_lifecycle_events_match_contract():
    started = run_started("thread-1", "run-1", timestamp=1)
    assert started == {
        "type": "RUN_STARTED",
        "threadId": "thread-1",
        "runId": "run-1",
        "timestamp": 1,
    }
    finished = run_finished("thread-1", "run-1", timestamp=2)
    assert finished["type"] == "RUN_FINISHED"
    assert "status" not in finished
    cancelled = run_finished("thread-1", "run-1", status="cancelled", timestamp=3)
    assert cancelled["status"] == "cancelled"
    error = run_error("boom", timestamp=4)
    assert error == {"type": "RUN_ERROR", "message": "boom", "timestamp": 4}


def test_runtime_bound_is_opaque_checkpoint_ref():
    event = runtime_bound("run-1", "ckpt:thread-1", timestamp=9)
    assert event["type"] == "CUSTOM"
    assert event["name"] == RUNTIME_BOUND_EVENT
    assert event["value"] == {
        "provider": "deepagents",
        "version": "v1",
        "checkpointRef": "ckpt:thread-1",
    }


def test_text_reply_emits_start_content_end():
    events = text_reply_events("msg_1", "你好", timestamp=5)
    assert [event["type"] for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]
    assert events[1]["delta"] == "你好"
    assert events[0]["role"] == "assistant"


def test_hitl_interrupt_maps_action_request_to_agent_interrupt():
    event = build_interrupt_event(
        run_id="run-ask",
        value={
            "action_requests": [
                {
                    "id": "call_ask_1",
                    "name": "ask_user",
                    "args": {"question": "需要我继续吗？", "options": ["继续", "停止"]},
                }
            ]
        },
        timestamp=11,
    )
    assert event["type"] == "CUSTOM"
    assert event["name"] == INTERRUPT_EVENT_NAME
    assert event["value"] == {
        "type": AGENT_INTERRUPT_TYPE,
        "toolCallId": "call_ask_1",
        "toolName": "ask_user",
        "runId": "run-ask",
        "args": {"question": "需要我继续吗？", "options": ["继续", "停止"]},
        "suspendPayload": {"question": "需要我继续吗？", "options": ["继续", "停止"]},
        "resumeSchema": {"type": "object"},
    }


def test_write_todos_interrupt_maps_to_submit_plan():
    event = build_interrupt_event(
        run_id="run-plan",
        value={
            "action_requests": [
                {"name": "write_todos", "args": {"todos": [{"content": "整理问题"}]}}
            ]
        },
        timestamp=12,
    )
    assert event["value"]["toolName"] == "submit_plan"
    assert event["value"]["toolCallId"].startswith("call_write_todos_")


def test_tool_call_result_is_a_tool_role_message():
    event = tool_call_result("call_1", "write_todos", "Updated todo list", timestamp=7)
    assert event["type"] == "TOOL_CALL_RESULT"
    assert event["toolCallId"] == "call_1"
    assert event["toolCallName"] == "write_todos"
    assert event["content"] == "Updated todo list"
    assert event["role"] == "tool"
    assert event["messageId"]


def test_sse_frame_is_data_json():
    frame = encode_sse({"type": "RUN_STARTED", "runId": "r1"})
    assert frame.startswith("data: {")
    assert frame.endswith("\n\n")
    assert '"runId": "r1"' in frame
