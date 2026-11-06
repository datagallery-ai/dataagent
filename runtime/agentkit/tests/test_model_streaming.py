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
"""Verify explicit stream collection and caller-controlled native agent streaming."""

import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from test_launch import MODEL, config

from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.model_stream import collect_model_stream
from dataagent.models import build_model


@pytest.mark.parametrize("value", [True, False, None])
def test_streaming_config_is_not_exposed(tmp_path, value):
    path = config(tmp_path / "invalid.json", {"models": {"primary": {
        **MODEL["models"]["primary"], "streaming": value,
    }}})
    with pytest.raises(ValueError, match="streaming"):
        prepare_runtime(LaunchOptions(config=path))


def response(body, *, tool=False):
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    if body["stream"]:
        deltas = (
            [{"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
               "function": {"name": "lookup", "arguments": '{"id":'}}]},
             {"tool_calls": [{"index": 0, "function": {"arguments": "42}"}}]}]
            if tool else [{"role": "assistant", "content": "complete "}, {"content": "answer"}]
        )
        chunks = [{"id": "offline", "object": "chat.completion.chunk", "created": 1,
                   "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                  for delta in deltas]
        chunks.append({"id": "offline", "object": "chat.completion.chunk", "created": 1,
                       "model": "test-model", "choices": [{"index": 0, "delta": {},
                       "finish_reason": "tool_calls" if tool else "stop"}]})
        chunks.append({"id": "offline", "object": "chat.completion.chunk", "created": 1,
                       "model": "test-model", "choices": [], "usage": usage})
        content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    return httpx.Response(200, json={
        "id": "offline", "object": "chat.completion", "created": 1, "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "complete answer"},
                     "finish_reason": "stop"}], "usage": usage,
    })


@pytest.mark.parametrize("mode", ["planner_events", "planner_invoke", "collect", "plain"])
async def test_native_call_modes_use_correct_http_protocol(runtime, mode):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return response(body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = build_model(runtime)
        # Keep the real configured model; replace only the provider's HTTP transport.
        await model.root_async_client.close()
        model.root_async_client._client = client
        try:
            assert "streaming" not in model.model_fields_set
            messages = [HumanMessage(content="test")]
            if mode.startswith("planner_"):
                async with create_agent(runtime, model=model) as agent:
                    if mode == "planner_events":
                        output = None
                        async for event in agent.astream_events({"messages": messages}, version="v2"):
                            if event["event"] == "on_chain_end" and not event.get("parent_ids"):
                                output = event["data"]["output"]
                        result = output["messages"][-1]
                    else:
                        result = (await agent.ainvoke({"messages": messages}))["messages"][-1]
            elif mode == "collect":
                result = await collect_model_stream(model, messages)
            else:
                result = await model.ainvoke(messages)
            assert requests[0]["stream"] is (mode in {"planner_events", "collect"})
            assert result.content == "complete answer"
            assert result.usage_metadata["total_tokens"] == 3
            if mode.startswith("planner_"):
                # Streaming callbacks must not mutate the shared model.
                await model.ainvoke(messages)
                assert requests[-1]["stream"] is False
        finally:
            await model.root_async_client.close()
            model.root_client.close()


async def test_stream_collection_preserves_tool_calls_and_usage(runtime):
    async def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        return response(body, tool=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = build_model(runtime)
        await model.root_async_client.close()
        model.root_async_client._client = client
        try:
            result = await collect_model_stream(model, [HumanMessage(content="lookup")])
            assert result.tool_calls == [{"name": "lookup", "args": {"id": 42}, "id": "call_1", "type": "tool_call"}]
            assert result.usage_metadata["total_tokens"] == 3
        finally:
            await model.root_async_client.close()
            model.root_client.close()


@pytest.mark.parametrize("partial", [False, True])
async def test_failed_or_empty_stream_does_not_replay_model(partial):
    calls = []

    async def stream(messages):
        calls.append(messages)
        if partial:
            yield AIMessageChunk(content="partial")
            raise RuntimeError("stream interrupted")

    model = SimpleNamespace(astream=stream)
    with pytest.raises(RuntimeError, match="interrupted" if partial else "without a response"):
        await collect_model_stream(model, [HumanMessage(content="test")])
    assert len(calls) == 1
