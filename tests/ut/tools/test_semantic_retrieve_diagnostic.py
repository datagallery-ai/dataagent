from __future__ import annotations

import json
from hashlib import sha256
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, reset_current_sandbox, set_current_sandbox
from dataagent.actions.tools.semantic_tool import search_tables_with_schema
from dataagent.core.flex.hooks.semantic_retrieve import semantic_retrieve_context_loader


class _FakeSemanticClient:
    def __init__(self) -> None:
        self.last_query = ""
        self.calls = 0

    def semantic_search_tables(self, query: str) -> dict[str, Any]:
        """Return a semantic retrieve response with diagnostic payload."""
        self.last_query = query
        self.calls += 1
        return {
            "dataAccessPlan": {
                "tables": [
                    {
                        "db": "demo_db",
                        "table": "orders",
                        "description": "订单表",
                    }
                ]
            },
            "diagnostic": {
                "llmCalls": 2,
                "toolCalls": 1,
                "steps": 3,
                "latencyMs": 1234,
                "toolTrace": [
                    {
                        "tool": "search",
                        "arguments": {"query": query},
                        "resultChars": 12,
                        "ok": True,
                        "latencyMs": 20,
                        "result": [{"table": "orders"}],
                    }
                ],
            },
        }


def test_search_tables_with_semantic_retrieve_saves_diagnostic(monkeypatch, tmp_path) -> None:
    """semantic retrieve diagnostic should be persisted under workspace .semantic."""
    parameters = signature(search_tables_with_schema.search_tables_with_semantic_retrieve).parameters
    assert list(parameters) == ["_tool_context"]

    fake_client = _FakeSemanticClient()
    monkeypatch.setattr(
        search_tables_with_schema.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: fake_client),
    )
    monkeypatch.setattr(search_tables_with_schema, "get_table_description", lambda table_name, client: "订单表")

    runtime = SimpleNamespace(user_id="u1", session_id="s1", run_id="r1", parent_user_query="查一下订单")
    context = ToolExecutionContext(config_manager=SimpleNamespace(), runtime=runtime)
    token = set_current_sandbox(NoopSandbox(workspace_root=tmp_path))
    try:
        result = search_tables_with_schema.search_tables_with_semantic_retrieve(_tool_context=context)
    finally:
        reset_current_sandbox(token)

    assert fake_client.last_query == "查一下订单"
    assert "demo_db.orders" in result.get("data", "")

    files = list((tmp_path / ".semantic").glob("semantic_retrieve_diagnostic_*.json"))
    assert len(files) == 1

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    diagnostic = payload.get("diagnostic", {})
    tool_trace = diagnostic.get("toolTrace", [])

    assert payload.get("tool") == "search_tables_with_semantic_retrieve"
    assert payload.get("endpoint") == "semantic/retrieve"
    assert payload.get("query") == "查一下订单"
    assert diagnostic.get("llmCalls") == 2
    assert tool_trace[0].get("result") == [{"table": "orders"}]


def test_semantic_retrieve_context_loader_injects_user_query_and_reuses_query_cache(monkeypatch, tmp_path) -> None:
    """Agent pre-hook should reuse semantic context for the same original query across subagents."""
    fake_client = _FakeSemanticClient()
    monkeypatch.setattr(
        search_tables_with_schema.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: fake_client),
    )
    monkeypatch.setattr(search_tables_with_schema, "get_table_description", lambda table_name, client: "订单表")

    runtime = _FakeRuntime(parent_user_query="查一下订单")
    state = {
        "workspace": str(tmp_path),
        "user_query": "查一下订单",
        "user_id": "u1",
        "session_id": "s1",
        "run_id": 7,
        "sub_id": 101,
    }
    state_keys = set(state)

    out = semantic_retrieve_context_loader(state, runtime)

    assert fake_client.calls == 1
    assert set(out) == state_keys
    assert "查一下订单" in out.get("user_query", "")
    assert "<semantic_retrieve_context>" in out.get("user_query", "")
    assert "demo_db.orders" in out.get("user_query", "")
    assert "Cached file:" not in out.get("user_query", "")
    metric_dir = tmp_path / ".metric_dir"
    assert len(list(metric_dir.glob("output_search_tables_with_semantic_retrieve_*.json"))) == 1
    assert len(list(metric_dir.glob("output_search_tables_with_retrieve_summary_*.txt"))) == 1

    query_hash = sha256("查一下订单".encode()).hexdigest()
    cache_path = tmp_path / ".semantic" / f"semantic_retrieve_context_{query_hash}.json"
    assert cache_path.is_file()
    cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached_payload.get("query") == "查一下订单"
    assert cached_payload.get("cache_path") == str(cache_path)
    direct_cache = search_tables_with_schema.read_semantic_retrieve_context_cache(
        "查一下订单",
        workspace_root=tmp_path,
    )
    assert direct_cache is not None
    assert direct_cache.get("context_text") == cached_payload.get("context_text")
    assert direct_cache.get("cache_path") == str(cache_path)

    monkeypatch.setattr(
        search_tables_with_schema.SemanticServiceClient,
        "from_config",
        classmethod(lambda cls, config_manager: (_ for _ in ()).throw(AssertionError("cache should be reused"))),
    )
    cached_tool_context = ToolExecutionContext(
        config_manager=SimpleNamespace(),
        runtime=SimpleNamespace(
            user_id="u1",
            session_id="s1",
            run_id=8,
            sub_id=0,
            parent_user_query="查一下订单",
        ),
    )
    token = set_current_sandbox(NoopSandbox(workspace_root=tmp_path))
    try:
        cached_tool_result = search_tables_with_schema.search_tables_with_semantic_retrieve(
            _tool_context=cached_tool_context
        )
    finally:
        reset_current_sandbox(token)
    assert "demo_db.orders" in cached_tool_result.get("data", "")

    second_runtime = _FakeRuntime(parent_user_query="查一下订单")
    second_runtime.config_manager = None
    second_state = {
        "workspace": str(tmp_path),
        "user_query": "查一下订单",
        "user_id": "u1",
        "session_id": "s1",
        "run_id": 8,
        "sub_id": 102,
    }
    second_out = semantic_retrieve_context_loader(second_state, second_runtime)

    assert fake_client.calls == 1
    assert set(second_out) == set(second_state)
    assert "demo_db.orders" in second_out.get("user_query", "")


def test_metadata_recall_agent_declares_preloaded_semantic_context() -> None:
    """metadata recall agent should describe the preloaded semantic context."""
    config_path = Path("dataagent/agents/metadata_recall/metadata_recall_agent.yaml")
    text = config_path.read_text(encoding="utf-8")

    assert "semantic_retrieve_context_loader" in text
    assert "<semantic_retrieve_context>" in text
    assert "Cached file:" not in text
    assert ".semantic/semantic_retrieve_context_<query_hash>.json" not in text
    assert "search_tables_with_semantic_retrieve" not in text


class _FakeRuntime:
    def __init__(self, *, parent_user_query: str) -> None:
        self.parent_user_query = parent_user_query
        self.user_query = parent_user_query
        self.user_id = "u1"
        self.session_id = "s1"
        self.run_id = 7
        self.sub_id = 2
        self.config_manager = SimpleNamespace()
        self.workspace_dir = None
