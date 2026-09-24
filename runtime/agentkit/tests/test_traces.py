"""OTLP file interoperability and native SDK/REST execution lifecycle."""

import asyncio
import base64
import copy
import json
import re
from dataclasses import replace
from uuid import UUID

import langsmith as ls
import pytest
from conftest import ScriptedModel, call
from google.protobuf.json_format import ParseDict
from langchain_core.messages import AIMessage
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from test_api import client_for, query, sse, terminal

from dataagent import build_agent
from dataagent.extensions.tracing import LocalLangChainTracer


@pytest.fixture(autouse=True)
def no_trace_network(monkeypatch):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("Trace must not make HTTP requests")

    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    yield
    assert not attempts


def spans(data):
    return [span for resource in data["resourceSpans"]
            for scope in resource["scopeSpans"] for span in scope["spans"]]


def attributes(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span.get("attributes", [])}


def root(data):
    result, = [span for span in spans(data) if not span.get("parentSpanId")]
    return result


def validate_file(path):
    data = json.loads(path.read_text())
    assert set(data) == {"resourceSpans"}
    records = spans(data)
    ids = {span["spanId"] for span in records}
    assert len(ids) == len(records)
    assert path.stat().st_mode & 0o777 == 0o600
    assert {span["traceId"] for span in records} == {UUID(path.stem).hex}
    for span in records:
        assert re.fullmatch("[0-9a-f]{32}", span["traceId"])
        assert re.fullmatch("[0-9a-f]{16}", span["spanId"])
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"]) > 0
        assert not span.get("parentSpanId") or span["parentSpanId"] in ids
    root(data)
    by_id = {span["spanId"]: span for span in records}
    for span in records:
        visited = set()
        while span.get("parentSpanId"):
            assert span["spanId"] not in visited
            visited.add(span["spanId"])
            span = by_id[span["parentSpanId"]]
    # Strict official protobuf schema parse after OTLP JSON's hex → bytes rule.
    protobuf_json = copy.deepcopy(data)
    for span in spans(protobuf_json):
        for record in (span, *span.get("links", [])):
            for key in ("traceId", "spanId", "parentSpanId"):
                if record.get(key):
                    record[key] = base64.b64encode(bytes.fromhex(record[key])).decode()
    parsed = ParseDict(protobuf_json, ExportTraceServiceRequest())
    assert parsed.resource_spans
    return data


def read_trace(runtime, body):
    matches = []
    for path in runtime.paths.for_session(body["threadId"]).traces.glob("*.json"):
        data = validate_file(path)
        if attributes(root(data)).get("langsmith.metadata.dataagent_root_run_id") == body["runId"]:
            matches.append(data)
    assert len(matches) == 1
    return matches[0]


def assert_collector_empty(graph):
    collector = next(item for item in graph.config["callbacks"] if isinstance(item, LocalLangChainTracer))
    assert collector.latest_run is None
    assert not collector.run_has_token_event_map
    assert not collector.run_map
    assert not collector.order_map
    assert not collector.exporter._spans
    assert not collector.client.otel_exporter._span_info


async def test_trace_preserves_subagent_model_tool_tree_and_redacts(runtime):
    body = query('trace request test-secret sk-example-key Bearer example-token')
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "general-purpose", "description": "Compute statistics"}),
        call("common__summarize_numbers", {"numbers": [1, 2, 3]}),
        AIMessage(content="mean 2"), AIMessage(content="answer test-secret"),
    ])
    async with client_for(runtime, model) as (client, _):
        events = sse(await client.post("/dataagent/stream", json=body))
        assert terminal(events) == ["RUN_FINISHED"]
        assert [event["toolCallName"] for event in events if event["type"] == "TOOL_CALL_START"] == ["task"]
    data = read_trace(runtime, body)
    assert model.cursor == 4
    records = spans(data)
    assert root(data)["name"] == "dataagent-v2"
    task, = [s for s in records if s["name"] == "task"]
    child, = [s for s in records if s["name"] == "general-purpose"]
    assert child["parentSpanId"] == task["spanId"]
    assert len([s for s in records if attributes(s).get("langsmith.span.kind") == "llm"]) == 4
    assert any(s["name"] == "common__summarize_numbers" for s in records)
    text = json.dumps(data)
    assert "[REDACTED]" in text
    assert all(secret not in text for secret in ("test-secret", "sk-example-key", "example-token"))
    assert "new_token" not in text


@pytest.mark.parametrize("timeout", [False, True])
async def test_error_or_timeout_still_saves_native_trace(runtime, timeout):
    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.Event().wait()

    if timeout:
        runtime = replace(runtime, settings=runtime.settings.model_copy(update={
            "dataagent": runtime.settings.dataagent.model_copy(update={
                "limits": runtime.settings.dataagent.limits.model_copy(update={"timeout_seconds": 0.1}),
            }),
        }))
    model = WaitingModel(responses=[]) if timeout else ScriptedModel(responses=[])
    body = query()
    async with client_for(runtime, model) as (client, _):
        assert terminal(sse(await client.post("/dataagent/stream", json=body))) == ["RUN_ERROR"]
    assert root(read_trace(runtime, body))["status"]["code"] == 2


async def test_traces_are_isolated_across_concurrent_sessions_and_turns(runtime):
    model = ScriptedModel(responses=[AIMessage(content="ok") for _ in range(3)])
    bodies = [query("first-only", thread="one"), query("second-only", thread="two")]
    async with client_for(runtime, model) as (client, _):
        responses = await asyncio.gather(*(client.post("/dataagent/stream", json=body) for body in bodies))
        assert all(terminal(sse(response)) == ["RUN_FINISHED"] for response in responses)
        first = read_trace(runtime, bodies[0])
        second = read_trace(runtime, bodies[1])
        assert "second-only" not in json.dumps(first)
        assert "first-only" not in json.dumps(second)
        third = query("next", thread="one")
        assert terminal(sse(await client.post("/dataagent/stream", json=third))) == ["RUN_FINISHED"]
        assert read_trace(runtime, bodies[0]) == first
        assert root(read_trace(runtime, third))["traceId"] != root(first)["traceId"]


@pytest.mark.parametrize("model_error", [False, True])
async def test_trace_write_failure_does_not_change_answer_or_session_status(runtime, monkeypatch, caplog, model_error):
    def fail(*args):
        raise OSError("disk failure test-secret")

    monkeypatch.setattr("dataagent.extensions.trace_exporter.LocalFileExporter._write", fail)
    model = ScriptedModel(responses=[] if model_error else [AIMessage(content="ok")])
    async with client_for(runtime, model) as (client, _):
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == (["RUN_ERROR"] if model_error else ["RUN_FINISHED"])
        if model_error:
            assert "Scripted model ran out of responses" in events[-1]["message"]
        assert (await client.get("/sessions/thread-1")).json()["status"] == ("error" if model_error else "complete")
    assert "TRACE_SAVE_ERROR" in caplog.text and "test-secret" not in caplog.text


def test_sdk_sync_calls_save_distinct_traces_without_retaining_runs(runtime):
    graph = build_agent(runtime, model=ScriptedModel(responses=[AIMessage(content="ok")] * 2))
    for prompt in ("first-only", "second-only"):
        graph.invoke({"messages": [("user", prompt)]})
        assert_collector_empty(graph)
    files = list(runtime.paths.for_session("sdk").traces.glob("*.json"))
    assert len(files) == 2
    for path in files:
        text = json.dumps(validate_file(path))
        assert sum(prompt in text for prompt in ("first-only", "second-only")) == 1


async def test_sdk_concurrent_invocations_and_stream_share_graph_not_trace(runtime):
    class OverlappingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.sleep(0.02)
            return await super()._agenerate(*args, **kwargs)

    graph = build_agent(runtime, model=OverlappingModel(responses=[AIMessage(content="ok")] * 3))
    await asyncio.gather(*(
        graph.ainvoke({"messages": [("user", prompt)]}) for prompt in ("first-only", "second-only")
    ))
    assert_collector_empty(graph)
    async for _ in graph.astream({"messages": [("user", "stream-only")]}):
        pass
    assert_collector_empty(graph)
    files = list(runtime.paths.for_session("sdk").traces.glob("*.json"))
    assert len(files) == 3
    for path in files:
        text = json.dumps(validate_file(path))
        assert sum(prompt in text for prompt in ("first-only", "second-only", "stream-only")) == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_sdk_failure_and_cancel_save_trace_and_release_collection(runtime, cancelled):
    started = asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    model = WaitingModel(responses=[]) if cancelled else ScriptedModel(responses=[])
    graph = build_agent(runtime, model=model)
    task = asyncio.create_task(graph.ainvoke({"messages": [("user", "hi")]}))
    if cancelled:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await task
    assert_collector_empty(graph)
    path, = runtime.paths.for_session("sdk").traces.glob("*.json")
    assert root(validate_file(path))["status"]["code"] == 2


@pytest.mark.parametrize("enabled", [False, "local", True])
async def test_local_tracer_never_uploads_even_with_global_tracing(runtime, monkeypatch, enabled):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_TRACING_MODE", "hybrid")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    graph = build_agent(runtime, model=ScriptedModel(responses=[AIMessage(content="ok")]))
    with ls.tracing_context(enabled=enabled):
        await graph.ainvoke({"messages": [("user", "hi")]})
    assert_collector_empty(graph)
    if enabled is False:
        assert not list(runtime.paths.for_session("sdk").traces.glob("*.json"))
        return
    path, = runtime.paths.for_session("sdk").traces.glob("*.json")
    validate_file(path)


def test_tracer_metadata_clones_keep_local_settings_and_shared_maps(runtime):
    graph = build_agent(runtime, model=ScriptedModel(responses=[]))
    tracer = graph.config["callbacks"][0]
    clone = tracer.copy_with_metadata_defaults(metadata={"ls_agent_type": "subagent"}, tags=["child"])
    assert clone.directory == tracer.directory
    assert clone.secrets == tracer.secrets
    assert clone.run_map is tracer.run_map and clone.order_map is tracer.order_map
    assert clone.client is tracer.client
    assert clone.exporter is tracer.exporter and clone.provider is tracer.provider
    assert clone.tracing_metadata["ls_agent_type"] == "subagent"


async def test_parallel_subagents_have_distinct_parent_tools(runtime):
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class ParallelModel(ScriptedModel):
        def _generate(self, messages, **kwargs):
            if isinstance(messages[-1], HumanMessage) and messages[-1].content == "delegate twice":
                response = AIMessage(content="", tool_calls=[
                    {"name": "task", "args": {"subagent_type": "general-purpose", "description": job},
                     "id": job, "type": "tool_call"} for job in ("job-left", "job-right")
                ])
            else:
                response = AIMessage(content="same result" if not isinstance(messages[-1], ToolMessage) else "done")
            return ChatResult(generations=[ChatGeneration(message=response)])

        async def _agenerate(self, messages, **kwargs):
            await asyncio.sleep(0.01)
            return self._generate(messages)

    graph = build_agent(runtime, model=ParallelModel(responses=[]))
    await graph.ainvoke({"messages": [("user", "delegate twice")]})
    assert_collector_empty(graph)
    path, = runtime.paths.for_session("sdk").traces.glob("*.json")
    records = spans(validate_file(path))
    children = [s for s in records if s["name"] == "general-purpose"]
    tools = [s for s in records if s["name"] == "task"]
    assert len(children) == len(tools) == 2
    assert {s["parentSpanId"] for s in children} == {s["spanId"] for s in tools}
    assert {json.loads(attributes(s)["gen_ai.prompt"])["messages"][0]["content"]
            for s in children} == {"job-left", "job-right"}


async def test_tool_failure_and_nested_traceable_are_exported(runtime):
    from langchain_core.tools import tool

    @ls.traceable(name="nested_compute", run_type="tool")
    def compute():
        raise RuntimeError("broken tool test-secret")

    @tool
    def broken() -> str:
        """Fail with a non-business error."""
        return compute()

    graph = build_agent(runtime, model=ScriptedModel(responses=[call("broken", {})]), mcp_tools=[broken])
    with pytest.raises(RuntimeError, match="broken tool"):
        await graph.ainvoke({"messages": [("user", "hi")]})
    assert_collector_empty(graph)
    path, = runtime.paths.for_session("sdk").traces.glob("*.json")
    data = validate_file(path)
    assert root(data)["status"]["code"] == 2
    assert "test-secret" not in path.read_text()
    tool_span, = [s for s in spans(data) if s["name"] == "broken"]
    nested, = [s for s in spans(data) if s["name"] == "nested_compute"]
    assert nested["parentSpanId"] == tool_span["spanId"]
    assert nested["status"]["code"] == tool_span["status"]["code"] == 2


async def test_tool_error_result_does_not_mark_successful_root_as_error(runtime):
    graph = build_agent(runtime, model=ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": "invalid"}), AIMessage(content="Please provide numbers"),
    ]))
    await graph.ainvoke({"messages": [("user", "hi")]})
    assert_collector_empty(graph)
    path, = runtime.paths.for_session("sdk").traces.glob("*.json")
    data = validate_file(path)
    assert root(data)["status"]["code"] == 1
    assert "Please provide numbers" in json.dumps(data)


def test_exporter_preserves_binary_attributes_links_and_refuses_overwrite(tmp_path):
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Link, SpanContext, TraceFlags

    from dataagent.extensions.trace_exporter import LocalFileExporter

    run_id = "11111111-1111-4111-8111-111111111111"
    other = SpanContext(0x123, 0x456, True, TraceFlags(1))
    span = ReadableSpan(
        "sample", context=SpanContext(0xabc, 0xdef, False, TraceFlags(1)),
        resource=Resource({"service.name": "test"}),
        attributes={"binary": b"\x00\xff", "langsmith.metadata.dataagent_trace_id": run_id,
                    "langsmith.metadata.dataagent_run_id": run_id,
                    "langsmith.metadata.dataagent_parent_run_id": "",
                    "langsmith.metadata.dataagent_dotted_order": "20260924T000000000000Z" + run_id},
        links=[Link(other)], start_time=10, end_time=20,
        status=trace.Status(trace.StatusCode.OK),
    )
    exporter = LocalFileExporter(tmp_path)
    exporter.export([span])
    exporter.finish(run_id, run_id)
    path = tmp_path / f"{run_id}.json"
    record = root(validate_file(path))
    assert next(a for a in record["attributes"] if a["key"] == "binary")["value"] == {"bytesValue": "AP8="}
    assert record["links"][0]["traceId"] == f"{0x123:032x}"
    assert record["links"][0]["spanId"] == f"{0x456:016x}"
    original = path.read_bytes()
    exporter.export([span])
    with pytest.raises(FileExistsError):
        exporter.finish(run_id, run_id)
    assert path.read_bytes() == original
    assert not exporter._spans


def test_incomplete_file_is_not_published_on_write_error(tmp_path, monkeypatch):
    from dataagent.extensions.trace_exporter import LocalFileExporter

    def fail(data, stream, **kwargs):
        stream.write('{"resourceSpans":')
        raise OSError("disk full")

    monkeypatch.setattr("dataagent.extensions.trace_exporter.json.dump", fail)
    with pytest.raises(OSError, match="disk full"):
        LocalFileExporter._write(tmp_path / "trace.json", {})
    assert not list(tmp_path.iterdir())


async def test_large_parallel_trace_survives_sdk_batch_boundaries(tmp_path):
    from langchain_core.runnables import RunnableLambda, RunnableParallel

    async def identity(value):
        await asyncio.sleep(0.01)
        return value

    tracer = LocalLangChainTracer(tmp_path)
    graph = RunnableParallel({f"child-{i}": RunnableLambda(identity) for i in range(120)})
    graph = graph.with_config(callbacks=[tracer])
    assert len(await graph.ainvoke("test")) == 120
    assert_collector_empty(graph)
    path, = tmp_path.glob("*.json")
    data = validate_file(path)
    assert len(spans(data)) == 121
    assert all(s.get("parentSpanId") == root(data)["spanId"]
               for s in spans(data) if s is not root(data))
