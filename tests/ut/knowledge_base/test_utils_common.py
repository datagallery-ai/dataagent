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
"""Security regression tests for knowledge-base query construction."""

from typing import Any

import pytest

from dataagent.common_utils.knowledge_base.utils_common import (
    MySQLReader,
    StorageConnectorElasticSearch,
    StorageConnectorGaussVector,
)

EVIL_TABLE = "documents; DROP TABLE audit; --"
EVIL_COLUMN = 'content"; DROP TABLE audit; --'
EVIL_VALUE = "needle'; DROP TABLE audit; --"


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.description = [("value",)]
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def _storage(cursor: _FakeCursor) -> StorageConnectorGaussVector:
    storage = StorageConnectorGaussVector.__new__(StorageConnectorGaussVector)
    storage.gs = cursor
    return storage


def test_load_table_quotes_mysql_table_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.dialects import mysql

    reader = MySQLReader.__new__(MySQLReader)
    reader.engine = type("_Engine", (), {"dialect": mysql.dialect()})()
    captured: dict[str, Any] = {}

    def fake_read_sql(sql: Any, con: Any) -> str:
        captured.update(sql=str(sql), con=con)
        return "result"

    monkeypatch.setattr("dataagent.common_utils.knowledge_base.utils_common.pd.read_sql", fake_read_sql)

    assert reader.load_table(f"database.{EVIL_TABLE}") == "result"
    assert captured["sql"] == "SELECT * FROM `database`.`documents; DROP TABLE audit; --`"
    assert captured["con"] is reader.engine


def test_create_table_quotes_table_and_column_identifiers() -> None:
    cursor = _FakeCursor()
    mapping = {
        "mappings": {
            "properties": {
                EVIL_COLUMN: {"type": "keyword"},
                "public.vector": {"type": "dense_vector"},
                "select": {"type": "integer"},
                "ignored": {"type": "unsupported"},
            }
        }
    }

    _storage(cursor).create_table(EVIL_TABLE, mapping)

    assert cursor.calls[-1] == (
        'CREATE TABLE IF NOT EXISTS "documents; DROP TABLE audit; --" '
        '("content""; DROP TABLE audit; --" text, "public.vector" floatvector(1024), "select" int)',
        None,
    )


def test_gaussvector_binds_query_values() -> None:
    cases = [
        (
            lambda storage: storage.query_fulltext("documents", "content", EVIL_VALUE, 10),
            "SELECT * FROM documents WHERE content LIKE %s LIMIT %s",
            (f"%{EVIL_VALUE}%", 10),
        ),
        (
            lambda storage: storage.delete_data(EVIL_TABLE, [EVIL_COLUMN], [EVIL_VALUE], "fulltext"),
            'DELETE FROM "documents; DROP TABLE audit; --" WHERE "content""; DROP TABLE audit; --" LIKE %s',
            (f"%{EVIL_VALUE}%",),
        ),
        (
            lambda storage: storage.delete_data(
                EVIL_TABLE, ["public.content", 'type" OR TRUE; --'], ["needle", "text"], "AND"
            ),
            'DELETE FROM "documents; DROP TABLE audit; --" WHERE public.content = %s AND "type"" OR TRUE; --" = %s',
            ("needle", "text"),
        ),
        (
            lambda storage: storage.update_value(
                EVIL_TABLE,
                ["public.id", 'type" OR TRUE; --'],
                ["doc-1", "text"],
                EVIL_COLUMN,
                EVIL_VALUE,
            ),
            'UPDATE "documents; DROP TABLE audit; --" SET "content""; DROP TABLE audit; --" = %s '
            'WHERE public.id = %s AND "type"" OR TRUE; --" = %s',
            (EVIL_VALUE, "doc-1", "text"),
        ),
    ]

    for operation, expected_sql, expected_params in cases:
        cursor = _FakeCursor()
        operation(_storage(cursor))

        sql, params = cursor.calls[-1]
        assert sql == expected_sql
        assert params == expected_params
        assert all(not isinstance(value, str) or value not in sql for value in expected_params)


def test_result_list_parsing_does_not_execute_code() -> None:
    value = "[__import__('os').system('echo unsafe')]"
    cursor = _FakeCursor(rows=[(value,)])

    result = _storage(cursor).execute_sql_and_fetch_dict("SELECT value FROM documents")

    assert result == [{"value": value}]


def test_vector_script_binds_query_schema_as_parameter() -> None:
    for major_version, expected_source in [
        (8, "cosineSimilarity(params.query_vector, doc[params.query_schema]) + 1.0"),
        (9, "cosineSimilarity(params.query_vector, params.query_schema) + 1.0"),
    ]:
        query_schema = "vector']); return 1; //"
        storage = StorageConnectorElasticSearch.__new__(StorageConnectorElasticSearch)
        storage._es_major_version = major_version

        query = storage._build_script_score_query(query_schema, [0.1, 0.2], 5)

        script = query["query"]["script_score"]["script"]
        assert script["source"] == expected_source
        assert query_schema not in script["source"]
        assert script["params"]["query_schema"] == query_schema
