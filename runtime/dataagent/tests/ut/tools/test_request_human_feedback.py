from __future__ import annotations

from typing import Any

from dataagent.actions.tools import request_human_feedback as feedback_module


def test_request_human_feedback_emits_frontend_compatible_interrupt(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def capture_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return payload

    monkeypatch.setattr(feedback_module, "interrupt", capture_interrupt)
    result = feedback_module.request_human_feedback.func(
        reason="Need a decision",
        pending_action="Choose a table",
        tool_call_id="call-1",
    )

    assert result.get("type") == "agent_interrupt"
    assert result.get("toolCallId") == "call-1"
    assert result.get("toolName") == "ask_user"
    assert result.get("suspendPayload", {}).get("question") == "Choose a table"
    assert result.get("args", {}).get("question") == "Choose a table"
