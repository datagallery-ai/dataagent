# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.nodes.validator import ValidatorNode
from dataagent.agents.nl2sql.workflow.state import Result


def _node() -> ValidatorNode:
    node = object.__new__(ValidatorNode)
    node.name = "validator"
    node.db_explain = True
    node.keyword_match = False
    node.metadata_match = False
    node.read_only = True
    node.config = {}
    node._config_manager = MagicMock()
    node._config_manager.get.side_effect = lambda key, default=None: {
        "DATABASE.config": {},
        "DATABASE.engine": "sqlite",
        "DATABASE.sql_service_engine": "sqlite",
    }.get(key, default)
    return node


@pytest.mark.asyncio
async def test_validate_with_db_explain_returns_issue_on_sql_service_error(monkeypatch):
    """Explain path SQLServiceError must become issues, not crash the node."""
    node = _node()

    class _Svc:
        def explain(self, sql, **_kwargs):
            raise SQLServiceError(detail="explain failed")

    @contextmanager
    def _fake_build(_engine, _config):
        yield _Svc()

    monkeypatch.setattr(
        "dataagent.agents.nl2sql.nodes.validator.build_sql_service",
        _fake_build,
    )

    issues = await ValidatorNode._validate_with_db_explain(node, "DELETE FROM t")
    assert issues
    assert "explain" in issues[0].lower()


@pytest.mark.asyncio
async def test_validate_syntax_survives_explain_sql_service_error(monkeypatch):
    """Syntax validation should convert EXPLAIN failures into issues."""
    node = _node()

    class _Svc:
        def explain(self, sql, **_kwargs):
            raise SQLServiceError(detail="explain unavailable")

    @contextmanager
    def _fake_build(_engine, _config):
        yield _Svc()

    monkeypatch.setattr(
        "dataagent.agents.nl2sql.nodes.validator.build_sql_service",
        _fake_build,
    )
    monkeypatch.setattr(
        "dataagent.agents.nl2sql.nodes.validator.guard_sql",
        lambda *_a, **_k: None,
    )

    gen = [Result(id=0, sql="SELECT 1", score=0)]
    syntax = await ValidatorNode._validate_syntax(node, gen)
    assert len(syntax) == 1
    assert syntax[0]["score"] == 0
    assert syntax[0]["issues"]


@pytest.mark.asyncio
async def test_validate_syntax_skips_db_explain_when_guard_rejects(monkeypatch):
    """Hard-gate failures must not reach EXPLAIN / the database."""
    node = _node()
    explain_called: list[str] = []

    class _Svc:
        def explain(self, sql, **_kwargs):
            explain_called.append(sql)
            return None

    @contextmanager
    def _fake_build(_engine, _config):
        yield _Svc()

    monkeypatch.setattr(
        "dataagent.agents.nl2sql.nodes.validator.build_sql_service",
        _fake_build,
    )

    gen = [Result(id=0, sql="DELETE FROM t", score=0)]
    syntax = await ValidatorNode._validate_syntax(node, gen)
    assert syntax[0]["score"] == 0
    assert syntax[0]["issues"]
    assert explain_called == []


def test_validate_with_sqlglot_passes_engine_dialect(monkeypatch):
    """MySQL backticks need dialect on the Validator guard_sql path (not Service)."""
    node = _node()
    node._config_manager.get.side_effect = lambda key, default=None: {
        "DATABASE.config": {},
        "DATABASE.engine": "mysql",
        "DATABASE.sql_service_engine": "mysql",
    }.get(key, default)
    captured: dict = {}

    def _fake_guard(sql, *, dialect=None, read_only=True):
        captured["sql"] = sql
        captured["dialect"] = dialect
        captured["read_only"] = read_only

    monkeypatch.setattr(
        "dataagent.agents.nl2sql.nodes.validator.guard_sql",
        _fake_guard,
    )

    sql = "SELECT `col` FROM `db`.`t`"
    issues = ValidatorNode._validate_with_sqlglot(node, sql)
    assert issues == []
    assert captured["sql"] == sql
    assert captured["dialect"] == "mysql"
