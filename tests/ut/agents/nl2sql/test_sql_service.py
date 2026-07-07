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
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.utils.sql_service import (
    BaseService,
    SparkConfig,
    SparkService,
    SQLiteConfig,
    SQLiteService,
    UDNConfig,
    UDNService,
    _readonly_select_sql_error,
)


class _Select:
    def __init__(self, *, into: bool = False, side_effect: object | None = None) -> None:
        self.args = {"into": object()} if into else {}
        self._side_effect = side_effect

    def find(self, *node_types: type) -> object | None:
        if self.args.get("into") and _Into in node_types:
            return object()
        if self._side_effect is not None and isinstance(self._side_effect, node_types):
            return self._side_effect
        return None


class _WriteStatement:
    pass


class _Into:
    pass


class _Delete:
    pass


@pytest.fixture
def fake_sqlglot(monkeypatch: pytest.MonkeyPatch) -> None:
    sqlglot = ModuleType("sqlglot")
    exp = SimpleNamespace(
        Select=_Select,
        Into=_Into,
        Delete=_Delete,
    )

    def parse(sql: str, error_level: Any = None) -> list[object]:
        normalized = sql.strip().lower()
        statements = [part for part in normalized.split(";") if part.strip()]
        if len(statements) > 1:
            return [_Select(), _WriteStatement()]
        if not statements or "invalid" in normalized:
            raise ValueError("invalid sql")
        if normalized.startswith("with deleted as"):
            return [_Select(side_effect=_Delete())]
        if normalized.startswith("select"):
            return [_Select(into=" into " in f" {normalized} ")]
        if normalized.startswith("with"):
            return [_Select()]
        return [_WriteStatement()]

    sqlglot.parse = parse
    sqlglot.exp = exp
    sqlglot.errors = SimpleNamespace(ErrorLevel=SimpleNamespace(RAISE="raise"))
    monkeypatch.setitem(sys.modules, "sqlglot", sqlglot)


class _FailingConnectionService(BaseService):
    def _get_conn(self) -> Any:
        raise RuntimeError("failed to open /srv/private/database.sqlite")

    def _handle_explain_error(self, e: Exception) -> str:
        raise e


class _FailingCursor:
    def execute(self, _sql: str) -> None:
        raise RuntimeError("failed to read /srv/private/schema.sql")


class _FailingErrorHandlerService(BaseService):
    def _get_conn(self) -> Any:
        return type("_Connection", (), {"cursor": lambda _self: _FailingCursor()})()

    def _handle_explain_error(self, e: Exception) -> str:
        raise e


def test_explain_does_not_expose_internal_exception_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("certificate path: /srv/private/client.pem")

    monkeypatch.setattr("dataagent.agents.nl2sql.utils.sql_service.urlopen", fail_urlopen)
    cases = [
        (_FailingConnectionService(), "/srv/private/database.sqlite"),
        (_FailingErrorHandlerService(), "/srv/private/schema.sql"),
        (UDNService(UDNConfig(path="https://sql.example.test")), "/srv/private/client.pem"),
    ]

    for service, sensitive_text in cases:
        with pytest.raises(SQLServiceError) as exc_info:
            service.explain("SELECT 1")
        payload = exc_info.value.to_dict()
        assert payload["detail"] is None
        assert sensitive_text not in str(payload)


def test_sqlite_get_conn_rejects_path_escape_and_uses_readonly_uri(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="current working directory"):
        SQLiteService(SQLiteConfig(path="../secret.sqlite"))._get_conn()

    db_path = tmp_path / "demo?mode=rw.sqlite"
    db_path.touch()
    captured: dict[str, Any] = {}

    def fake_connect(database_uri: str, *, uri: bool, check_same_thread: bool) -> object:
        captured.update(uri=database_uri, uri_flag=uri, check_same_thread=check_same_thread)
        return object()

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    SQLiteService(SQLiteConfig(path=db_path.name))._get_conn()

    assert captured == {
        "uri": f"file:{tmp_path}/demo%3Fmode%3Drw.sqlite?mode=ro",
        "uri_flag": True,
        "check_same_thread": False,
    }


def test_spark_explain_does_not_return_internal_exception_details(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SparkService(SparkConfig(warehouse_dir="/srv/private/warehouse"))
    monkeypatch.setattr(service, "_get_spark", lambda: (_ for _ in ()).throw(RuntimeError("/srv/private/warehouse")))

    assert service.explain("SELECT 1") == "SQL explain failed."


def test_readonly_sql_validation(fake_sqlglot: None) -> None:
    cases = {
        "DROP TABLE users": "Only read-only SELECT statements are allowed.",
        "SELECT * FROM users; DELETE FROM users": "Only a single SQL statement is allowed.",
        "SELECT 'DROP TABLE users' AS text": None,
        "WITH recent AS (SELECT * FROM users) SELECT * FROM recent": None,
        "WITH deleted AS (DELETE FROM users RETURNING *) SELECT * FROM deleted": (
            "Only read-only SELECT statements are allowed."
        ),
        "SELECT * INTO OUTFILE '/tmp/users.csv' FROM users": "Only read-only SELECT statements are allowed.",
    }

    for sql, expected_error in cases.items():
        assert _readonly_select_sql_error(sql) == expected_error, sql


def test_fallback_readonly_sql_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sqlglot", None)

    assert _readonly_select_sql_error("WITH recent AS (SELECT * FROM users) SELECT * FROM recent") is None
    assert _readonly_select_sql_error("WITH deleted AS (DELETE FROM users RETURNING *) SELECT * FROM deleted") == (
        "Only read-only SELECT statements are allowed."
    )


class _RecordingService(BaseService):
    def _get_conn(self) -> Any:
        raise AssertionError("connection should not be opened")

    def _handle_explain_error(self, e: Exception) -> str:
        raise e


def test_services_reject_unsafe_sql_before_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _RecordingService()
    assert service.explain("DROP TABLE users") == "Only read-only SELECT statements are allowed."
    assert service.execute("DROP TABLE users") == (None, None, "Only read-only SELECT statements are allowed.")

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("request should not be sent")

    monkeypatch.setattr("dataagent.agents.nl2sql.utils.sql_service.urlopen", fail_urlopen)
    assert UDNService(UDNConfig(path="https://sql.example.test")).execute("DELETE FROM users") == (
        None,
        None,
        "Only read-only SELECT statements are allowed.",
    )
