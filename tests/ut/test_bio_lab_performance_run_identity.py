import importlib
import sys
from pathlib import Path

BIO_LAB_DIR = Path(__file__).resolve().parents[1] / "e2e" / "bio_lab"


def _import_helper(module_name: str):
    sys.path.insert(0, str(BIO_LAB_DIR))
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(str(BIO_LAB_DIR))


def test_build_run_parameter_label_includes_test_parameters():
    run_identity = _import_helper("performance_run_identity")

    label = run_identity.build_run_parameter_label(
        model_choice="bailian",
        config_file="main_config_retrieve.yaml",
        quick=True,
        skip_slow=False,
        compress_message_cnt=100,
        recent_turns=50,
        cache_threshold_profile="baseline",
    )

    assert label == "quick__model-bailian__cfg-main-config-retrieve__compress-100__recent-50__threshold-baseline"


def test_build_run_parameter_label_can_include_semantic_layer_source():
    run_identity = _import_helper("performance_run_identity")

    label = run_identity.build_run_parameter_label(
        model_choice="openai",
        config_file="main_config.yaml",
        quick=False,
        skip_slow=False,
        query_group="TC",
        compress_message_cnt=200,
        recent_turns=200,
        cache_threshold_profile="off",
        semantic_layer_mode="semantic_layer",
        semantic_layer_url="http://8.92.9.219:32000",
    )

    assert (
        label
        == "tc__model-openai__cfg-main-config__sem-semantic-layer-http-8-92-9-219-32000__compress-200__recent-200__threshold-off"
    )


def test_build_cache_test_ids_place_parameter_label_after_original_directory_name():
    run_identity = _import_helper("performance_run_identity")

    user_id, session_id = run_identity.build_cache_test_ids(
        run_stamp="20260728_120000",
        run_suffix="abcd",
        parameter_label="quick__model-bailian",
    )

    assert user_id == "cache_test_user_v3_20260728_120000_abcd_quick__model-bailian"
    assert session_id == "cache_test_session_v3_20260728_120000_abcd_quick__model-bailian"


def test_semantic_layer_mock_mode_uses_inline_mock(monkeypatch):
    semantic_mock = _import_helper("performance_semantic_mock")
    monkeypatch.delenv("SEMANTIC_SERVICE_URL", raising=False)

    semantic_mock.set_mock_port(32123)
    semantic_mock.configure_semantic_layer("mock")
    config = {"SEMANTIC_LAYER": {}}

    semantic_mock._apply_semantic_layer_config(config)

    assert semantic_mock.get_semantic_layer_mode() == "mock"
    assert config["SEMANTIC_LAYER"]["base_url"] == "http://localhost:32123"


def test_semantic_layer_url_mode_requires_url(monkeypatch):
    semantic_mock = _import_helper("performance_semantic_mock")
    monkeypatch.delenv("SEMANTIC_SERVICE_URL", raising=False)

    try:
        semantic_mock.configure_semantic_layer("semantic_layer")
    except ValueError as exc:
        assert "--semantic_layer_url" in str(exc)
    else:
        raise AssertionError("semantic_layer mode should require an explicit URL")


def test_semantic_layer_url_mode_writes_config_and_no_proxy(monkeypatch):
    semantic_mock = _import_helper("performance_semantic_mock")
    monkeypatch.delenv("SEMANTIC_SERVICE_URL", raising=False)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    semantic_mock.configure_semantic_layer("semantic_layer", "http://8.92.9.219:32000")
    config = {"SEMANTIC_LAYER": {}}

    semantic_mock._apply_semantic_layer_config(config)

    assert semantic_mock.get_semantic_layer_mode() == "semantic_layer"
    assert config["SEMANTIC_LAYER"]["base_url"] == "http://8.92.9.219:32000"
    assert config["SEMANTIC_LAYER"]["verify_ssl"] is False
    assert config["SEMANTIC_LAYER"]["timeout"] == 180
    assert "8.92.9.219" in semantic_mock.os.environ["NO_PROXY"].split(",")


def test_imported_tc_group_matches_note_import_rules():
    query_cases = _import_helper("performance_query_cases")

    tc_keys = set(query_cases.TC_QUERY_SEQUENCES)
    assert not (tc_keys & {"TC09", "TC11", "TC19"})
    assert "TC21" in tc_keys
    assert {"TC01", "TC25"} <= tc_keys
    assert not query_cases.TC_AMBIGUOUS_QUERY_KEYS
    assert len(query_cases.TC_AMBIGUOUS_EXPECTED_SQLS["TC22"]) == 8
    assert not query_cases.TC_PENDING_QUERY_KEYS
    non_blocking_keys = {
        key for key, spec in query_cases.TC_NON_BLOCKING_REVIEW_CASES.items() if spec.get("skip_expected_assertion")
    }
    assert set(query_cases.TC_EXPECTED_SQL_ASSERTIONS) == (
        tc_keys - query_cases.TC_AMBIGUOUS_QUERY_KEYS - non_blocking_keys
    )
    assert query_cases.TC_QUERY_SEQUENCES["TC24"]["needs_feedback"] is True
    assert query_cases.TC_QUERY_SEQUENCES["TC25"]["needs_feedback"] is True


def test_imported_tc_expected_sql_executes_on_fixture():
    query_cases = _import_helper("performance_query_cases")
    assertions = _import_helper("performance_functional_assertions")
    db_path = BIO_LAB_DIR / "data" / "bio_lab.sqlite"

    for tc_key, spec in query_cases.TC_EXPECTED_SQL_ASSERTIONS.items():
        expected_sqls = spec.get("expected_sqls") or ([spec["expected_sql"]] if "expected_sql" in spec else [])
        if not expected_sqls:
            assert {"allow_unqueryable_answer", "allow_empty_result_pass", "absence_assertion"} & set(spec)
            continue
        for expected_sql in expected_sqls:
            result_sets, errors = assertions._execute_sql_result_sets(
                db_path,
                expected_sql,
                source=tc_key,
            )

            assert not errors, f"{tc_key} expected SQL should execute cleanly: {errors}"
            assert result_sets, f"{tc_key} expected SQL should produce a comparable result set"


def test_tc06_worst_xbb1_antibody_excludes_failed_fits():
    query_cases = _import_helper("performance_query_cases")
    assertions = _import_helper("performance_functional_assertions")
    db_path = BIO_LAB_DIR / "data" / "bio_lab.sqlite"
    tc06 = query_cases.TC_EXPECTED_SQL_ASSERTIONS["TC06"]

    assert "nifd.fit_success = 1" in tc06["expected_sql"]
    result_sets, errors = assertions._execute_sql_result_sets(
        db_path,
        tc06["expected_sql"],
        source="TC06",
    )

    assert not errors
    assert result_sets[0]["canonical_rows"] == [("LY-CoV1404", "903026", "0.177275")]


def _result_set(rows: list[tuple[str, ...]], column_count: int) -> dict:
    return {
        "source": "test.sql",
        "statement_index": 1,
        "row_count": len(rows),
        "preview_rows": [list(row) for row in rows],
        "canonical_rows": rows,
        "columns": [f"column_{index}" for index in range(column_count)],
    }


def test_tc_core_result_columns_allow_nonessential_projection_differences():
    assertions = _import_helper("performance_functional_assertions")
    expected = _result_set([("900500", "BD-368", "0.018614")], 3)
    generated = _result_set([("900500", "0.018614", "IgG1")], 3)

    matched = assertions._match_tc_result_sets(
        [expected],
        [generated],
        {"core_result_columns": {"expected": [0, 2], "generated": [0, 1]}},
    )

    assert matched is not None
    assert matched["match_mode"] == "core_result_columns"
    assert matched["core_preview_rows"] == [["900500", "0.018614"]]


def test_tc_core_json_field_compares_expected_json_with_generated_extraction():
    assertions = _import_helper("performance_functional_assertions")
    expected = _result_set([("900700", '{"p_value": 0.05}')], 2)
    generated = _result_set([("900700", "0.05")], 2)

    matched = assertions._match_tc_result_sets(
        [expected],
        [generated],
        {
            "core_json_field": {
                "expected_id_column": 0,
                "expected_json_column": 1,
                "generated_id_column": 0,
                "generated_value_column": 1,
                "json_key": "p_value",
            }
        },
    )

    assert matched is not None
    assert matched["match_mode"] == "core_json_field"


def test_tc_known_semantic_mismatch_does_not_pass_from_matching_empty_rows():
    assertions = _import_helper("performance_functional_assertions")
    empty_expected = _result_set([], 3)
    empty_generated = _result_set([], 3)

    matched = assertions._match_tc_result_sets(
        [empty_expected],
        [empty_generated],
        {"semantic_mismatch_reason": "expected scope differs from generated scope"},
    )

    assert matched is None


def test_tc_unqueryable_answer_requires_no_sql_and_schema_boundary_explanation():
    assertions = _import_helper("performance_functional_assertions")
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
    assertions = _import_helper("performance_functional_assertions")
    generated = _result_set([], 2)
    generated["sql"] = "SELECT id FROM cells WHERE name LIKE '%HeLa%'"
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


def test_tc_absence_assertion_accepts_separate_empty_checks_for_each_target():
    assertions = _import_helper("performance_functional_assertions")
    first_target = _result_set([], 1)
    first_target["sql"] = "SELECT id FROM proteins WHERE name = '抗体999'"
    second_target = _result_set([], 1)
    second_target["sql"] = "SELECT id FROM proteins WHERE name = 'SA99'"
    rule = {
        "sql_term_groups": [["抗体999"], ["sa99"]],
        "required_answer_terms": ["抗体999", "SA99"],
        "absence_answer_terms": ["不存在"],
    }
    response = {"final_answer": "抗体999 和 SA99 均不存在。", "sql_files": ["query.sql"]}

    assert assertions._matches_absence_assertion(response, [first_target, second_target], rule)
    assert not assertions._matches_absence_assertion(response, [first_target], rule)


def test_tc_json_absence_assertion_requires_full_null_projection():
    assertions = _import_helper("performance_functional_assertions")
    generated = _result_set([("1", "<NULL>"), ("2", "<NULL>")], 2)
    generated["sql"] = "SELECT id, json_extract(fit_info, '$.p_value') AS p_value FROM fit_data"
    rule = {
        "sql_term_groups": [["p_value"]],
        "required_answer_terms": ["p_value"],
        "absence_answer_terms": ["不存在"],
        "generated_null_column": 1,
        "expected_generated_row_count": 2,
        "allow_empty_generated_result": False,
    }
    response = {"final_answer": "p_value 不存在。", "sql_files": ["query.sql"]}

    assert assertions._matches_absence_assertion(response, [generated], rule)
    assert not assertions._matches_absence_assertion(
        response,
        [{**generated, "row_count": 1, "canonical_rows": [("1", "<NULL>")]}],
        rule,
    )
    assert not assertions._matches_absence_assertion(
        response,
        [{**generated, "row_count": 0, "canonical_rows": []}],
        rule,
    )


def test_tc_absence_oracles_match_the_fixture():
    assertions = _import_helper("performance_functional_assertions")
    query_cases = _import_helper("performance_query_cases")
    db_path = BIO_LAB_DIR / "data" / "bio_lab.sqlite"

    tc20_rule = query_cases.TC_EXPECTED_SQL_ASSERTIONS["TC20"]["absence_assertion"]
    assert assertions._schema_fields_are_absent(db_path, tc20_rule)

    for tc_key in ("TC21", "TC22", "TC23"):
        rule = query_cases.TC_EXPECTED_SQL_ASSERTIONS[tc_key]["absence_assertion"]
        result_sets, errors = assertions._execute_sql_result_sets(
            db_path,
            rule["oracle_sql"],
            source=tc_key,
        )
        assert not errors
        assert result_sets
        assert all(result["row_count"] == 0 for result in result_sets)
