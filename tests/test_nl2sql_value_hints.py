"""Value grounding uses bounded read-only queries, with literal SQL escaping."""

import json
import sqlite3
from pathlib import Path
from threading import Event

from dataagent.extensions.loading import PythonLoader


def test_literal_hints_use_real_rows_and_preserve_quoted_identifiers(tmp_path):
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    loader = PythonLoader()
    collect = loader.load(plugin, "nl2sql_local/hooks/value_hints.py:collect_hints")
    config_type = loader.load(plugin, "nl2sql_local/tools/database.py:Config")
    path = tmp_path / "fixture.sqlite"
    injected = "x'); DROP TABLE decoy; --"
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE "odd""table" ("text" TEXT); CREATE TABLE decoy (text TEXT);')
        db.executemany(
            'INSERT INTO "odd""table" VALUES (?)',
            [("contains triggered ability info",), (injected,)],
        )
        db.execute("INSERT INTO decoy VALUES (?)", ("unrelated content",))
    before = path.read_bytes()
    tables = [
        {"table": name, "columns": {"text": {"value_type": "TEXT"}}}
        for name in ['odd"table', "decoy"]
    ]
    question = "text contains 'triggered ability' or \"" + injected + '"'
    source = {"engine": "sqlite", "connection": {"path": str(path)}}
    report = collect(
        config_type(database={"path": str(path), "db_id": "fixture"}),
        source,
        tables,
        question,
        Event(),
    )
    assert {h["literal"] for h in report["hits"]} == {"triggered ability", injected}
    assert all(h["table"] == 'odd"table' and h["column"] == "text" for h in report["hits"])
    assert len(report["queries"]) == 2 and report["incomplete"] is True
    assert len(json.dumps(report, ensure_ascii=False)) <= 6000
    assert path.read_bytes() == before
    cancelled = Event()
    cancelled.set()
    assert (
        collect(
            config_type(database={"path": str(path), "db_id": "fixture"}),
            source,
            tables,
            question,
            cancelled,
        )["queries"]
        == []
    )
    assert (
        collect(
            config_type(database={"path": str(path), "db_id": "fixture"}),
            source,
            tables,
            "No quoted literals",
            Event(),
        )["queries"]
        == []
    )


def test_literal_extraction_has_bounds_and_decodes_sql_quotes():
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    parse = PythonLoader().load(plugin, "nl2sql_local/hooks/value_hints.py:literals")
    assert parse("name = 'Ancestor''s Chosen'; language = 'Korean'") == [
        "Ancestor's Chosen",
        "Korean",
    ]
    assert len(parse(" ".join(f'"value{i}"' for i in range(20)))) == 6
    assert parse("'x' '" + "a" * 81 + "'") == []
    assert parse("UserId = '3025'; name = 'Customer six'") == ["Customer six"]
