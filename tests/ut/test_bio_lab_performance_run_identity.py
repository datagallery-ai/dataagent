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
    assert not query_cases.TC_PENDING_QUERY_KEYS
    assert query_cases.TC_QUERY_SEQUENCES["TC24"]["needs_feedback"] is True
    assert query_cases.TC_QUERY_SEQUENCES["TC25"]["needs_feedback"] is True
