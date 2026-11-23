"""Exercise real callback/middleware propagation, stopping, isolation and false positives."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult
from langgraph.checkpoint.memory import InMemorySaver
from test_api import _entry_runtime, client_for, query, sse, terminal
from test_launch import MODEL, config
from test_traces import attributes, read_trace, spans

from dataagent import LoopDetectedError
from dataagent.assembly.context import build_model
from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.bootstrap.config_files import load_layers, merge
from dataagent.config.settings import LoopDetectionSettings
from dataagent.extensions.loop_detection import (
    LoopDetectionCallback,
    LoopDetectionMiddleware,
    TextLoopDetector,
    check_action_loop,
)
from dataagent.lowlevel import build_graph

REPEAT = "我需要重新分析同一个问题，但目前没有任何新信息。We need to analyze this again. "


def configured(runtime, **options):
    settings = runtime.settings.model_copy(
        update={
            "dataagent": runtime.settings.dataagent.model_copy(
                update={
                    "loop_detection": LoopDetectionSettings(**options),
                }
            ),
        }
    )
    return replace(runtime, settings=settings)


def history(*, size=5, cycle=1, changing=False):
    messages = [HumanMessage(content="Investigate the data")]
    for index in range(size * cycle):
        action = call("lookup", {"page": index % cycle}, id=f"call-{index}")
        messages.extend(
            [
                action,
                ToolMessage(
                    content=f"result-{index if changing else index % cycle}",
                    tool_call_id=action.tool_calls[0]["id"],
                ),
            ]
        )
    return messages


def generations(message):
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def started(guard, **kwargs):
    run_id = uuid4()
    guard.on_chat_model_start({}, [[HumanMessage(content="test")]], run_id=run_id, **kwargs)
    return run_id


@pytest.mark.parametrize("chunk_size", [1, 2, 17, 50, 4096])
def test_repetition_detection_is_independent_of_transport_chunks(chunk_size):
    text = REPEAT * 30
    detector = TextLoopDetector()
    assert any(
        detector.feed(text[index : index + chunk_size]) for index in range(0, len(text), chunk_size)
    )


@pytest.mark.parametrize("fence", ["```", "~~~"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_split_code_fences_are_exempt_but_prose_after_them_is_checked(fence, newline):
    detector = TextLoopDetector()
    for char in fence + "sql" + newline + REPEAT * 30 + newline + fence:
        assert not detector.feed(char)
    assert detector.feed(newline + REPEAT * 30)


@pytest.mark.parametrize(
    "line,opens",
    [
        ("```", True),
        ("   ```sql", True),  # Up to three leading spaces still opens a fence.
        ("~~~~", True),
        ("     ```", False),  # Four or more spaces is indented code, not a fence.
        ("value = ```not a fence```", False),  # A fence must start its own line.
        ("```literal``` is inline code", False),
        ("```lang `invalid info`", False),
        ("~~~lang `valid info`", True),  # Tilde fences may have backticks in their info.
    ],
)
def test_fence_openers_follow_commonmark_indentation(line, opens):
    detector = TextLoopDetector()
    detector.feed(line + "\n")
    assert bool(detector._fence) is opens


@pytest.mark.parametrize(
    "closing,closes",
    [
        ("````", True),
        ("`````", True),  # A longer closing fence of the same character still closes.
        ("   ````", True),
        ("```", False),  # CommonMark: the closing fence cannot be shorter.
        ("~~~~", False),  # Nor may it switch characters.
    ],
)
def test_closing_fences_must_match_character_and_length(closing, closes):
    detector = TextLoopDetector()
    detector.feed("````\n")
    assert (detector._fence, detector._fence_size) == ("`", 4)
    detector.feed(f"{REPEAT * 30}\n{closing}\n")
    assert bool(detector._fence) is not closes
    # Prose is only checked again once the fence is genuinely closed.
    assert bool(detector.feed(REPEAT * 30)) is closes


def test_info_string_inside_a_fence_does_not_close_it():
    """CommonMark: only spaces or tabs may follow a closing fence."""
    detector = TextLoopDetector()
    detector.feed("```\n```csv\n")
    assert detector._fence == "`"
    assert not detector.feed(REPEAT * 30 + "\n")
    detector.feed("``` \t\n")
    assert not detector._fence


def test_structure_is_exempt_before_its_line_ends():
    """A long table row must not trip the detector before its newline arrives."""
    detector = TextLoopDetector()
    row = "| 0 " * 24 + "|"
    assert not detector.feed(row * 20)
    assert not detector.feed("\n" + "1. " + REPEAT * 30)


@pytest.mark.parametrize("prefix", ["12", "``", "   "])
def test_undecided_line_starts_are_still_checked(prefix):
    detector = TextLoopDetector()
    assert any(detector.feed(char) for char in prefix + REPEAT * 30)


def test_inline_backticks_do_not_open_a_fence():
    detector = TextLoopDetector()
    assert detector.feed(f"value = ```{REPEAT * 30}```")


@pytest.mark.parametrize("prefix", ["| ", "- ", "* ", "+ ", "# ", "> ", "1. ", "9)\t"])
def test_markdown_structure_repeats_by_design_and_is_exempt(prefix):
    detector = TextLoopDetector()
    assert not any(detector.feed(f"{prefix}{REPEAT}\n") for _ in range(40))


@pytest.mark.parametrize("line", ["---", "***", "___", "* * *", "===", "#", "1."])
def test_complete_structure_lines_break_the_text_window(line):
    detector = TextLoopDetector()
    assert not detector.feed(REPEAT * 8 + "\n" + line + "\n")
    assert not detector.feed(REPEAT * 8)


@pytest.mark.parametrize(
    "prefix",
    [
        "**Analysis:** ",
        "*Analysis*: ",
        "__Analysis__: ",
        "#topic ",
        "-1 is the result. ",
        "1.25 is the result. ",
        "---ordinary text ",
        "===ordinary text ",
        "```literal``` is inline code\n",
        "```literal``` is inline code ",
    ],
)
@pytest.mark.parametrize("chunk_size", [1, 17, 4096])
def test_inline_markdown_and_non_block_prefixes_do_not_exempt_prose(prefix, chunk_size):
    detector = TextLoopDetector()
    text = prefix + REPEAT * 30
    assert any(
        detector.feed(text[index : index + chunk_size]) for index in range(0, len(text), chunk_size)
    )


def test_repeated_prose_lines_are_still_detected():
    detector = TextLoopDetector()
    assert any(detector.feed(REPEAT + "\n") for _ in range(40))


def test_shared_prefixes_and_bounded_memory():
    detector = TextLoopDetector()
    for index in range(400):
        assert not detector.feed(
            f"This is a deliberately long common prefix for each record {index}. "
        )
    # The buffer is compacted once per window, so it never exceeds two windows.
    assert len(detector._buffer) <= 10000
    assert len(detector._buffer) - detector._start <= 5000
    assert len(detector._order) <= 5000
    assert len(detector._positions) <= 5000


@pytest.mark.parametrize("cycle", [1, 2, 5])
def test_repeated_actions_and_results_are_detected(cycle):
    with pytest.raises(LoopDetectedError, match="tool_cycle"):
        check_action_loop(history(cycle=cycle))


def test_changed_results_and_explicit_polling_are_not_loops():
    check_action_loop(history(changing=True))
    check_action_loop(history(), ignored_tools=("lookup",))


def test_reused_call_ids_and_argument_key_order():
    messages = [HumanMessage(content="test")]
    for index in range(5):
        args = {"a": 1, "b": 2} if index % 2 else {"b": 2, "a": 1}
        messages.extend(
            [
                call("lookup", args, id="reused"),
                ToolMessage(
                    content="same",
                    tool_call_id="reused",
                ),
            ]
        )
    with pytest.raises(LoopDetectedError):
        check_action_loop(messages)
    messages[-1].content = "new result"
    check_action_loop(messages)


def parallel_history(steps, *, width=2, changing=False, order=range):
    """One assistant response per step, each issuing ``width`` calls at once."""
    messages = [HumanMessage(content="test")]
    for index in range(steps):
        calls = [
            call("lookup", {"page": page}, id=f"{index}-{page}").tool_calls[0]
            for page in order(width)
        ]
        messages.append(AIMessage(content="", tool_calls=calls))
        messages.extend(
            ToolMessage(
                content=f"page-{page}-{index if changing else ''}", tool_call_id=f"{index}-{page}"
            )
            for page in reversed(range(width))
        )
    return messages


def test_parallel_results_match_their_calls_not_arrival_order():
    with pytest.raises(LoopDetectedError):
        check_action_loop(parallel_history(5))


def test_one_assistant_response_counts_as_one_step():
    # Four parallel steps are eight calls but only four steps, so not yet a cycle.
    check_action_loop(parallel_history(4))
    check_action_loop(parallel_history(8, changing=True))
    with pytest.raises(LoopDetectedError):
        # Call order inside a step is normalized before comparison.
        check_action_loop(parallel_history(5, order=lambda width: reversed(range(width))))


@pytest.mark.parametrize(
    "injected",
    [
        HumanMessage(content="summary", additional_kwargs={"lc_source": "summarization"}),
        HumanMessage(content="revise", name="rubric_grader"),
        HumanMessage(content="<media path='x'/>", additional_kwargs={"media": True}),
    ],
)
def test_messages_injected_mid_turn_do_not_reset_the_count(injected):
    messages = history()
    messages.insert(6, injected)
    with pytest.raises(LoopDetectedError, match="tool_cycle"):
        check_action_loop(messages)


def test_middleware_marks_the_turn_boundary_once_per_invocation():
    middleware = LoopDetectionMiddleware()
    assert middleware.before_agent({"messages": [1, 2, 3]}, None) == {"loop_turn_start": 3}
    earlier = history()
    # A completed loop from an earlier turn is outside this turn's slice.
    assert (
        middleware.before_model(
            {
                "messages": [*earlier, HumanMessage(content="new request")],
                "loop_turn_start": len(earlier) + 1,
            },
            None,
        )
        is None
    )
    assert middleware.before_model({"messages": earlier, "loop_turn_start": 0}, None)
    # A summarization rewrite can leave the index past the end; that must not raise.
    assert middleware.before_model({"messages": earlier, "loop_turn_start": 10_000}, None) is None


def test_first_cycle_adds_one_hint_and_the_next_cycle_stops():
    middleware = LoopDetectionMiddleware()
    messages = history()
    update = middleware.before_model({"messages": messages, "loop_turn_start": 0}, None)
    [hint] = update["messages"]
    messages.append(hint)
    # Steps before the hint do not count again, so the model gets a full retry.
    assert middleware.before_model({"messages": messages, "loop_turn_start": 0}, None) is None
    messages.extend(history()[1:-2])
    assert middleware.before_model({"messages": messages, "loop_turn_start": 0}, None) is None
    messages.extend(history()[-2:])
    with pytest.raises(LoopDetectedError, match="tool_cycle"):
        middleware.before_model({"messages": messages, "loop_turn_start": 0}, None)
    # A hint from an earlier turn does not use up this turn's retry.
    assert middleware.before_model(
        {"messages": [*messages, *history()], "loop_turn_start": len(messages)},
        None,
    )


class RepeatingModel(ScriptedModel):
    chunks_read: int = 0
    stream_closed: bool = False
    reasoning: bool = False
    prefix: str = ""

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(messages)
        try:
            if self.prefix:
                yield ChatGenerationChunk(message=AIMessageChunk(content=self.prefix))
            for _ in range(100):
                self.chunks_read += 1
                message = (
                    AIMessageChunk(content="", additional_kwargs={"reasoning_content": REPEAT})
                    if self.reasoning
                    else AIMessageChunk(content=REPEAT)
                )
                yield ChatGenerationChunk(message=message)
        finally:
            self.stream_closed = True

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        source = self._stream(messages, stop, run_manager, **kwargs)
        try:
            for chunk in source:
                yield chunk
        finally:
            source.close()


@pytest.mark.parametrize("reasoning", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_stream_stops_early_cleans_state_and_propagates_in_both_modes(mode, reasoning):
    guard = LoopDetectionCallback()
    model = RepeatingModel(responses=[], reasoning=reasoning)
    with pytest.raises(LoopDetectedError, match="reasoning" if reasoning else "text"):
        if mode == "sync":
            list(model.stream("test", config={"callbacks": [guard]}))
        else:
            async for _ in model.astream("test", config={"callbacks": [guard]}):
                pass
    # Allow native async-generator finalizers to close the provider stream.
    for _ in range(5):
        await asyncio.sleep(0)
    assert model.chunks_read < 20
    assert model.stream_closed
    assert not guard._streams


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_nonstreaming_repetition_is_not_returned_as_success(runtime, mode):
    model = ScriptedModel(responses=[AIMessage(content=REPEAT * 30)])
    graph = build_graph(runtime, model=model)
    with pytest.raises(LoopDetectedError):
        if mode == "sync":
            graph.invoke({"messages": [("user", "test")]})
        else:
            await graph.ainvoke({"messages": [("user", "test")]})
    assert len(model.requests) == 1


LOOP_CALL = ("common__summarize_numbers", {"numbers": [1, 2]})


@pytest.mark.parametrize("subagent", [False, True])
async def test_tool_loop_hint_lets_the_model_change_approach(runtime, subagent):
    responses = [call(*LOOP_CALL) for _ in range(5)]
    responses.append(AIMessage(content="The mean is 1.5"))
    if subagent:
        responses.insert(
            0,
            call(
                "task",
                {
                    "subagent_type": "general-purpose",
                    "description": "Compute the mean",
                },
            ),
        )
        responses.append(AIMessage(content="The subagent reports a mean of 1.5"))
    model = ScriptedModel(responses=responses)
    result = await build_graph(runtime, model=model).ainvoke(
        {
            "messages": [("user", "Compute the mean")],
        }
    )
    assert result["messages"][-1].content == responses[-1].content
    hinted = [
        request
        for request in model.requests
        if any(getattr(message, "name", None) == "loop_detection" for message in request)
    ]
    assert len(hinted) == 1


@pytest.mark.parametrize("subagent", [False, True])
async def test_tool_loop_stops_without_replaying_work_and_next_turn_can_continue(runtime, subagent):
    responses = [call(*LOOP_CALL) for _ in range(10)]
    if subagent:
        responses.insert(
            0,
            call(
                "task",
                {
                    "subagent_type": "general-purpose",
                    "description": "Compute the mean",
                },
            ),
        )
    model = ScriptedModel(responses=responses + [AIMessage(content="New request completed")])
    graph = build_graph(runtime, model=model, checkpointer=InMemorySaver())
    run_config = {"configurable": {"thread_id": "loop-test"}}
    with pytest.raises(LoopDetectedError, match="tool_cycle"):
        await graph.ainvoke({"messages": [("user", "Compute the mean")]}, run_config)
    assert len(model.requests) == len(responses)
    result = await graph.ainvoke(
        {"messages": [("user", "Stop querying and summarize")]}, run_config
    )
    assert result["messages"][-1].content == "New request completed"


async def test_tool_loop_leaves_complete_model_spans_in_the_trace(runtime):
    """The guard raises between model runs, so no span is left unterminated."""
    body = query("Compute the mean")
    responses = [call(*LOOP_CALL) for _ in range(10)]
    model = ScriptedModel(responses=responses)
    async with client_for(runtime, model) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=body))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "LOOP_DETECTED"
    # read_trace validates every span's start/end, parent links and OTLP schema.
    data = read_trace(runtime, body)
    models = [span for span in spans(data) if attributes(span).get("langsmith.span.kind") == "llm"]
    assert len(models) == len(responses)


async def test_disabled_guard_preserves_existing_behavior(runtime):
    model = ScriptedModel(responses=[AIMessage(content=REPEAT * 30)])
    result = await build_graph(configured(runtime, enabled=False), model=model).ainvoke(
        {
            "messages": [("user", "Repeat this intentionally")],
        }
    )
    assert result["messages"][-1].content == REPEAT * 30


async def test_disabled_guard_also_skips_tool_cycles(runtime):
    responses = [call("common__summarize_numbers", {"numbers": [1, 2]}) for _ in range(6)]
    model = ScriptedModel(responses=[*responses, AIMessage(content="finished anyway")])
    result = await build_graph(configured(runtime, enabled=False), model=model).ainvoke(
        {
            "messages": [("user", "Poll repeatedly on purpose")],
        }
    )
    assert result["messages"][-1].content == "finished anyway"


@pytest.mark.parametrize("entry", [False, True])
async def test_http_stream_reports_one_error_for_root_and_compiled_entry(runtime, tmp_path, entry):
    if entry:
        runtime = _entry_runtime(runtime, tmp_path)
    model = RepeatingModel(responses=[])
    async with client_for(runtime, model) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "LOOP_DETECTED"
        assert REPEAT not in events[-1]["message"]
    assert len(model.requests) == 1
    assert model.chunks_read < 20


@pytest.mark.parametrize(
    "prefix",
    [
        "**Analysis:** ",
        "```text\r\nok\r\n```\r\n",
        "```literal``` is inline code\n",
    ],
)
async def test_http_stream_stops_repeated_prose_after_markdown_prefixes(runtime, prefix):
    model = RepeatingModel(responses=[], prefix=prefix)
    async with client_for(runtime, model) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["code"] == "LOOP_DETECTED"
    assert model.chunks_read < 20
    assert model.stream_closed


def test_interleaved_model_runs_and_channels_are_isolated():
    guard = LoopDetectionCallback()
    ids = [started(guard), started(guard)]
    for _ in range(5):
        for run_id in ids:
            guard.on_llm_new_token(REPEAT, run_id=run_id)
    guard.on_llm_error(RuntimeError("cancelled"), run_id=ids[0])
    for _ in range(3):
        guard.on_llm_new_token(
            "",
            run_id=ids[1],
            chunk=ChatGenerationChunk(
                message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": REPEAT}),
            ),
        )
    guard.on_llm_end(LLMResult(generations=[]), run_id=ids[1])
    assert not guard._streams


@pytest.mark.parametrize(
    "metadata",
    [
        {"lc_internal_call": "token"},
        {"lc_source": "summarization"},
        {"rubric_grader_configured_model": "primary"},
    ],
)
def test_middleware_owned_model_calls_are_not_checked(metadata):
    """Summarization and rubric grading run their own models; they must not stop the agent."""
    guard = LoopDetectionCallback()
    run_id = started(guard, metadata=metadata)
    assert run_id not in guard._streams
    for _ in range(50):
        guard.on_llm_new_token(REPEAT, run_id=run_id)
    guard.on_llm_end(generations(AIMessage(content=REPEAT * 30)), run_id=run_id)
    assert started(guard, metadata={"langgraph_node": "model"}) in guard._streams


@pytest.mark.parametrize(
    "message,channel",
    [
        (AIMessage(content=REPEAT * 30), "text"),
        (AIMessage(content="", additional_kwargs={"reasoning_content": REPEAT * 30}), "reasoning"),
        (AIMessage(content=[{"type": "thinking", "thinking": REPEAT * 30}]), "reasoning"),
        (
            AIMessage(
                content=[{"type": "thinking", "thinking": REPEAT * 30}],
                response_metadata={"model_provider": "anthropic"},
            ),
            "reasoning",
        ),
        (AIMessage(content=[{"type": "reasoning", "reasoning": REPEAT * 30}]), "reasoning"),
        (
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": REPEAT * 30}],
                    }
                ]
            ),
            "reasoning",
        ),
        (
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": REPEAT * 30}],
                    }
                ],
                response_metadata={"model_provider": "openai"},
            ),
            "reasoning",
        ),
    ],
)
def test_every_provider_reasoning_shape_reaches_its_channel(message, channel):
    """``content_blocks`` normalizes known providers; unknown shapes are unwrapped."""
    guard = LoopDetectionCallback()
    run_id = started(guard)
    with pytest.raises(LoopDetectedError, match=f"repeated_{channel}"):
        guard.on_llm_end(generations(message), run_id=run_id)


def test_channels_streamed_already_are_not_rechecked_at_the_end():
    guard = LoopDetectionCallback()
    run_id = started(guard)
    for _ in range(3):
        guard.on_llm_new_token(REPEAT, run_id=run_id)
    # The accumulated final message would trip a fresh detector; the streamed
    # channel was already checked incrementally and must not be replayed.
    guard.on_llm_end(generations(AIMessage(content=REPEAT * 30)), run_id=run_id)
    assert not guard._streams


def test_channels_only_present_in_the_final_message_are_checked_once():
    guard = LoopDetectionCallback()
    run_id = started(guard)
    guard.on_llm_new_token("a short answer", run_id=run_id)
    with pytest.raises(LoopDetectedError, match="repeated_reasoning"):
        guard.on_llm_end(
            generations(
                AIMessage(
                    content="a short answer",
                    additional_kwargs={"reasoning_content": REPEAT * 30},
                )
            ),
            run_id=run_id,
        )


def test_every_block_of_an_unstreamed_channel_is_checked():
    guard = LoopDetectionCallback()
    run_id = started(guard)
    with pytest.raises(LoopDetectedError, match="repeated_text"):
        guard.on_llm_end(
            generations(
                AIMessage(
                    content=[
                        {"type": "text", "text": "A short opening sentence."},
                        {"type": "text", "text": REPEAT * 30},
                    ]
                )
            ),
            run_id=run_id,
        )


def _write_swallowing_entry(root):
    """An entry like NL2SQL: it catches every model error and returns a reply."""
    (root / "subagents").mkdir(parents=True)
    (root / ".plugin.yaml").write_text(
        json.dumps(
            {
                "id": "direct",
                "subagents": ["subagents/direct.yaml"],
            }
        )
    )
    (root / "subagents" / "direct.yaml").write_text(
        json.dumps(
            {
                "kind": "compiled",
                "name": "direct",
                "description": "Swallows errors.",
                "entrypoint": "subagents/direct.py:build",
            }
        )
    )
    (root / "subagents" / "direct.py").write_text(
        "from langchain_core.messages import AIMessage, HumanMessage\n"
        "from langchain_core.runnables import RunnableLambda\n"
        "\n"
        "def build(params, services):\n"
        "    async def run(state):\n"
        "        try:\n"
        "            await services.get_model().ainvoke([HumanMessage(content='q')])\n"
        "        except Exception as error:\n"
        "            return {'messages': [AIMessage(content=f'failed: {error}')]}\n"
        "        return {'messages': [AIMessage(content='visible-reply')]}\n"
        "\n"
        "    return RunnableLambda(run)\n",
        encoding="utf-8",
    )


async def test_compiled_entry_cannot_turn_a_detection_into_a_reply(runtime, tmp_path):
    _write_swallowing_entry(tmp_path / "home" / "plugins" / "direct")
    (runtime.paths.home / "config.yaml").write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test-model",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "test-secret",
                    }
                },
                "plugins": {"enabled": ["direct"]},
                "dataagent": {"entry": "direct"},
            }
        )
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path))
    async with client_for(runtime, RepeatingModel(responses=[])) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["code"] == "LOOP_DETECTED"


async def test_real_provider_stream_closes_without_retry(runtime):
    class Stream(httpx.AsyncByteStream):
        closed = False
        chunks_read = 0

        async def __aiter__(self):
            for _ in range(100):
                self.chunks_read += 1
                chunk = {
                    "id": "loop",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": None,
                            "delta": {"role": "assistant", "content": REPEAT},
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()

        async def aclose(self):
            self.closed = True

    stream = Stream()
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    model = build_model(runtime)
    await model.root_async_client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model.root_async_client._client = client
        guard = LoopDetectionCallback()
        try:
            with pytest.raises(LoopDetectedError):
                async for _ in model.astream("test", config={"callbacks": [guard]}):
                    pass
            for _ in range(10):
                await asyncio.sleep(0)
            assert stream.closed
            assert stream.chunks_read < 20
            assert len(requests) == 1
        finally:
            await model.root_async_client.close()
            model.root_client.close()


async def test_consumer_close_releases_detection_state():
    guard = LoopDetectionCallback()
    model = RepeatingModel(responses=[])
    stream = model.astream("test", config={"callbacks": [guard]})
    await anext(stream)
    assert guard._streams
    await stream.aclose()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not guard._streams
    assert model.stream_closed


def test_tool_arguments_and_duplicate_reasoning_representations_do_not_false_alarm():
    guard = LoopDetectionCallback()
    run_id = started(guard)
    for _ in range(5):
        guard.on_llm_new_token(
            "",
            run_id=run_id,
            chunk=ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[{"type": "reasoning", "reasoning": REPEAT}],
                    additional_kwargs={"reasoning_content": REPEAT},
                    tool_call_chunks=[{"name": "write_file", "args": REPEAT * 20, "index": 0}],
                )
            ),
        )
    guard.on_llm_error(RuntimeError("stop"), run_id=run_id)
    assert not guard._streams


def test_untracked_runs_are_ignored():
    guard = LoopDetectionCallback()
    for _ in range(50):
        guard.on_llm_new_token(REPEAT, run_id=uuid4())
    assert not guard._streams


def test_config_layers_preserve_loop_settings(tmp_path):
    config(
        tmp_path / "home/config.yaml",
        {
            **MODEL,
            "dataagent": {"loop_detection": {"ignored_tools": ["poll_job"]}},
        },
    )
    override = config(
        tmp_path / "override.yaml",
        {
            "dataagent": {"loop_detection": {"enabled": False}},
        },
    )
    home = tmp_path / "home"
    settings = merge((*load_layers(home, {}, None), *load_layers(home, {}, override)))
    assert not settings.dataagent.loop_detection.enabled
    assert settings.dataagent.loop_detection.ignored_tools == ("poll_job",)
