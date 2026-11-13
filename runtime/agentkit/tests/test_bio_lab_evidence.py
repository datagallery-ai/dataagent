"""Regression checks for Bio Lab evidence at query time and schema-only answers."""

import importlib.util
import json
import sqlite3
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

SPEC = importlib.util.spec_from_file_location(
    "bio_evidence_replay", Path(__file__).parent / "e2e/bio_lab/run.py"
)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def exchange(name, payload, *, args=None):
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": "call"}]),
        ToolMessage(content=json.dumps(payload), tool_call_id="call", name=name),
    ]


def test_write_query_uses_execution_time_result(tmp_path):
    db = tmp_path / "case.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE experiments(id INTEGER, status TEXT)")
        conn.executemany("INSERT INTO experiments VALUES (?, 'NEW')", [(1,), (2,)])
    messages = exchange(
        "task",
        {
            "sql": "SELECT id FROM experiments WHERE status='NEW'",
            "executed": True,
            "columns": ["id"],
            "rows_preview": [[1]],
            "row_count": 1,
            "total_known": True,
        },
        args={"subagent_type": "nl2sql"},
    ) + [AIMessage(content="创建成功")]
    _, evidence, errors = replay.collect_evidence(messages, db, replay_sql=False)
    assert not errors
    assert evidence[0]["rows"] == [[1]]
    assert evidence[0]["row_count"] == 1
    assert replay.collect_evidence(messages, db)[2] == [
        "NL2SQL row_count differs from read-only replay"
    ]
    payload = json.loads(messages[1].content)
    payload["error"] = "execution failed"
    messages[1].content = json.dumps(payload)
    assert replay.collect_evidence(messages, db, replay_sql=False)[2]


def test_schema_only_answer_requires_successful_schema(tmp_path):
    fixtures = replay.load_cases()
    answer = AIMessage(content="SA58 的生产厂家和采购价格字段不在 schema 中，无法查询。")
    schema = {
        "database": "bio_lab",
        "tables": [{"antibodies": "抗体"}],
        "columns": {"antibodies": {"id": {"type": "TEXT"}}},
        "joins": [],
    }
    messages = exchange("data-qa__get_ontology_description_tool", schema) + [answer]
    requirement = replay.rule_for(fixtures, "TC20").get("evidence_requirement", "sql")

    def check(items):
        result = replay.collect_evidence(
            items, tmp_path / "unused.sqlite", evidence_requirement=requirement
        )
        return replay.evaluate_case(fixtures, "TC20", *result)

    assert check(messages)["status"] == "passed"
    unexecuted = exchange(
        "task", {"executed": False, "sql": "", "error": None}, args={"subagent_type": "nl2sql"}
    )
    assert check(messages[:-1] + unexecuted + [answer])["status"] == "passed"
    for invalid in (
        [answer],
        [messages[1], answer],
        exchange("data-qa__get_ontology_description_tool", {**schema, "columns": {}}) + [answer],
        [messages[0], messages[1].model_copy(update={"status": "error"}), answer],
    ):
        assert check(invalid)["status"] == "failed"


def test_write_feedback_report_uses_plugin_name(tmp_path, monkeypatch):
    fixtures = replay.load_cases()
    db = tmp_path / "db.sqlite"
    db.write_bytes(b"unchanged")
    monkeypatch.setattr(
        replay, "capture_write_effects", lambda *args: {"kind": "none", "matched": False}
    )
    messages = exchange("data-qa__request_human_feedback", "确认", args={"question": "确认创建？"})
    result = replay.evaluate_write_case(
        fixtures, "TC-W01", {"messages": messages}, db, "original", "", [], ["deliberate failure"]
    )
    assert result["hitl"]["observed_feedback_calls"] == messages[0].tool_calls
