"""Check the optional Step2 deployment contract's structural completeness."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any


ALLOWED_PLAN_KINDS = {"entity", "user_aggregation", "scalar"}
ALLOWED_JOIN_TYPES = {"LEFT", "INNER", "CROSS"}
ALLOWED_NULL_POLICIES = {"preserve", "fill"}
QUALIFIED_COLUMN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.\s*"
    r"`?([A-Za-z_][A-Za-z0-9_]*)`?"
)
PARAMETER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain one JSON object")
    return value


def _schema_tables(schema: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw_tables = schema.get("tables")
    if isinstance(raw_tables, dict):
        raw_tables = raw_tables.get("tables")
    if not isinstance(raw_tables, list):
        raw_tables = schema.get("source_tables")
    if not isinstance(raw_tables, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for item in raw_tables:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        columns: dict[str, str] = {}
        for column in item.get("columns", []):
            if not isinstance(column, dict) or not column.get("name"):
                continue
            columns[str(column["name"])] = str(
                column.get("valueType", column.get("type", "Unknown"))
            )
        result[str(item["name"])] = columns
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_columns(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return next(csv.reader(stream))
    except (OSError, StopIteration, csv.Error) as exc:
        raise SystemExit(f"Cannot read CSV header from {path}: {exc}") from exc


def _expression_references(value: Any) -> list[tuple[str, str]]:
    expression = str(value or "")
    without_block_comments = re.sub(r"/\*.*?\*/", " ", expression, flags=re.DOTALL)
    without_line_comments = re.sub(r"--[^\r\n]*", " ", without_block_comments)
    without_literals = re.sub(r"'(?:''|\\.|[^'])*'", "''", without_line_comments)
    return QUALIFIED_COLUMN_PATTERN.findall(without_literals)


def _parameters(value: Any) -> set[str]:
    return set(PARAMETER_PATTERN.findall(str(value or "")))


def validate_contract(
    contract: dict[str, Any],
    schema: dict[str, Any],
    csv_columns: list[str],
    expanded_sql: Path,
) -> dict[str, Any]:
    """Validate deployment-contract structure against schema and wide-table columns."""
    errors: list[str] = []
    warnings: list[str] = []
    table_columns = _schema_tables(schema)
    if not table_columns:
        errors.append("step1_output_meta.json contains no physical table schema")

    if contract.get("contract_version") != 1:
        errors.append("contract_version must equal 1")
    if contract.get("source_artifact") != expanded_sql.name:
        errors.append(
            f"source_artifact must equal the validated SQL filename {expanded_sql.name!r}"
        )

    entity = contract.get("entity")
    if not isinstance(entity, dict):
        errors.append("entity must be an object")
        entity = {}
    if entity.get("grain") != "user":
        errors.append("entity.grain must equal 'user'")
    entity_key = str(entity.get("entity_key") or "").strip()
    label_column = str(entity.get("label_column") or "").strip()
    base_plan_id = str(entity.get("base_relation_plan") or "").strip()
    if not entity_key:
        errors.append("entity.entity_key is required")
    if not base_plan_id:
        errors.append("entity.base_relation_plan is required")
    if not label_column:
        errors.append("entity.label_column is required")

    plans = contract.get("relation_plans")
    if not isinstance(plans, dict) or not plans:
        errors.append("relation_plans must be a non-empty object")
        plans = {}
    features = contract.get("features")
    if not isinstance(features, dict) or not features:
        errors.append("features must be a non-empty object")
        features = {}

    plan_alias_tables: dict[str, dict[str, str]] = {}
    entity_plans: list[str] = []
    for plan_id, raw_plan in plans.items():
        if not IDENTIFIER_PATTERN.fullmatch(str(plan_id)):
            errors.append(f"relation_plans contains unsafe plan id {plan_id!r}")
        if not isinstance(raw_plan, dict):
            errors.append(f"relation_plans.{plan_id} must be an object")
            continue
        kind = str(raw_plan.get("kind") or "")
        if kind not in ALLOWED_PLAN_KINDS:
            errors.append(f"relation_plans.{plan_id}.kind is unsupported: {kind!r}")
        if kind == "entity":
            entity_plans.append(str(plan_id))
        source = raw_plan.get("source")
        if not isinstance(source, dict):
            errors.append(f"relation_plans.{plan_id}.source must be an object")
            continue
        table = str(source.get("table") or "").strip()
        alias = str(source.get("alias") or "").strip()
        if table not in table_columns:
            errors.append(f"relation_plans.{plan_id}: unknown source table {table!r}")
        if not IDENTIFIER_PATTERN.fullmatch(alias):
            errors.append(f"relation_plans.{plan_id}: unsafe source alias {alias!r}")
        aliases: dict[str, str] = {alias: table} if alias else {}

        joins = raw_plan.get("joins", [])
        if not isinstance(joins, list):
            errors.append(f"relation_plans.{plan_id}.joins must be a list")
            joins = []
        for index, join in enumerate(joins):
            if not isinstance(join, dict):
                errors.append(f"relation_plans.{plan_id}.joins[{index}] must be an object")
                continue
            join_type = str(join.get("type") or "").upper()
            join_table = str(join.get("table") or "").strip()
            join_alias = str(join.get("alias") or "").strip()
            if join_type not in ALLOWED_JOIN_TYPES:
                errors.append(
                    f"relation_plans.{plan_id}.joins[{index}]: unsupported type {join_type!r}"
                )
            if join_table not in table_columns:
                errors.append(
                    f"relation_plans.{plan_id}.joins[{index}]: unknown table {join_table!r}"
                )
            if not IDENTIFIER_PATTERN.fullmatch(join_alias):
                errors.append(
                    f"relation_plans.{plan_id}.joins[{index}]: unsafe alias {join_alias!r}"
                )
            elif join_alias in aliases:
                errors.append(
                    f"relation_plans.{plan_id}: duplicate alias {join_alias!r}"
                )
            else:
                aliases[join_alias] = join_table
            on_expression = str(join.get("on") or "").strip()
            if join_type == "CROSS" and on_expression:
                errors.append(
                    f"relation_plans.{plan_id}.joins[{index}]: CROSS join must not define on"
                )
            if join_type != "CROSS" and not on_expression:
                errors.append(
                    f"relation_plans.{plan_id}.joins[{index}]: {join_type or 'non-CROSS'} join requires on"
                )
            for ref_alias, column in _expression_references(on_expression):
                physical_table = aliases.get(ref_alias)
                if physical_table is None:
                    errors.append(
                        f"relation_plans.{plan_id}.joins[{index}].on: "
                        f"alias {ref_alias!r} is not available at this JOIN"
                    )
                elif column not in table_columns.get(physical_table, {}):
                    errors.append(
                        f"relation_plans.{plan_id}.joins[{index}].on: unknown column "
                        f"{physical_table}.{column}"
                    )

        plan_alias_tables[str(plan_id)] = aliases
        expression_fields: list[tuple[str, Any]] = []
        filters = raw_plan.get("filters", [])
        if not isinstance(filters, list):
            errors.append(f"relation_plans.{plan_id}.filters must be a list")
            filters = []
        expression_fields.extend((f"filters[{index}]", value) for index, value in enumerate(filters))
        if kind == "user_aggregation":
            key_expression = str(raw_plan.get("entity_key_expression") or "").strip()
            if not key_expression:
                errors.append(
                    f"relation_plans.{plan_id}.entity_key_expression is required"
                )
            expression_fields.append(("entity_key_expression", key_expression))
        if kind == "entity":
            key_expression = str(raw_plan.get("entity_key_expression") or "").strip()
            if not key_expression:
                errors.append(
                    f"relation_plans.{plan_id}.entity_key_expression is required"
                )
            expression_fields.append(("entity_key_expression", key_expression))
        for field_name, value in expression_fields:
            unknown_parameters = _parameters(value) - {"target_game"}
            if unknown_parameters:
                errors.append(
                    f"relation_plans.{plan_id}.{field_name}: unsupported parameters "
                    + ", ".join(sorted(unknown_parameters))
                )
            for ref_alias, column in _expression_references(value):
                physical_table = aliases.get(ref_alias)
                if physical_table is None:
                    errors.append(
                        f"relation_plans.{plan_id}.{field_name}: undeclared alias {ref_alias!r}"
                    )
                elif column not in table_columns.get(physical_table, {}):
                    errors.append(
                        f"relation_plans.{plan_id}.{field_name}: unknown column "
                        f"{physical_table}.{column}"
                    )

    if entity_plans != [base_plan_id]:
        errors.append(
            "exactly one entity relation plan must exist and match entity.base_relation_plan"
        )

    for feature, raw_feature in features.items():
        if not IDENTIFIER_PATTERN.fullmatch(str(feature)):
            errors.append(f"features contains unsafe output name {feature!r}")
        if not isinstance(raw_feature, dict):
            errors.append(f"features.{feature} must be an object")
            continue
        plan_id = str(raw_feature.get("relation_plan") or "")
        if plan_id not in plans:
            errors.append(f"features.{feature}: unknown relation_plan {plan_id!r}")
            continue
        expression = str(raw_feature.get("expression") or "").strip()
        if not expression:
            errors.append(f"features.{feature}: expression is required")
        unknown_parameters = _parameters(expression) - {"target_game"}
        if unknown_parameters:
            errors.append(
                f"features.{feature}: unsupported parameters "
                + ", ".join(sorted(unknown_parameters))
            )
        aliases = plan_alias_tables.get(plan_id, {})
        expression_refs = set(_expression_references(expression))
        declared_refs: set[tuple[str, str]] = set()
        source_columns = raw_feature.get("source_columns")
        if not isinstance(source_columns, list):
            errors.append(f"features.{feature}: source_columns must be a list")
            source_columns = []
        for index, source_column in enumerate(source_columns):
            if not isinstance(source_column, dict):
                errors.append(
                    f"features.{feature}.source_columns[{index}] must be an object"
                )
                continue
            alias = str(source_column.get("alias") or "")
            column = str(source_column.get("column") or "")
            declared_refs.add((alias, column))
            table = aliases.get(alias)
            if table is None:
                errors.append(f"features.{feature}: undeclared source alias {alias!r}")
            elif column not in table_columns.get(table, {}):
                errors.append(f"features.{feature}: unknown source column {table}.{column}")
        for alias, column in expression_refs:
            table = aliases.get(alias)
            if table is None:
                errors.append(f"features.{feature}: expression uses undeclared alias {alias!r}")
            elif column not in table_columns.get(table, {}):
                errors.append(f"features.{feature}: expression uses unknown column {table}.{column}")
        missing_declared = expression_refs - declared_refs
        if missing_declared:
            errors.append(
                f"features.{feature}: source_columns does not cover expression references "
                + ", ".join(f"{alias}.{column}" for alias, column in sorted(missing_declared))
            )
        extra_declared = declared_refs - expression_refs
        if extra_declared:
            errors.append(
                f"features.{feature}: source_columns contains references absent from expression "
                + ", ".join(f"{alias}.{column}" for alias, column in sorted(extra_declared))
            )
        null_policy = raw_feature.get("null_policy")
        if not isinstance(null_policy, dict):
            errors.append(f"features.{feature}: null_policy must be an object")
        else:
            policy_kind = str(null_policy.get("kind") or "")
            if policy_kind not in ALLOWED_NULL_POLICIES:
                errors.append(
                    f"features.{feature}: unsupported null_policy.kind {policy_kind!r}"
                )
            if policy_kind == "fill" and "value" not in null_policy:
                errors.append(f"features.{feature}: fill null policy requires value")
        if not str(raw_feature.get("output_type") or "").strip():
            errors.append(f"features.{feature}: output_type is required")

    csv_set = set(csv_columns)
    excluded = {entity_key, label_column}
    expected_features = csv_set - excluded
    actual_features = set(map(str, features))
    missing_features = sorted(expected_features - actual_features)
    extra_features = sorted(actual_features - expected_features)
    if entity_key and entity_key not in csv_set:
        errors.append(f"wide CSV does not contain entity key {entity_key!r}")
    if label_column and label_column not in csv_set:
        errors.append(f"wide CSV does not contain label column {label_column!r}")
    if missing_features:
        errors.append(
            "deployment contract is missing wide-table features: "
            + ", ".join(missing_features[:20])
            + (" ..." if len(missing_features) > 20 else "")
        )
    if extra_features:
        errors.append(
            "deployment contract contains features absent from wide CSV: "
            + ", ".join(extra_features[:20])
            + (" ..." if len(extra_features) > 20 else "")
        )

    if not expanded_sql.is_file() or not expanded_sql.stat().st_size:
        errors.append(f"expanded SQL is missing or empty: {expanded_sql}")

    return {
        "scope": "structural_completeness_only",
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "feature_count": len(actual_features),
        "wide_feature_count": len(expected_features),
        "relation_plan_count": len(plans),
        "source_artifact": expanded_sql.name,
        "source_artifact_sha256": _sha256(expanded_sql) if expanded_sql.is_file() else None,
        "wide_csv_header_sha256": hashlib.sha256(
            "\x1f".join(csv_columns).encode("utf-8")
        ).hexdigest(),
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary_path = Path(stream.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    """Run the CLI validator and persist its result into the contract atomically."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--wide-csv", type=Path, required=True)
    parser.add_argument("--expanded-sql", type=Path, required=True)
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="return a non-zero exit status when the structural check fails",
    )
    args = parser.parse_args()

    contract = _read_json(args.contract)
    schema = _read_json(args.schema)
    validation = validate_contract(
        contract,
        schema,
        _csv_columns(args.wide_csv),
        args.expanded_sql,
    )
    contract["validation"] = {
        "structural_validation": validation,
        "runtime_validation": {
            "performed": False,
            "expected_stage": "nl2sql_source_trial",
        },
    }
    _atomic_write_json(args.contract, contract)
    if not validation["passed"]:
        message = (
            "Step2 deployment feature contract structural check failed: "
            + " | ".join(validation["errors"])
        )
        if args.enforce:
            raise SystemExit(message)
        return


if __name__ == "__main__":
    main()
