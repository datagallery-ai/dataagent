from __future__ import annotations

from types import SimpleNamespace

import pytest

from dataagent.agents.nl2sql.nodes.executor import ExecutorNode
from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.nodes.selector import SelectorNode
from dataagent.agents.nl2sql.workflow.state import Result
from dataagent.core.errors import DataAgentError


def _cm() -> SimpleNamespace:
    return SimpleNamespace(get=lambda key, default=None: default)


@pytest.mark.asyncio
async def test_generator_all_failures_raise_nl2sql_001(monkeypatch) -> None:
    node = GeneratorNode(strategies=["prompt"], num_workers=1, config_manager=_cm())

    async def boom(*_args, **_kwargs):
        raise RuntimeError("strategy failed")

    monkeypatch.setattr(node, "run_strategy", boom)
    with pytest.raises(DataAgentError) as caught:
        await node._aprocess(
            {
                "question": "q",
                "schema_str": "",
                "sql_rules": "",
                "evidence": "",
                "few_shot_examples": "",
                "generation_results": [],
            }
        )
    assert caught.value.source == "llm"
    assert "strategy failed" in caught.value.fact
    assert "strategy=prompt" in caught.value.fact
    assert caught.value.fact != "未能生成可执行 SQL"


@pytest.mark.asyncio
async def test_executor_zero_row_success_continues(monkeypatch) -> None:
    node = ExecutorNode(config_manager=_cm())
    monkeypatch.setattr(
        node,
        "_execute_queries",
        lambda *_a, **_k: [([], [], None)],
    )
    state = {
        "validation_results": [Result(id=0, sql="select 1", prompt="", strategy="prompt")],
        "execution_results": [],
    }
    out = await node._aprocess(state)
    assert out["execution_results"][0].error is None
    assert out["execution_results"][0].rows == []


class _FakeSqlService:
    def __init__(self, execute):
        self._execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        return self._execute(sql)


@pytest.mark.asyncio
async def test_executor_records_sql_service_error_and_continues(monkeypatch) -> None:
    import dataagent.agents.nl2sql.nodes.executor as executor_mod

    def _raise(_sql):
        raise DataAgentError(source="tool", fact="can't connect", component="nl2sql")

    monkeypatch.setattr(executor_mod, "build_sql_service", lambda *_a, **_k: _FakeSqlService(_raise))
    node = ExecutorNode(config_manager=_cm())
    out = await node._aprocess(
        {
            "validation_results": [Result(id=0, sql="select 1", prompt="", strategy="prompt")],
            "execution_results": [],
        }
    )
    assert out["execution_results"][0].error == "can't connect"
    assert out["execution_results"][0].rows is None


@pytest.mark.asyncio
async def test_executor_keeps_sql_business_error_on_candidate(monkeypatch) -> None:
    import dataagent.agents.nl2sql.nodes.executor as executor_mod

    monkeypatch.setattr(
        executor_mod,
        "build_sql_service",
        lambda *_a, **_k: _FakeSqlService(lambda _sql: (None, None, "no such table: t")),
    )
    node = ExecutorNode(config_manager=_cm())
    out = await node._aprocess(
        {
            "validation_results": [Result(id=0, sql="select 1", prompt="", strategy="prompt")],
            "execution_results": [],
        }
    )
    assert out["execution_results"][0].error == "no such table: t"
    assert out["execution_results"][0].rows is None


def _selector_state(*results: Result, sel_retries: int = 0) -> dict:
    return {
        "question": "q",
        "schema_str": "",
        "sql_rules": "",
        "execution_results": list(results),
        "sel_retries": sel_retries,
        "ref_retries": 0,
        "error": None,
    }


@pytest.mark.asyncio
async def test_selector_copies_candidate_error_when_accepting(monkeypatch) -> None:
    node = SelectorNode(config_manager=_cm(), threshold=0.5)

    async def scores(*_args, **_kwargs):
        return [{"score": 0.2, "issues": []}, {"score": 0.9, "issues": []}]

    monkeypatch.setattr(node, "execute_with_llm_json", scores)
    ok = Result(id=0, sql="select 1", columns=["c"], rows=[], rows_preview=[], error=None)
    failed = Result(
        id=1,
        sql="select * from missing",
        columns=None,
        rows=None,
        rows_preview=None,
        error="no such table: missing",
    )
    out = await node._aprocess(_selector_state(ok, failed))
    assert out["sql"] == "select * from missing"
    assert out["rows"] is None
    assert out["error"] == "no such table: missing"


@pytest.mark.asyncio
async def test_selector_zero_row_success_does_not_set_error(monkeypatch) -> None:
    node = SelectorNode(config_manager=_cm(), threshold=0.5)

    async def scores(*_args, **_kwargs):
        return [{"score": 1, "issues": []}]

    monkeypatch.setattr(node, "execute_with_llm_json", scores)
    empty = Result(id=0, sql="select 1 where 0", columns=["c"], rows=[], rows_preview=[], error=None)
    out = await node._aprocess(_selector_state(empty))
    assert out["rows"] == []
    assert out["error"] is None


@pytest.mark.asyncio
async def test_selector_retry_does_not_write_final_error(monkeypatch) -> None:
    node = SelectorNode(config_manager=_cm(), threshold=0.9)

    async def scores(*_args, **_kwargs):
        return [{"score": 0.1, "issues": []}]

    monkeypatch.setattr(node, "execute_with_llm_json", scores)
    failed = Result(
        id=0,
        sql="select * from missing",
        columns=None,
        rows=None,
        rows_preview=None,
        error="no such table: missing",
    )
    out = await node._aprocess(_selector_state(failed, sel_retries=1))
    assert out["proceed"] is False
    assert out["error"] is None
    assert "sql" not in out or out.get("sql") in (None, "")


@pytest.mark.asyncio
async def test_nl2sql_tool_raises_when_state_has_error(monkeypatch, tmp_path) -> None:
    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.actions.tools.local_tool import tools as tools_mod
    from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, reset_current_sandbox, set_current_sandbox

    cfg = tmp_path / "nl2sql.yaml"
    cfg.write_text("DATABASE:\n  dialect: sqlite\n", encoding="utf-8")

    async def fake_sub_agent_tool(**_kwargs):
        return {
            "original_msg": {"status": "success"},
            "state": {
                "sql": "select * from missing",
                "columns": None,
                "rows": None,
                "error": "no such table: missing",
            },
            "sub_id": None,
        }

    monkeypatch.setattr(tools_mod, "sub_agent_tool", fake_sub_agent_tool)
    monkeypatch.setattr(tools_mod.shutil, "copytree", lambda *_a, **_k: None)

    token = set_current_sandbox(NoopSandbox(workspace_root=str(tmp_path)))
    try:
        ctx = ToolExecutionContext(
            config_manager=SimpleNamespace(get=lambda key, default=None: {} if default is None else default),
            tool_config={"source_config_path": str(cfg)},
            runtime=SimpleNamespace(workspace_dir=str(tmp_path), user_id="u", session_id="s"),
        )
        with pytest.raises(DataAgentError) as caught:
            await tools_mod.nl2sql_sub_agent_tool("q", "a.sql", "a.csv", _tool_context=ctx)
        assert caught.value.source == "tool"
        assert "no such table: missing" in caught.value.fact
        assert "select * from missing" in caught.value.fact
        assert "select * from missing" in caught.value.actor_text()
        assert not (tmp_path / "a.csv").exists()
    finally:
        reset_current_sandbox(token)


@pytest.mark.asyncio
async def test_nl2sql_tool_raises_when_sql_empty(monkeypatch, tmp_path) -> None:
    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.actions.tools.local_tool import tools as tools_mod
    from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, reset_current_sandbox, set_current_sandbox

    cfg = tmp_path / "nl2sql.yaml"
    cfg.write_text("DATABASE:\n  dialect: sqlite\n", encoding="utf-8")

    async def fake_sub_agent_tool(**_kwargs):
        return {
            "original_msg": {"status": "success"},
            "state": {"sql": "", "columns": [], "rows": [], "error": None},
            "sub_id": None,
        }

    monkeypatch.setattr(tools_mod, "sub_agent_tool", fake_sub_agent_tool)
    monkeypatch.setattr(tools_mod.shutil, "copytree", lambda *_a, **_k: None)

    token = set_current_sandbox(NoopSandbox(workspace_root=str(tmp_path)))
    try:
        ctx = ToolExecutionContext(
            config_manager=SimpleNamespace(get=lambda key, default=None: {} if default is None else default),
            tool_config={"source_config_path": str(cfg)},
            runtime=SimpleNamespace(workspace_dir=str(tmp_path), user_id="u", session_id="s"),
        )
        with pytest.raises(DataAgentError) as caught:
            await tools_mod.nl2sql_sub_agent_tool("q", "a.sql", "a.csv", _tool_context=ctx)
        assert caught.value.source == "tool"
        assert caught.value.component == "nl2sql"
        assert "未生成 SQL" in caught.value.fact
        assert not (tmp_path / "a.sql").exists()
        assert not (tmp_path / "a.csv").exists()
    finally:
        reset_current_sandbox(token)


@pytest.mark.asyncio
async def test_nl2sql_tool_zero_row_success_writes_csv(monkeypatch, tmp_path) -> None:
    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.actions.tools.local_tool import tools as tools_mod
    from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, reset_current_sandbox, set_current_sandbox

    cfg = tmp_path / "nl2sql.yaml"
    cfg.write_text("DATABASE:\n  dialect: sqlite\n", encoding="utf-8")

    async def fake_sub_agent_tool(**_kwargs):
        return {
            "original_msg": {"status": "success"},
            "state": {"sql": "SELECT 1 WHERE 0", "columns": ["value"], "rows": [], "error": None},
            "sub_id": None,
        }

    monkeypatch.setattr(tools_mod, "sub_agent_tool", fake_sub_agent_tool)
    monkeypatch.setattr(tools_mod.shutil, "copytree", lambda *_a, **_k: None)

    token = set_current_sandbox(NoopSandbox(workspace_root=str(tmp_path)))
    try:
        ctx = ToolExecutionContext(
            config_manager=SimpleNamespace(get=lambda key, default=None: {} if default is None else default),
            tool_config={"source_config_path": str(cfg)},
            runtime=SimpleNamespace(workspace_dir=str(tmp_path), user_id="u", session_id="s"),
        )
        result = await tools_mod.nl2sql_sub_agent_tool("q", "a.sql", "a.csv", _tool_context=ctx)
        assert "SQL 执行完成" in result["original_msg"]
        assert (tmp_path / "a.sql").exists()
        assert (tmp_path / "a.csv").exists()
        written_sql = " ".join((tmp_path / "a.sql").read_text(encoding="utf-8").split())
        assert "SELECT 1" in written_sql
    finally:
        reset_current_sandbox(token)


def test_perceptor_missing_base_url_exposes_config_key() -> None:
    node = PerceptorNode(config_manager=_cm())
    with pytest.raises(DataAgentError) as caught:
        _ = node.semantic_client
    assert caught.value.source == "config"
    assert "SEMANTIC_LAYER.base_url" in caught.value.fact
    assert "SEMANTIC_LAYER.base_url" in caught.value.actor_text()
