"""!577 diagnostics stay per invocation and outside model-visible tool content."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai.chat_models.base import _convert_message_to_dict
from langgraph.types import Command

from dataagent.declarations import HookSpec
from dataagent.extensions.hooks import compile_hook
from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
FIELD = {"field_id": "time_context", "value": {"input_windows": []}, "unresolved": []}


@pytest.fixture
def h(tmp_path, monkeypatch):
    loader = PythonLoader()
    before = loader.load(PLUGIN, "hooks/ir.py:ensure_ir_ready")
    post = loader.load(PLUGIN, "hooks/ir.py:record_ir_diagnostics")
    update = loader.load(PLUGIN, "hooks/ir.py:update_ir")
    core = before.__globals__["core"]
    states = {}
    options = {"data_task_ir": {"enabled": True}}
    context = SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path), get_plugin_state=lambda _: states,
        get_plugin_config=lambda _: options, get_model=lambda _: None,
    )
    fill = AsyncMock(side_effect=TimeoutError("offline diagnostic"))
    monkeypatch.setattr(core, "fill_ir_fields", fill)
    monkeypatch.setattr(core, "_ir_consistency_gate", AsyncMock(return_value=([], True)))
    monkeypatch.setattr(core, "list_field_templates", lambda _: [{"field_id": "time_context"}])

    def request(call_id="call", query="q", name="task", actual_tool=None):
        return ToolCallRequest(
            tool_call={"name": name, "id": call_id, "args": {"subagent_type": "nl2sql"}
                       if name == "task" else {}},
            tool=actual_tool, state={"messages": [HumanMessage(content=query)]},
            runtime=SimpleNamespace(context=context),
        )

    return SimpleNamespace(before=before, post=post, update=update, core=core, states=states,
                           options=options, fill=fill, request=request, context=context)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "error"])
async def test_post_preserves_tool_content_status_artifact_and_existing_metadata(h, status):
    request = h.request()
    assert await h.before(request, params={}) is None
    result = ToolMessage(content="original SQL result", tool_call_id="call", status=status,
                         artifact={"sql_path": "insert.sql"}, response_metadata={"other": "value"})
    original = result.model_dump()
    updated = await h.post(request, result, params={})
    assert updated.status == status
    assert updated.content == result.content
    assert updated.artifact == result.artifact
    assert updated.response_metadata["other"] == "value"
    assert updated.response_metadata["data_task_ir"]["status"] == "fallback_without_ir"
    assert updated.response_metadata["data_task_ir"]["signature"]
    assert result.model_dump() == original
    wire = _convert_message_to_dict(updated)
    assert wire == {"content": "original SQL result", "role": "tool", "tool_call_id": "call"}
    assert not h.states["data_task_ir"]["ir_call_diagnostics"]


@pytest.mark.asyncio
async def test_parallel_recovery_and_later_failure_do_not_change_earlier_call_verdicts(h):
    first = h.request("first", "q1")
    await h.before(first, params={})
    first_fallback = deepcopy(h.states["data_task_ir"]["ir_fallback"])
    h.fill.side_effect = None
    h.fill.return_value = [FIELD]
    recovered = h.request("recovered", "q2")
    await h.before(recovered, params={})
    assert h.states["data_task_ir"]["ir_fallback"] is None
    h.fill.side_effect = TimeoutError("later failure")
    later = h.request("later", "q3")
    await h.before(later, params={})
    original = ToolMessage(content="original", tool_call_id="recovered")
    assert await h.post(recovered, original, params={}) is None
    updated = await h.post(first, ToolMessage(content="original", tool_call_id="first"), params={})
    assert updated.response_metadata["data_task_ir"] == {"status": "fallback_without_ir", **first_fallback}
    assert updated.response_metadata["data_task_ir"]["signature"] != h.states["data_task_ir"]["ir_fallback"]["signature"]
    assert not original.response_metadata


@pytest.mark.asyncio
async def test_command_annotation_preserves_other_updates_messages_and_routing(h):
    request = h.request()
    await h.before(request, params={})
    other = ToolMessage(content="other", tool_call_id="other")
    result = Command(update={"todos": [{"content": "keep", "status": "completed"}],
                             "messages": [other, ToolMessage(content="original", tool_call_id="call")]},
                     goto="next")
    updated = await h.post(request, result, params={})
    assert updated.goto == result.goto
    assert updated.update["todos"] == result.update["todos"]
    assert updated.update["messages"][0] is other
    assert updated.update["messages"][1].response_metadata["data_task_ir"]
    assert not result.update["messages"][1].response_metadata


@pytest.mark.asyncio
async def test_success_disabled_and_unrelated_calls_do_not_receive_diagnostics(h):
    h.fill.side_effect = None
    h.fill.return_value = [FIELD]
    request = h.request()
    await h.before(request, params={})
    result = ToolMessage(content="original", tool_call_id="call", response_metadata={"keep": True})
    assert await h.post(request, result, params={}) is None
    assert result.response_metadata == {"keep": True}
    # No pre-hook capture: never read an unrelated consumer's shared fallback.
    h.states["data_task_ir"]["ir_fallback"] = {"reason": "unrelated", "signature": "other"}
    assert await h.post(h.request("uncaptured"), result, params={}) is None
    h.options["data_task_ir"]["enabled"] = False
    assert await h.post(request, result, params={}) is None


@pytest.mark.asyncio
async def test_write_todos_records_its_post_refresh_fallback_without_losing_plan(h):
    request = h.request(name="write_todos")
    todos = [{"content": "source", "status": "completed"}]
    request.tool_call["args"] = {"todos": todos}
    result = Command(update={"todos": todos,
                             "messages": [ToolMessage(content="plan saved", tool_call_id="call")]})
    updated = await h.update(request, result, params={})
    assert updated.update["todos"] == todos
    message = updated.update["messages"][0]
    assert message.content == "plan saved" and message.status == "success"
    assert message.response_metadata["data_task_ir"]["status"] == "fallback_without_ir"


@pytest.mark.asyncio
async def test_registered_adapters_propagate_fallback_into_native_callback_metadata(h, monkeypatch):
    @tool
    async def echo() -> str:
        """Return a result unchanged by IR diagnostics."""
        return "tool result"

    class Recorder(BaseCallbackHandler):
        def __init__(self):
            self.metadata = []

        def on_tool_start(self, serialized, input_str, *, metadata=None, **kwargs):
            self.metadata.append(metadata)

    echo.metadata = {"existing": "keep"}
    request = h.request(name="de-agent__check_and_repair_sql", actual_tool=echo)
    # The same adapters used by the real graph must accept the overridden tool.
    before = compile_hook(HookSpec(event="before_tool", entrypoint="hooks/ir.py:ensure_ir_ready"),
                          h.before, name="before")
    after = compile_hook(HookSpec(event="after_tool", entrypoint="hooks/ir.py:record_ir_diagnostics"),
                         h.post, name="after")
    recorder = Recorder()
    from dataagent.extensions.tracing import LocalLangChainTracer

    monkeypatch.delenv("DATAAGENT_OTLP_TRACES_ENDPOINT", raising=False)
    traces = h.context.session.outputs / "traces"
    traces.mkdir()
    tracer = LocalLangChainTracer(directory=traces)

    async def execute(req):
        assert req.tool is not echo
        assert req.tool_call is request.tool_call  # No changes to model-supplied args/identity.
        return await req.tool.ainvoke({**req.tool_call, "type": "tool_call"},
                                     config={"callbacks": [recorder, tracer]})

    async def wrapped(req):
        return await before.awrap_tool_call(req, execute)

    result = await after.awrap_tool_call(request, wrapped)
    assert result.content == "tool result" and result.status == "success"
    assert result.response_metadata["data_task_ir"]["status"] == "fallback_without_ir"
    assert recorder.metadata[0]["data_task_ir"] == result.response_metadata["data_task_ir"]
    assert recorder.metadata[0]["existing"] == "keep"
    assert echo.metadata == {"existing": "keep"}  # Shared tool is never mutated.
    files = list(traces.glob("*.json"))
    assert len(files) == 1
    trace = json.loads(files[0].read_text())
    spans = [span for resource in trace["resourceSpans"]
             for scope in resource["scopeSpans"] for span in scope["spans"]]
    assert any("data_task_ir" in attribute["key"]
               and "fallback_without_ir" in json.dumps(attribute["value"])
               for span in spans for attribute in span.get("attributes", []))


def test_manifest_registers_diagnostic_pairs_for_every_native_ir_consumer():
    hooks = json.loads((PLUGIN / ".plugin.json").read_text())["hooks"]
    before = {entry["matcher"] for entry in hooks["before_tool"]
              if entry["entrypoint"] == "hooks/ir.py:ensure_ir_ready"}
    after = {entry["matcher"] for entry in hooks["after_tool"]
             if entry["entrypoint"] == "hooks/ir.py:record_ir_diagnostics"}
    assert before == after == {"task", "de-agent__read_ir", "de-agent__check_and_repair_sql"}
