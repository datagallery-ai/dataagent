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
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.utils.sql_service import GaussVectorService, build_sql_service


def _patch_psycopg2(conn: MagicMock):
    """Inject a fake psycopg2 module so tests work without the optional driver installed."""
    fake = MagicMock()
    fake.connect.return_value = conn
    return patch.dict(sys.modules, {"psycopg2": fake}), fake


def test_build_sql_service_gaussvector_returns_service() -> None:
    config = {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "u",
        "password": "p",
        "database": "db",
    }
    service = build_sql_service("gaussvector", config)
    assert isinstance(service, GaussVectorService)


def test_build_sql_service_gaussvector_missing_field_raises() -> None:
    with pytest.raises(SQLServiceError):
        build_sql_service("gaussvector", {"host": "127.0.0.1"})


def test_gaussvector_execute_returns_columns_and_rows() -> None:
    config = {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "u",
        "password": "p",
        "database": "db",
    }
    cursor = MagicMock()
    cursor.description = [("id",), ("name",)]
    cursor.fetchall.return_value = [(1, "a")]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    module_patch, fake_psycopg2 = _patch_psycopg2(conn)

    with module_patch, build_sql_service("gaussvector", config) as service:
        columns, rows, err = service.execute("SELECT id, name FROM t")

    fake_psycopg2.connect.assert_called_once_with(
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="db",
    )
    cursor.execute.assert_called_once_with("SELECT id, name FROM t")
    assert columns == ["id", "name"]
    assert rows == [(1, "a")]
    assert err is None


def test_gaussvector_explain_success_returns_none() -> None:
    config = {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "u",
        "password": "p",
        "database": "db",
    }
    cursor = MagicMock()
    cursor.fetchall.return_value = [("Plan",)]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    module_patch, _ = _patch_psycopg2(conn)

    with module_patch, build_sql_service("gaussvector", config) as service:
        assert service.explain("SELECT 1") is None

    cursor.execute.assert_called_once_with("EXPLAIN SELECT 1")


def test_gaussvector_explain_error_returns_message() -> None:
    config = {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "u",
        "password": "p",
        "database": "db",
    }
    cursor = MagicMock()
    cursor.execute.side_effect = Exception("syntax error")
    conn = MagicMock()
    conn.cursor.return_value = cursor
    module_patch, _ = _patch_psycopg2(conn)

    with module_patch, build_sql_service("gaussvector", config) as service:
        assert service.explain("SELECT BAD") == "syntax error"


@pytest.mark.asyncio
async def test_generator_strips_backticks_for_gaussvector() -> None:
    from unittest.mock import PropertyMock

    from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
    from dataagent.agents.nl2sql.nodes.generator import GeneratorNode

    node = GeneratorNode.__new__(GeneratorNode)
    node.name = "generator"
    node.num_samples = 1

    class _Resp:
        content = "```sql\nSELECT `id` FROM t\n```"

    with (
        patch.object(BaseNL2SQLNode, "engine", new_callable=PropertyMock, return_value="gaussvector"),
        patch("dataagent.agents.nl2sql.nodes.generator.llm_manager") as llm_manager,
        patch("dataagent.agents.nl2sql.nodes.generator.PromptTemplate") as prompt_cls,
        patch.object(GeneratorNode, "_dump_llm_context"),
    ):
        llm_manager.get_default_llm.return_value.ainvoke = AsyncMock(return_value=_Resp())
        prompt_template = MagicMock()
        prompt_template.apply_prompt_template.return_value = "prompt-text"
        prompt_cls.from_package_relative.return_value = prompt_template
        results = await node.generate_with_llm("prompt", {"num_samples": 1}, {"q": "x"})

    assert results[0][0] == "SELECT id FROM t"
