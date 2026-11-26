"""A2A transports native interrupts and resumes the same checkpointed task."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage
from test_a2a_server import a2a_events, client_for, message, rpc, stream_turn, task_of
from test_api import client_for as rest_client_for

from dataagent.server.a2a.agent_executor import HITLContextBuilder
from dataagent.server.app import create_app


@pytest.fixture
def feedback_runtime(runtime):
    return replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={
                "plugins": runtime.settings.plugins.model_copy(update={"enabled": ("data-qa",)}),
            }
        ),
    )


@asynccontextmanager
async def serving(runtime, model, host):
    if host == "standalone":
        async with client_for(runtime, model) as client:
            yield client
        return
    app = create_app(runtime, instance_id="hitl-test", model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8790"
        ) as client:
            yield client


def pending_request(task):
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    data = next(part["data"] for part in task["status"]["message"]["parts"] if "data" in part)
    assert data["kind"] == "dataagent.hitl.request"
    return data


def pending(task):
    return pending_request(task)["interrupts"]


async def get_task(client, task_id):
    return (
        await rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": "query",
                "method": "GetTask",
                "params": {"id": task_id},
            },
        )
    ).json()["result"]


def response_for(task, responses):
    return {
        "jsonrpc": "2.0",
        "id": str(uuid4()),
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": str(uuid4()),
                "contextId": task["contextId"],
                "taskId": task["id"],
                "parts": [
                    {
                        "data": {
                            "kind": "dataagent.hitl.response",
                            "pauseId": pending_request(task)["pauseId"],
                            "responses": responses,
                        }
                    }
                ],
            }
        },
    }


@pytest.mark.parametrize("host", ["standalone", "control-plane"])
@pytest.mark.parametrize("mode", ["blocking", "nonblocking", "streaming"])
async def test_feedback_interrupt_query_and_resume(feedback_runtime, host, mode):
    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": "按含税口径统计吗？"}),
            AIMessage(content="已按人工确认的口径完成。"),
        ]
    )
    async with serving(feedback_runtime, model, host) as client:
        payload = message("请先确认统计口径。", "hitl-feedback")
        if mode == "streaming":
            payload["method"] = "SendStreamingMessage"
            events = a2a_events(await rpc(client, payload))
            paused = events[-1]["statusUpdate"]
            waiting = await get_task(client, paused["taskId"])
            assert pending_request(paused) == pending_request(waiting)
        else:
            payload["params"]["configuration"] = {"returnImmediately": mode == "nonblocking"}
            waiting = task_of(await rpc(client, payload))
        async with asyncio.timeout(5):
            while waiting["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
                await asyncio.sleep(0.01)
                waiting = await get_task(client, waiting["id"])
        (interrupt,) = pending(waiting)
        assert interrupt["value"]["kind"] == "human_feedback"
        assert interrupt["value"]["question"] == "按含税口径统计吗？"
        assert not waiting.get("artifacts")
        assert len(model.requests) == 1
        assert pending(await get_task(client, waiting["id"])) == [interrupt]
        from dataagent.server.sessions import Sessions

        async with Sessions(feedback_runtime.paths.sessions_db) as sessions:
            row = await sessions.get(feedback_runtime.paths.user_id, waiting["contextId"])
        assert row["status"] == "waiting" and row["last_checkpoint_id"]
        resume = response_for(waiting, {interrupt["id"]: "按含税口径。"})
        if mode == "streaming":
            resume["method"] = "SendStreamingMessage"
            events = a2a_events(await rpc(client, resume))
            assert events[0]["task"]["id"] == waiting["id"]
            assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
            assert events[-1]["statusUpdate"]["taskId"] == waiting["id"]
            finished = await get_task(client, waiting["id"])
        else:
            finished = task_of(await rpc(client, resume))
        assert finished["id"] == waiting["id"]
        assert finished["contextId"] == waiting["contextId"]
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert finished["metadata"] == waiting["metadata"]
        assert finished["artifacts"][0]["parts"][0]["text"] == "已按人工确认的口径完成。"
        feedback = next(
            item
            for item in model.requests[-1]
            if getattr(item, "name", None) == "data-qa__request_human_feedback"
        )
        assert feedback.content == "按含税口径。"
        assert len(model.requests) == 2
        repeated = await rpc(client, response_for(waiting, {interrupt["id"]: "重复回答。"}))
        assert "error" in repeated.json()
        assert len(model.requests) == 2


@pytest.fixture
def require_file_approval(monkeypatch):
    import dataagent.assembly.graph as assembly

    build = assembly.create_deep_agent

    def with_approval(**kwargs):
        return build(
            **kwargs,
            interrupt_on={
                "write_file": {"allowed_decisions": ["approve", "reject"]},
            },
        )

    monkeypatch.setattr(assembly, "create_deep_agent", with_approval)


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("subagent", [False, True])
async def test_native_tool_approval_executes_only_when_approved(
    runtime,
    require_file_approval,
    decision,
    subagent,
):
    destination = runtime.paths.for_session("hitl-approval").outputs / "approved.txt"
    model = ScriptedModel(
        responses=[
            *(
                [call("task", {"subagent_type": "general-purpose", "description": "写入文件。"})]
                if subagent
                else []
            ),
            call("write_file", {"file_path": str(destination), "content": "approved content"}),
            *([AIMessage(content="子代理完成。")] if subagent else []),
            AIMessage(content="结束。"),
        ]
    )
    async with serving(runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("写入文件。", "hitl-approval")))
        (interrupt,) = pending(waiting)
        assert interrupt["value"]["action_requests"][0]["name"] == "write_file"
        assert not destination.exists()
        finished = task_of(
            await rpc(
                client,
                response_for(
                    waiting,
                    {
                        interrupt["id"]: {"decisions": [{"type": decision}]},
                    },
                ),
            )
        )
        assert finished["id"] == waiting["id"]
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert destination.exists() == (decision == "approve")
        if destination.exists():
            assert destination.read_text() == "approved content"


async def test_invalid_approval_can_be_corrected_on_the_same_task(runtime, require_file_approval):
    destination = runtime.paths.for_session("hitl-invalid").outputs / "approved.txt"
    model = ScriptedModel(
        responses=[
            call("write_file", {"file_path": str(destination), "content": "approved content"}),
            AIMessage(content="结束。"),
        ]
    )
    async with serving(runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("写入文件。", "hitl-invalid")))
        (interrupt,) = pending(waiting)
        for invalid in ("approve", {"decisions": []}, {"decisions": [{"type": "edit"}]}):
            reply = await rpc(client, response_for(waiting, {interrupt["id"]: invalid}))
            assert reply.json().get("error", {}).get("code") == -32602, reply.json()
            assert pending(await get_task(client, waiting["id"])) == [interrupt]
        assert len(model.requests) == 1
        assert not destination.exists()
        finished = task_of(
            await rpc(
                client,
                response_for(
                    waiting,
                    {
                        interrupt["id"]: {"decisions": [{"type": "approve"}]},
                    },
                ),
            )
        )
        assert finished["id"] == waiting["id"]
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert destination.read_text() == "approved content"


async def test_waiting_a2a_task_keeps_session_readable_and_rejects_rest_turn(feedback_runtime):
    model = ScriptedModel(
        responses=[
            AIMessage(content="已完成普通会话。"),
            call("data-qa__request_human_feedback", {"question": "继续吗？"}),
            AIMessage(content="完成。"),
        ]
    )
    async with rest_client_for(feedback_runtime, model) as (client, app):
        await rpc(client, message("普通会话", "finished"))
        waiting = task_of(await rpc(client, message("先确认。", "hitl-conflict")))
        (interrupt,) = pending(waiting)
        sessions = app.state.sessions
        user = feedback_runtime.paths.user_id
        row = await sessions.get(user, waiting["contextId"])

        listing = (await client.get("/api/v1/sessions")).json()["data"]["sessions"]
        assert {item["threadId"]: item["status"] for item in listing} == {
            "finished": "completed",
            "hitl-conflict": "interrupted",
        }
        detail = (await client.get("/api/v1/sessions/hitl-conflict")).json()["data"]
        assert detail["status"] == "interrupted"
        assert detail["notice"] == "This run is waiting for human input."
        rest = await client.post("/api/v1/runs", json=stream_turn("普通消息", waiting["contextId"]))
        assert rest.status_code == 409
        assert await sessions.get(user, waiting["contextId"]) == row
        assert pending_request(await get_task(client, waiting["id"])) == pending_request(waiting)
        assert len(model.requests) == 2

        finished = task_of(await rpc(client, response_for(waiting, {interrupt["id"]: "继续"})))
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert len(model.requests) == 3


async def test_shared_hitl_feedback_resumes_through_its_original_entrypoint(runtime, common_plugin):
    from test_api import query, sse
    from test_api_hitl import enable_hitl, pending_event

    enable_hitl(common_plugin)
    model = ScriptedModel(
        responses=[
            call("request_human_feedback", {"question": "Which ID?"}, "a2a-ask"),
            AIMessage(content="A2A completed"),
            call("request_human_feedback", {"question": "Which month?"}, "rest-ask"),
            AIMessage(content="REST completed"),
        ]
    )
    thread = "shared-hitl-entrypoint"
    async with rest_client_for(runtime, model) as (client, _):
        waiting = task_of(await rpc(client, message("Confirm ID", thread)))
        (item,) = pending(waiting)
        assert item["value"]["type"] == "human_feedback"
        before = await get_task(client, waiting["id"])
        for invalid in ({"answer": "user_id"}, " "):
            rejected = await rpc(client, response_for(waiting, {item["id"]: invalid}))
            assert rejected.json()["error"]["code"] == -32602
            assert await get_task(client, waiting["id"]) == before
        detail = (await client.get(f"/api/v1/sessions/{thread}")).json()["data"]
        assert detail["pendingInteractions"] == []
        wrong_entry = await client.post(
            "/api/v1/runs", json=query(thread=thread, messages=[], answers={item["id"]: "user_id"})
        )
        assert wrong_entry.status_code == 409
        assert pending_request(await get_task(client, waiting["id"])) == pending_request(waiting)
        finished = task_of(await rpc(client, response_for(waiting, {item["id"]: "user_id"})))
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"

        events = sse(await client.post("/api/v1/runs", json=query("Confirm month", thread)))
        assert events[-1]["metadata"]["status"] == "waiting_for_input"
        (interaction,) = pending_event(events)
        detail = (await client.get(f"/api/v1/sessions/{thread}")).json()["data"]
        assert detail["pendingInteractions"] == [interaction]
        events = sse(
            await client.post(
                "/api/v1/runs",
                json=query(thread=thread, messages=[], answers={interaction["interruptId"]: "11"}),
            )
        )
        assert events[-1]["metadata"]["status"] == "completed"
        assert len(model.requests) == 4


async def test_hitl_conflict_keeps_original_interrupt_for_retry(feedback_runtime):
    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": "继续吗？"}),
            AIMessage(content="完成。"),
        ]
    )
    async with rest_client_for(feedback_runtime, model) as (client, app):
        waiting = task_of(await rpc(client, message("先确认。", "hitl-conflict")))
        (interrupt,) = pending(waiting)
        key = (feedback_runtime.paths.user_id, waiting["contextId"])
        app.state.conversation_busy.add(key)
        try:
            response = await rpc(client, response_for(waiting, {interrupt["id"]: "继续"}))
            retry = task_of(response)
            assert retry["id"] == waiting["id"]
            assert retry["metadata"] == waiting["metadata"]
            assert pending_request(retry) == pending_request(waiting)
            assert "Retry" in retry["status"]["message"]["parts"][0]["text"]
            assert pending_request(await get_task(client, waiting["id"])) == pending_request(
                waiting
            )
            assert len(model.requests) == 1
        finally:
            app.state.conversation_busy.remove(key)
        finished = task_of(await rpc(client, response_for(waiting, {interrupt["id"]: "继续"})))
        assert finished["id"] == waiting["id"]
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert len(model.requests) == 2


async def test_repeated_interrupts_reject_wrong_responses_without_advancing(feedback_runtime):
    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": "第一个问题"}),
            call("data-qa__request_human_feedback", {"question": "第二个问题"}),
            AIMessage(content="完成。"),
        ]
    )
    async with serving(feedback_runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("逐项确认。", "hitl-repeated")))
        (first,) = pending(waiting)
        invalid = await rpc(client, response_for(waiting, {"unknown-interrupt": "错误回答"}))
        assert invalid.json()["error"]["code"] == -32602
        assert len(model.requests) == 1
        ordinary = task_of(await rpc(client, message("不能绕过确认。", waiting["contextId"])))
        assert ordinary["metadata"]["dataagent"]["error"]["code"] == "CONFLICT"
        assert len(model.requests) == 1
        waiting_again = task_of(await rpc(client, response_for(waiting, {first["id"]: "答复一"})))
        (second,) = pending(waiting_again)
        assert waiting_again["id"] == waiting["id"]
        assert second["id"] != first["id"]
        stale = await rpc(client, response_for(waiting_again, {first["id"]: "旧回答"}))
        assert stale.json()["error"]["code"] == -32602
        assert len(model.requests) == 2
        response = response_for(waiting_again, {second["id"]: "答复二"})
        del response["params"]["message"]["contextId"]  # Native A2A infers it from the task.
        finished = task_of(await rpc(client, response))
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert finished["metadata"] == waiting["metadata"]
        assert len(model.requests) == 3


async def test_concurrent_hitl_retry_keeps_the_next_interrupt_resumable(
    feedback_runtime, monkeypatch
):
    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": "第一个问题"}),
            call("data-qa__request_human_feedback", {"question": "第二个问题"}),
            AIMessage(content="完成。"),
        ]
    )
    async with serving(feedback_runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("逐项确认。", "hitl-concurrent")))
        (first,) = pending(waiting)
        both_validated = asyncio.Barrier(2)
        build = HITLContextBuilder.build

        async def build_together(self, **kwargs):
            context = await build(self, **kwargs)
            await both_validated.wait()
            return context

        async with asyncio.timeout(10):
            with monkeypatch.context() as patch:
                patch.setattr(HITLContextBuilder, "build", build_together)
                replies = await asyncio.gather(
                    *(rpc(client, response_for(waiting, {first["id"]: "答复一"})) for _ in range(2))
                )
            waiting_again = task_of(replies[0])
            (second,) = pending(waiting_again)
            assert second["id"] != first["id"]
            assert pending(task_of(replies[1])) == [second]
            finished = task_of(
                await rpc(client, response_for(waiting_again, {second["id"]: "答复二"}))
            )
    assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
    assert finished["id"] == waiting["id"]
    assert len(model.requests) == 3


@pytest.mark.parametrize("partial", [False, True])
async def test_stream_returns_all_parallel_interrupts(feedback_runtime, partial):
    calls = [
        call("data-qa__request_human_feedback", {"question": question}).tool_calls[0]
        for question in ("First?", "Second?")
    ]
    model = ScriptedModel(
        responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")]
    )
    async with serving(feedback_runtime, model, "standalone") as client:
        req = message("ask both", "parallel-questions")
        req["method"] = "SendStreamingMessage"
        events = a2a_events(await rpc(client, req))
        waiting = await get_task(client, events[0]["task"]["id"])
        items = pending(events[-1]["statusUpdate"])
        assert {item["value"]["question"] for item in items} == {"First?", "Second?"}
        if partial:
            first, second = items
            resume = response_for(waiting, {first["id"]: "yes"})
            resume["method"] = "SendStreamingMessage"
            events = a2a_events(await rpc(client, resume))
            items = pending(events[-1]["statusUpdate"])
            assert items == [second]
            waiting = await get_task(client, waiting["id"])
        resume = response_for(waiting, {item["id"]: "yes" for item in items})
        resume["method"] = "SendStreamingMessage"
        events = a2a_events(await rpc(client, resume))
        assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_hitl_resume_does_not_replay_a_completed_parallel_tool(feedback_runtime):
    destination = feedback_runtime.paths.for_session("hitl-no-replay").outputs / "parallel.txt"
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    call(
                        "write_file", {"file_path": str(destination), "content": "首次写入"}
                    ).tool_calls[0],
                    call("data-qa__request_human_feedback", {"question": "继续吗？"}).tool_calls[0],
                ],
            ),
            AIMessage(content="完成。"),
        ]
    )
    async with serving(feedback_runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("写文件并确认。", "hitl-no-replay")))
        (interrupt,) = pending(waiting)
        assert destination.read_text() == "首次写入"
        destination.write_text("等待期间的修改")
        finished = task_of(await rpc(client, response_for(waiting, {interrupt["id"]: "继续"})))
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
    assert destination.read_text() == "等待期间的修改"


@pytest.mark.parametrize("host", ["standalone", "control-plane"])
async def test_waiting_task_can_be_cancelled(feedback_runtime, host):
    from dataagent.server.sessions import Sessions

    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": "继续吗？"}),
        ]
    )
    thread_id = "hitl-cancel"
    user_id = feedback_runtime.paths.user_id
    async with serving(feedback_runtime, model, host) as client:
        waiting = task_of(await rpc(client, message("需要确认。", thread_id)))
        (interrupt,) = pending(waiting)
        async with Sessions(feedback_runtime.paths.sessions_db) as sessions:
            row = await sessions.get(user_id, thread_id)
            assert row["status"] == "waiting"
            assert row["last_checkpoint_id"]
        cancelled = (
            await rpc(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": "cancel",
                    "method": "CancelTask",
                    "params": {"id": waiting["id"]},
                },
            )
        ).json()["result"]
        assert cancelled["status"]["state"] == "TASK_STATE_CANCELED"
        response = await rpc(client, response_for(waiting, {interrupt["id"]: "继续"}))
        assert "error" in response.json()
        assert len(model.requests) == 1
    async with Sessions(feedback_runtime.paths.sessions_db) as sessions:
        saved = await sessions.get(user_id, thread_id)
        assert saved["status"] == "interrupted"
        assert saved["last_checkpoint_id"] == row["last_checkpoint_id"]


async def test_cancel_waits_for_session_cleanup_during_pause(feedback_runtime, monkeypatch):
    from dataagent.server.sessions import Sessions

    waiting_saved = asyncio.Event()
    publish_pause = asyncio.Event()
    finish = Sessions.finish

    async def hold_pause(self, user_id, thread_id, status, checkpoint_id=None):
        await finish(self, user_id, thread_id, status, checkpoint_id)
        if status == "waiting":
            waiting_saved.set()
            await publish_pause.wait()

    monkeypatch.setattr(Sessions, "finish", hold_pause)
    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": "继续吗？"}),
        ]
    )
    thread_id = "hitl-cancel-during-pause"
    user_id = feedback_runtime.paths.user_id
    async with rest_client_for(feedback_runtime, model) as (client, app):
        payload = message("需要确认。", thread_id)
        payload["params"]["configuration"] = {"returnImmediately": True}
        submitted = task_of(await rpc(client, payload))
        try:
            async with asyncio.timeout(5):
                await waiting_saved.wait()
                row = await app.state.sessions.get(user_id, thread_id)
                assert row["status"] == "waiting"
                assert row["last_checkpoint_id"]
                task = await get_task(client, submitted["id"])
                assert task["status"]["state"] == "TASK_STATE_WORKING"
                canceled = (
                    await rpc(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": "cancel",
                            "method": "CancelTask",
                            "params": {"id": task["id"]},
                        },
                    )
                ).json()["result"]
                assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
                saved = await app.state.sessions.get(user_id, thread_id)
                assert saved["status"] == "interrupted"
                assert saved["last_checkpoint_id"] == row["last_checkpoint_id"]
        finally:
            publish_pause.set()


async def test_interrupt_payload_is_complete_and_redacted(feedback_runtime):
    question = "需要确认的内容\n" * 300 + " test-secret"
    model = ScriptedModel(
        responses=[
            call("data-qa__request_human_feedback", {"question": question}),
        ]
    )
    async with serving(feedback_runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("需要确认。", "hitl-payload")))
        (interrupt,) = pending(waiting)
        assert interrupt["value"]["question"] == question.replace("test-secret", "[REDACTED]")


async def test_verify_answer_belongs_to_current_pause_when_interrupt_id_is_reused(
    runtime, monkeypatch
):
    from langchain_core.tools import tool
    from langgraph.types import interrupt

    import dataagent.assembly.graph as assembly

    @tool
    def ask_twice() -> str:
        """Collect year and month with separate interruptions of the same tool."""
        year = interrupt({"kind": "human_feedback", "question": "Which year?"})
        month = interrupt({"kind": "human_feedback", "question": "Which month?"})
        return f"{year}-{month}"

    build = assembly.create_deep_agent

    def with_tool(**kwargs):
        return build(**{**kwargs, "tools": [*kwargs["tools"], ask_twice]})

    monkeypatch.setattr(assembly, "create_deep_agent", with_tool)
    model = ScriptedModel(responses=[call("ask_twice", {}), AIMessage(content="Done")])
    async with serving(runtime, model, "standalone") as client:
        first = task_of(await rpc(client, message("Choose a period", "hitl-reused-id")))
        (first_interrupt,) = pending(first)
        first_reply = {first_interrupt["id"]: "2025"}
        second = task_of(await rpc(client, response_for(first, first_reply)))
        (second_interrupt,) = pending(second)
        assert second_interrupt["id"] == first_interrupt["id"]
        assert second_interrupt["value"]["question"] == "Which month?"
        assert pending_request(second)["pauseId"] != pending_request(first)["pauseId"]

        old = await rpc(client, response_for(first, first_reply))
        assert old.json()["error"]["code"] == -32602
        assert len(model.requests) == 1
        assert pending_request(await get_task(client, first["id"])) == pending_request(second)
        finished = task_of(await rpc(client, response_for(second, {second_interrupt["id"]: "11"})))

    assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [item.content for item in model.requests[-1] if item.type == "tool"] == ["2025-11"]


@pytest.mark.parametrize("record_id", [42, 2**63 + 1])
@pytest.mark.parametrize("decision", ["approve", "edit"])
async def test_pending_approval_redacts_unknown_credential_fields_without_changing_args(
    runtime,
    monkeypatch,
    decision,
    record_id,
):
    from langchain_core.tools import tool

    import dataagent.assembly.graph as assembly

    executed = []

    @tool
    def save_record(data: dict) -> str:
        """Save a record with its metadata."""
        executed.append(data)
        return "Saved"

    build = assembly.create_deep_agent

    def with_approval(**kwargs):
        return build(
            **{
                **kwargs,
                "tools": [*kwargs["tools"], save_record],
                "interrupt_on": {"save_record": {"allowed_decisions": ["approve", "edit"]}},
            }
        )

    monkeypatch.setattr(assembly, "create_deep_agent", with_approval)
    nested_secret = "fixture-unregistered-nested-value"
    record = {
        "id": record_id,
        "connections": [
            {
                "password": "fixture-only-password",
                "credentials": {"opaque": nested_secret},
                "name": "warehouse",
            }
        ],
    }
    model = ScriptedModel(
        responses=[
            call("save_record", {"data": record}),
            AIMessage(content="Done"),
        ]
    )
    async with serving(runtime, model, "standalone") as client:
        task = task_of(await rpc(client, message("Save a record", "hitl-private-args")))
        saved = await get_task(client, task["id"])
        for outward in (task, saved):
            assert nested_secret not in repr(outward)
            assert "fixture-only-password" not in repr(outward)
        (item,) = pending(saved)
        assert pending_request(saved) == pending_request(task)
        (proposal,) = item["value"]["action_requests"]
        assert proposal["name"] == "save_record"
        args = json.loads(proposal["args"]) if record_id >= 2**53 else proposal["args"]
        assert args == {
            "data": {
                "id": record_id,
                "connections": [
                    {
                        "password": "[REDACTED]",
                        "credentials": "[REDACTED]",
                        "name": "warehouse",
                    }
                ],
            }
        }
        assert not executed
        args = {**args, "label": "changed-label"}
        copied = {
            "name": proposal["name"],
            "args": json.dumps(args) if isinstance(proposal["args"], str) else args,
        }
        invalid = response_for(
            task, {item["id"]: {"decisions": [{"type": "edit", "edited_action": copied}]}}
        )
        rejected = await rpc(client, invalid)
        assert rejected.json()["error"]["code"] == -32602
        assert await get_task(client, task["id"]) == saved
        assert not executed
        edited = {"id": record_id + 2, "password": "new-test-password", "name": "new-name"}
        action = {"type": decision}
        if decision == "edit":
            action["edited_action"] = {"name": "save_record", "args": {"data": edited}}
            if record_id >= 2**53:
                invalid = response_for(task, {item["id"]: {"decisions": [action]}})
                rejected = await rpc(client, invalid)
                assert rejected.json()["error"]["code"] == -32602
                assert pending_request(await get_task(client, task["id"])) == pending_request(task)
                action["edited_action"]["args"] = json.dumps({"data": edited})
        response = {item["id"]: {"decisions": [action]}}
        finished = task_of(await rpc(client, response_for(task, response)))
        saved = await get_task(client, task["id"])
        for outward in (finished, saved):
            assert "new-test-password" not in repr(outward)
            if decision == "edit":
                assert "new-name" in repr(outward["history"])
    assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
    assert executed == [edited if decision == "edit" else record]


async def test_native_interrupt_preserves_large_ids_in_both_directions(runtime, monkeypatch):
    from langchain_core.tools import tool
    from langgraph.types import interrupt

    import dataagent.assembly.graph as assembly

    business_id = 2**63 + 1
    received = []

    @tool
    def choose_record() -> str:
        """Ask which record to use."""
        received.append(interrupt({"records": [business_id]}))
        return "Done"

    build = assembly.create_deep_agent
    monkeypatch.setattr(
        assembly,
        "create_deep_agent",
        lambda **kw: build(**{**kw, "tools": [*kw["tools"], choose_record]}),
    )
    model = ScriptedModel(responses=[call("choose_record", {}), AIMessage(content="Done")])
    async with serving(runtime, model, "standalone") as client:
        waiting = task_of(await rpc(client, message("Choose", "large-native-id")))
        (item,) = pending(waiting)
        assert json.loads(item["valueJson"]) == {"records": [business_id]}
        answer = {item["id"]: {"id": business_id + 2, "password": "private-choice"}}
        retry = response_for(waiting, answer)
        invalid = await rpc(client, retry)
        assert invalid.json()["error"]["code"] == -32602
        assert pending_request(await get_task(client, waiting["id"])) == pending_request(waiting)
        part = retry["params"]["message"]["parts"][0]["data"]
        part["responsesJson"] = json.dumps(answer)
        ambiguous = await rpc(client, retry)
        assert ambiguous.json()["error"]["code"] == -32602
        del part["responses"]
        part["responsesJson"] = "{"
        malformed = await rpc(client, retry)
        assert malformed.json()["error"]["code"] == -32602
        assert await get_task(client, waiting["id"]) == waiting
        part["responsesJson"] = json.dumps(answer)
        finished = task_of(await rpc(client, retry))
        assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
        assert "private-choice" not in repr(await get_task(client, waiting["id"]))
        assert received == [answer[item["id"]]]
