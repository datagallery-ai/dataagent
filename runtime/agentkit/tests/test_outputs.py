"""Outputs come from session files, not custom AG-UI artifact events."""

import os

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage
from test_api import client_for, query, sse, terminal

from restapi.outputs import PREVIEW_BYTES, list_outputs


async def test_native_output_listing_and_preview_survive_restart(runtime):
    model = ScriptedModel(responses=[
        call("write_file", {"file_path": str(runtime.paths.for_session("thread-1").outputs / "销售报告.md"),
                            "content": "# Sales\n\nTotal: 42"}),
        AIMessage(content="Saved"),
    ])
    async with client_for(runtime, model) as (client, _):
        assert (await client.get("/sessions/new/outputs")).json() == {"files": []}
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == ["RUN_FINISHED"]
        assert not any(event.get("name") == "artifact" for event in events)
        response = await client.get("/sessions/thread-1/outputs")
        assert response.status_code == 200
        assert [file["path"] for file in response.json()["files"]] == ["销售报告.md"]
        assert response.json()["files"][0]["size"] > 0

    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert (await client.get("/sessions/thread-1/outputs")).json() == response.json()
        preview = await client.get("/sessions/thread-1/outputs/preview", params={"path": "销售报告.md"})
        assert preview.json()["content"] == "# Sales\n\nTotal: 42"
        assert not app.state.agents.graphs  # Listing/preview never assembles an Agent.


async def test_outputs_only_list_current_session_regular_business_files(runtime, tmp_path):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        for thread in ("one", "two"):
            runtime.paths.open_session(thread, create=True)
            await app.state.sessions.start(runtime.paths.user_id, thread, "run", "test")
        root = runtime.paths.for_session("one").outputs
        other = runtime.paths.for_session("two").outputs
        (other / "private.txt").write_text("other-session")
        (root / "nested").mkdir()
        (root / "nested" / "result.csv").write_text("name,total\na,42")
        for name in ("conversation_history", "large_tool_results"):
            (root / "artifacts" / name).mkdir()
            (root / "artifacts" / name / "internal.txt").write_text("internal")
        (root / "artifacts" / "chart.svg").write_text("<svg/>")
        (root / "link.txt").symlink_to(other / "private.txt")
        (root / "linked-directory").symlink_to(other, target_is_directory=True)
        os.mkfifo(root / "pipe")
        files = (await client.get("/sessions/one/outputs")).json()["files"]
        assert {file["path"] for file in files} == {"nested/result.csv", "artifacts/chart.svg"}
        for path in ("link.txt", "linked-directory/private.txt", "pipe", "missing.txt",
                     "artifacts/conversation_history/internal.txt"):
            result = await client.get("/sessions/one/outputs/preview", params={"path": path})
            assert result.status_code == 404
        for path in ("../session.json", str(other / "private.txt"), "nested/../../session.json"):
            result = await client.get("/sessions/one/outputs/preview", params={"path": path})
            assert result.status_code == 400
        await app.state.sessions.start("someone-else", "foreign", "run", "test")
        assert (await client.get("/sessions/foreign/outputs")).status_code == 404
        (root / "nested" / "result.csv").unlink()
        files = (await client.get("/sessions/one/outputs")).json()["files"]
        assert [file["path"] for file in files] == ["artifacts/chart.svg"]


async def test_output_preview_limits_binary_and_changed_root(runtime, tmp_path):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        runtime.paths.open_session("one", create=True)
        await app.state.sessions.start(runtime.paths.user_id, "one", "run", "test")
        root = runtime.paths.for_session("one").outputs
        (root / "large.md").write_text("中" * PREVIEW_BYTES)
        (root / "binary.png").write_bytes(b"\x89PNG\x00\xff")
        (root / "empty.txt").touch()
        response = await client.get("/sessions/one/outputs/preview", params={"path": "large.md"})
        assert response.status_code == 200
        assert "Preview truncated" in response.json()["content"]
        assert len(response.content) < PREVIEW_BYTES + 1024
        assert (await client.get("/sessions/one/outputs/preview", params={"path": "binary.png"})).status_code == 415
        assert (await client.get("/sessions/one/outputs/preview", params={"path": "empty.txt"})).json()["content"] == ""
        root.rename(root.with_name("saved-outputs"))
        root.symlink_to(tmp_path, target_is_directory=True)
        assert (await client.get("/sessions/one/outputs")).status_code == 409
        assert (await client.get("/sessions/one/outputs/preview", params={"path": "something"})).status_code == 404


def test_listing_errors_are_not_hidden_as_empty(tmp_path, monkeypatch):
    def failed_walk(root, *, onerror, followlinks):
        onerror(PermissionError("denied"))
        return []
    monkeypatch.setattr("restapi.outputs.os.walk", failed_walk)
    with pytest.raises(PermissionError):
        list_outputs(tmp_path)
