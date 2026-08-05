"""Focused tests for Bio Lab TC SQL functional assertions."""

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
    assert len(query_cases.TC_AMBIGUOUS_EXPECTED_SQLS["TC22"]) == 8
    assert not query_cases.TC_PENDING_QUERY_KEYS
    assert {"TC10", "TC22"} == set(query_cases.TC_NON_BLOCKING_REVIEW_CASES)
    review_only_keys = {
        key for key, spec in query_cases.TC_NON_BLOCKING_REVIEW_CASES.items() if spec.get("skip_expected_assertion")
    }
    assert review_only_keys == {"TC10"}
    assert set(query_cases.TC_EXPECTED_SQL_ASSERTIONS) == tc_keys - (
        query_cases.TC_AMBIGUOUS_QUERY_KEYS | query_cases.TC_PENDING_QUERY_KEYS | review_only_keys
    )
    assert set(query_cases.TC_EXPECTED_ANSWER_ASSERTIONS) == tc_keys


def test_tc_expected_sql_executes_on_fixture():
    db_path = BIO_LAB_DIR / "data" / "bio_lab.sqlite"

    for tc_key, spec in query_cases.TC_EXPECTED_SQL_ASSERTIONS.items():
        expected_sqls = spec.get("expected_sqls") or ([spec["expected_sql"]] if "expected_sql" in spec else [])
        if not expected_sqls:
            assert {"allow_unqueryable_answer", "allow_empty_result_pass", "absence_assertion"} & set(spec)
            continue
        for expected_sql in expected_sqls:
            result_sets, errors = assertions._execute_sql_result_sets(db_path, expected_sql, source=tc_key)

            assert not errors, f"{tc_key} expected SQL should execute cleanly: {errors}"
            assert result_sets, f"{tc_key} expected SQL should produce a comparable result set"


def test_tc_unqueryable_answer_requires_no_sql_and_schema_boundary_explanation():
    rule = {
        "required_terms": ["生产厂家", "采购价格"],
        "unavailable_terms": ["没有存储", "未存储", "无法查询", "不支持查询"],
    }

    assert assertions._matches_unqueryable_answer(
        {"final_answer": "当前系统没有存储抗体的生产厂家和采购价格信息。", "sql_files": []},
        rule,
    )
    assert not assertions._matches_unqueryable_answer(
        {"final_answer": "当前系统没有存储抗体的生产厂家和采购价格信息。", "sql_files": ["query.sql"]},
        rule,
    )


def test_tc_absence_assertion_requires_relevant_empty_sql_and_absence_answer():
    generated = {
        "source": "test.sql",
        "statement_index": 1,
        "row_count": 0,
        "preview_rows": [],
        "canonical_rows": [],
        "columns": ["id", "name"],
        "sql": "SELECT id FROM cells WHERE name LIKE '%HeLa%'",
    }
    rule = {
        "sql_term_groups": [["hela"]],
        "required_answer_terms": ["HeLa"],
        "absence_answer_terms": ["未找到", "不存在"],
    }

    assert assertions._matches_absence_assertion(
        {"final_answer": "未找到 HeLa 对应的中和原始读数。", "sql_files": ["query.sql"]},
        [generated],
        rule,
    )
    assert not assertions._matches_absence_assertion(
        {"final_answer": "找到 HeLa 对应的中和原始读数。", "sql_files": ["query.sql"]},
        [generated],
        rule,
    )
    assert not assertions._matches_absence_assertion(
        {"final_answer": "未找到 HeLa 对应的中和原始读数。", "sql_files": ["query.sql"]},
        [
            {**generated, "row_count": 1, "canonical_rows": [["1", "HeLa"]]},
            {**generated, "sql": "SELECT id FROM cells WHERE name LIKE '%Vero%'"},
        ],
        rule,
    )


def test_tc_manual_review_is_reported_without_changing_a_passing_assertion():
    tc_results = {"TC22": {"status": "passed_by_absence_assertion"}}
    responses = {
        "TC10": {"sql_files": ["cells.sql"], "final_answer": "No cells without STORED samples"},
        "TC22": {"sql_files": ["sars.sql"], "final_answer": "No matching relation"},
    }

    assertions._record_non_blocking_tc_reviews(responses, tc_results)

    assert tc_results["TC10"]["status"] == "passed_with_non_discriminating_fixture"
    assert tc_results["TC10"]["manual_review"]["blocking"] is False
    assert tc_results["TC22"]["status"] == "passed_by_absence_assertion"
    assert tc_results["TC22"]["manual_review"]["blocking"] is False


def test_tc_answer_assertion_matches_precomputed_facts():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC05"]

    matched = assertions._matches_tc_answer_assertion("最强的是 BD-368，最低 IC50 为 0.018614。", spec)

    assert matched["matched"]
    assert not matched["missing_terms"]


def test_tc08_answer_assertion_only_requires_r2_value():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC08"]

    matched = assertions._matches_tc_answer_assertion("查询结果显示 R2 拟合优度为 0.98。", spec)

    assert matched["matched"]
    assert not matched["missing_terms"]


def test_tc_answer_assertion_does_not_require_unasked_full_id_lists():
    tc07 = assertions._matches_tc_answer_assertion(
        "共有 44 个实验，其中 43 个是 COMPLETED，1 个是 NEW。",
        query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC07"],
    )
    tc15 = assertions._matches_tc_answer_assertion(
        "共有 38 个状态为 COMPLETED 且使用 3-param logistic 模型的 IC50 拟合实验。",
        query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC15"],
    )

    assert tc07["matched"]
    assert tc15["matched"]


def test_tc23_answer_assertion_does_not_require_enumerated_absence_language():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC23"]

    matched = assertions._matches_tc_answer_assertion('fit_info 的 "p_value" 检查结果为 0 项可用值。', spec)

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


def test_tc_answer_assertion_requires_absence_language():
    spec = query_cases.TC_EXPECTED_ANSWER_ASSERTIONS["TC21"]

    matched = assertions._matches_tc_answer_assertion("抗体999 和 SA99 的重链 DNA 序列如下。", spec)

    assert not matched["matched"]
    assert matched["missing_absence_terms"]


def test_absent_tc_answer_correctness_accepts_human_feedback_pass(tmp_path):
    results = assertions._verify_tc_answer_correctness(
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
