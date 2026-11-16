"""Real SQLite checkpoints and AG-UI resume, through the shared Web/TUI API."""

import json

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage
from test_api import client_for, query, sse, terminal

from dataagent.bootstrap import LaunchOptions, prepare_runtime

NAME = "de-agent__request_human_feedback"
ARGS = {"reason": "口径缺失", "pending_action": "统计近 7 天还是近 30 天？"}


@pytest.fixture
def hitl_runtime(tmp_path, runtime):
    config = tmp_path / "hitl.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "unused",
                    }
                },
                "plugins": {
                    "enabled": ["de-agent"],
                    "config": {
                        "de-agent": {"hitl": {"enabled": True}, "dataops": {"enabled": False}}
                    },
                },
            }
        )
    )
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))


async def test_api_wait_restart_and_plain_text_resume(hitl_runtime):
    async with client_for(hitl_runtime, ScriptedModel(responses=[call(NAME, ARGS, "ask")])) as (
        client,
        _,
    ):
        events = sse(await client.post("/dataagent/stream", json=query("统计活跃用户")))
        assert terminal(events) == ["RUN_FINISHED"]
        finish = events[-1]
        assert finish["metadata"]["status"] == "waiting_for_input"
        detail = (await client.get("/sessions/thread-1")).json()
        assert detail["status"] == "waiting_for_input"
        assert ARGS["pending_action"] in detail["messages"][-1]["content"]
        pending_id = detail["pendingInterrupts"][0]["interruptId"]
        stale = await client.post(
            "/dataagent/stream",
            json=query(
                messages=[],
                resume=[{"interruptId": "stale", "status": "resolved", "payload": "近7天"}],
            ),
        )
        assert stale.status_code == 409
        assert (await client.get("/sessions/thread-1")).json()["status"] == "waiting_for_input"
    # Rebuild server/agent/cache, keeping only SQLite files.
    async with client_for(
        hitl_runtime, ScriptedModel(responses=[AIMessage(content="按近7天执行")])
    ) as (client, app):
        detail = (await client.get("/sessions/thread-1")).json()
        assert detail["pendingInterrupts"][0]["interruptId"] == pending_id
        events = sse(await client.post("/dataagent/stream", json=query("近7天")))
        assert terminal(events) == ["RUN_FINISHED"]
        detail = (await client.get("/sessions/thread-1")).json()
        assert detail["status"] == "complete" and detail["pendingInterrupts"] == []
        tools = [m for m in detail["messages"] if m["role"] == "tool"]
        assert len(tools) == 1 and tools[0]["content"] == "近7天"
        _, agent, _ = await app.state.agents.get("thread-1", create=False)
        snapshot = await agent.graph.aget_state({"configurable": {"thread_id": "thread-1"}})
        assert snapshot.values["hitl"]["count"] == 1
        # Reusing a completed interrupt cannot add another answer.
        repeated = await client.post(
            "/dataagent/stream",
            json=query(
                messages=[],
                resume=[{"interruptId": pending_id, "status": "resolved", "payload": "近30天"}],
            ),
        )
        assert repeated.status_code == 409


async def test_explicit_resume_and_cross_session_rejection(hitl_runtime):
    async with client_for(
        hitl_runtime, ScriptedModel(responses=[call(NAME, ARGS, "ask"), AIMessage(content="继续")])
    ) as (client, _):
        sse(await client.post("/dataagent/stream", json=query()))
        detail = (await client.get("/sessions/thread-1")).json()
        entry = {
            "interruptId": detail["pendingInterrupts"][0]["interruptId"],
            "status": "resolved",
            "payload": "近7天",
        }
        other = await client.post(
            "/dataagent/stream", json=query(thread="other", messages=[], resume=[entry])
        )
        assert other.status_code == 409
        events = sse(
            await client.post("/dataagent/stream", json=query(messages=[], resume=[entry]))
        )
        assert terminal(events) == ["RUN_FINISHED"]
        assert (await client.get("/sessions/thread-1")).json()["status"] == "complete"
