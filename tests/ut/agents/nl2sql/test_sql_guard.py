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
import pytest

from dataagent.agents.nl2sql.utils.sql_guard import SQLGuardError, guard_sql


def test_guard_allows_simple_select():
    guard_sql("SELECT 1 AS n")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE users",
        "DELETE FROM users",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "SELECT pg_sleep(1)",
        "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    ],
)
def test_guard_rejects_dangerous_sql(sql: str):
    with pytest.raises(SQLGuardError):
        guard_sql(sql)


def test_guard_hard_fails_without_sqlglot(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sqlglot" or name.startswith("sqlglot."):
            raise ImportError("forced missing sqlglot")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SQLGuardError, match="sqlglot is required"):
        guard_sql("SELECT 1")


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * INTO u FROM t", "postgres"),
        ("SELECT * INTO TEMP TABLE u FROM t", "postgres"),
        ("SELECT id INTO TEMP u FROM t", "postgres"),
        # INTO may sit on a UNION branch, not the top-level Select.
        ("SELECT 1 UNION ALL SELECT 2 INTO t", "postgres"),
        ("SELECT 1 AS n UNION SELECT 2 INTO TEMP u", "postgres"),
    ],
)
def test_guard_rejects_select_into_write_target(sql: str, dialect: str):
    with pytest.raises(SQLGuardError, match="INTO|read-only|write"):
        guard_sql(sql, dialect=dialect)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 /* INTO OUTFILE */",
        "SELECT 'into outfile' AS x",
        "SELECT 1 -- into outfile\n",
        "SELECT 1 /* LOAD DATA */",
        "SELECT 'copy t to /tmp/x' AS x",
    ],
)
def test_guard_allows_dangerous_keywords_only_in_comments_or_literals(sql: str):
    """Raw-text regex must not false-positive on comments / string literals."""
    guard_sql(sql)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * FROM t INTO OUTFILE '/tmp/x'", "mysql"),
        ("SELECT * FROM t INTO DUMPFILE '/tmp/x'", "mysql"),
        ("LOAD DATA INFILE 'x' INTO TABLE t", "mysql"),
        ("COPY t TO '/tmp/x'", "postgres"),
    ],
)
def test_guard_still_rejects_real_outfile_load_copy(sql: str, dialect: str):
    with pytest.raises(SQLGuardError):
        guard_sql(sql, dialect=dialect)


def test_guard_rejects_sqlite_load_extension():
    with pytest.raises(SQLGuardError, match="[Dd]angerous SQL function"):
        guard_sql("SELECT load_extension('evil')", dialect="sqlite")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t FOR SHARE",
        "SELECT * FROM t FOR UPDATE NOWAIT",
        "SELECT * FROM t FOR UPDATE SKIP LOCKED",
        "SELECT * FROM (SELECT * FROM t FOR UPDATE) s",
        "WITH x AS (SELECT * FROM t FOR UPDATE) SELECT * FROM x",
    ],
)
def test_guard_rejects_row_locking_in_read_only_mode(sql: str):
    with pytest.raises(SQLGuardError, match="[Ll]ock|FOR UPDATE|FOR SHARE|read-only"):
        guard_sql(sql, dialect="postgres", read_only=True)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ('SELECT "pg_sleep"(1)', "postgres"),
        ("SELECT `sleep`(1)", "mysql"),
        ('SELECT "PG_SLEEP"(1)', "postgres"),
    ],
)
def test_guard_rejects_quoted_dangerous_function_names(sql: str, dialect: str):
    with pytest.raises(SQLGuardError, match="[Dd]angerous SQL function"):
        guard_sql(sql, dialect=dialect)
