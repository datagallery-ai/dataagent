"""Database service contracts at the public SDK entry point."""

import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dataagent import DataAgentError, build_sql_service
from dataagent.data_gateway import sql_service


def test_sqlite_uses_a_shared_deadline_only_when_configured(tmp_path, monkeypatch):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE entries (value INTEGER)")
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(sql_service, "time", SimpleNamespace(monotonic=clock))
    query = "SELECT COUNT(*) FROM entries"
    with (
        build_sql_service("sqlite", {"path": str(path)}, timeout_seconds=5) as bounded,
        build_sql_service("sqlite3", {"path": str(path)}) as per_query,
    ):
        assert bounded.search_objects()["objects"][0]["table_name"] == "entries"
        assert per_query.execute(query)[1] == [(0,)]
        clock.return_value = 6.0
        with pytest.raises(DataAgentError, match="deadline exceeded"):
            bounded.execute_readonly(query, limit=1)
        assert per_query.execute(query)[1] == [(0,)]


def test_sqlite_reported_query_timeout_allows_the_next_candidate(tmp_path, monkeypatch):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE entries (value INTEGER)")
    with build_sql_service("sqlite", {"path": str(path)}) as service:
        monkeypatch.setattr(type(service), "TIME_OUT", 0)
        expensive = """
            WITH RECURSIVE numbers(n) AS (
                VALUES (1) UNION ALL SELECT n + 1 FROM numbers WHERE n < 100000
            )
            SELECT SUM(n) FROM numbers
        """
        assert service.execute(expensive) == (None, None, "interrupted")
        assert service.execute("SELECT 1") == (["1"], [(1,)], None)


@pytest.mark.parametrize(
    ("engine", "module", "extra_kwargs"),
    [("mysql", "pymysql", {"charset": "utf8mb4"}), ("gaussvector", "psycopg2", {})],
)
def test_dbapi_drivers_share_the_gateway_contract(monkeypatch, engine, module, extra_kwargs):
    config = {"host": "localhost", "port": 1234, "user": "u", "password": "p", "database": "d"}
    # Creating the service needs no driver. Only opening the connection uses it.
    monkeypatch.setitem(sys.modules, module, None)
    service = build_sql_service(engine, config)
    cursor = Mock(description=[("count",)])
    cursor.fetchmany.return_value = [(2,), (3,)]
    connection = Mock()
    connection.cursor.return_value = cursor
    driver = Mock()
    driver.connect.return_value = connection
    monkeypatch.setitem(sys.modules, module, driver)

    with service:
        assert service.explain("SELECT 1") is None
        cursor.execute.assert_called_once_with("EXPLAIN SELECT 1")
        assert service.execute("SELECT 1", limit=1) == (["count"], [(2,), (3,)], None)
        cursor.fetchmany.assert_called_once_with(2)
    driver.connect.assert_called_once_with(**config, **extra_kwargs)
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("engine", ["hive", "spark"])
def test_spark_session_is_lazy_and_closed(monkeypatch, engine):
    monkeypatch.setitem(sys.modules, "pyspark.sql", None)
    service = build_sql_service(engine, {"warehouse_dir": "/warehouse"})
    session = Mock()
    frame = session.sql.return_value
    frame.columns = ["count"]
    frame.take.return_value = [(2,), (3,)]
    frame.collect.return_value = [(2,), (3,), (4,)]
    builder = Mock()
    builder.config.return_value = builder
    builder.enableHiveSupport.return_value = builder
    builder.getOrCreate.return_value = session
    monkeypatch.setitem(
        sys.modules, "pyspark.sql", SimpleNamespace(SparkSession=SimpleNamespace(builder=builder))
    )

    with service:
        assert service.explain("SELECT 1") is None
        session.sql.assert_called_once_with("EXPLAIN SELECT 1")
        assert service.execute("SELECT 1", limit=1) == (["count"], [(2,), (3,)], None)
        frame.take.assert_called_once_with(2)
        assert service.execute("SELECT 1") == (["count"], [(2,), (3,), (4,)], None)
    builder.config.assert_called_once_with("spark.sql.warehouse.dir", "/warehouse")
    builder.getOrCreate.assert_called_once_with()
    session.stop.assert_called_once_with()
