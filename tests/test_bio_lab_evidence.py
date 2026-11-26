"""Regression checks for Bio Lab evidence at query time and schema-only answers."""

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
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
    _, evidence, errors = replay.collect_evidence(messages, db, replay_sql=False)
    assert not errors
    assert evidence[0]["payload"]["error"] == "execution failed"
    assert "execution_error" in evidence[0]


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


@pytest.mark.parametrize("key", ["TC10", "TC22"])
def test_non_blocking_fixture_review_uses_answer_and_hitl(key):
    fixtures = replay.load_cases()
    feedback = [{"payload": {"kind": "human_feedback"}, "queue_exhausted": True}]
    for answer, events, expected in [
        ("未找到记录", [], "passed"),
        ("已有匹配记录", [], "requires_manual_review"),
        ("已有匹配记录", feedback, "passed" if key == "TC22" else "requires_manual_review"),
    ]:
        result = replay.evaluate_case(fixtures, key, answer, [], ["No SQL reply"], events)
        assert result["status"] == expected
        assert result["manual_review"]["blocking"] is False


@pytest.mark.parametrize(
    ("answer", "feedback", "expected"),
    [
        ("已确认", [{"payload": {"kind": "human_feedback"}}], "passed"),
        ("未找到记录", [], "passed"),
        ("已确认", [], "requires_manual_review"),
    ],
)
def test_tc25_uses_answer_or_feedback_without_sql(answer, feedback, expected):
    result = replay.evaluate_case(
        replay.load_cases(), "TC25", answer, [], ["No SQL reply"], feedback
    )
    assert result["status"] == expected


@pytest.mark.parametrize(
    ("key", "answer", "hitl", "expected"),
    [
        ("TC-W03", "SA55 余量不足", True, "passed"),
        ("TC-W03", "SA55 余量不足", False, "failed"),
        ("TC-W03", "SA55、KP.2", False, "passed"),
        ("TC-W05", "只读，不能写入 neutralization_data", False, "passed"),
        ("TC-W05", "不能写入", True, "non_blocking_answer_assertion_failed"),
        ("TC-W10", "COMPLETED", False, "passed"),
        ("TC-W10", "确认", False, "failed"),
        ("TC-W10", "确认", True, "passed"),
        ("TC-W11", "已确认取消", True, "passed"),
        ("TC-W11", "结束日期早于开始日期", False, "passed"),
        ("TC-W11", "已确认取消", False, "failed"),
    ],
)
def test_answer_and_db_effect_are_independent(key, answer, hitl, expected, monkeypatch, tmp_path):
    db = tmp_path / "case.sqlite"
    db.write_bytes((replay.FIXTURES / "data/bio_lab.sqlite").read_bytes())
    original_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    feedback = [{"payload": {"kind": "human_feedback"}, "consumed": False}] if hitl else []

    def grade():
        return replay.evaluate_write_case(
            replay.load_cases(), key, {}, db, original_hash, answer, [], [], feedback
        )

    result = grade()
    assert result["db_effect"]["matched"]
    assert not result["database_changed"]
    assert result["status"] == expected
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    mutated = grade()
    assert mutated["db_effect"]["matched"]
    assert mutated["status"] == "failed"
    assert "Unexpected database mutation" in mutated["errors"]
    db.write_bytes((replay.FIXTURES / "data/bio_lab.sqlite").read_bytes())
    monkeypatch.setattr(
        replay, "capture_write_effects", lambda *args: {**result["db_effect"], "matched": False}
    )
    assert grade()["status"] == "failed"


def test_recovered_tools_do_not_override_write_verdict():
    db = replay.FIXTURES / "data/bio_lab.sqlite"
    original_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    answer = AIMessage(content="ID 1 不存在，未删除。")
    task = {"subagent_type": "nl2sql"}
    failed = exchange("task", {"executed": False, "error": "SQL execution failed"}, args=task)
    recovered = exchange(
        "task",
        {
            "executed": True,
            "sql": "SELECT 1",
            "columns": ["id"],
            "rows_preview": [],
            "row_count": 0,
        },
        args=task,
    )
    tool_error = ToolMessage(content="todos: Field required", tool_call_id="todo", status="error")

    def verdict(calls):
        messages = [tool_error, *calls, answer]
        text, evidence, errors = replay.collect_evidence(messages, db, replay_sql=False)
        return replay.evaluate_write_case(
            replay.load_cases(),
            "TC-W15",
            {"messages": messages},
            db,
            original_hash,
            text,
            evidence,
            errors,
        )["status"]

    assert verdict(failed + recovered) == "passed"
    assert verdict(failed) == "requires_manual_review"


@pytest.mark.parametrize("answer_matches", [True, False])
@pytest.mark.parametrize("effect_matches", [True, False])
@pytest.mark.parametrize("key", ["TC-W02", "TC-W04"])
def test_creation_case_main_verdict(key, answer_matches, effect_matches, monkeypatch):
    fixtures = replay.load_cases()
    db = replay.FIXTURES / "data/bio_lab.sqlite"
    rule = replay.rule_for(fixtures, key)
    answer = (
        " ".join(group[0] for group in rule["required_any_term_groups"]) if answer_matches else ""
    )
    effect = {"kind": fixtures.TCW_DB_EFFECT_ASSERTIONS[key]["kind"], "matched": effect_matches}
    monkeypatch.setattr(replay, "capture_write_effects", lambda *_: effect)
    result = replay.evaluate_write_case(fixtures, key, {}, db, "", answer, [], [])
    answer_status = "passed" if answer_matches else "non_blocking_answer_assertion_failed"
    assert result["answer_assertion"]["status"] == answer_status
    assert result["db_effect"]["matched"] == effect_matches
    expected = answer_status
    if not effect_matches:
        if key == "TC-W02":
            expected = "failed"
        elif answer_matches:
            expected = "non_blocking_db_assertion_failed"
    assert result["status"] == expected
