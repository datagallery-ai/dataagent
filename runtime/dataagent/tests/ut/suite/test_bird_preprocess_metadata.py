"""Offline coverage of the benchmark worker's asset provenance."""

import argparse
import json
from unittest.mock import AsyncMock

import pytest

from dataagent.core.suite.builtin_suites.bird_benchmark import run_bird


@pytest.mark.parametrize(
    ("settings", "metadata", "model", "digest"),
    [
        ({"model": "answer-model", "preprocess_model": "requested-model"}, None, "unknown", "unknown"),
        (
            {"semantic_preprocess_model": "actual-model", "semantic_model_digest": "actual-digest"},
            {"requested_settings": {"model": "requested-model"}, "databases": {"db": {"reused_descriptions": 89}}},
            "actual-model",
            "actual-digest",
        ),
        (
            {"preprocess_model": "current-request"},
            {
                "requested_settings": {"model": "asset-model"},
                "databases": {"db": {"reused_descriptions": 0}},
                "semantic_model_digest": "asset-digest",
            },
            "asset-model",
            "asset-digest",
        ),
        (
            {"preprocess_model": "current-request"},
            {"requested_settings": {"model": "asset-request"}, "databases": {"db": {"reused_descriptions": 89}}},
            "unknown",
            "unknown",
        ),
        (
            {},
            {
                "requested_settings": {"model": "asset-request"},
                "databases": {"db": {"reused_descriptions": 0}, "cached": {"reused_descriptions": 1}},
            },
            "unknown",
            "unknown",
        ),
        ({}, {"requested_settings": {"model": "asset-request"}, "databases": {}}, "unknown", "unknown"),
        ({}, {"requested_settings": {"model": "asset-request"}, "databases": {"db": {}}}, "unknown", "unknown"),
        (
            {},
            {"requested_settings": {"model": "other-model"}, "databases": {"other": {"reused_descriptions": 0}}},
            "unknown",
            "unknown",
        ),
    ],
)
def test_worker_passes_asset_metadata(tmp_path, monkeypatch, settings, metadata, model, digest):
    if metadata is not None:
        metadata = {"semantic_db_prefix": "bird", **metadata}
        (tmp_path / "semantic_preprocess_settings.json").write_text(json.dumps(metadata))
    settings = run_bird.resolve_options(
        {"bird_data_dir": str(tmp_path), "semantic_service_url": "http://semantic.test", **settings}, environ={}
    )
    settings["preprocess_root"] = str(tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(settings))
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps([{"db_id": "db", "question_id": 0}]))
    evaluate = AsyncMock(return_value=[])
    monkeypatch.setattr(run_bird.evaluator, "run_evaluation", evaluate)

    run_bird._worker(
        argparse.Namespace(settings=settings_path, questions=questions_path, run_dir=tmp_path / "run", resume=False)
    )

    evaluate.assert_awaited_once()
    kwargs = evaluate.call_args.kwargs
    assert kwargs["semantic_preprocess_model"] == model
    assert kwargs["semantic_model_digest"] == digest
    assert kwargs["model_name"] == settings.get("model")


def test_metadata_requires_coverage_for_all_evaluated_databases(tmp_path):
    (tmp_path / "semantic_preprocess_settings.json").write_text(
        json.dumps(
            {
                "semantic_db_prefix": "bird",
                "requested_settings": {"model": "second-model"},
                "semantic_model_digest": "second-assets",
                "databases": {"second": {"reused_descriptions": 0}},
            }
        )
    )
    assert run_bird._preprocess_metadata(
        {"preprocess_root": str(tmp_path), "semantic_db_prefix": "bird"}, {"first", "second"}
    ) == {
        "semantic_preprocess_model": "unknown",
        "semantic_model_digest": "unknown",
    }


def test_metadata_cli_environment_and_saved_options():
    args = run_bird._parser().parse_args(
        ["run", "--semantic-preprocess-model", "cli-model", "--semantic-model-digest", "cli-digest"]
    )
    env = {"BIRD_SEMANTIC_PREPROCESS_MODEL": "env-model", "BIRD_SEMANTIC_MODEL_DIGEST": "env-digest"}
    resolved = run_bird.resolve_options(vars(args), environ=env)
    assert (resolved["semantic_preprocess_model"], resolved["semantic_model_digest"]) == ("cli-model", "cli-digest")
    resolved = run_bird.resolve_options({}, environ=env)
    assert (resolved["semantic_preprocess_model"], resolved["semantic_model_digest"]) == ("env-model", "env-digest")
    assert run_bird.resolve_options({}, environ={}, saved=resolved) == resolved


@pytest.mark.parametrize("record_prefix", ["new_assets", "old_assets", None])
@pytest.mark.parametrize(
    "explicit", [{}, {"semantic_preprocess_model": "explicit-model"}, {"semantic_model_digest": "explicit-digest"}]
)
def test_metadata_requires_matching_namespace(tmp_path, record_prefix, explicit):
    metadata = {
        "semantic_db_prefix": record_prefix,
        "requested_settings": {"model": "asset-model"},
        "semantic_model_digest": "asset-digest",
        "databases": {"financial": {"reused_descriptions": 0}},
    }
    (tmp_path / "semantic_preprocess_settings.json").write_text(json.dumps(metadata))
    settings = {"preprocess_root": str(tmp_path), "semantic_db_prefix": "new_assets", **explicit}
    matched = record_prefix == "new_assets"
    assert run_bird._preprocess_metadata(settings, {"financial"}) == {
        "semantic_preprocess_model": explicit.get("semantic_preprocess_model", "asset-model" if matched else "unknown"),
        "semantic_model_digest": explicit.get("semantic_model_digest", "asset-digest" if matched else "unknown"),
    }
