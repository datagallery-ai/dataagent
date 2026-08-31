from __future__ import annotations

from dataagent.core.errors import DataAgentError
from dataagent.core.managers.action_manager.base import ToolResult


def test_tool_result_failure_uses_structured_error() -> None:
    error = DataAgentError(source="tool", component="tool")
    result = ToolResult(error=error)

    assert result.success is False
    assert result.error is error


def test_unknown_exception_does_not_use_message_classification() -> None:
    from dataagent.core.managers.action_manager.base import ErrorType, classify_exception

    error_type, _policy = classify_exception(Exception("timeout 429"))
    assert error_type == ErrorType.UNKNOWN
    error = DataAgentError.from_exception(Exception("timeout 429 schema"))
    assert error.source == "internal"
    assert "Exception" in error.fact
