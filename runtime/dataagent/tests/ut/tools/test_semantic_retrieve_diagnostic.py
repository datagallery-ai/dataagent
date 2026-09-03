from __future__ import annotations

import json
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, reset_current_sandbox, set_current_sandbox
from dataagent.actions.tools.semantic_tool import search_tables_with_schema


class _FakeSemanticClient:
    def __init__(self) -> None:
        self.last_query = ""
        self.calls = 0

    def semantic_retrieve(self, query: str) -> dict[str, Any]:
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
    """Semantic retrieve should persist diagnostic and query-cache artifacts."""
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

    diagnostic_files = list((tmp_path / ".semantic").glob("semantic_retrieve_diagnostic_*.json"))
    cache_files = list((tmp_path / ".semantic").glob("semantic_retrieve_context_*.json"))
    assert len(diagnostic_files) == 1
    assert len(cache_files) == 1

    payload = json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
    diagnostic = payload.get("diagnostic", {})
    tool_trace = diagnostic.get("toolTrace", [])
    assert payload.get("tool") == "search_tables_with_semantic_retrieve"
    assert payload.get("endpoint") == "semantic/retrieve"
    assert payload.get("query") == "查一下订单"
    assert diagnostic.get("llmCalls") == 2
    assert tool_trace[0].get("result") == [{"table": "orders"}]


def test_metadata_recall_agent_declares_explicit_semantic_tool() -> None:
    """Metadata recall should call semantic retrieval as a native local tool."""
    config_path = Path("dataagent/agents/metadata_recall/metadata_recall_agent.yaml")
    text = config_path.read_text(encoding="utf-8")

    assert "semantic_retrieve_context_loader" not in text
    assert 'name: "search_tables_with_semantic_retrieve"' in text
    assert 'function: "search_tables_with_semantic_retrieve"' in text
