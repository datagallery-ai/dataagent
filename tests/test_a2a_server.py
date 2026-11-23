import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from dataagent.server.a2a import create_a2a_server
from dataagent.server.a2a.agent_executor import AgentKitExecutor, final_text
from dataagent.server.app import create_app


def message(text, context_id):
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": str(uuid4()),
                "contextId": context_id,
                "parts": [{"text": text}],
            }
        },
    }


def rpc(client, payload, **kwargs):
    headers = {"A2A-Version": "1.0", **kwargs.pop("headers", {})}
    return client.post("/a2a/jsonrpc", json=payload, headers=headers, **kwargs)


def task_of(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]["task"]


@asynccontextmanager
async def client_for(runtime, model, **kwargs):
    app = create_a2a_server(runtime, host="127.0.0.1", port=9999, model=model, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
        ) as client:
            yield client


def test_agent_card_publishes_a_dialable_url():
    from a2a.server.request_handlers.response_helpers import agent_card_to_dict

    from dataagent.server.a2a.agent_card import build_agent_card

    def published(host):
        card = agent_card_to_dict(build_agent_card(host=host, port=9999))
        return card["supportedInterfaces"][0]["url"]

    assert published("127.0.0.1") == "http://127.0.0.1:9999/a2a/jsonrpc"
    assert published("0.0.0.0") == "http://127.0.0.1:9999/a2a/jsonrpc"
    assert published("::") == "http://127.0.0.1:9999/a2a/jsonrpc"
    assert published("::0") == "http://127.0.0.1:9999/a2a/jsonrpc"
    assert published("[::]") == "http://127.0.0.1:9999/a2a/jsonrpc"
    assert published("::1") == "http://[::1]:9999/a2a/jsonrpc"
    assert published("[::1]") == "http://[::1]:9999/a2a/jsonrpc"
    assert published("10.0.0.8") == "http://10.0.0.8:9999/a2a/jsonrpc"


def test_final_text_prefers_the_last_ai_message():
    assert (
        final_text(
            {
                "messages": [
                    AIMessage(content="older"),
                    AIMessage(content=[{"type": "text", "text": "newer"}]),
                ]
            }
        )
        == "newer"
    )


async def test_agent_card_uses_the_request_host(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as client:
        response = await client.get(
            "/.well-known/agent-card.json", headers={"host": "agents.example:9999"}
        )
    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "DataAgent"
    assert card["skills"][0]["id"] == "chat"
    assert card["capabilities"]["streaming"] is False
    interfaces = card["supportedInterfaces"]
    assert len(interfaces) == 1
    assert interfaces[0]["protocolBinding"] == "JSONRPC"
    assert interfaces[0]["protocolVersion"] == "1.0"
    assert interfaces[0]["url"] == "http://agents.example:9999/a2a/jsonrpc"
    assert "securitySchemes" not in card
    assert "securityRequirements" not in card
    assert "test-secret" not in response.text


async def test_blank_message_and_unsafe_context_fail_without_calling_the_model(runtime):
    model = ScriptedModel(responses=[])
    async with client_for(runtime, model) as client:
        blank = task_of(await rpc(client, message("   ", "thread-a2a-3")))
        unsafe = task_of(await rpc(client, message("hi", "bad/context")))
    assert blank["status"]["state"] == "TASK_STATE_FAILED"
    assert "No user message" in blank["status"]["message"]["parts"][0]["text"]
    assert unsafe["status"]["state"] == "TASK_STATE_FAILED"
    assert "context id" in unsafe["status"]["message"]["parts"][0]["text"]
    assert model.requests == []


async def test_invocation_failure_redacts_secrets(runtime):
    async def boom(_thread_id, _text):
        raise RuntimeError("upstream returned test-secret")

    executor = AgentKitExecutor(boom, secrets=("test-secret",))
    async with client_for(runtime, ScriptedModel(responses=[]), executor=executor) as client:
        task = task_of(await rpc(client, message("hello", "thread-a2a-5")))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    text = task["status"]["message"]["parts"][0]["text"]
    assert "test-secret" not in text
    assert "[REDACTED]" in text


@pytest.mark.parametrize("explicit_token", [None, "secret-token"])
async def test_bearer_token_protects_invocation_and_not_discovery(runtime, explicit_token):
    secured = replace(runtime, a2a_auth_token="environment-token")
    token = explicit_token or secured.a2a_auth_token
    model = ScriptedModel(responses=[AIMessage(content="ok")])
    async with client_for(secured, model, auth_token=explicit_token) as client:
        denied = await rpc(client, message("hello", "thread-a2a-4"))
        opened = await client.get("/.well-known/agent-card.json")
        accepted = await rpc(
            client,
            message("hello", "thread-a2a-4"),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"
    assert opened.status_code == 200
    card = opened.json()
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]
    assert "environment-token" not in opened.text
    assert "secret-token" not in opened.text
    assert task_of(accepted)["status"]["state"] == "TASK_STATE_COMPLETED"
    assert token not in accepted.text


async def test_bearer_token_applies_under_a_mount_prefix(runtime):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    model = ScriptedModel(responses=[AIMessage(content="ok")])
    app = create_a2a_server(
        runtime, host="127.0.0.1", port=9999, model=model, auth_token="secret-token"
    )
    gateway = Starlette(routes=[Mount("/gateway", app=app)])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://127.0.0.1:9999"
        ) as client:
            headers = {"A2A-Version": "1.0"}
            denied = await client.post(
                "/gateway/a2a/jsonrpc", json=message("hello", "thread-a2a-mount"), headers=headers
            )
            opened = await client.get("/gateway/.well-known/agent-card.json")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"
    assert opened.status_code == 200


async def test_bearer_token_strips_root_path_only_on_a_segment_boundary(runtime):
    from fastapi import FastAPI

    model = ScriptedModel(responses=[AIMessage(content="ok")])
    app = create_a2a_server(
        runtime, host="127.0.0.1", port=9999, model=model, auth_token="secret-token"
    )
    gateway = FastAPI(root_path="/a")
    gateway.mount("/", app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://127.0.0.1:9999"
        ) as client:
            headers = {"A2A-Version": "1.0"}
            for path in ("/a2a/jsonrpc", "/a/a2a/jsonrpc"):
                denied = await client.post(
                    path, json=message("hello", f"thread{path}"), headers=headers
                )
                assert denied.status_code == 401, path
    assert model.requests == []


def test_serve_a2a_help_has_no_legacy_bindings(capsys):
    from dataagent.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["serve-a2a", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "--auth-token" in help_text
    assert "--jsonrpc-path" not in help_text
    assert "--rest-path" not in help_text
    assert "--enable-v0-3-compat" not in help_text


class _GatedModel(ScriptedModel):
    """Block inside the first model call until the test releases it."""

    entered: asyncio.Event = None
    release: asyncio.Event = None

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


async def test_stream_cannot_enter_a_session_while_a2a_is_running(runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        responses=[AIMessage(content="from-a2a"), AIMessage(content="from-gui")],
        entered=entered,
        release=release,
    )
    app = create_app(runtime, instance_id="test-instance", model=model)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8790") as a2a_client,
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8790") as gui_client,
        ):
            a2a_task = asyncio.create_task(rpc(a2a_client, message("from a2a", "shared-thread")))
            await asyncio.wait_for(entered.wait(), timeout=5)
            rejected = await gui_client.post(
                "/api/v1/runs", json=stream_turn("from gui", "shared-thread")
            )
            assert rejected.status_code == 409
            assert "already active" in rejected.text
            assert len(model.requests) == 0
            release.set()
            completed = task_of(await a2a_task)
            follow = await gui_client.post(
                "/api/v1/runs", json=stream_turn("from gui", "shared-thread")
            )
            assert follow.status_code == 200
            await follow.aread()
    assert completed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert completed["artifacts"][0]["parts"][0]["text"] == "from-a2a"
    assert len(model.requests) == 2


def stream_turn(text, thread_id):
    return {
        "threadId": thread_id,
        "runId": str(uuid4()),
        "state": {},
        "messages": [{"id": str(uuid4()), "role": "user", "content": text}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


async def test_a2a_checkpoint_record_matches_the_stream(runtime):
    class FailThird(ScriptedModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if len(self.requests) == 2:
                self.requests.append(messages)
                raise RuntimeError("model exploded")
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    model = FailThird(
        responses=[
            AIMessage(content="saved answer"),
            AIMessage(content="from-a2a"),
            AIMessage(content="continued"),
        ]
    )
    thread_id = "thread-a2a-archive"
    app = create_app(runtime, instance_id="test-instance", model=model)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8790"
        ) as client:
            saved = await client.post("/api/v1/runs", json=stream_turn("saved", thread_id))
            assert saved.status_code == 200
            assert "RUN_FINISHED" in saved.text
            noted = task_of(await rpc(client, message("CODE-7", thread_id)))
            assert noted["status"]["state"] == "TASK_STATE_COMPLETED"
            assert "saved" in [item.content for item in model.requests[1]]
            failed = task_of(await rpc(client, message("BLUE-42", thread_id)))
            assert failed["status"]["state"] == "TASK_STATE_FAILED"
            restored = (await client.get(f"/api/v1/sessions/{thread_id}")).json()["data"]
            assert restored["status"] == "error"
            contents = [item.get("content") for item in restored["messages"]]
            assert "saved" in contents and "CODE-7" in contents and "BLUE-42" in contents
            follow = await client.post("/api/v1/runs", json=stream_turn("what was it", thread_id))
            assert follow.status_code == 200
            assert "RUN_FINISHED" in follow.text
            seen = [item.content for item in model.requests[-1]]
            assert "BLUE-42" in seen and "CODE-7" in seen and "saved" in seen


async def test_other_profile_cannot_read_the_same_context(tmp_path, common_plugin):
    import json

    from dataagent import LaunchOptions, prepare_runtime

    path = tmp_path / "test-config.yaml"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test-model",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "test-secret",
                    }
                },
                "plugins": {"enabled": ["common"]},
            }
        )
    )
    alice = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path, user_id="alice"))
    bob = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path, user_id="bob"))
    alice_model = ScriptedModel(responses=[AIMessage(content="alice-answer")])
    bob_model = ScriptedModel(responses=[AIMessage(content="bob-should-not-run")])
    async with client_for(alice, alice_model) as client:
        first = task_of(await rpc(client, message("secret history", "shared-context")))
    async with client_for(bob, bob_model) as client:
        second = task_of(await rpc(client, message("read it", "shared-context")))
    assert first["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second["status"]["state"] == "TASK_STATE_FAILED"
    assert "does not belong to user" in second["status"]["message"]["parts"][0]["text"]
    assert bob_model.requests == []


async def test_serve_uses_shared_token_only_on_jsonrpc(runtime):
    secured = replace(runtime, a2a_auth_token="shared-token")
    assert "shared-token" in secured.redaction_secrets
    app = create_app(
        secured, instance_id="test", model=ScriptedModel(responses=[AIMessage(content="ok")])
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8790"
        ) as client:
            denied = await rpc(client, message("hello", "thread-shared"))
            accepted = await rpc(
                client,
                message("hello", "thread-shared"),
                headers={"Authorization": "Bearer shared-token"},
            )
            assert (await client.get("/healthz")).status_code == 200
            opened = await client.get("/.well-known/agent-card.json")
            assert opened.status_code == 200
            card = opened.json()
            assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
            assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]
            assert "shared-token" not in opened.text
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"
    assert task_of(accepted)["status"]["state"] == "TASK_STATE_COMPLETED"


def test_a2a_token_comes_from_effective_environment(tmp_path, monkeypatch):
    from test_launch import MODEL, config

    from dataagent import LaunchOptions, prepare_runtime

    monkeypatch.delenv("DATAAGENT_A2A_AUTH_TOKEN", raising=False)
    cli = config(tmp_path / "explicit.yaml", MODEL)
    env_file = tmp_path / "token.env"
    env_file.write_text("DATAAGENT_A2A_AUTH_TOKEN=from-dotenv\n")
    options = LaunchOptions(cwd=tmp_path, config=cli, env_file=env_file)
    assert prepare_runtime(options).a2a_auth_token == "from-dotenv"
    monkeypatch.setenv("DATAAGENT_A2A_AUTH_TOKEN", "from-process")
    assert prepare_runtime(options).a2a_auth_token == "from-process"
    assert prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli)).a2a_auth_token == "from-process"


def test_serve_a2a_defaults_to_configured_listen_address(runtime, monkeypatch):
    import dataagent.cli as cli
    import dataagent.server.a2a as a2a_server
    from dataagent.config.settings import ServerSettings

    configured = replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={"server": ServerSettings(host="127.0.0.1", port=8801)}
        ),
        a2a_auth_token="env-token",
    )
    seen = {}
    monkeypatch.setattr(cli, "check_port", lambda host, port: None)
    monkeypatch.setattr(cli, "configure_logging", lambda runtime: None)
    monkeypatch.setattr(a2a_server, "run_a2a_server", lambda rt, **kw: seen.update(kw))

    cli.serve_a2a(configured, cli._parser().parse_args(["serve-a2a"]))
    assert seen == {"host": "127.0.0.1", "port": 8801, "auth_token": "env-token"}
    cli.serve_a2a(
        configured,
        cli._parser().parse_args(["serve-a2a", "--port", "9000", "--auth-token", "cli-token"]),
    )
    assert seen == {"host": "127.0.0.1", "port": 9000, "auth_token": "cli-token"}
