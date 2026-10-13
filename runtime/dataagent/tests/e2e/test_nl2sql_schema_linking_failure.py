"""Real DeepSeek main-agent regression with deliberate metadata HTTP faults."""

from __future__ import annotations

import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml
from langchain_core.messages import AIMessage, ToolMessage

from dataagent.interface.sdk.agent import DataAgent


@pytest.mark.parametrize("metadata_status", [200, 503])
@pytest.mark.asyncio
async def test_main_agent_stops_after_schema_linking_failure(
    metadata_status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real main-agent/tool/subprocess execution must explain the blocker after one NL2SQL call."""
    if os.getenv("RUN_DEEPSEEK_E2E") != "1":
        pytest.skip("Set RUN_DEEPSEEK_E2E=1 to enable real DeepSeek validation.")
    if not os.getenv("DEEPSEEK_BASE_URL") or not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required.")

    requests: list[str] = []

    class MetadataFaultHandler(BaseHTTPRequestHandler):
        """Inject an empty catalog or service outage over real HTTP, without replacing any LLM."""

        def do_GET(self) -> None:
            """Return controlled metadata failures and record retrieval attempts."""
            requests.append(self.path)
            payload = [] if metadata_status == 200 else {"message": "metadata unavailable"}
            body = json.dumps(payload).encode()
            self.send_response(metadata_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            """Suppress the HTTP server's unrelated access logging."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), MetadataFaultHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "runtime-home"))
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "false")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = {
        "AGENT_CONFIG": {"name": "Schema failure regression", "type": "react", "backend": "langgraph"},
        "WORKSPACE": {"path": str(workspace)},
        "MODEL": {
            "chat_model": {
                "model_type": "chat",
                "provider": "deepseek",
                "params": {
                    "model": "deepseek-v4-flash",
                    "base_url": "$env{DEEPSEEK_BASE_URL}",
                    "api_key": "$env{DEEPSEEK_API_KEY}",
                    "temperature": 0,
                },
            }
        },
        "SCENARIO": {"chat": {"instructions": "使用 NL2SQL 工具完成数据库查询；如果无法完成，如实说明原因。"}},
        "TOOLS": {
            "local_functions": [
                {
                    "module": "dataagent.actions.tools.local_tool.tools",
                    "function": "nl2sql_sub_agent_tool",
                    "config": {
                        "llm_model": "chat_model",
                        "core_config": {"perceptor": {"schema_mode": "schema_linking"}},
                    },
                }
            ]
        },
        "DATABASE": {"db_id": "orders", "dialect": "sqlite", "config": {"path": ""}},
        "SEMANTIC_LAYER": {"base_url": f"http://127.0.0.1:{server.server_port}", "timeout": 2},
    }
    config_path = tmp_path / "main.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    try:
        agent = DataAgent.from_config(config_path)
        result = await asyncio.wait_for(agent.chat("查询订单总数，保存 SQL 和 CSV，并告诉我结果。"), timeout=180)
        messages = result.get("messages", [])
        calls = [
            call
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if "nl2sql_sub_agent_tool" in call.get("name", "")
        ]
        errors = [message for message in messages if isinstance(message, ToolMessage) and message.status == "error"]
        assert len(calls) == 1, f"NL2SQL was called {len(calls)} times"
        assert len(errors) == 1
        assert "停止重复调用 NL2SQL" in str(errors[0].content)
        assert len([path for path in requests if "semantic-search-columns" in path]) == 1
        assert result.get("complete") is True
        reply = str(messages[-1].content)
        assert "元数据" in reply or "表结构" in reply or "schema" in reply.lower()
        assert not list(workspace.rglob("*.sql"))
        assert not list(workspace.rglob("*.csv"))
        print(
            json.dumps(
                {"metadata_status": metadata_status, "nl2sql_calls": len(calls), "reply": reply}, ensure_ascii=False
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
