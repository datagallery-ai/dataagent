"""Focused tests for Bio Lab TC answer / TC-W DB-effect assertions."""

import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

BIO_LAB_DIR = Path(__file__).resolve().parents[1] / "e2e" / "bio_lab"
sys.path.insert(0, str(BIO_LAB_DIR))

import performance_cache_analysis as cache_analysis  # noqa: E402
import performance_functional_assertions as assertions  # noqa: E402
import performance_query_cases as query_cases  # noqa: E402


def test_tc_expected_assertions_exclude_ambiguous_cases():
    tc_keys = set(query_cases.TC_QUERY_SEQUENCES)

    assert not (tc_keys & {"TC09", "TC11", "TC19"})
    assert "TC21" in tc_keys
    assert not query_cases.TC_AMBIGUOUS_QUERY_KEYS
    assert not query_cases.TC_PENDING_QUERY_KEYS
    assert {"TC10", "TC22"} == set(query_cases.TC_NON_BLOCKING_REVIEW_CASES)
    review_only_keys = {
        key for key, spec in query_cases.TC_NON_BLOCKING_REVIEW_CASES.items() if spec.get("skip_expected_assertion")
    }
    assert review_only_keys == {"TC10"}
    assert set(query_cases.TC_EXPECTED_ANSWER_ASSERTIONS) == tc_keys


def test_tcw_cases_are_registered_with_assertions_and_rollback_flags():
    tcw_keys = set(query_cases.TCW_QUERY_SEQUENCES)

    assert len(tcw_keys) == 15
    assert query_cases.select_query_keys("TC-W", query_numbers=[1, 15]) == ["TC-W01", "TC-W15"]
    assert set(query_cases.TCW_EXPECTED_ANSWER_ASSERTIONS) == tcw_keys
    assert set(query_cases.TCW_DB_EFFECT_ASSERTIONS) == tcw_keys
    assert {key for key, spec in query_cases.TCW_QUERY_SEQUENCES.items() if spec.get("rollback_database")} == tcw_keys
    assert all(key in query_cases.ALL_EXPECTED_ANSWER_ASSERTIONS for key in tcw_keys)
    assert all(key in query_cases.QUERY_GROUPS["TC-W"] for key in tcw_keys)


def test_tcw_database_effect_capture_finds_created_target_experiment(tmp_path):
    source_db = BIO_LAB_DIR / "data" / "bio_lab.sqlite"
    db_path = tmp_path / "bio_lab.sqlite"
    shutil.copy2(source_db, db_path)

    with sqlite3.connect(db_path) as conn:
        experiment_id = 990901
        today = assertions.datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            """
            INSERT INTO experiments (id, start_date, submitter, operator, status, type)
            VALUES (?, ?, 900001, NULL, 'NEW', 'neutralization')
            """,
            (experiment_id, today),
        )
        conn.execute(
            """
            INSERT INTO neutralization_experiments (
                id, cell_sample_id, inhibitor_sample_id, pseudovirus_sample_id,
                inhibitor_concentration, dilution_factors, result_id
            )
            VALUES (?, 800001, 11396, 900412, 1, '[5, 5, 5, 5]', NULL)
            """,
            (experiment_id,),
        )

    captured = assertions.capture_tcw_database_effects(db_path, "TC-W01")

    assert captured["matched"]
    assert captured["created_experiment_ids"] == [experiment_id]


def test_tc_manual_review_is_reported_without_changing_a_passing_assertion():
    tc_results = {"TC22": {"status": "passed_by_answer_assertion"}}
    responses = {
        "TC10": {"sql_files": ["cells.sql"], "final_answer": "No cells without STORED samples"},
        "TC22": {"sql_files": ["sars.sql"], "final_answer": "No matching relation"},
    }

    assertions._record_non_blocking_tc_reviews(responses, tc_results)

    assert tc_results["TC10"]["status"] == "passed_with_non_discriminating_fixture"
    assert tc_results["TC10"]["manual_review"]["blocking"] is False
    assert tc_results["TC22"]["status"] == "passed_by_answer_assertion"
    assert tc_results["TC22"]["manual_review"]["blocking"] is False


def test_tc_answer_assertion_matches_precomputed_facts():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC05"]

    matched = assertions._match_answer_terms("最强的是 BD-368，最低 IC50 为 0.018614。", spec)

    assert matched["matched"]
    assert not matched["missing_terms"]


def test_tc08_answer_assertion_only_requires_r2_value():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC08"]

    matched = assertions._match_answer_terms("查询结果显示 R2 拟合优度为 0.98。", spec)

    assert matched["matched"]
    assert not matched["missing_terms"]


def test_tc_answer_assertion_does_not_require_unasked_full_id_lists():
    tc07 = assertions._match_answer_terms(
        "共有 44 个实验，其中 43 个是 COMPLETED，1 个是 NEW。",
        query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC07"],
    )
    tc15 = assertions._match_answer_terms(
        "共有 38 个状态为 COMPLETED 且使用 3-param logistic 模型的 IC50 拟合实验。",
        query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC15"],
    )

    assert tc07["matched"]
    assert tc15["matched"]


def test_tc23_answer_assertion_does_not_require_enumerated_absence_language():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC23"]

    matched = assertions._match_answer_terms('fit_info 的 "p_value" 检查结果为 0 项可用值。', spec)

    assert matched["matched"]
    assert not matched["missing_absence_terms"]


def test_tc23_sql_evidence_accepts_all_null_or_empty_p_value_result():
    rule = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC23"]["sql_evidence"]
    all_null_generated = {
        "source": "test.sql",
        "statement_index": 1,
        "row_count": 2,
        "preview_rows": [["900700", None], ["900701", None]],
        "canonical_rows": [("900700", "<NULL>"), ("900701", "<NULL>")],
        "columns": ["id", "p_value"],
        "sql": "WITH fits AS (SELECT * FROM neutralization_ic50_fit_data) "
        "SELECT id, fit_info ->> '$.p_value' FROM fits",
    }
    empty_generated = {
        "source": "empty.sql",
        "statement_index": 1,
        "row_count": 0,
        "preview_rows": [],
        "canonical_rows": [],
        "columns": ["id", "p_value"],
        "sql": "SELECT id, json_extract(fit_info, '$.p_value') AS p_value "
        "FROM neutralization_ic50_fit_data "
        "WHERE json_extract(fit_info, '$.p_value') IS NOT NULL",
    }

    matched = assertions._matches_sql_evidence([all_null_generated], rule)

    assert matched is not None
    assert matched["row_count"] == 2
    empty_matched = assertions._matches_sql_evidence([empty_generated], rule)
    assert empty_matched is not None
    assert empty_matched["row_count"] == 0


def test_tc23_sql_evidence_rejects_non_null_or_irrelevant_results():
    rule = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC23"]["sql_evidence"]
    generated = {
        "source": "test.sql",
        "statement_index": 1,
        "row_count": 1,
        "preview_rows": [["900700", "0.05"]],
        "canonical_rows": [("900700", "0.05")],
        "columns": ["id", "p_value"],
        "sql": "SELECT id, p_value FROM fit_results",
    }

    assert assertions._matches_sql_evidence([generated], rule) is None
    assert assertions._matches_sql_evidence([{**generated, "sql": "SELECT id, r2 FROM fit_results"}], rule) is None


def test_extract_final_assistant_text_prefers_last_completed_todo_content():
    messages = [
        _ai("查询结果如下：900700 0.98", tool_calls=[_tool_call("complete_current_todo", "call_1")]),
        _tool("Todo '汇总并呈现结果' completed successfully.", "call_1"),
        _ai("任务已完成。以下是本次查询的总结：900700 0.98"),
    ]

    assert cache_analysis._extract_final_assistant_text(messages) == "查询结果如下：900700 0.98"


def test_extract_final_assistant_text_uses_response_after_completed_todo_when_tool_call_has_no_content():
    messages = [
        _ai("", tool_calls=[_tool_call("complete_current_todo", "call_1")]),
        _tool("Todo '查询数据' completed successfully.", "call_1"),
        _ai("根据查询结果，VSV 的平均 IC50 为 0.1648。"),
    ]

    assert cache_analysis._extract_final_assistant_text(messages) == "根据查询结果，VSV 的平均 IC50 为 0.1648。"


def test_extract_final_assistant_text_ignores_error_after_completed_todo_answer():
    messages = [
        _ai(
            "共有 5 种抗体：BD-368、SA58、LY-CoV1404、S2E12、COV2-2196。",
            tool_calls=[_tool_call("complete_current_todo", "call_1")],
        ),
        _tool("Todo '汇总并呈现结果' completed successfully.", "call_1"),
        _ai("推理执行错误: [repetition_detected repetition]", additional_kwargs={"error": True}),
    ]

    assert (
        cache_analysis._extract_final_assistant_text(messages)
        == "共有 5 种抗体：BD-368、SA58、LY-CoV1404、S2E12、COV2-2196。"
    )


def test_extract_final_assistant_text_falls_back_without_plan():
    messages = [
        _ai("", tool_calls=[_tool_call("nl2sql_sub_agent_tool", "call_1")]),
        _tool("SQL 执行完成", "call_1"),
        _ai("针对 JN.1，最强的是 BD-368，IC50 为 0.018614。"),
    ]

    assert cache_analysis._extract_final_assistant_text(messages) == "针对 JN.1，最强的是 BD-368，IC50 为 0.018614。"


def test_tc_answer_assertion_without_absence_language_is_advisory_only():
    """Absence vocabulary missing no longer fails: terms present -> matched; absence is advisory."""
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC21"]

    matched = assertions._match_answer_terms("抗体999 和 SA99 的重链 DNA 序列如下。", spec)

    assert matched["matched"]
    assert matched["missing_absence_terms"]
    assert not matched["has_absence_answer"]


def test_check_answer_assertion_absence_missing_is_non_blocking_manual_review():
    """Absence vocabulary missing -> requires_manual_review, non-blocking (not a hard fail)."""
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC21"]

    # Terms present but no absence language (e.g. agent invented data instead of reporting none).
    result = assertions._check_answer_assertion(
        {"final_answer": "抗体999 和 SA99 的重链 DNA 序列如下：ATCG…", "hitl_triggered": False},
        spec,
    )

    assert result["status"] == "requires_manual_review"
    assert result["blocking"] is False
    assert result["manual_review_required"] is True
    assert result["missing_absence_terms"]


def test_check_answer_assertion_absence_present_passes_when_terms_match():
    """Absence vocabulary present + required terms match -> passed_by_answer_assertion."""
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC21"]

    result = assertions._check_answer_assertion(
        {"final_answer": "抗体999 和 SA99 不存在，无法提供重链 DNA 序列。", "hitl_triggered": False},
        spec,
    )

    assert result["status"] == "passed_by_answer_assertion"
    assert result["blocking"] is True


def test_absent_tc_answer_correctness_accepts_human_feedback_pass(tmp_path):
    results = assertions.verify_tc_answer_assertions(
        {
            "TC20": {
                "final_answer": "待确认后按空结果处理。",
                "hitl_triggered": True,
            },
            "TC21": {
                "final_answer": "待确认后按空结果处理。",
                "hitl_triggered": True,
            },
            "TC22": {
                "final_answer": "待确认后按空结果处理。",
                "hitl_triggered": True,
            },
            "TC23": {
                "final_answer": "待确认后按空结果处理。",
                "hitl_triggered": True,
            },
            "TC24": {
                "final_answer": "待确认后按空结果处理。",
                "hitl_triggered": True,
            },
        },
        tmp_path,
    )

    assert results["TC20"]["status"] == "passed_by_human_feedback"
    assert results["TC21"]["status"] == "passed_by_human_feedback"
    assert results["TC22"]["status"] == "passed_by_human_feedback"
    assert results["TC23"]["status"] == "passed_by_human_feedback"
    assert results["TC24"]["status"] == "passed_by_human_feedback"
    assert (tmp_path / "tc_answer_functional_results_v3.json").exists()


def test_soft_required_terms_do_not_block_but_are_reported():
    """Soft (query-echo) terms are advisory: missing them does not fail the run."""
    spec = {
        "expected_summary": "test",
        "required_terms": ["38"],
        "soft_required_terms": ["COMPLETED", "3-param logistic"],
    }
    result = assertions._match_answer_terms("共有 38 个实验。", spec)
    assert result["matched"]
    assert result["missing_soft_terms"] == ["COMPLETED", "3-param logistic"]

    check = assertions._check_answer_assertion(
        {"final_answer": "共有 38 个实验。", "hitl_triggered": False},
        spec,
    )
    assert check["status"] == "passed_by_answer_assertion"
    assert check["missing_soft_terms"] == ["COMPLETED", "3-param logistic"]


def test_tc20_missing_entity_name_is_soft_non_blocking():
    """TC20: missing SA58 (query-echo) does not fail — only absence language matters."""
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC20"]
    result = assertions._check_answer_assertion(
        {"final_answer": "当前数据库没有生产厂家和采购价格字段，无法提供。", "hitl_triggered": False},
        spec,
    )
    assert result["status"] == "passed_by_answer_assertion"
    assert "SA58" in result["missing_soft_terms"]


def test_tc25_missing_user_name_is_soft_non_blocking():
    """TC25: missing 小张 (query-echo) does not fail — only absence language matters."""
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC25"]
    result = assertions._check_answer_assertion(
        {"final_answer": "不存在还没开始但已经做完的实验，这是逻辑矛盾。", "hitl_triggered": False},
        spec,
    )
    assert result["status"] == "passed_by_answer_assertion"
    assert "小张" in result["missing_soft_terms"]


def _ai(content: str, *, tool_calls: list[dict[str, object]] | None = None, additional_kwargs: dict | None = None):
    return SimpleNamespace(
        type="ai",
        content=content,
        tool_calls=tool_calls or [],
        additional_kwargs=additional_kwargs or {},
    )


def _tool(content: str, tool_call_id: str):
    return SimpleNamespace(
        type="tool",
        content=content,
        tool_call_id=tool_call_id,
    )


def _tool_call(name: str, tool_call_id: str):
    return {"name": name, "id": tool_call_id, "args": {}, "type": "tool_call"}
