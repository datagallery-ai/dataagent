"""Result delivery contract: task isolation, explicit selection and real persistence."""

import asyncio
import csv
import json
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
TOOL = "nl2sql-react__execute_sql"


@pytest.fixture
def hooks():
    capture = PythonLoader().load(PLUGIN, "nl2sql_local/hooks/results.py:capture_result")
    return sys.modules[capture.__module__]


def task(tmp_path):
    state = {"messages": [HumanMessage(content="Same question", id=uuid4().hex)]}
    context = SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path), redaction_secrets=("secret",)
    )
    return SimpleNamespace(state=state, context=context)


async def capture(hooks, runtime, *, purpose="answer", rows=None, truncated=False, error=None):
    observed = {
        "source": "fixture",
        "db_id": "fixture",
        "dialect": "sqlite",
        "operation": "execute_sql",
        "purpose": purpose,
        "sql": f"SELECT '{purpose}'",
        "columns": ["value"],
        "rows": rows if rows is not None else [[1]],
        "truncated": truncated,
        "error": error,
    }
    observed["row_count"] = len(observed["rows"])
    result = ToolMessage(content=json.dumps(observed), tool_call_id=uuid4().hex, name=TOOL)
    request = SimpleNamespace(runtime=runtime, tool_call={"name": TOOL, "id": result.tool_call_id})
    updated = await hooks.capture_result(request, result, params={})
    runtime.state["messages"].append(updated or result)
    return json.loads((updated or result).content)


async def finalize(hooks, runtime, selection):
    text = selection if isinstance(selection, str) else json.dumps(selection)
    final = AIMessage(content=text, id=uuid4().hex)
    runtime.state["messages"].append(final)
    updated = await hooks.finalize_result(runtime.state, runtime, params={})
    assert (
        updated["messages"][0].id == final.id
    )  # Native reducer replaces, never appends a fake turn.
    return json.loads(updated["messages"][0].content)


async def test_selected_answer_survives_later_probe_and_model_fabrication(hooks, tmp_path):
    runtime = task(tmp_path)
    answer = await capture(
        hooks, runtime, rows=[[None], [None], [9007199254740993], [1.23456789012345]]
    )
    await capture(hooks, runtime, purpose="probe", rows=[[999]])
    result = await finalize(
        hooks,
        runtime,
        {
            "status": "success",
            "query_id": answer["query_id"],
            "definitions": ["Observed answer"],
            "sql": "fabricated",
            "rows": [[123]],
            "artifacts": ["/missing"],
            "returned_row_count": 100,
        },
    )
    assert result["status"] == "success" and result["sql"] == "SELECT 'answer'"
    assert result["rows"] == answer["rows"] and result["returned_row_count"] == 4
    paths = {Path(p).name: Path(p) for p in result["artifacts"]}
    assert all(path.is_file() for path in paths.values())
    assert json.loads(paths["data.json"].read_text())["rows"] == answer["rows"]
    with paths["data.csv"].open(newline="") as stream:
        assert list(csv.reader(stream)) == [
            ["value"],
            [""],
            [""],
            ["9007199254740993"],
            ["1.23456789012345"],
        ]
    assert json.loads(Path(result["final_manifest"]).read_text())["query_id"] == answer["query_id"]


@pytest.mark.parametrize("kind", ["probe", "unknown", "sibling", "missing", "failed"])
async def test_rejects_unverified_selection(hooks, tmp_path, kind):
    runtime = task(tmp_path)
    source = task(tmp_path) if kind == "sibling" else runtime
    observed = await capture(
        hooks,
        source,
        purpose="probe" if kind == "probe" else "answer",
        error={"code": "database_error"} if kind == "failed" else None,
    )
    if kind == "failed":
        assert "query_id" not in observed
    query_id = {"unknown": "invented", "missing": None}.get(kind, observed.get("query_id"))
    result = await finalize(hooks, runtime, {"status": "success", "query_id": query_id})
    assert result["status"] == "error" and result["error"]["code"] == "invalid_result_selection"
    assert result["sql"] is None and result["rows"] == [] and result["artifacts"] == []
    assert not list(tmp_path.rglob("final.json"))


@pytest.mark.parametrize("truncated", [True, False])
async def test_preview_cap_is_distinct_from_database_truncation(hooks, tmp_path, truncated):
    runtime = task(tmp_path)
    observed = await capture(hooks, runtime, rows=[[i] for i in range(25)], truncated=truncated)
    # The visible tool body may have been offloaded. Selection uses its native artifact.
    runtime.state["messages"][-1].content = "Offloaded to a file"
    result = await finalize(hooks, runtime, {"status": "success", "query_id": observed["query_id"]})
    assert result["status"] == ("partial" if truncated else "success")
    assert result["returned_row_count"] == 25 and len(result["rows"]) == 20
    assert result["preview_truncated"] and result["truncated"] is truncated
    saved = next(Path(p) for p in result["artifacts"] if p.endswith("data.json"))
    assert json.loads(saved.read_text())["rows"] == observed["rows"]


@pytest.mark.parametrize("writer", ["_write_snapshot", "_write_final"])
async def test_write_failure_keeps_verified_result_and_reports_only_saved_paths(
    hooks, tmp_path, monkeypatch, writer
):
    def fail(*args):
        raise OSError("disk unavailable: secret")

    monkeypatch.setattr(hooks, writer, fail)
    runtime = task(tmp_path)
    observed = await capture(hooks, runtime)
    result = await finalize(hooks, runtime, {"status": "success", "query_id": observed["query_id"]})
    assert result["status"] == "partial" and result["rows"] == [[1]]
    assert result["sql"] == "SELECT 'answer'" and result["error"] is None
    assert result["artifact_error"] and "secret" not in json.dumps(result)
    assert all(Path(path).is_file() for path in result["artifacts"])
    assert result["final_manifest"] is None and not list(tmp_path.rglob("final.json"))
    if writer == "_write_snapshot":
        assert result["artifacts"] == []


async def test_partial_snapshot_write_is_not_published(hooks, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("JSON write failed after SQL was written")

    monkeypatch.setattr(hooks, "_json", fail)
    observed = await capture(hooks, task(tmp_path))
    assert observed["artifacts"] == [] and observed["artifact_error"]
    assert not list(tmp_path.rglob("query.sql")) and not list(tmp_path.rglob("*.tmp"))


async def test_writes_release_event_loop_but_are_awaited(hooks, tmp_path, monkeypatch):
    entered, release = Event(), Event()
    writer = hooks._write_snapshot

    def delayed(*args):
        entered.set()
        assert release.wait(timeout=5)
        return writer(*args)

    monkeypatch.setattr(hooks, "_write_snapshot", delayed)
    running = asyncio.create_task(capture(hooks, task(tmp_path)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.sleep(0)  # Event loop progresses while the writer is blocked.
        assert not running.done() and not list(tmp_path.rglob("data.json"))
    finally:
        release.set()
    observed = await asyncio.wait_for(running, 5)
    assert all(Path(path).is_file() for path in observed["artifacts"])


async def test_parallel_identical_tasks_use_distinct_directories(hooks, tmp_path):
    first, second = task(tmp_path), task(tmp_path)
    observations = await asyncio.gather(capture(hooks, first), capture(hooks, second))
    results = await asyncio.gather(
        *(
            finalize(hooks, runtime, {"status": "success", "query_id": observed["query_id"]})
            for runtime, observed in zip((first, second), observations)
        )
    )
    assert results[0]["task_id"] != results[1]["task_id"]
    assert set(results[0]["artifacts"]).isdisjoint(results[1]["artifacts"])
    assert len(list(tmp_path.rglob("final.json"))) == 2


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "Result:\n{}"])
async def test_single_selection_normalization(hooks, tmp_path, wrapper):
    runtime = task(tmp_path)
    observed = await capture(hooks, runtime)
    selection = json.dumps({"status": "success", "query_id": observed["query_id"]})
    result = await finalize(hooks, runtime, wrapper.format(selection))
    assert result["status"] == "success"


@pytest.mark.parametrize("text", ["No JSON", "{} {}", '{"status":"success","definitions":[42]}'])
async def test_invalid_or_ambiguous_selection_is_explicit_error(hooks, tmp_path, text):
    result = await finalize(hooks, task(tmp_path), text)
    assert result["error"]["code"] == "invalid_result_selection" and result["status"] == "error"


@pytest.mark.parametrize("status", ["needs_clarification", "error"])
async def test_blocked_task_does_not_promote_probe(hooks, tmp_path, status):
    runtime = task(tmp_path)
    await capture(hooks, runtime, purpose="probe")
    result = await finalize(
        hooks,
        runtime,
        {"status": status, "query_id": None, "unresolved": ["Metric definition is missing"]},
    )
    assert result["status"] == status and result["sql"] is None and result["query_id"] is None
    assert not list(tmp_path.rglob("final.json"))
