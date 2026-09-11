"""BIRD contracts across nodes, semantic transport, SDK dispatch and REST output."""

import asyncio
import hashlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml

import dataagent.agents.bird.agent as bird_module
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.agents.bird.context_dump import current_context_dump
from dataagent.agents.bird.nodes import ExecutorNode, GeneratorNode, PerceptorNode, ReflectorNode, ValidatorNode
from dataagent.agents.bird.nodes.selector import SelectorNode
from dataagent.agents.bird.workflow.router import BirdRouter
from dataagent.agents.bird.workflow.state import Result, get_default_state
from dataagent.config.config_manager import ConfigManager
from dataagent.core.errors import DataAgentError
from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.sdk.agent import DataAgent


@pytest.fixture
def config():
    manager = ConfigManager()
    manager.settings = yaml.safe_load(Path(bird_module.__file__).with_name("bird_agent.yaml").read_text())
    manager.settings["SEMANTIC_LAYER"] = {"base_url": "http://semantic.test"}
    return manager


@pytest.mark.asyncio
async def test_config_security_and_sdk_dispatch(monkeypatch, config):
    monkeypatch.setattr(bird_module, "create_workflow_backend", lambda **kwargs: object())
    config.settings["CORE"]["validator"]["sql_security_enabled"] = True
    agent = bird_module.BirdAgent.from_config(config.settings, config_manager=config)
    assert [node.name for node in agent.nodes] == [
        "perceptor",
        "generator",
        "validator",
        "reflector",
        "executor",
        "selector",
    ]
    assert all(node.sql_security_enabled for node in agent.nodes if isinstance(node, (ValidatorNode, ReflectorNode)))

    async def stream():
        yield {"sql": "SELECT secret"}

    assert [item async for item in agent._yield_perf_stream(None, stream())] == [{"sql": ""}]
    factory = Mock(return_value=agent)
    monkeypatch.setattr(bird_module.BirdAgent, "from_config", factory)
    sdk = object.__new__(DataAgent)
    sdk.type, sdk.config = "bird", config
    assert sdk.select_engine(config) is agent
    assert factory.call_args.kwargs["config"]["mode"] == "chat"
    assert factory.call_args.kwargs["config_manager"] is config


@pytest.mark.asyncio
async def test_perceptor_evidence_and_service_fallback(monkeypatch, config):
    node = PerceptorNode(config_manager=config)
    schema = {"schools": {"columns": {"id": {"value_type": "integer"}}}}
    linking = AsyncMock(return_value=(schema, [], {"version": 1, "warnings": []}))
    monkeypatch.setattr(node, "multi_stage_schema_linking", linking)
    result = await node._aprocess(get_default_state("question", evidence="evidence"))
    linking.assert_awaited_once_with("question", "evidence")
    assert result["evidence"] == "evidence" and result["schema_linking_trace"]["version"] == 1
    assert result["schema_linking_error"] == ""
    linking.side_effect = httpx.ConnectError("unavailable")
    monkeypatch.setattr(node, "full_schema", lambda **kwargs: (schema, []))
    result = await node._aprocess(get_default_state("question"))
    assert result["schema"] == schema
    assert result["schema_linking_trace"]["warnings"][0]["fallback"] == "full_schema_fallback"


@pytest.mark.asyncio
async def test_validation_reflection_execution_routes(monkeypatch, config):
    validator = ValidatorNode(config_manager=config)
    reflector = ReflectorNode(config_manager=config, threshold=0.9)
    executor = ExecutorNode(config_manager=config)
    candidate = Result(id=1, sql="SELECT 1", strategy="dc", phase=1)
    state = get_default_state(
        "question",
        generation_results=[candidate],
        generation_phase=1,
        generation_targets={"dc": 1},
        generation_attempts={"1:dc": 1},
        generation_max_route_attempts=1,
        budget_complete=False,
    )
    for method in ("_validate_semantic", "_validate_syntax"):
        monkeypatch.setattr(validator, method, AsyncMock(return_value=[{"score": 1, "issues": []}]))
    monkeypatch.setattr(executor, "_execute_queries", lambda *args: [(["value"], [(1,)], None)])
    router = BirdRouter()
    await validator._aprocess(state)
    assert router.route_from_validator(state) == "reflector"
    await reflector._aprocess(state)
    assert candidate.validation_passed and router.route_from_reflector(state) == "executor"
    await executor._aprocess(state)
    assert candidate.columns == ["value"] and candidate.rows == [(1,)]
    assert router.route_from_executor(state) == "selector"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_sql_security_switch_and_invalid_semantic_output(monkeypatch, config, enabled):
    node = ValidatorNode(
        config_manager=config, sql_security_enabled=enabled, semantic_failure_score=0, allow_unfenced_json=True
    )
    monkeypatch.setattr(node, "_validate_with_db_explain", AsyncMock(return_value=[]))
    candidate = Result(id=7, sql="SELECT random()")
    scores = await node._validate_syntax([candidate], {})
    assert candidate.security_checked is enabled and bool(candidate.security_violations) is enabled
    assert scores[0]["score"] == (0 if enabled else 1)
    node.execute_with_llm = AsyncMock(return_value='{"results":[{"id":7,"score":false,"issues":[]}]}')
    state = get_default_state("question", generation_results=[candidate], schema_str="", sql_rules="")
    assert await node._validate_semantic(state) == [
        {"score": 0.0, "issues": ["Semantic validator output was invalid."]}
    ]
    assert node.execute_with_llm.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("path", ["initial", "repair", "fallback"])
async def test_accepted_sql_normalization_reaches_explain_and_execution(monkeypatch, config, enabled, path):
    raw_sql = "SELECT  current_time FROM t LIMIT 1"
    expected_sql = raw_sql.replace("current_time", '"current_time"') if enabled else raw_sql
    validator = ValidatorNode(config_manager=config, sql_security_enabled=enabled)
    reflector = ReflectorNode(config_manager=config, sql_security_enabled=enabled)
    executor = ExecutorNode(config_manager=config)
    candidate = Result(id=1, sql="SELECT 1" if path == "repair" else raw_sql, syntax_validated=True, strategy="dc")
    state = get_default_state(
        "question",
        schema={"t": {"columns": {"current_time": {}}}},
        generation_results=[candidate] if path == "initial" else [],
        generation_targets={"dc": 1},
        generation_attempts={"1:dc": 1},
        generation_max_route_attempts=1,
    )
    if path != "initial":
        reflector._apply_repair(state, candidate, "DELETE FROM t" if path == "fallback" else raw_sql)
        assert candidate.sql == expected_sql
    monkeypatch.setattr(validator, "_validate_semantic", AsyncMock(return_value=[{"score": 1, "issues": []}]))
    with closing(sqlite3.connect(":memory:", check_same_thread=False)) as db:
        db.execute('CREATE TABLE t ("current_time" TEXT)')
        db.execute("INSERT INTO t VALUES ('stored-value')")

        async def explain(sql):
            db.execute(f"EXPLAIN {sql}").fetchall()
            return []

        explain_mock = AsyncMock(side_effect=explain)
        execute_mock = Mock(
            side_effect=lambda _config, sqls: [(["current_time"], db.execute(sql).fetchall(), None) for sql in sqls]
        )
        monkeypatch.setattr(validator, "_validate_with_db_explain", explain_mock)
        monkeypatch.setattr(executor, "_execute_queries", execute_mock)
        await validator._aprocess(state)
        explain_mock.assert_awaited_once_with(expected_sql)
        assert candidate.sql == expected_sql and candidate.security_checked is enabled
        await reflector._aprocess(state)
        assert state["sql"] == expected_sql and candidate.validation_passed
        await executor._aprocess(state)
        assert execute_mock.call_args.args[1] == [expected_sql]
        assert (candidate.rows == [("stored-value",)]) is enabled


@pytest.mark.asyncio
async def test_service_backed_icl_and_qualified_sample_values(monkeypatch, config):
    requests = []
    column = "db.schools.email"

    def respond(request):
        requests.append(request)
        payload = (
            [{"query": "example", "expression": "SELECT 1"}]
            if request.url.path.endswith("sql-few-shots")
            else {column: {"sample_values": [{"value": "a"}]}}
        )
        return httpx.Response(200, json=payload)

    transport = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: transport)
    client = SemanticServiceClient("http://semantic.test")
    monkeypatch.setattr(SemanticServiceClient, "from_config", lambda manager: client)
    node = GeneratorNode(config_manager=config, **config.settings["CORE"]["generator"])
    examples = await node._load_icl_examples(get_default_state("question"))
    assert "example" in examples and "SELECT 1" in examples
    assert dict(requests[-1].url.params) == {"query": "question", "topK": "3"}
    assert client.get_columns_sample_values([column], sample_count=1) == {column: ["a"]}
    assert dict(requests[-1].url.params) == {"columnNames": column, "sampleValuesNumber": "1", "limit": "1"}
    for invalid in ("column", "table.column", "db..column", ""):
        with pytest.raises(ValueError, match=r"db\.table\.column"):
            client.get_columns_sample_values([invalid])
    assert len(requests) == 2
    transport.close()


@pytest.mark.asyncio
async def test_generator_stops_after_bounded_failures(monkeypatch, config):
    node = GeneratorNode(config_manager=config, num_workers=1, phase1_budget=1, max_route_attempts=3)
    generate = AsyncMock(return_value=[])
    monkeypatch.setattr(node, "run_strategy", generate)
    with pytest.raises(RuntimeError, match="Generator produced no SQL candidates"):
        await node._aprocess(get_default_state("question", schema_str="CREATE TABLE t(id INTEGER);"))
    assert [call.args[0] for call in generate.await_args_list].count("dc") == 6
    assert [call.args[0] for call in generate.await_args_list].count("skeleton") == 6
    assert generate.await_count == 12


@pytest.mark.asyncio
async def test_selector_uses_majority_result_without_llm_review(config):
    selector = SelectorNode(config_manager=config, review_consistency_threshold=0.5)
    selector.execute_with_llm_json = AsyncMock()
    candidates = [
        Result(
            id=index, sql=sql, validation_passed=True, columns=["value"], rows=[(value,)], rows_preview=[(str(value),)]
        )
        for index, sql, value in [(1, "SELECT 1", 1), (2, "SELECT 111", 1), (3, "SELECT 2", 2)]
    ]
    state = get_default_state("question", execution_results=candidates)
    best, _summary, vote_count = await selector._choose_result(state)
    assert best is candidates[0]
    assert best.confidence == pytest.approx(2 / 3)
    assert vote_count == 2
    selector.execute_with_llm_json.assert_not_awaited()


def test_bird_import_is_independent_of_nl2sql():
    script = "import sys; import dataagent.agents.bird.agent\n"
    script += "assert not any(name.startswith('dataagent.agents.nl2sql') for name in sys.modules)"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_bird_rest_output_errors_and_request_cleanup():
    service = DataAgentService()
    service._cached_agent_type = "bird"
    service._agent = SimpleNamespace(config={"USER_ID": "anonymous", "RUN_ID": 0, "SUB_ID": 0, "WORKSPACE": {}})
    result = service._format_result(
        {"sql": "SELECT 1", "confidence": 0.9, "generation_results": [{"id": 0, "sql": "SELECT 1", "prompt": "SECRET"}]}
    )
    payload = result["result"]
    assert payload["success"] is True and payload["candidates"][0]["sql"] == "SELECT 1"
    assert payload["sql_fingerprint"] == hashlib.sha256(b"SELECT 1").hexdigest()
    assert "SECRET" not in str(result)
    empty = service._format_result({"sql": "", "trace_id": "abc"})["result"]
    assert empty["sql"] == "" and "sql_fingerprint" not in empty
    with pytest.raises(DataAgentError) as caught:
        service._format_result({"sql": "", "error": "no such table: t", "session_id": "s"})
    error = caught.value.to_dict()
    assert error["source"] == "tool" and error["component"] == "bird"
    assert "no such table: t" in error["fact"]
    with service._request_scope() as request:
        workspace = Path(request.get("workspace"))
        assert request.get("session_id") and workspace.is_dir()
    assert not workspace.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_id", "run_id", "_parent_session_id", "_parent_run_id"])
async def test_bird_sdk_rejects_unsafe_identity_before_filesystem(monkeypatch, config, field):
    sdk = object.__new__(DataAgent)
    sdk.type, sdk.config = "bird", config
    filesystem = Mock(side_effect=AssertionError("filesystem called before identity validation"))
    monkeypatch.setattr(sdk, "_ensure_workspace", filesystem)
    monkeypatch.setattr("dataagent.interface.sdk.agent.setup_session_log", filesystem)
    state = {field: "a/../../escape"}
    with pytest.raises(ValueError, match="safe path component"):
        await sdk.chat("question", initial_state=state)
    with pytest.raises(ValueError, match="safe path component"):
        sdk.astream(input=state)
    filesystem.assert_not_called()


@pytest.mark.asyncio
async def test_bird_dump_isolates_concurrent_calls_and_cancellation(monkeypatch, config, tmp_path):
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "1")
    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "0")
    ready, release = asyncio.Event(), asyncio.Event()
    entered, snapshots = [], {}

    async def invoke(state):
        name = state["question"]
        entered.append(name)
        snapshots[name] = current_context_dump()
        if len(entered) == 2 or name == "cancel":
            ready.set()
        await release.wait()
        agent.nodes[0]._dump_llm_context("system", name, "result", "perceptor", "")
        return state

    monkeypatch.setattr(bird_module, "create_workflow_backend", lambda **kw: SimpleNamespace(ainvoke=invoke))
    agent = bird_module.BirdAgent.from_config(config.settings, config_manager=config)
    agent._context_recording_enabled = False
    calls = [
        asyncio.create_task(agent.chat(name, session_id=name, initial_state={"workspace": tmp_path / name}))
        for name in ("first", "second")
    ]
    await asyncio.wait_for(ready.wait(), 2)
    release.set()
    await asyncio.gather(*calls)
    for name in entered:
        dump_file = tmp_path / name / ".memory/context_dump/run_0/bird_01/01_round_perceptor.txt"
        assert f"--- [1] HUMAN ---\n{name}\n" in dump_file.read_text()
        assert snapshots[name].sequence == 1 and not snapshots[name].active
    ready.clear()
    release.clear()
    cancelled = asyncio.create_task(agent.chat("cancel", initial_state={"workspace": tmp_path / "cancel"}))
    await asyncio.wait_for(ready.wait(), 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert current_context_dump() is None and not snapshots["cancel"].active
    assert not list((tmp_path / "cancel").rglob("*round*.txt"))


def test_bird_dump_rejects_resolved_symlink_escape(tmp_path):
    agent = object.__new__(bird_module.BirdAgent)
    agent.config = {}
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    (workspace / ".memory").mkdir(parents=True)
    outside.mkdir()
    (workspace / ".memory/context_dump").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the session memory"):
        agent._create_context_dump_dir(user_id="anonymous", session_id="safe", workspace=workspace, run_id=0)
    assert not list(outside.iterdir())
