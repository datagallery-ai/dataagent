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
"""NL2SQL nodes start a collected model stream over when it is cut off after answer text."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter, LLMResponse, LLMStreamChunk
from dataagent.core.managers.llm_manager.llm_client import (
    LLMCallError,
    LLMClient,
    LLMErrorCategory,
    LLMRepetitionError,
)

SQL = "SELECT name, COUNT(*) AS n FROM employees GROUP BY name"
PROMPTS = [{"role": "user", "content": "How many employees share each name?"}]


def _text(content: str) -> LLMStreamChunk:
    return LLMStreamChunk(content=content)


def _final(content: str) -> LLMStreamChunk:
    return LLMStreamChunk(final_response=LLMResponse(content=content, usage_metadata={}), done=True)


def _cut(category: LLMErrorCategory = LLMErrorCategory.CONNECTION) -> LLMCallError:
    return LLMCallError(category, "peer closed connection without sending complete message body")


class _ScriptedModel:
    """Each ``astream`` call plays the next scripted attempt: its chunks, then the error it ends in."""

    def __init__(self, *attempts: tuple[list[LLMStreamChunk], BaseException | None]) -> None:
        self._attempts = attempts
        self.calls: list[object] = []

    async def astream(self, prompts):
        self.calls.append(prompts)
        chunks, error = self._attempts[min(len(self.calls), len(self._attempts)) - 1]
        for chunk in chunks:
            yield chunk
        if error is not None:
            raise error


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted default LLM and return it."""

    def install(*attempts: tuple[list[LLMStreamChunk], BaseException | None]) -> _ScriptedModel:
        model = _ScriptedModel(*attempts)
        monkeypatch.setattr(llm_manager, "get_default_llm", lambda: model)
        return model

    return install


async def _collect() -> str:
    return await BaseNL2SQLNode(name="generator")._collect_llm_stream(PROMPTS)


@pytest.mark.asyncio
async def test_completed_stream_is_collected_from_one_call(scripted) -> None:
    model = scripted(([_text("SELECT"), _text(" 1"), _final("SELECT 1")], None))

    assert await _collect() == "SELECT 1"
    assert model.calls == [PROMPTS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category", [LLMErrorCategory.CONNECTION, LLMErrorCategory.TIMEOUT, LLMErrorCategory.STREAM_INTERRUPTED]
)
async def test_answer_text_cut_off_is_discarded_and_the_call_restarted(scripted, category) -> None:
    """Only the attempt that finished is returned; its text is not appended to the lost prefix."""
    model = scripted(
        ([_text("SELECT name")], _cut(category)),
        ([_text(SQL[:20]), _text(SQL[20:])], None),
    )

    assert await _collect() == SQL
    assert model.calls == [PROMPTS, PROMPTS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before_cut",
    [[], [LLMStreamChunk(reasoning_content="Check the table first.")], [_text(" \n")]],
    ids=["no output", "reasoning only", "blank text"],
)
async def test_failure_before_answer_text_is_not_restarted(scripted, before_cut) -> None:
    """Without answer text the failure stays with the client's own retry policy."""
    error = _cut()
    model = scripted((before_cut, error), ([_text(SQL)], None))

    with pytest.raises(LLMCallError) as raised:
        await _collect()
    assert raised.value is error
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LLMCallError(LLMErrorCategory.BAD_REQUEST, "bad request"),
        LLMCallError(LLMErrorCategory.AUTH, "invalid key"),
        LLMCallError(LLMErrorCategory.SERVER_ERROR, "internal error"),
        LLMRepetitionError("ngram", "repeat detected"),
        ValueError("not a model failure"),
        TimeoutError("caller deadline"),
    ],
    ids=lambda error: str(getattr(error, "category", type(error).__name__)),
)
async def test_other_failures_after_answer_text_propagate_unchanged(scripted, error) -> None:
    model = scripted(([_text("SELECT name")], error), ([_text(SQL)], None))

    with pytest.raises(type(error)) as raised:
        await _collect()
    assert raised.value is error
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_restarts_are_bounded_and_the_last_failure_is_raised(scripted) -> None:
    errors = [_cut() for _ in range(5)]
    model = scripted(*(([_text("SELECT name")], error) for error in errors))

    with pytest.raises(LLMCallError) as raised:
        await _collect()
    assert len(model.calls) == 3
    assert raised.value is errors[2]


@pytest.mark.asyncio
async def test_cancellation_is_not_restarted(monkeypatch) -> None:
    calls: list[object] = []
    closed: list[bool] = []

    class _HangingModel:
        async def astream(self, prompts):
            calls.append(prompts)
            try:
                yield _text("SELECT name")
                await asyncio.Event().wait()
            finally:
                closed.append(True)

    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: _HangingModel())
    task = asyncio.create_task(_collect())
    while not calls:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
    assert closed == [True]


def _sse(content: str, finish_reason: str | None = None) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


_SSE_HEADERS = {"content-type": "text/event-stream"}
_PARTIAL = _sse("```sql\nSELECT name")
_COMPLETE = _sse(f"```sql\n{SQL}\n```", "stop") + b"data: [DONE]\n\n"


class _ResetStream(httpx.AsyncByteStream):
    """A response body whose connection is lost after ``prefix``."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    async def __aiter__(self):
        yield self._prefix
        raise httpx.ReadError("peer closed connection without sending complete message body")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_response",
    [
        lambda request: httpx.Response(200, headers=_SSE_HEADERS, content=_PARTIAL, request=request),
        lambda request: httpx.Response(200, headers=_SSE_HEADERS, stream=_ResetStream(_PARTIAL), request=request),
    ],
    ids=["ends without finish_reason", "connection reset"],
)
async def test_generator_restarts_a_statement_cut_off_mid_stream(monkeypatch, first_response) -> None:
    """Node, adapter and client together: the client declines to retry after output, the node starts over."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return first_response(request)
        return httpx.Response(200, headers=_SSE_HEADERS, content=_COMPLETE, request=request)

    async_client_class = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return async_client_class(transport=httpx.MockTransport(handler), **kwargs)

    llm = LangChainChatModelAdapter(LLMClient(model="m", api_base="http://t", api_key="k", num_retries=2))
    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: llm)
    with patch("dataagent.core.managers.llm_manager.llm_client.httpx.AsyncClient", side_effect=client_factory):
        candidates = await GeneratorNode(num_samples=1).generate_with_llm(
            "prompt",
            {"dialect": "sqlite", "num_samples": 1, "sql_rules": ""},
            {"question": PROMPTS[0]["content"], "schema": "", "evidence": "", "few_shot_examples": ""},
        )

    assert [sql for sql, _, _ in candidates] == [SQL]
    # The restart repeats the original request instead of asking the model to continue the lost text.
    assert len(requests) == 2
    assert requests[1]["messages"] == requests[0]["messages"]
