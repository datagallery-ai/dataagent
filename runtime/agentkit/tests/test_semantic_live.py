"""Real DeepSeek Agent acceptance against local and opt-in deployed semantic services.

DATAAGENT_SEMANTIC_LIVE_ENV=/absolute/path/to/.env uv run pytest tests/test_semantic_live.py -q
The fixture verifies client transport and injection, not a deployed semantic-service implementation.
Set DATAAGENT_SEMANTIC_LIVE_URL as well to exercise a deployed service's read-only typedefs API.
"""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from dataagent import LaunchOptions, create_agent, prepare_runtime
from restapi.app import create_app


@pytest.mark.live
def test_real_deepseek_reads_semantic_context_after_plugin_refresh(tmp_path):
    """Exercise JSON bootstrap, a real HTTP client, Hook injection, LLM, SSE and shutdown."""
    env_path = os.environ.get("DATAAGENT_SEMANTIC_LIVE_ENV")
    if not env_path:
        pytest.skip("Set DATAAGENT_SEMANTIC_LIVE_ENV to a real DeepSeek credential file")
    marker = "SEMANTIC_CHECK_" + uuid4().hex[:12]
    requests = []

    class SemanticEndpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            """Serve a synthetic semantic context over an actual HTTP connection."""
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append((self.path, payload))
            data = json.dumps({"verification_code": marker}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            """Keep provider and test diagnostics free of request payloads."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), SemanticEndpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plugin = tmp_path / "semantic-probe"
    plugin.mkdir()
    hook = {
        "event": "before_model",
        "entrypoint": "hook.py:handle",
        "params": {"query": "probe-one"},
    }
    manifest = plugin / ".plugin.json"
    manifest.write_text(json.dumps({"schema_version": 2, "id": plugin.name, "hooks": [hook]}))
    (plugin / "hook.py").write_text(
        "import asyncio\n"
        "from langchain_core.messages import SystemMessage\n"
        "async def handle(state, runtime, *, params):\n"
        '    """Inject the semantic result through the shared client."""\n'
        '    result = await asyncio.to_thread(runtime.context.semantic_client.semantic_retrieve, params.get("query"))\n'
        '    code = result.get("verification_code")\n'
        '    return {"messages": [SystemMessage(content=f"语义服务校验码：{code}。只回复校验码，不调用工具。", id="semantic-context")]}\n'
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": os.environ.get("DATAAGENT_SEMANTIC_LIVE_MODEL", "deepseek-chat"),
                        "base_url": "$env{DEEPSEEK_BASE_URL}",
                        "api_key": "$env{DEEPSEEK_API_KEY}",
                    }
                },
                "dataagent": {"limits": {"timeout_seconds": 120, "model_calls_per_agent": 3}},
                "plugins": {"enabled": [plugin.name], "paths": [str(plugin)]},
                "semantic": {"base_url": f"http://127.0.0.1:{server.server_port}", "timeout": 5},
            }
        )
    )
    try:
        runtime = prepare_runtime(
            LaunchOptions(cwd=tmp_path, config=config, env_file=Path(env_path))
        )
        app = create_app(runtime)
        with TestClient(app) as http:
            client = app.state.agents.semantic_client
            initial_revision = app.state.agents.revision
            contexts = []
            for index in range(2):
                if index:
                    hook.get("params").update(query="probe-two")
                    manifest.write_text(
                        json.dumps({"schema_version": 2, "id": plugin.name, "hooks": [hook]})
                    )
                body = {
                    "threadId": f"semantic-{index}",
                    "runId": f"run-{index}",
                    "state": {},
                    "messages": [
                        {
                            "id": f"message-{index}",
                            "role": "user",
                            "content": "请只回复语义服务校验码。",
                        }
                    ],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {},
                }
                response = http.post("/dataagent/stream", json=body)
                assert response.status_code == 200
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                errors = [event for event in events if event.get("type") == "RUN_ERROR"]
                assert not errors, errors
                assert any(event.get("type") == "RUN_FINISHED" for event in events)
                snapshots = [event for event in events if event.get("type") == "MESSAGES_SNAPSHOT"]
                assert snapshots
                answers = [
                    message.get("content", "")
                    for message in snapshots[-1].get("messages", [])
                    if message.get("role") == "assistant"
                ]
                assert any(marker in str(answer) for answer in answers)
                assert app.state.agents.semantic_client is client
                assert not client.client.is_closed
                context = next(iter(app.state.agents.cache.values())).context
                assert context.semantic_client is client
                contexts.append(context)
            assert app.state.agents.revision != initial_revision
            assert contexts[0] is not contexts[1]
        assert client.client.is_closed
        assert {payload.get("query") for _, payload in requests} == {"probe-one", "probe-two"}
        assert all(path == "/api/semantic/v1/semantic/retrieve" for path, _ in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.live
def test_real_deepseek_reads_deployed_semantic_types_without_plugins(tmp_path):
    """Load a deployed service from Agent JSON and consume it in an independent Hook."""
    env_path = os.environ.get("DATAAGENT_SEMANTIC_LIVE_ENV")
    base_url = os.environ.get("DATAAGENT_SEMANTIC_LIVE_URL")
    if not env_path or not base_url:
        pytest.skip("Set DATAAGENT_SEMANTIC_LIVE_ENV and DATAAGENT_SEMANTIC_LIVE_URL")
    observation = tmp_path / "semantic-observation.json"
    (tmp_path / "hook.py").write_text(
        dedent('''\
            import asyncio
            import json
            from pathlib import Path

            from langchain_core.messages import SystemMessage


            async def handle(state, runtime, *, params):
                """Read real typedefs using the client created from Agent configuration."""
                result = await asyncio.to_thread(runtime.context.semantic_client.get, "types/typedefs")
                vertices = result.get("vertexTypes", [])
                edges = result.get("edgeTypes", [])
                summary = {
                    "vertex_count": len(vertices),
                    "edge_count": len(edges),
                    "first_vertex_type": next(iter(vertices), None),
                    "first_edge_type": next(iter(edges), None),
                }
                record = {"response": result, "summary": summary}
                Path(params.get("observation_file")).write_text(json.dumps(record))
                message = SystemMessage(
                    content="Semantic service observed JSON: " + json.dumps(summary),
                    id="semantic-context",
                )
                return {"messages": [message]}
            '''),
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": os.environ.get("DATAAGENT_SEMANTIC_LIVE_MODEL", "deepseek-chat"),
                        "base_url": "$env{DEEPSEEK_BASE_URL}",
                        "api_key": "$env{DEEPSEEK_API_KEY}",
                    }
                },
                "dataagent": {
                    "limits": {"timeout_seconds": 120, "model_calls_per_agent": 3},
                    "hooks": [
                        {
                            "event": "before_model",
                            "entrypoint": "hook.py:handle",
                            "params": {"observation_file": str(observation)},
                        }
                    ],
                },
                "plugins": {"enabled": [], "paths": []},
                "semantic": {"base_url": base_url, "timeout": 15},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config, env_file=Path(env_path)))
    assert runtime.settings.semantic.base_url == base_url
    assert not runtime.settings.plugins.enabled
    app = create_app(runtime)
    body = {
        "threadId": "deployed-semantic",
        "runId": "deployed-semantic-run",
        "state": {},
        "messages": [
            {
                "id": "deployed-semantic-message",
                "role": "user",
                "content": "请原样回复语义服务观测结果的 JSON 对象。只输出 JSON，不加代码块，不调用工具。",
            }
        ],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with TestClient(app) as http:
        client = app.state.agents.semantic_client
        assert client is not None
        response = http.post("/dataagent/stream", json=body)
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        errors = [event for event in events if event.get("type") == "RUN_ERROR"]
        assert not errors, errors
        event_types = [event.get("type") for event in events]
        assert "RUN_FINISHED" in event_types
        assert "TOOL_CALL_START" not in event_types
        snapshots = [event for event in events if event.get("type") == "MESSAGES_SNAPSHOT"]
        assert snapshots
        answers = [
            message.get("content", "")
            for message in snapshots[-1].get("messages", [])
            if message.get("role") == "assistant"
        ]
        assert answers
        answer = json.loads(answers[-1])
        observed = json.loads(observation.read_text())
        assert observed.get("summary", {}).get("vertex_count", 0) > 0
        assert observed.get("summary", {}).get("edge_count", 0) > 0
        assert answer == observed.get("summary")
        assert app.state.agents.semantic_client is client
        assert not client.client.is_closed
    assert client.client.is_closed
    report = {
        "backend": client.base_url,
        "model": runtime.model_name,
        "enabled_plugins": list(runtime.settings.plugins.enabled),
        "observed_summary": observed.get("summary"),
        "model_answer": answer,
        "sse_event_types": sorted(set(event_types)),
        "client_closed_on_shutdown": client.client.is_closed,
    }
    (tmp_path / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.live
@pytest.mark.parametrize("host", ["sdk", "rest"])
async def test_real_deployed_context_reaches_child_hooks_and_tools(tmp_path, host):
    """Use real delegation, Hooks, a local tool, DeepSeek and a deployed semantic backend."""
    env_path = os.environ.get("DATAAGENT_SEMANTIC_LIVE_ENV")
    base_url = os.environ.get("DATAAGENT_SEMANTIC_LIVE_URL")
    if not env_path or not base_url:
        pytest.skip("Set DATAAGENT_SEMANTIC_LIVE_ENV and DATAAGENT_SEMANTIC_LIVE_URL")
    plugin = tmp_path / "semantic-consumer"
    plugin.mkdir()
    (plugin / "probe.py").write_text(
        dedent('''\
        import json
        from pathlib import Path
        from uuid import uuid4

        from langchain.tools import ToolRuntime
        from dataagent import ModelContext


        def _record(context, scope, **extra):
            client = context.semantic_client
            assert client is not None and not client.client.is_closed
            record = {"scope": scope, "context_id": id(context), "client_id": id(client), **extra}
            with Path(__file__).with_name("observations.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\\n")


        def handle(state, runtime, *, params):
            """Observe the native state Hook context without additional parameters."""
            _record(runtime.context, params.get("scope"))


        def before_tool(request, *, params):
            """Observe the native tool request's context."""
            _record(request.runtime.context, "before_tool")


        def after_tool(request, result, *, params):
            """Verify the context is retained after tool execution."""
            _record(request.runtime.context, "after_tool")


        def inspect_types(runtime: ToolRuntime[ModelContext]) -> str:
            """Read live semantic type counts and return a JSON verification record."""
            payload = runtime.context.semantic_client.get("types/typedefs")
            summary = {
                "vertex_count": len(payload.get("vertexTypes", [])),
                "edge_count": len(payload.get("edgeTypes", [])),
                "verification": uuid4().hex,
            }
            _record(runtime.context, "tool", summary=summary,
                    model=runtime.context.get_model().model_name)
            return json.dumps(summary)
        '''),
        encoding="utf-8",
    )
    (plugin / "root.md").write_text(
        "必须使用 task 将用户任务交给 semantic-inspector。不要自己调用 inspect_types。"
        "子 Agent 完成后，原样返回它给出的 JSON 对象，只输出 JSON，不使用 Markdown 代码块。"
    )
    (plugin / "child.md").write_text(
        "你是语义检查员。必须调用 semantic-consumer__inspect_types 一次获取实时结果，"
        "然后原样返回工具输出的完整 JSON，包括 verification。不要调用其他工具。"
    )
    (plugin / "child.json").write_text(
        json.dumps(
            {
                "name": "semantic-inspector",
                "description": "Read semantic type counts using the live backend.",
                "system_prompt": "child.md",
                "tools": ["semantic-consumer__inspect_types"],
                "hooks": [
                    {
                        "event": "before_agent",
                        "entrypoint": "probe.py:handle",
                        "params": {"scope": "child"},
                    },
                    {"event": "before_tool", "entrypoint": "probe.py:before_tool"},
                    {"event": "after_tool", "entrypoint": "probe.py:after_tool"},
                ],
            }
        )
    )
    (plugin / ".plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "semantic-consumer",
                "system_prompt": "root.md",
                "tools": {"inspect_types": {"entrypoint": "probe.py:inspect_types"}},
                "hooks": [
                    {
                        "event": "before_agent",
                        "entrypoint": "probe.py:handle",
                        "params": {"scope": "root"},
                    }
                ],
                "subagents": ["child.json"],
            }
        )
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": os.environ.get("DATAAGENT_SEMANTIC_LIVE_MODEL", "deepseek-chat"),
                        "base_url": "$env{DEEPSEEK_BASE_URL}",
                        "api_key": "$env{DEEPSEEK_API_KEY}",
                    }
                },
                "dataagent": {"limits": {"timeout_seconds": 120, "model_calls_per_agent": 5}},
                "plugins": {"enabled": ["semantic-consumer"], "paths": [str(plugin)]},
                "semantic": {"base_url": base_url, "timeout": 15},
            },
            indent=2,
        )
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config, env_file=Path(env_path)))
    prompt = "请调用 task，让 semantic-inspector 查询实时语义类型统计。最后原样回复其完整 JSON，包含校验码。"
    if host == "sdk":
        async with create_agent(runtime) as agent:
            client = agent.context.semantic_client
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}),
                timeout=120,
            )
            answer = json.loads(result.get("messages")[-1].content)
            assert not client.client.is_closed
    else:
        app = create_app(runtime)
        with TestClient(app) as http:
            client = app.state.agents.semantic_client
            response = http.post(
                "/dataagent/stream",
                json={
                    "threadId": "semantic-child",
                    "runId": "semantic-child-run",
                    "state": {},
                    "messages": [{"id": "query", "role": "user", "content": prompt}],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {},
                },
            )
            assert response.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            errors = [event for event in events if event.get("type") == "RUN_ERROR"]
            assert not errors, errors
            assert any(event.get("type") == "RUN_FINISHED" for event in events)
            snapshots = [event for event in events if event.get("type") == "MESSAGES_SNAPSHOT"]
            answers = [
                message.get("content")
                for message in snapshots[-1].get("messages", [])
                if message.get("role") == "assistant"
            ]
            answer = json.loads(answers[-1])
            agent = next(iter(app.state.agents.cache.values()))
            assert agent.context.semantic_client is client
            assert not client.client.is_closed
    records = [
        json.loads(line) for line in (plugin / "observations.jsonl").read_text().splitlines()
    ]
    assert {record.get("scope") for record in records} == {
        "root",
        "child",
        "before_tool",
        "tool",
        "after_tool",
    }
    assert {record.get("client_id") for record in records} == {id(client)}
    assert {record.get("context_id") for record in records} == {id(agent.context)}
    observed = next(record.get("summary") for record in records if record.get("scope") == "tool")
    assert observed.get("vertex_count", 0) > 0 and observed.get("edge_count", 0) > 0
    assert answer == observed
    assert agent.context.model_dump() == {}
    assert client.client.is_closed
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "host": host,
                "backend": client.base_url,
                "observed": observed,
                "answer": answer,
                "shared_client": True,
                "shared_native_context": True,
                "closed_on_shutdown": True,
            },
            indent=2,
        )
    )
