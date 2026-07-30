import pytest

from dataagent.agents.nl2sql.errors import LLMOutputParseError
from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode


class _JSONNode(BaseNL2SQLNode):
    def __init__(self, responses: list[str | Exception]) -> None:
        super().__init__(name="json_test")
        self._responses = iter(responses)
        self.calls = 0

    def execute_with_llm(self, context: dict[str, str], action: str = "") -> str:
        self.calls += 1
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


class _RetryingPerceptorNode(PerceptorNode):
    def __init__(self, responses: list[str]) -> None:
        super().__init__()
        self._responses = iter(responses)
        self.calls = 0

    def execute_with_llm(self, context: dict[str, str], action: str = "") -> str:
        self.calls += 1
        return next(self._responses)


def test_execute_with_llm_json_retries_parse_errors_until_success() -> None:
    node = _JSONNode(["not json", "```json\n{\n```", '```json\n{"ok": true}\n```'])

    assert node.execute_with_llm_json({}) == {"ok": True}
    assert node.calls == 3


def test_execute_with_llm_json_reports_third_parse_error() -> None:
    node = _JSONNode(["not json", "still not json", "```json\n{\n```"])

    with pytest.raises(LLMOutputParseError, match="JSON parsing failed after 3 attempts") as exc_info:
        node.execute_with_llm_json({})

    assert node.calls == 3
    assert "Expecting property name" in str(exc_info.value)
    assert "Expecting property name" in exc_info.value.detail


def test_execute_with_llm_json_returns_after_first_success() -> None:
    node = _JSONNode(["```json\n[1]\n```"])

    assert node.execute_with_llm_json({}) == [1]
    assert node.calls == 1


def test_execute_with_llm_json_does_not_retry_non_parse_errors() -> None:
    node = _JSONNode([RuntimeError("LLM unavailable")])

    with pytest.raises(RuntimeError, match="LLM unavailable"):
        node.execute_with_llm_json({})

    assert node.calls == 1


def test_keyword_extraction_retries_malformed_json_output() -> None:
    node = _RetryingPerceptorNode(["not json", "```json\n{\n```", '```json\n{"keywords": ["k"]}\n```'])

    assert node._keyword_extraction("question") == ["k"]
    assert node.calls == 3
