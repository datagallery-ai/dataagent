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
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("elasticsearch", reason="requires elasticsearch for utils_common import")

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.utils.sql_service import (
    SQLiteConfig,
    SQLiteService,
    UDNConfig,
    UDNService,
    _resolve_sqlite_path,
)
from dataagent.common_utils.knowledge_base.knowledge_base import KnowledgeBase
from dataagent.common_utils.knowledge_base.utils_common import (
    StorageConnectorElasticSearch,
    StorageConnectorGaussVector,
    _escape_es_wildcard,
    _quote_postgres_identifier,
)

EVIL_TABLE = 't"; DROP TABLE users; --'
EVIL_COLUMN = 'c"; DROP TABLE users; --'
EVIL_VALUE = "'); DROP TABLE users; --"


def _gauss_with_mock_cursor() -> tuple[StorageConnectorGaussVector, MagicMock]:
    connector = StorageConnectorGaussVector.__new__(StorageConnectorGaussVector)
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchall.return_value = []
    connector.gs = cursor
    return connector, cursor


def test_quote_postgres_identifier_escapes_double_quotes():
    assert _quote_postgres_identifier('a"b') == '"a""b"'


def test_drop_table_quotes_identifier():
    connector, cursor = _gauss_with_mock_cursor()
    connector.drop_table(EVIL_TABLE)
    sql = cursor.execute.call_args.args[0]
    assert sql == f"DROP TABLE IF EXISTS {_quote_postgres_identifier(EVIL_TABLE)}"


def test_insert_data_uses_parameterized_values():
    connector, cursor = _gauss_with_mock_cursor()
    connector.insert_data(EVIL_TABLE, {EVIL_COLUMN: EVIL_VALUE})
    sql, params = cursor.execute.call_args.args
    assert _quote_postgres_identifier(EVIL_TABLE) in sql
    assert _quote_postgres_identifier(EVIL_COLUMN) in sql
    assert "%s" in sql
    assert EVIL_VALUE not in sql
    assert params == (EVIL_VALUE,)


def test_query_fulltext_parameterizes_text_and_limit():
    connector, cursor = _gauss_with_mock_cursor()
    connector.query_fulltext(EVIL_TABLE, EVIL_COLUMN, EVIL_VALUE, 3)
    sql, params = cursor.execute.call_args.args
    assert "LIKE %s" in sql
    assert "LIMIT %s" in sql
    assert EVIL_VALUE not in sql
    assert params == (f"%{EVIL_VALUE}%", 3)


def test_query_metadata_columns_by_filepath_parameterizes_path():
    connector, cursor = _gauss_with_mock_cursor()
    connector.query_metadata_columns_by_filepath(EVIL_TABLE, EVIL_VALUE)
    sql, params = cursor.execute.call_args.args
    assert "LIKE %s" in sql
    assert EVIL_VALUE not in sql
    assert params == (f"%{EVIL_VALUE}%",)


def test_query_metadata_column_fulltext_parameterizes_values():
    connector, cursor = _gauss_with_mock_cursor()
    connector.query_metadata_column_fulltext(EVIL_TABLE, EVIL_COLUMN, EVIL_VALUE, "column", 5)
    sql, params = cursor.execute.call_args.args
    assert EVIL_VALUE not in sql
    assert "type = %s" in sql
    assert params == ("column", f"%{EVIL_VALUE}%", 5)


def test_escape_es_wildcard():
    assert _escape_es_wildcard("a*b?c\\d") == "a\\*b\\?c\\\\d"


def test_process_user_query_rejects_path_outside_allowed_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._allowed_root = tmp_path.resolve()
    with pytest.raises(ValueError, match="outside the allowed root"):
        kb.process_user_query("prompt", "/etc/passwd")


def test_process_user_query_rejects_symlink(tmp_path):
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._allowed_root = tmp_path.resolve()
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        kb.process_user_query("prompt", str(link))


def test_process_markdown_rejects_non_markdown(tmp_path):
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._allowed_root = tmp_path.resolve()
    txt = tmp_path / "notes.txt"
    txt.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="markdown"):
        kb.process_markdown(str(txt))


def test_es_update_value_rejects_invalid_update_field():
    connector = StorageConnectorElasticSearch.__new__(StorageConnectorElasticSearch)
    connector.es = MagicMock()
    with pytest.raises(ValueError, match="invalid update_field"):
        connector.update_value("idx", ["id"], ["1"], "evil;drop", "x")


def test_resolve_sqlite_path_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="current working directory"):
        _resolve_sqlite_path("/etc/passwd")


def test_resolve_sqlite_path_allows_under_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "demo.sqlite"
    db.write_bytes(b"")
    assert _resolve_sqlite_path(str(db)) == db.resolve()


def test_sqlite_service_get_conn_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    svc = SQLiteService(SQLiteConfig(path="/etc/passwd"))
    with pytest.raises(ValueError, match="current working directory"):
        svc._get_conn()


def test_udn_execute_does_not_expose_exception_detail():
    svc = UDNService(UDNConfig(path="http://127.0.0.1:9/query"))
    with (
        patch("dataagent.agents.nl2sql.utils.sql_service.urlopen", side_effect=OSError("/secret/cert.pem")),
        pytest.raises(SQLServiceError) as exc_info,
    ):
        svc.execute("SELECT 1")
    assert exc_info.value.detail is None
    assert "/secret/cert.pem" not in str(exc_info.value)
