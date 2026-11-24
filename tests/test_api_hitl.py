import json

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage
from test_api import client_for, query, sse, terminal

from dataagent import read_yaml


def enable_hitl(plugin, **config):
    path = plugin / ".plugin.yaml"
    spec = read_yaml(path)
    spec["hitl"] = {"enabled": True, **config}
    path.write_text(json.dumps(spec))


def pending_event(events):
    return next(
        e["value"] for e in events if e["type"] == "CUSTOM" and e["name"] == "human_feedback"
    )


async def test_pause_restart_resume_and_replay(runtime, common_plugin):
    enable_hitl(common_plugin)
    model = ScriptedModel(
        responses=[
            call("request_human_feedback", {"question": "Which ID?", "options": ["user_id"]}, "ask")
        ]
    )
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == ["RUN_FINISHED"], events
        pending = next(
            e["value"] for e in events if e["type"] == "CUSTOM" and e["name"] == "human_feedback"
        )
        assert pending[0]["type"] == "human_feedback"
        assert events[-1]["metadata"]["status"] == "waiting_for_input"
        assert (await client.post("/api/v1/runs", json=query())).status_code == 409
    async with client_for(runtime, ScriptedModel(responses=[AIMessage(content="done")])) as (
        client,
        app,
    ):
        detail = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert detail["pendingInteractions"] == pending
        body = query(messages=[], answers={pending[0]["interruptId"]: "user_id"})
        events = sse(await client.post("/api/v1/runs", json=body))
        assert terminal(events) == ["RUN_FINISHED"], events
        assert events[-1]["metadata"]["status"] == "completed"
        assert (await client.post("/api/v1/runs", json=body)).status_code == 409
        assert (await client.get("/api/v1/sessions/thread-1")).json()["data"][
            "pendingInteractions"
        ] == []


async def test_invalid_answer_reinterrupts_and_stale_ids_are_rejected(runtime, common_plugin):
    enable_hitl(common_plugin)
    model = ScriptedModel(
        responses=[
            call("request_human_feedback", {"question": "Which ID?"}, "ask"),
            AIMessage(content="done"),
        ]
    )
    async with client_for(runtime, model) as (client, app):
        pending = pending_event(sse(await client.post("/api/v1/runs", json=query())))
        unknown = query(messages=[], answers={"unknown": "user_id"})
        assert (await client.post("/api/v1/runs", json=unknown)).status_code == 409
        injected = query(
            messages=[], answers={pending[0]["interruptId"]: "user_id"}, state={"injected": True}
        )
        assert (await client.post("/api/v1/runs", json=injected)).status_code == 422
        events = sse(
            await client.post(
                "/api/v1/runs", json=query(messages=[], answers={pending[0]["interruptId"]: " "})
            )
        )
        retry = pending_event(events)
        assert retry[0]["validationError"]
        assert events[-1]["metadata"]["status"] == "waiting_for_input"
        events = sse(
            await client.post(
                "/api/v1/runs",
                json=query(messages=[], answers={retry[0]["interruptId"]: "user_id"}),
            )
        )
        assert events[-1]["metadata"]["status"] == "completed"
        assert (
            await client.post(
                "/api/v1/runs", json=query(messages=[], answers={retry[0]["interruptId"]: "again"})
            )
        ).status_code == 409


@pytest.mark.parametrize("at", ["before_tool", "after_tool"])
async def test_streaming_tool_guards_allow_partial_parallel_resumes(runtime, common_plugin, at):
    (common_plugin / "hitl_handler.py").write_text(
        "from dataagent import HITLHandler, ResumeDecision\n"
        "class Handler(HITLHandler):\n"
        "    def resume_handler(self, context, *, params):\n"
        '        return ResumeDecision(action="continue")\n'
    )
    enable_hitl(
        common_plugin,
        policies=[
            {
                "id": "approval",
                "at": at,
                "matcher": "common__summarize_numbers",
                "handler": "hitl_handler.py:Handler",
                "params": {"question": "Approve statistics?"},
            }
        ],
    )
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    call("common__summarize_numbers", {"numbers": [1, 3]}, "one").tool_calls[0],
                    call("common__summarize_numbers", {"numbers": [2, 4]}, "two").tool_calls[0],
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == ["RUN_FINISHED"], events
        pending = pending_event(events)
        assert len(pending) == 2
        events = sse(
            await client.post(
                "/api/v1/runs", json=query(messages=[], answers={pending[0]["interruptId"]: "yes"})
            )
        )
        assert terminal(events) == ["RUN_FINISHED"], events
        remaining = pending_event(events)
        assert [item["interruptId"] for item in remaining] == [pending[1]["interruptId"]]
        assert events[-1]["metadata"]["status"] == "waiting_for_input"
        events = sse(
            await client.post(
                "/api/v1/runs",
                json=query(messages=[], answers={remaining[0]["interruptId"]: "yes"}),
            )
        )
        assert terminal(events) == ["RUN_FINISHED"], events
        assert events[-1]["metadata"]["status"] == "completed"
        snapshot = next(e["messages"] for e in events if e["type"] == "MESSAGES_SNAPSHOT")
        results = [
            m for m in snapshot if m["role"] == "tool" and m.get("toolCallId") in {"one", "two"}
        ]
        assert len(results) == 2
        assert {json.loads(m["content"])["mean"] for m in results} == {2, 3}
