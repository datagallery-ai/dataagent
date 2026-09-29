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
"""Unit tests for NL2SQL step4_1 schema-resolution fallback and scorecard SQL shape."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "dataagent"
    / "core"
    / "suite"
    / "builtin_suites"
    / "data_analysis"
    / "skill"
    / "nl2sql"
    / "scripts"
    / "step4_1_generate_sql.py"
)


def _load_step4_1():
    """Load the standalone step4_1 generator module from its skill script path."""
    spec = importlib.util.spec_from_file_location("step4_1_generate_sql_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def step4_1():
    """Provide a freshly imported step4_1 module with a clean warning list."""
    module = _load_step4_1()
    module.INPUT_NORMALIZATION_WARNINGS.clear()
    yield module
    module.INPUT_NORMALIZATION_WARNINGS.clear()


def test_schema_role_value_reads_feature_engineering_top_level_keys(step4_1) -> None:
    """FE schema_resolution puts user_table/user_id at the top level, without roles."""
    schema = {"user_table": "user_info", "user_id": "usid", "game_id": "game_name"}
    assert step4_1._schema_role_value(schema, "<user_table>", "user_table") == "user_info"
    assert step4_1._schema_role_value(schema, "<user_id>", "user_id") == "usid"
    assert step4_1._schema_role_value(schema, "<game_id>", "game_id") == "game_name"


def test_schema_role_value_prefers_roles_over_top_level(step4_1) -> None:
    """When both shapes exist, the explicit roles object remains authoritative."""
    schema = {
        "user_table": "wrong_table",
        "roles": {"<user_table>": "user_info", "<user_id>": "usid"},
    }
    assert step4_1._schema_role_value(schema, "<user_table>", "user_table") == "user_info"


def test_scorecard_expression_uses_arraysum_instead_of_left_plus_chain(step4_1, tmp_path: Path) -> None:
    """Scorecard SQL must not nest binary Plus once per rule (ClickHouse AST depth 1000)."""
    frame = pd.DataFrame(
        {
            "feature": [f"feat_{index}" for index in range(8)],
            "condition": ["<= 1.0000"] * 8,
            "weighted_score": ["0.1"] * 8,
        }
    )
    csv_path = tmp_path / "step3_6_score_rule.csv"
    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    candidate = step4_1.build_scorecard_candidate(csv_path)
    assert candidate.rule_count == 8
    assert candidate.parse_coverage == 1.0
    assert "arraySum(" in candidate.expression
    assert candidate.expression.count("if(") == 8
    assert "\n      + " not in candidate.expression


def test_scorecard_expression_stays_shallow_for_more_than_1000_rules(step4_1, tmp_path: Path) -> None:
    """1430-rule scorecards must not emit a 1429-deep left-associative plus chain."""
    n_rules = 1430
    frame = pd.DataFrame(
        {
            "feature": ["game_interest_u"] * n_rules,
            "condition": ["<= 1.0000"] * n_rules,
            "weighted_score": ["0.01"] * n_rules,
        }
    )
    csv_path = tmp_path / "step3_6_score_rule.csv"
    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    candidate = step4_1.build_scorecard_candidate(csv_path)
    assert candidate.rule_count == n_rules
    assert "arraySum(" in candidate.expression
    assert candidate.expression.count("if(") == n_rules
    assert candidate.expression.count("\n      + ") == 0


def test_choose_strategy_systemexit_includes_parse_and_deployment_errors(step4_1) -> None:
    """When both candidates are undeployable, SystemExit must name the actual failures."""
    tree = step4_1.CandidateSQL(
        name="decision_tree",
        expression="CAST(0 AS Float64)",
        features={"feat_a"},
        rule_count=2,
        parse_coverage=0.5,
        renderable=False,
        render_errors=["row 3: Unsupported decision-tree condition: foo"],
        deployment_errors=["reconstructed decision-tree preprocessing did not pass validation"],
    )
    scorecard = step4_1.CandidateSQL(
        name="scorecard",
        expression="CAST(0 AS Float64)",
        features={"feat_b"},
        rule_count=4,
        parse_coverage=0.0,
        renderable=False,
        render_errors=["row 2: Unsupported scorecard condition: between 1 and 2"],
        deployment_errors=["feat_b: unknown source table dim_x"],
    )
    aligned = pd.DataFrame(
        {
            "user_id": ["u1", "u2"],
            "label": [1, 0],
            "teacher_score": [0.9, 0.1],
            "tree_score": [0.8, 0.2],
            "scorecard_score": [0.7, 0.3],
        }
    )
    with pytest.raises(SystemExit) as exc_info:
        step4_1.choose_strategy(aligned, tree, scorecard)
    message = str(exc_info.value)
    assert "decision_tree" in message
    assert "parse_coverage=0.5" in message
    assert "Unsupported decision-tree condition: foo" in message
    assert "preprocessing did not pass validation" in message
    assert "scorecard" in message
    assert "parse_coverage=0.0" in message
    assert "Unsupported scorecard condition" in message
    assert "unknown source table dim_x" in message
    assert "Do not rewrite rule scores" in message


def _label_metadata(labels: list[str]) -> dict:
    """Build categorical metadata whose encoder index follows sorted label order."""
    ordered = sorted(labels)
    return {
        "kind": "categorical_label_encoder",
        "classes": ordered,
        "mapping": {label: index for index, label in enumerate(ordered)},
        "unknown_encoded_value": -1.0,
        "missing_string_value": "nan",
    }


def _assigned_bucket(label: str, plan: list[tuple[str | None, float]]) -> float:
    """Apply the same first-match string bounds the generated multiIf uses."""
    for boundary, representative in plan:
        if boundary is None or label <= boundary:
            return representative
    raise AssertionError(f"{label!r} matched no bucket")


def test_rule_card_cutpoints_follow_that_run_and_skip_unsafe_operators(step4_1, tmp_path: Path) -> None:
    """Cutpoints come from the rule card of the current run, not from fixed numbers."""
    frame = pd.DataFrame(
        {
            "condition": [
                "funny_time <= 10.00 AND other > 1.00",
                "funny_time <= 80.00",
                "funny_time > 80.00",
                "city = 3",
            ],
            "score": [0.1, 0.2, 0.3, 0.4],
        }
    )
    path = tmp_path / "step3_5_rule_card.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    cuts = step4_1._rule_card_cutpoints(path)
    assert cuts["funny_time"] == [10.0, 80.0]
    assert cuts["other"] == [1.0]
    assert "city" not in cuts


def test_small_categorical_column_keeps_one_equality_per_class(step4_1) -> None:
    """A short label list stays on the original per-class SQL even when cuts exist."""
    expression = step4_1._tree_preprocessing_expression(
        "city",
        _label_metadata(["bj", "sh", "sz"]),
        "raw_features",
        [1.0],
    )
    assert expression.count(" = ") == 3
    assert " <= " not in expression


def test_large_categorical_column_buckets_by_this_runs_cutpoints(step4_1) -> None:
    """A long label list is folded at the rule card's own cuts, whatever those numbers are."""
    labels = [f"{index:04d}" for index in range(2000)]
    metadata = _label_metadata(labels)
    thresholds = [10.0, 80.0]
    expression = step4_1._tree_preprocessing_expression(
        "funny_time",
        metadata,
        "raw_features",
        thresholds,
    )
    body = expression.split("multiIf(", 1)[1]
    assert "= 'nan'" in body
    assert "= '0000'" not in body
    assert "<= '0010'" in expression
    assert "toFloat64(0)" in expression
    assert "<= '0080'" in expression
    assert "toFloat64(11)" in expression
    assert expression.endswith("toFloat64(81))")
    ordered = step4_1._class_labels_in_index_order("funny_time", metadata["classes"], metadata["mapping"])
    plan = step4_1._threshold_bucket_plan(ordered, thresholds)
    assert plan is not None
    for index, label in ordered:
        representative = _assigned_bucket(label, plan)
        assert step4_1._same_threshold_sides(float(index), representative, thresholds)


def test_bucket_bounds_follow_label_order_not_numeric_order(step4_1) -> None:
    """String order can place '10045' before '4214'. Bounds must follow that order."""
    labels = [str(index) for index in range(2500)]
    labels.extend(["0.4260833333333333", "10045.834166666667", "4214.57305", "\\N"])
    metadata = _label_metadata(labels)
    ordered = step4_1._class_labels_in_index_order("funny_time", metadata["classes"], metadata["mapping"])
    index_by_label = {label: index for index, label in ordered}
    thresholds = [
        float(index_by_label["0.4260833333333333"]),
        float(index_by_label["10045.834166666667"]),
        float(index_by_label["4214.57305"]),
    ]
    plan = step4_1._threshold_bucket_plan(ordered, thresholds)
    assert plan is not None
    boundaries = [boundary for boundary, _representative in plan if boundary is not None]
    assert boundaries == [
        "0.4260833333333333",
        "10045.834166666667",
        "4214.57305",
    ]
    assert boundaries != sorted(boundaries, key=float)
    for index, label in ordered:
        representative = _assigned_bucket(label, plan)
        assert step4_1._same_threshold_sides(float(index), representative, thresholds)


def test_threshold_buckets_rejected_when_representative_would_change_comparisons(step4_1) -> None:
    """A negative cut that 0 does not share with the encoded index must not be bucketed."""
    ordered = [(-2, "a"), (0, "b")]
    assert step4_1._threshold_bucket_plan(ordered, [-0.5]) is None
