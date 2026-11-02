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
"""Offline contracts for embedding the legacy IR without a legacy Runtime."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from dataagent.extensions.loading import PythonLoader
from dataagent.models import ModelContext

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
FIELD = {
    "field_id": "time_window",
    "value": {
        "time_field": "event_time",
        "range": {"direction": "past", "number": 7, "unit": "day"},
    },
    "unresolved": [],
}
TEMPLATE = {"field_id": "time_window", "writable_template": deepcopy(FIELD)}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = {"data_task_ir": {"enabled": True, "refresh_policy": "on_consume"}}
    states = {}
    context = SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path),
        get_plugin_config=lambda _: config,
        get_plugin_state=lambda _: states,
        get_model=lambda _: None,
    )
    loader = PythonLoader()
    after = loader.load(PLUGIN, "hooks/ir.py:update_ir")
    before = loader.load(PLUGIN, "hooks/ir.py:ensure_ir_ready")
    reader = loader.load(PLUGIN, "tools/ir.py:read_ir")
    core = after.__globals__["core"]
    native_fill = core.fill_ir_fields
    fill = AsyncMock(return_value=[deepcopy(FIELD)])
    gate = AsyncMock(return_value=([], True))
    monkeypatch.setattr(core, "load_template_catalog", lambda: {})
    monkeypatch.setattr(core, "list_field_templates", lambda _: [deepcopy(TEMPLATE)])
    monkeypatch.setattr(core, "fill_ir_fields", fill)
    monkeypatch.setattr(core, "_ir_consistency_gate", gate)

    def request(name, args=None, state=None, call_id="call"):
        return SimpleNamespace(
            runtime=SimpleNamespace(context=context),
            state=state or {"messages": [HumanMessage(content="past 7 days")]},
            tool_call={"name": name, "id": call_id, "args": args or {}},
        )

    return SimpleNamespace(
        context=context,
        config=config,
        states=states,
        loader=loader,
        after=after,
        before=before,
        reader=reader,
        core=core,
        fill=fill,
        native_fill=native_fill,
        gate=gate,
        request=request,
    )


def todo_result(todos):
    return Command(
        update={"todos": todos, "messages": [ToolMessage(content="ok", tool_call_id="call")]}
    )


async def initialize(h):
    todo = [{"content": "source evidence", "status": "in_progress"}]
    result = await h.after(h.request("write_todos", {"todos": todo}), todo_result(todo), params={})
    assert result is None


@pytest.mark.asyncio
async def test_disabled_and_failed_tools_do_not_create_state(harness):
    h = harness
    h.config["data_task_ir"]["enabled"] = False
    assert await h.after(h.request("write_todos"), todo_result([]), params={}) is None
    assert not h.states
    h.config["data_task_ir"]["enabled"] = True
    await h.after(
        h.request("write_todos"),
        ToolMessage(content="failed", tool_call_id="call", status="error"),
        params={},
    )
    assert not h.states
    h.fill.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_completion_then_consumer_refresh_once(harness):
    h = harness
    await initialize(h)
    todo = [{"content": "source evidence", "status": "completed"}]
    await h.after(h.request("write_todos", {"todos": todo}), todo_result(todo), params={})
    h.fill.assert_not_called()
    req = h.request("task", {"subagent_type": "nl2sql"})
    assert await h.before(req, params={}) is None
    assert await h.before(req, params={}) is None
    assert h.fill.await_count == h.gate.await_count == 1
    assert h.states["data_task_ir"]["ir_input_signature"]


@pytest.mark.asyncio
async def test_only_newly_completed_todo_refreshes_eager_ir(harness):
    h = harness
    h.config["data_task_ir"]["refresh_policy"] = "eager"
    await initialize(h)
    todo = [{"content": "renamed source step", "status": "in_progress"}]
    await h.after(h.request("write_todos", {"todos": todo}), todo_result(todo), params={})
    h.fill.assert_not_called()
    todo[0]["status"] = "completed"
    await h.after(h.request("write_todos", {"todos": todo}), todo_result(todo), params={})
    assert h.fill.await_count == 1
    await h.after(h.request("write_todos", {"todos": todo}), todo_result(todo), params={})
    assert h.fill.await_count == 1


@pytest.mark.asyncio
async def test_failed_gate_blocks_consumer_and_retry_reuses_filled_fields(harness):
    h = harness
    await initialize(h)
    h.gate.side_effect = [(["incomplete"], False), ([], True)]
    req = h.request("task", {"subagent_type": "nl2sql"})
    failed = await h.before(req, params={})
    assert failed.status == "error"
    assert h.states["data_task_ir"]["ir_input_signature"] is None
    assert await h.before(req, params={}) is None
    assert h.fill.await_count == 1 and h.gate.await_count == 2


@pytest.mark.asyncio
async def test_parallel_consumers_share_one_refresh(harness):
    h = harness
    await initialize(h)
    req = h.request("task", {"subagent_type": "nl2sql"})
    results = await asyncio.gather(*(h.before(req, params={}) for _ in range(3)))
    assert results == [None, None, None]
    assert h.fill.await_count == 1


@pytest.mark.asyncio
async def test_other_tasks_do_not_consume_ir(harness):
    h = harness
    assert (
        await h.before(h.request("task", {"subagent_type": "metadata-recall"}), params={}) is None
    )
    assert not h.states


@pytest.mark.asyncio
async def test_evidence_and_user_change_invalidate_signature(harness):
    h = harness
    await initialize(h)
    req = h.request("task", {"subagent_type": "nl2sql"})
    await h.before(req, params={})
    folder = h.context.session.outputs / "metadata"
    folder.mkdir()
    (folder / "schema.json").write_text(
        json.dumps(
            {"recorded_at_ns": 1, "kind": "schema", "query": "db.fact", "payload": {"id": "string"}}
        )
    )
    await h.before(req, params={})
    assert h.fill.await_count == 2
    req.state["messages"].append(HumanMessage(content="actually past 30 days"))
    await h.before(req, params={})
    assert h.fill.await_count == 3
    assert "past 30 days" in h.fill.call_args.kwargs["confirmed_context"]
    assert h.fill.call_args.kwargs["current_values"] is None


def test_inputs_exclude_model_inference_and_generated_reports(harness, tmp_path):
    h = harness
    snapshot = h.core.input_state
    state = {
        "messages": [
            HumanMessage(content="original"),
            AIMessage(content="invented grain"),
            HumanMessage(content="explicit correction"),
        ]
    }
    assert snapshot(state) == {"user_query": "original", "confirmed_context": "explicit correction"}
    state["messages"].extend([
        AIMessage(content="", tool_calls=[{
            "name": "read_file", "args": {"file_path": str(tmp_path / "report.md")}, "id": "read",
        }]),
        ToolMessage(content="generated guesses", tool_call_id="read"),
    ])
    assert not h.core.collect_evidence(h.context, state)


def test_plugin_state_is_private_and_isolated_between_agent_handles(tmp_path):
    runtime = SimpleNamespace(
        settings=SimpleNamespace(plugins=SimpleNamespace(enabled=("de-agent",)))
    )
    session = SimpleNamespace(outputs=tmp_path)
    first = ModelContext(runtime, session=session)
    second = ModelContext(runtime, session=session)
    first.get_plugin_state("de-agent")["secret"] = "private IR data"
    assert second.get_plugin_state("de-agent") == {}
    assert first.model_dump() == {}
    assert "private IR data" not in repr(first)
    with pytest.raises(ValueError, match="not enabled"):
        first.get_plugin_state("missing")


@pytest.mark.asyncio
async def test_delivery_and_nl2sql_share_the_same_contract(harness):
    h = harness
    await initialize(h)
    await h.before(h.request("task", {"subagent_type": "nl2sql"}), params={})
    first = await h.reader(
        SimpleNamespace(
            context=h.context,
            state={"messages": [HumanMessage(content="past 7 days")]},
            tool_call_id="read",
        )
    )
    assert await h.before(h.request("de-agent__validate_deliverables"), params={}) is None
    second = await h.reader(
        SimpleNamespace(
            context=h.context,
            state={"messages": [HumanMessage(content="past 7 days")]},
            tool_call_id="read2",
        )
    )
    assert first == second and first
    assert h.fill.await_count == 1


def test_external_markdown_uses_successful_history_results_only(harness, tmp_path):
    h = harness
    path = tmp_path.parent / "business.md"
    state = {"messages": [
        AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {"file_path": str(path), "offset": 1}, "id": "ok"},
            {"name": "read_file", "args": {"file_path": str(path)}, "id": "failed"},
        ]),
        ToolMessage(content="confirmed reference", tool_call_id="ok"),
        ToolMessage(content="read failed", tool_call_id="failed", status="error"),
        ToolMessage(content="unmatched result", tool_call_id="unknown"),
    ]}
    evidence = h.core.collect_evidence(h.context, state)
    assert evidence == [{
        "tool": "read_file",
        "params": {"file_path": str(path), "offset": 1, "path": str(path)},
        "output": ["confirmed reference"],
    }]
    assert not h.states  # No duplicate document cache is created.


@pytest.mark.asyncio
async def test_missing_plan_preserves_legacy_empty_ir_behavior(harness):
    h = harness
    request = h.request("task", {"subagent_type": "nl2sql"}, call_id="missing-plan")
    assert await h.before(request, params={}) is None
    assert h.core.read_ir(h.context) == ""
    assert h.core.get_ir_context(h.context) == ""
    h.fill.assert_not_called()


@pytest.mark.asyncio
async def test_template_load_failure_preserves_plan_and_returns_tool_error(harness, monkeypatch):
    h = harness

    def fail_load():
        raise ValueError("invalid template")

    monkeypatch.setattr(h.core, "load_template_catalog", fail_load)
    todos = [{"content": "source evidence", "status": "in_progress"}]
    result = await h.after(h.request("write_todos", {"todos": todos}), todo_result(todos), params={})
    assert isinstance(result, Command)
    assert result.update["todos"] == todos
    message = result.update["messages"][-1]
    assert message.status == "error"
    assert message.tool_call_id == "call"
    assert "读取TEMPLATE IR失败" in message.content
    assert not h.states["data_task_ir"].get("ir_initialized")
    h.fill.assert_not_called()


@pytest.mark.asyncio
async def test_field_fill_uses_native_model_config_and_preserves_trace_runs(harness, monkeypatch):
    h = harness
    h.config["data_task_ir"].update(model="ir-model", max_concurrency=3)
    model = object()
    aliases = []

    def get_model(alias):
        aliases.append(alias)
        return model

    h.context.get_model = get_model
    existing = h.context.session.outputs / ".memory/ir_trace/run_4"
    existing.mkdir(parents=True)
    fill = AsyncMock(return_value=[deepcopy(FIELD)])
    monkeypatch.setattr(h.core, "fill_field_templates", fill)
    await initialize(h)
    result = await h.native_fill(h.context, user_query="past 7 days", tool_evidence="source")
    assert result == [FIELD]
    assert aliases == ["ir-model"]
    assert fill.call_args.kwargs["llm"] is model
    assert fill.call_args.kwargs["max_concurrency"] == 3
    assert Path(fill.call_args.kwargs["trace_dir"]).name == "run_5"
    assert existing.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["eager", "on_consume"])
async def test_read_ir_does_not_refresh_or_require_completed_gate(harness, policy):
    h = harness
    h.config["data_task_ir"]["refresh_policy"] = policy
    await initialize(h)
    cache = h.states["data_task_ir"]
    cache["ir_field_values"] = [deepcopy(FIELD)]
    cache["ir_rendered_context"] = "existing IR"
    cache["ir_input_signature"] = None
    runtime = SimpleNamespace(context=h.context, state={}, tool_call_id="read")
    assert await h.reader(runtime) == "existing IR"
    h.fill.assert_not_called()
    h.gate.assert_not_called()
    cache["ir_dirty"] = True
    result = await h.reader(runtime)
    assert result.status == "error"
    h.fill.assert_not_called()
    h.gate.assert_not_called()


@pytest.mark.asyncio
async def test_consumer_uses_document_history_and_semantic_files(harness, tmp_path):
    h = harness
    await initialize(h)
    folder = tmp_path / "metadata"
    folder.mkdir()
    (folder / "schema.json").write_text(json.dumps({
        "kind": "schema", "query": "db.fact", "payload": {"id": "string", "diagnostics": "hidden"},
    }))
    req = h.request("task", {"subagent_type": "nl2sql"})
    req.state["messages"].extend([
        AIMessage(content="", tool_calls=[{
            "name": "read_file", "args": {"file_path": str(tmp_path.parent / "business.md")}, "id": "doc",
        }]),
        ToolMessage(content="confirmed document excerpt", tool_call_id="doc"),
    ])
    assert await h.before(req, params={}) is None
    evidence = h.fill.call_args.kwargs["tool_evidence"]
    assert "confirmed document excerpt" in evidence
    assert "db.fact" in evidence
    assert "hidden" not in evidence
    assert "documents" not in h.states["data_task_ir"]
