"""Real HTTP + DataAgent + file tool + Ink; only the chat model is deterministic.

Run `npm run build:tui` before `uv run pytest tests/test_tui_e2e.py -q`.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import register_and_login
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class TuiSmokeModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "tui-smoke"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        humans = [message for message in messages if isinstance(message, HumanMessage)]
        tool_results = [message for message in messages if isinstance(message, ToolMessage)]
        if len(humans) > 1:
            assert any("TUI_SMOKE_OK" in str(message.content) for message in tool_results)
            answer = AIMessage(content="HISTORY_OK")
        elif tool_results:
            assert "TUI_SMOKE_OK" in str(tool_results[-1].content)
            answer = AIMessage(content="TUI_SMOKE_OK")
        else:
            answer = AIMessage(content="", tool_calls=[{
                "name": "read_workspace_file", "args": {"path": "smoke.txt"}, "id": "read-smoke",
            }])
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        answer = self._generate(messages).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(content=answer.content, tool_calls=answer.tool_calls))


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "snapshot"])
@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
def test_tui_python_tool_and_history_round_trip(client, auth_env, monkeypatch, streaming, local):
    from dataagent.core.deepagents.config import models

    from datafoundry_api.app import create_app
    from datafoundry_api.local_tui import seed_local_session
    from datafoundry_api.settings import Settings

    root = Path(__file__).resolve().parents[3]
    test_path = root / "apps/tui/dist/ui/App.test.js"
    if not test_path.exists() or not shutil.which("node"):
        pytest.skip("Run npm run smoke:tui-python with Node.js installed to build and exercise the TUI.")
    monkeypatch.setattr(models, "init_chat_model", lambda *args, **kwargs: TuiSmokeModel(disable_streaming=not streaming))
    local_env = {}
    if local:
        from dataclasses import replace

        token, csrf = "local-smoke-session" * 4, "local-smoke-csrf" * 4
        seed_local_session(replace(Settings.from_env(os.environ), registration_mode="closed"), token, csrf)
        client.cookies.set("df_session", token)
        client.cookies.set("df_csrf", csrf)
        local_env = {"TUI_SMOKE_LOCAL_TOKEN": token, "TUI_SMOKE_LOCAL_CSRF": csrf}
    else:
        register_and_login(client, email="tui@example.test", password="smoke-password")
    uploaded = client.post("/api/v1/files", data={"sessionId": "fixture"},
                           files=[("file", ("smoke.txt", b"TUI_SMOKE_OK", "text/plain"))])
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["data"]["files"][0]["id"]
    promoted = client.post(f"/api/v1/files/{file_id}/promote")
    assert promoted.status_code == 200, promoted.text

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Settings.from_env(os.environ)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "API did not start"
            time.sleep(0.05)
        result = subprocess.run(
            ["node", "--test", "--test-name-pattern=real Python", str(test_path)],
            cwd=root, env={**os.environ, **local_env, "TUI_SMOKE_API_URL": f"http://127.0.0.1:{port}"},
            capture_output=True, text=True, timeout=75, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
