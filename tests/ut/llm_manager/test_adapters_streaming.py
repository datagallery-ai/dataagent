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
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

# pylint: disable=protected-access
from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter


@dataclass
class FakeChunk:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    invalid_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage_metadata: dict[str, Any] = field(default_factory=dict)

    def __add__(self, other: "FakeChunk") -> "FakeChunk":
        return FakeChunk(
            content=self.content + other.content,
            tool_calls=[*self.tool_calls, *other.tool_calls],
            invalid_tool_calls=[*self.invalid_tool_calls, *other.invalid_tool_calls],
            usage_metadata=other.usage_metadata or self.usage_metadata,
        )


class FakeStreamingModel:
    def __init__(self, chunks: list[FakeChunk]):
        self._chunks = chunks

    async def astream(self, _messages: Any, **_kwargs: Any):
        for chunk in self._chunks:
            yield chunk


class FakeInvokeModel:
    def __init__(self, response: FakeChunk):
        self._response = response

    async def ainvoke(self, _messages: Any, **_kwargs: Any) -> FakeChunk:
        return self._response


def _collect_stream(adapter: LangChainChatModelAdapter) -> list[Any]:
    async def _run() -> list[Any]:
        return [chunk async for chunk in adapter.astream("hello")]

    return asyncio.run(_run())


def test_astream_aggregates_stream_chunks_into_final_response():
    adapter = LangChainChatModelAdapter(
        FakeStreamingModel(
            [
                FakeChunk(content="Hel"),
                FakeChunk(
                    content="lo",
                    tool_calls=[{"id": "call-1", "name": "demo_tool", "args": {"x": 1}}],
                    usage_metadata={"total_tokens": 7},
                ),
            ]
        ),
        config=SimpleNamespace(tool_call_mode="native"),
    )

    chunks = _collect_stream(adapter)

    assert [chunk.content for chunk in chunks[:-1]] == ["Hel", "lo"]
    assert chunks[-1].done is True
    assert chunks[-1].final_response is not None
    assert chunks[-1].final_response.content == "Hello"
    assert chunks[-1].final_response.tool_calls == [{"id": "call-1", "name": "demo_tool", "args": {"x": 1}}]
    assert chunks[-1].final_response.usage_metadata["input_tokens"] == 0
    assert chunks[-1].final_response.usage_metadata["output_tokens"] == 0
    assert chunks[-1].final_response.usage_metadata["total_tokens"] == 7


def test_astream_keeps_usage_when_trailing_tool_chunk_has_no_usage():
    adapter = LangChainChatModelAdapter(
        FakeStreamingModel(
            [
                FakeChunk(content="ok", usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}),
                FakeChunk(
                    content="",
                    tool_calls=[{"id": "call-1", "name": "demo_tool", "args": {"x": 1}}],
                ),
            ]
        ),
        config=SimpleNamespace(tool_call_mode="native"),
    )

    chunks = _collect_stream(adapter)

    assert chunks[-1].done is True
    assert chunks[-1].final_response is not None
    assert chunks[-1].final_response.usage_metadata["input_tokens"] == 10
    assert chunks[-1].final_response.usage_metadata["output_tokens"] == 3
    assert chunks[-1].final_response.usage_metadata["total_tokens"] == 13


def test_llm_perf_name_includes_logical_name_and_model():
    raw = SimpleNamespace(model="qwen3-coder")
    adapter = LangChainChatModelAdapter(raw, config=SimpleNamespace(name="planner", tool_call_mode="native"))

    assert adapter._llm_perf_name == "planner:qwen3-coder"


def test_astream_falls_back_to_ainvoke_when_streaming_is_unavailable():
    adapter = LangChainChatModelAdapter(
        FakeInvokeModel(FakeChunk(content="fallback response", usage_metadata={"total_tokens": 3})),
        config=SimpleNamespace(tool_call_mode="native"),
    )

    chunks = _collect_stream(adapter)

    assert len(chunks) == 1
    assert chunks[0].done is True
    assert chunks[0].final_response is not None
    assert chunks[0].final_response.content == "fallback response"
    assert chunks[0].final_response.usage_metadata["input_tokens"] == 0
    assert chunks[0].final_response.usage_metadata["output_tokens"] == 0
    assert chunks[0].final_response.usage_metadata["total_tokens"] == 3
