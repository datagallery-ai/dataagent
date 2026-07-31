"""Reconstruct and validate preprocessing used by the distilled decision tree.

The model-engineering script fits numeric quantile bins on the training wide
table and categorical label encoders on the concatenated train/validation
values, but historical artifacts do not persist those fitted transformers.
This local-only step reconstructs their deployable metadata from published
artifacts and validates it against the exported decision-tree leaf rules and
validation scores.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", ".")).resolve()
TEMPLATE_PATH = Path(
    os.environ.get(
        "NL2SQL_PREPROCESS_TEMPLATE_PATH",
        str(Path(__file__).resolve()),
    )
).resolve()
CHANGE_REASON = os.environ.get("NL2SQL_PREPROCESS_CHANGE_REASON", "").strip()
SCORE_TOLERANCE = float(os.environ.get("NL2SQL_TREE_SCORE_TOLERANCE", "0.00051"))

REQUIRED_INPUTS = (
    "step3_3_wide_table_train.csv",
    "step3_3_wide_table_valid.csv",
    "step3_3_univariate_analysis.csv",
    "step3_4_feature_importance.csv",
    "step3_5_rule_card.csv",
    "step3_5_white_box_scores.csv",
    "step3_5_model_report.json",
)

TREE_ATOM = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(<=|>=|!=|=|<|>)\s*"
    r"(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*"
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_json_safe(value), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance() -> dict[str, Any]:
    executed_path = Path(__file__).resolve()
    template_hash = _sha256_file(TEMPLATE_PATH)
    executed_hash = _sha256_file(executed_path)
    modified = bool(template_hash and executed_hash and template_hash != executed_hash)
    warnings: list[str] = []
    if not TEMPLATE_PATH.is_file():
        warnings.append("template_path_does_not_exist")
    if modified and not CHANGE_REASON:
        warnings.append("modified_script_missing_change_reason")
    return {
        "template_path": str(TEMPLATE_PATH),
        "template_sha256": template_hash,
        "executed_path": str(executed_path),
        "executed_sha256": executed_hash,
        "working_copy_modified": modified,
        "change_reason": CHANGE_REASON or None,
        "warnings": warnings,
    }


def _ensure_script_artifact() -> None:
    artifact = OUTPUT_DIR / "scripts" / Path(__file__).name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    executed = Path(__file__).resolve()
    if artifact.resolve() != executed:
        artifact.write_bytes(executed.read_bytes())


def _require_inputs() -> None:
    missing = [name for name in REQUIRED_INPUTS if not (OUTPUT_DIR / name).is_file()]
    if missing:
        raise SystemExit(
            "Missing tree-preprocessing reconstruction inputs: " + ", ".join(missing)
        )


def _feature_names_from_rules(rules: pd.DataFrame) -> set[str]:
    features: set[str] = set()
    for condition in rules["condition"].astype(str):
        for atom in re.split(r"\s+AND\s+", condition, flags=re.I):
            match = TREE_ATOM.fullmatch(atom)
            if match:
                features.add(match.group(1).split(".")[-1])
    return features


def _requested_bins(feature: str, analysis: dict[str, dict[str, Any]]) -> int:
    raw_unique = analysis.get(feature, {}).get("n_unique")
    try:
        unique_count = int(float(raw_unique))
    except (TypeError, ValueError):
        return 5
    return min(5, unique_count) if unique_count <= 20 else 5


def _numeric_metadata(
    feature: str,
    train: pd.Series,
    requested_bins: int,
) -> tuple[dict[str, Any], pd.Series]:
    numeric = pd.to_numeric(train, errors="coerce")
    non_null = numeric.dropna()
    if len(non_null) <= 10:
        metadata = {
            "kind": "numeric_identity",
            "fit_non_null_rows": len(non_null),
            "missing_encoded_value": -1.0,
        }
        return metadata, numeric.fillna(-1.0).astype(float)

    # sklearn's quantile KBinsDiscretizer uses linear percentiles and removes
    # consecutive edges whose width is <= 1e-8.
    percentiles = np.linspace(0.0, 100.0, requested_bins + 1)
    raw_edges = np.percentile(
        non_null.to_numpy(dtype=float),
        percentiles,
        method="linear",
    )
    kept_edges = [float(raw_edges[0])]
    for edge in raw_edges[1:]:
        if float(edge) - kept_edges[-1] > 1e-8:
            kept_edges.append(float(edge))
    if len(kept_edges) == 1:
        kept_edges.append(float(raw_edges[-1]))

    metadata = {
        "kind": "numeric_quantile_ordinal",
        "requested_n_bins": requested_bins,
        "effective_n_bins": max(1, len(kept_edges) - 1),
        "bin_edges": kept_edges,
        "fit_non_null_rows": len(non_null),
        "missing_encoded_value": -1.0,
        "boundary_semantics": "searchsorted_internal_edges_side_right",
    }
    return metadata, _transform_numeric(numeric, metadata)


def _transform_numeric(values: pd.Series, metadata: dict[str, Any]) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    if metadata["kind"] == "numeric_identity":
        return numeric.fillna(float(metadata["missing_encoded_value"])).astype(float)
    internal_edges = np.asarray(metadata["bin_edges"][1:-1], dtype=float)
    transformed = np.searchsorted(
        internal_edges,
        numeric.fillna(0).to_numpy(dtype=float),
        side="right",
    ).astype(float)
    transformed[numeric.isna().to_numpy()] = float(metadata["missing_encoded_value"])
    return pd.Series(transformed, index=values.index)


def _categorical_metadata(
    train: pd.Series,
    valid: pd.Series,
) -> tuple[dict[str, Any], pd.Series]:
    # Preserve the historical model script exactly: astype(str) occurs before
    # fillna, so a missing value is represented by the literal string "nan".
    all_values = pd.concat([train.astype(str), valid.astype(str)], ignore_index=True)
    classes = [str(value) for value in np.unique(all_values.to_numpy(dtype=str))]
    mapping = {value: index for index, value in enumerate(classes)}
    encoded_valid = valid.astype(str).map(mapping)
    metadata = {
        "kind": "categorical_label_encoder",
        "classes": classes,
        "mapping": mapping,
        "unknown_encoded_value": -1.0,
        "missing_string_value": "nan",
        "fit_scope": "train_and_valid_concatenated",
    }
    return metadata, encoded_valid.fillna(-1.0).astype(float)


def _transform_feature(values: pd.Series, metadata: dict[str, Any]) -> pd.Series:
    if metadata["kind"].startswith("numeric_"):
        return _transform_numeric(values, metadata)
    mapping = {str(key): int(value) for key, value in metadata["mapping"].items()}
    return values.astype(str).map(mapping).fillna(
        float(metadata["unknown_encoded_value"])
    ).astype(float)


def _evaluate_atom(values: np.ndarray, operator: str, threshold: float) -> np.ndarray:
    return {
        "<=": values <= threshold,
        ">=": values >= threshold,
        "!=": values != threshold,
        "=": values == threshold,
        "<": values < threshold,
        ">": values > threshold,
    }[operator]


def _validate_rules(
    rules: pd.DataFrame,
    valid_encoded: pd.DataFrame,
    exported_scores: pd.DataFrame,
    valid: pd.DataFrame,
) -> dict[str, Any]:
    label_candidates = [column for column in exported_scores if column.lower() == "label"]
    score_candidates = [
        column for column in exported_scores if column == "white_box_score"
    ]
    if not label_candidates or not score_candidates:
        raise SystemExit(
            "step3_5_white_box_scores.csv must contain label and white_box_score"
        )
    excluded = {label_candidates[0], score_candidates[0]}
    user_candidates = [column for column in exported_scores if column not in excluded]
    if len(user_candidates) != 1:
        raise SystemExit(
            "step3_5_white_box_scores.csv must contain exactly one user id column"
        )
    user_column = user_candidates[0]
    if user_column not in valid.columns:
        raise SystemExit(f"Validation wide table lacks user id column {user_column}")

    valid_ids = valid[user_column].astype(str)
    score_ids = exported_scores[user_column].astype(str)
    user_sets_aligned = (
        len(valid_ids) == len(score_ids)
        and not valid_ids.duplicated().any()
        and not score_ids.duplicated().any()
        and set(valid_ids) == set(score_ids)
    )
    if not user_sets_aligned:
        return {
            "passed": False,
            "rows": len(exported_scores),
            "user_id_column": user_column,
            "user_sets_aligned": False,
            "failure_reason": "validation_wide_table_and_scores_user_sets_differ",
        }

    encoded = valid_encoded.copy()
    encoded[user_column] = valid_ids
    encoded = encoded.set_index(user_column).loc[score_ids].reset_index(drop=True)
    predictions = np.zeros(len(encoded), dtype=float)
    match_counts = np.zeros(len(encoded), dtype=int)
    parse_errors: list[str] = []
    unique_rules = rules.drop_duplicates(subset=["condition", "score"]).reset_index(
        drop=True
    )
    for row_number, row in unique_rules.iterrows():
        mask = np.ones(len(encoded), dtype=bool)
        for atom in re.split(r"\s+AND\s+", str(row["condition"]), flags=re.I):
            match = TREE_ATOM.fullmatch(atom)
            if not match:
                parse_errors.append(f"row {row_number + 2}: unsupported atom {atom}")
                mask[:] = False
                break
            raw_feature, operator, raw_threshold = match.groups()
            feature = raw_feature.split(".")[-1]
            if feature not in encoded:
                parse_errors.append(f"row {row_number + 2}: unknown feature {feature}")
                mask[:] = False
                break
            mask &= _evaluate_atom(
                encoded[feature].to_numpy(dtype=float),
                operator,
                float(raw_threshold),
            )
        predictions[mask] = float(row["score"])
        match_counts += mask.astype(int)

    actual = pd.to_numeric(
        exported_scores[score_candidates[0]], errors="coerce"
    ).to_numpy(dtype=float)
    differences = np.abs(predictions - actual)
    finite = np.isfinite(actual)
    max_difference = float(np.max(differences[finite])) if finite.any() else None
    mean_difference = float(np.mean(differences[finite])) if finite.any() else None
    unmatched_rows = int((match_counts == 0).sum())
    multiply_matched_rows = int((match_counts > 1).sum())
    mismatch_rows = (
        int((differences[finite] > SCORE_TOLERANCE).sum()) if finite.any() else len(actual)
    )
    passed = bool(
        finite.all()
        and not parse_errors
        and unmatched_rows == 0
        and multiply_matched_rows == 0
        and mismatch_rows == 0
    )
    return {
        "passed": passed,
        "rows": len(exported_scores),
        "user_id_column": user_column,
        "user_sets_aligned": True,
        "unique_rule_count": len(unique_rules),
        "parse_errors": parse_errors,
        "unmatched_rows": unmatched_rows,
        "multiply_matched_rows": multiply_matched_rows,
        "score_tolerance": SCORE_TOLERANCE,
        "max_abs_difference": max_difference,
        "mean_abs_difference": mean_difference,
        "mismatch_rows": mismatch_rows,
        "score_difference_interpretation": (
            "rule-card scores are rounded to three decimals"
        ),
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _require_inputs()
    _ensure_script_artifact()

    train = pd.read_csv(
        OUTPUT_DIR / "step3_3_wide_table_train.csv", encoding="utf-8-sig"
    )
    valid = pd.read_csv(
        OUTPUT_DIR / "step3_3_wide_table_valid.csv", encoding="utf-8-sig"
    )
    univariate = pd.read_csv(
        OUTPUT_DIR / "step3_3_univariate_analysis.csv", encoding="utf-8-sig"
    )
    importance = pd.read_csv(
        OUTPUT_DIR / "step3_4_feature_importance.csv", encoding="utf-8-sig"
    )
    rules = pd.read_csv(OUTPUT_DIR / "step3_5_rule_card.csv", encoding="utf-8-sig")
    scores = pd.read_csv(
        OUTPUT_DIR / "step3_5_white_box_scores.csv", encoding="utf-8-sig"
    )
    model_report = json.loads(
        (OUTPUT_DIR / "step3_5_model_report.json").read_text(encoding="utf-8-sig")
    )

    if not {"feature", "n_unique"}.issubset(univariate.columns):
        raise SystemExit("step3_3_univariate_analysis.csv lacks feature/n_unique")
    if "feature" not in importance.columns:
        raise SystemExit("step3_4_feature_importance.csv lacks feature")
    if not {"condition", "score"}.issubset(rules.columns):
        raise SystemExit("step3_5_rule_card.csv lacks condition/score")

    top_count = min(30, len(importance))
    top_features = importance.head(top_count)["feature"].astype(str).tolist()
    missing_columns = [
        feature
        for feature in top_features
        if feature not in train.columns or feature not in valid.columns
    ]
    if missing_columns:
        raise SystemExit(
            "Wide tables lack model features: " + ", ".join(missing_columns)
        )

    analysis = {
        str(row["feature"]): row.to_dict() for _, row in univariate.iterrows()
    }
    object_features = set(
        train[top_features].select_dtypes(include=["object"]).columns.astype(str)
    )
    metadata: dict[str, dict[str, Any]] = {}
    encoded_valid = pd.DataFrame(index=valid.index)
    for feature in top_features:
        if feature in object_features:
            feature_metadata, transformed = _categorical_metadata(
                train[feature], valid[feature]
            )
        else:
            feature_metadata, _ = _numeric_metadata(
                feature,
                train[feature],
                _requested_bins(feature, analysis),
            )
            transformed = _transform_feature(valid[feature], feature_metadata)
        metadata[feature] = feature_metadata
        encoded_valid[feature] = transformed

    rule_features = _feature_names_from_rules(rules)
    missing_rule_metadata = sorted(rule_features - set(metadata))
    validation = _validate_rules(rules, encoded_valid, scores, valid)
    if missing_rule_metadata:
        validation["passed"] = False
        validation["missing_rule_feature_metadata"] = missing_rule_metadata

    result = {
        "version": 1,
        "method": "reconstructed_from_published_model_engineering_inputs",
        "compatible_model_script": "step3_5_white_box_model.py",
        "feature_selection": {
            "source": "step3_4_feature_importance.csv",
            "top_n": top_count,
            "features": top_features,
            "model_report_top_features_match": (
                model_report.get("top_features") == top_features
            ),
        },
        "rule_features": sorted(rule_features),
        "features": metadata,
        "validation": validation,
        "script_provenance": _provenance(),
    }
    output = OUTPUT_DIR / "step3_5_preprocessing_reconstructed.json"
    _write_json(output, result)
    print(f"Generated: {output}")
    print(f"Tree preprocessing validation passed: {str(validation['passed']).lower()}")


if __name__ == "__main__":
    main()
