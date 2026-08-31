"""Generate and validate one deployable white-box audience-selection SQL.

The final audience query is never executed without bounds:

* LightGBM is a teacher/reference model, never a deployment candidate.
* The only deployment candidates are the distilled decision tree, scorecard,
  or a deterministic blend of those two.
* New Step2 runs provide a validated machine-readable deployment feature
  contract. Historical runs fall back to Markdown/SQL lineage normalization.
* The emitted SQL targets the full ``source_database``.
* A separately rendered source-limited SELECT is executed for runtime validation.
* The unrestricted final SQL is never submitted to ClickHouse.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", DATA_DIR / "output")).resolve()
SQL_DIR = Path(os.environ.get("SQL_DIR", OUTPUT_DIR / "sql")).resolve()

PRIMARY_K = float(os.environ.get("NL2SQL_PRIMARY_K", "0.10"))
BOOTSTRAP_ITERATIONS = int(os.environ.get("NL2SQL_BOOTSTRAP_ITERATIONS", "500"))
CONFIDENCE_LEVEL = float(os.environ.get("NL2SQL_CONFIDENCE_LEVEL", "0.95"))
MIN_RELATIVE_UPLIFT = float(os.environ.get("NL2SQL_MIN_RELATIVE_UPLIFT", "0.02"))
RANDOM_SEED = int(os.environ.get("NL2SQL_RANDOM_SEED", "42"))
TEMPLATE_PATH = Path(
    os.environ.get("NL2SQL_TEMPLATE_PATH", str(Path(__file__).resolve()))
).resolve()
GENERATOR_CHANGE_REASON = os.environ.get("NL2SQL_GENERATOR_CHANGE_REASON", "").strip()
RUNTIME_DIR = OUTPUT_DIR / ".nl2sql_runtime"
SOURCE_VALIDATION_RESULT_PATH = Path(
    os.environ.get(
        "NL2SQL_SOURCE_VALIDATION_RESULT_PATH",
        str(RUNTIME_DIR / "source_validation_result.json"),
    )
).resolve()
TRIAL_ROWS_PER_TABLE = int(os.environ.get("NL2SQL_TRIAL_ROWS_PER_TABLE", "128"))
TRIAL_OUTPUT_ROWS = int(os.environ.get("NL2SQL_TRIAL_OUTPUT_ROWS", "10"))
TRIAL_MAX_EXECUTION_TIME = int(os.environ.get("NL2SQL_TRIAL_MAX_EXECUTION_TIME", "30"))
TRIAL_MAX_ROWS_TO_READ = int(os.environ.get("NL2SQL_TRIAL_MAX_ROWS_TO_READ", "100000"))
TRIAL_MAX_BYTES_TO_READ = int(os.environ.get("NL2SQL_TRIAL_MAX_BYTES_TO_READ", "500000000"))
TRIAL_MAX_MEMORY_USAGE = int(os.environ.get("NL2SQL_TRIAL_MAX_MEMORY_USAGE", "1000000000"))
TRIAL_READ_SAFETY_FACTOR = int(os.environ.get("NL2SQL_TRIAL_READ_SAFETY_FACTOR", "4"))
TRIAL_SOURCE_LIMIT_PLACEHOLDER = "__NL2SQL_TRIAL_SOURCE_ROWS__"

INPUT_NORMALIZATION_WARNINGS: list[str] = []

CORE_REQUIRED_INPUTS = (
    "step1_output_meta.json",
    "step1_sample_stats.json",
    "schema_resolution.json",
    "step3_4_valid_predictions.csv",
    "step3_4_model_report.json",
    "step3_5_rule_card.csv",
    "step3_5_white_box_scores.csv",
    "step3_5_model_report.json",
    "step3_5_preprocessing_reconstructed.json",
    "step3_6_score_rule.csv",
    "step3_6_white_box_scores.csv",
    "step3_6_model_report.json",
)
LEGACY_LINEAGE_INPUTS = (
    "step2_3_feature_derivation.md",
)
DEPLOYMENT_FEATURE_CONTRACT = "step2_3_deployment_feature_contract.json"

FORBIDDEN_SQL_PATTERNS = (
    (re.compile(r"\bWITH\b", re.I), "CTE/WITH"),
    (re.compile(r"MODE\s*\(\s*\)\s*WITHIN\s+GROUP", re.I), "MODE() WITHIN GROUP"),
    (re.compile(r"\bTRY_TO_NUMERIC\b", re.I), "TRY_TO_NUMERIC"),
    (re.compile(r"\bINTERVAL\b", re.I), "INTERVAL"),
    (re.compile(r"\bLIMIT\b", re.I), "LIMIT"),
)


@dataclass(frozen=True)
class RuntimeContract:
    source_database: str
    sampling_database: str
    target_game: str
    user_table: str
    user_id: str
    table_columns: dict[str, dict[str, str]]
    table_types: dict[str, str]
    validated_keys: dict[str, list[str]]
    user_id_aliases: tuple[str, ...]
    game_dimension_tables: set[str]
    game_keys: dict[str, str]
    one_to_one_tables: set[str]


@dataclass
class CandidateSQL:
    name: str
    expression: str
    features: set[str]
    rule_count: int
    parse_coverage: float
    renderable: bool = True
    render_errors: list[str] | None = None
    deployment_errors: list[str] | None = None


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generator_provenance() -> dict[str, Any]:
    executed_path = Path(__file__).resolve()
    executed_sha256 = _sha256_file(executed_path)
    template_sha256 = _sha256_file(TEMPLATE_PATH)
    modified: bool | None = None
    if template_sha256 and executed_sha256:
        modified = template_sha256 != executed_sha256
    warnings: list[str] = []
    if not TEMPLATE_PATH.is_file():
        warnings.append("template_path_does_not_exist")
    if modified is True and not GENERATOR_CHANGE_REASON:
        warnings.append("modified_generator_missing_change_reason")
    return {
        "template_available": TEMPLATE_PATH.is_file(),
        "template_path": str(TEMPLATE_PATH),
        "template_sha256": template_sha256,
        "executed_path": str(executed_path),
        "executed_sha256": executed_sha256,
        "working_copy_modified": modified,
        "change_reason": GENERATOR_CHANGE_REASON or None,
        "warnings": warnings,
    }


def _ensure_executed_generator_artifact() -> Path:
    artifact_path = OUTPUT_DIR / "scripts" / "step4_1_generate_sql.py"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    executed_path = Path(__file__).resolve()
    if artifact_path.resolve() != executed_path:
        artifact_path.write_bytes(executed_path.read_bytes())
    return artifact_path


def _require_inputs() -> None:
    required = list(CORE_REQUIRED_INPUTS)
    if not _has_usable_deployment_feature_contract():
        required.extend(LEGACY_LINEAGE_INPUTS)
    missing = [name for name in required if not (OUTPUT_DIR / name).is_file()]
    if missing:
        raise SystemExit("Missing NL2SQL input artifacts: " + ", ".join(missing))


def _structural_validation(deployment: dict[str, Any]) -> dict[str, Any]:
    validation = deployment.get("validation")
    if not isinstance(validation, dict):
        return {}
    nested = validation.get("structural_validation")
    if isinstance(nested, dict):
        return nested
    # Compatibility with the first contract draft, before validation scope was
    # made explicit.
    return validation


def _has_usable_deployment_feature_contract() -> bool:
    path = OUTPUT_DIR / DEPLOYMENT_FEATURE_CONTRACT
    if not path.is_file():
        return False
    try:
        deployment = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(deployment, dict) and _structural_validation(deployment).get(
        "passed"
    ) is True


def _read_json(name: str, *, allow_safe_repairs: bool = False) -> dict[str, Any]:
    path = OUTPUT_DIR / name
    raw = ""
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except OSError as exc:
        raise SystemExit(f"Cannot read {name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        if not allow_safe_repairs:
            raise SystemExit(f"Cannot read {name}: {exc}") from exc
        # Some historical FE artifacts emitted `"expected_unique_after": "<=" 7`.
        # Repair only this unambiguous serialization defect in memory. Never
        # rewrite the copied upstream artifact.
        repaired = re.sub(
            r'("expected_unique_after"\s*:\s*"<=)"\s*(-?\d+(?:\.\d+)?)',
            r'\1 \2"',
            raw,
        )
        if repaired == raw:
            raise SystemExit(f"Cannot read {name}: {exc}") from exc
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError:
            raise SystemExit(f"Cannot read {name}: {exc}") from exc
        INPUT_NORMALIZATION_WARNINGS.append(
            f"{name}: repaired malformed expected_unique_after value in memory"
        )
    if not isinstance(data, dict):
        raise SystemExit(f"{name} must contain one JSON object")
    return data


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_json_safe(value), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, Path):
        return str(value)
    return value


def _strip_markup(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("**", "").replace("__", "")
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        text = text[1:-1]
    return text.strip()


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [_strip_markup(cell) for cell in stripped.split("|")]


def _is_markdown_separator(line: str) -> bool:
    cells = _split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _canonical_header(header: str) -> str:
    normalized = re.sub(r"\s+", "_", _strip_markup(header).lower())
    aliases = {
        "字段": "feature",
        "特征": "feature",
        "特征名": "feature",
        "派生列": "feature",
        "状态": "status",
        "处理方式": "method",
        "聚合方式": "method",
        "来源表": "source_table",
        "来源字段": "source_feature",
        "原字段": "source_feature",
        "数据类型": "data_type",
        "类型": "data_type",
        "空值策略": "null_strategy",
        "sql_表达式": "sql_expression",
        "sql表达式": "sql_expression",
        "sql_expression_/_连接方式": "sql_expression",
        "sql_表达式_/_连接方式": "sql_expression",
    }
    if normalized in aliases:
        return aliases[normalized]
    if "sql" in normalized and ("expression" in normalized or "表达式" in normalized):
        return "sql_expression"
    return normalized


def _table_schema_map(table_schema: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    raw_tables = table_schema.get("tables", [])
    if isinstance(raw_tables, dict):
        tables = [
            {"name": table_name, **table_value}
            for table_name, table_value in raw_tables.items()
            if isinstance(table_value, dict)
        ]
    elif isinstance(raw_tables, list):
        tables = raw_tables
    else:
        tables = []
    for table in tables:
        if not isinstance(table, dict) or not table.get("name"):
            continue
        columns: dict[str, str] = {}
        for column in table.get("columns", []):
            if not isinstance(column, dict) or not column.get("name"):
                continue
            columns[str(column["name"])] = str(column.get("valueType", column.get("type", "Unknown")))
        result[str(table["name"])] = columns
    if not result:
        raise SystemExit("step1_output_meta.json contains no tables")
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _classification_tables(classification: Any, *keys: str) -> set[str]:
    if not isinstance(classification, dict):
        return set()
    result: set[str] = set()
    for key in keys:
        result.update(_string_list(classification.get(key)))
    return result


def _hint_game_key_candidates(output_meta: dict[str, Any]) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {}
    hints = output_meta.get("join_hints", [])
    if not isinstance(hints, list):
        return candidates
    for hint in hints:
        if not isinstance(hint, dict):
            continue
        for side in ("left", "right"):
            value = str(hint.get(side) or "").strip()
            match = re.fullmatch(
                r"`?([^.`]+)`?\s*\.\s*`?([^`]+)`?",
                value,
            )
            if not match:
                continue
            table, column = match.groups()
            candidates.setdefault(table, []).append(column)
    return candidates


def load_runtime_contract() -> RuntimeContract:
    output_meta = _read_json("step1_output_meta.json")
    sample_stats = _read_json("step1_sample_stats.json")
    schema_resolution = _read_json("schema_resolution.json")

    source_database = str(sample_stats.get("source_database") or output_meta.get("source_database") or "").strip()
    if not source_database:
        raise SystemExit("source_database is absent from step1 artifacts")
    schema_source = str(output_meta.get("source_database") or "").strip()
    if schema_source and schema_source != source_database:
        raise SystemExit("source_database mismatch between step1_output_meta.json and step1_sample_stats.json")

    legacy_schema_path = OUTPUT_DIR / "step1_0_table_schema.json"
    if legacy_schema_path.is_file():
        legacy_schema = _read_json("step1_0_table_schema.json")
        legacy_source = str(legacy_schema.get("source_database") or "").strip()
        if legacy_source and legacy_source != source_database:
            raise SystemExit("source_database mismatch in optional step1_0_table_schema.json")

    sampling_database = str(sample_stats.get("output_database") or "").strip()
    if not sampling_database:
        raise SystemExit("step1_sample_stats.json must provide output_database")
    target_game = str(sample_stats.get("target_game") or "").strip()
    roles = schema_resolution.get("roles", {})
    if not isinstance(roles, dict):
        raise SystemExit("schema_resolution.roles must be an object")
    user_table = str(roles.get("<user_table>") or roles.get("user_table") or "").strip()
    user_id = str(roles.get("<user_id>") or roles.get("user_id") or "").strip()
    if not user_table or not user_id:
        raise SystemExit("schema_resolution must resolve <user_table> and <user_id>")

    table_columns = _table_schema_map(output_meta)
    if user_table not in table_columns:
        raise SystemExit(f"Resolved user table does not exist in source schema: {user_table}")
    if user_id not in table_columns[user_table]:
        raise SystemExit(f"Resolved user id {user_id} does not exist in source table {user_table}")

    validated_keys: dict[str, list[str]] = {}
    key_mapping = schema_resolution.get("key_mapping", {})
    if isinstance(key_mapping, dict):
        for table, raw_mapping in key_mapping.items():
            if not isinstance(raw_mapping, dict):
                continue
            for key in ("user_key", "alternative_keys"):
                values = raw_mapping.get(key)
                if isinstance(values, list):
                    validated_keys.setdefault(str(table), []).extend(_string_list(values))
                elif values:
                    validated_keys.setdefault(str(table), []).append(str(values))
    key_validation = schema_resolution.get("key_validation", {})
    if isinstance(key_validation, dict):
        for item in key_validation.get("candidate_keys", []):
            if not isinstance(item, dict) or not item.get("validated"):
                continue
            table = str(item.get("table") or "")
            column = str(item.get("column") or "")
            if table and column:
                validated_keys.setdefault(table, []).append(column)

    aliases = output_meta.get("column_aliases", {})
    user_id_aliases = _string_list(
        aliases.get("user_id_columns") if isinstance(aliases, dict) else []
    )

    table_types: dict[str, str] = {}
    projections = sample_stats.get("projection_tables", [])
    if isinstance(projections, list):
        for projection in projections:
            if not isinstance(projection, dict):
                continue
            table = str(projection.get("table") or "").strip()
            table_type = str(projection.get("type") or "").strip()
            if table and table_type:
                table_types[table] = table_type

    classification = schema_resolution.get("table_classification", {})
    one_to_one: set[str] = {user_table}
    one_to_one.update(
        _classification_tables(
            classification,
            "1:1_tables",
            "one_to_one_tables",
            "one_to_one",
        )
    )
    game_dimension_tables = {
        table for table, table_type in table_types.items() if table_type == "game_keyed"
    }
    game_dimension_tables.update(
        _classification_tables(
            classification,
            "game_dimension",
            "game_dimension_tables",
            "game_dimensions",
        )
    )

    role_game_key = str(roles.get("<game_id>") or roles.get("game_id") or "").strip()
    hint_candidates = _hint_game_key_candidates(output_meta)
    game_keys: dict[str, str] = {}
    for table in sorted(game_dimension_tables):
        columns = table_columns.get(table, {})
        mapping = key_mapping.get(table, {}) if isinstance(key_mapping, dict) else {}
        candidates: list[str] = []
        if isinstance(mapping, dict):
            for key in ("game_key", "game_id", "game_name"):
                value = mapping.get(key)
                if value:
                    candidates.append(str(value))
        if role_game_key:
            candidates.append(role_game_key)
        candidates.extend(hint_candidates.get(table, []))
        resolved = next((candidate for candidate in candidates if candidate in columns), None)
        if resolved:
            game_keys[table] = resolved

    return RuntimeContract(
        source_database=source_database,
        sampling_database=sampling_database,
        target_game=target_game,
        user_table=user_table,
        user_id=user_id,
        table_columns=table_columns,
        table_types=table_types,
        validated_keys=validated_keys,
        user_id_aliases=tuple(dict.fromkeys([user_id, *user_id_aliases])),
        game_dimension_tables=game_dimension_tables,
        game_keys=game_keys,
        one_to_one_tables=one_to_one,
    )


def _known_tables_in_text(text: str, known_tables: Iterable[str]) -> list[str]:
    found: list[str] = []
    for table in known_tables:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])", text):
            found.append(table)
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)_(\d+)~(\d+)\b", text):
        prefix, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        for index in range(start, end + 1):
            table = f"{prefix}_{index}"
            if table in known_tables and table not in found:
                found.append(table)
    return found


def _sql_source_columns(
    expression: str,
    contract: RuntimeContract | None = None,
    source_tables: Iterable[str] = (),
) -> list[str]:
    if not expression:
        return []
    code = _strip_sql_comments_and_literals(expression)
    if contract is None:
        # Compatibility mode for callers outside lineage normalization. Only
        # return identifiers that are not immediately followed by ``(``, which
        # excludes SQL function names without maintaining a fragile blacklist.
        identifiers = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\()", code)
        return list(dict.fromkeys(identifiers))

    tables = [table for table in source_tables if table in contract.table_columns]
    qualified: list[str] = []
    for table, column in re.findall(
        r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.\s*`?([A-Za-z_][A-Za-z0-9_]*)`?",
        code,
    ):
        if table in contract.table_columns and column in contract.table_columns[table]:
            qualified.append(column)

    known_columns: set[str] = set()
    search_tables = tables or list(contract.table_columns)
    for table in search_tables:
        known_columns.update(contract.table_columns.get(table, {}))
    identifiers = re.findall(r"(?<!\.)\b[A-Za-z_][A-Za-z0-9_]*\b(?!\s*\()", code)
    result: list[str] = []
    for identifier in [*qualified, *identifiers]:
        if identifier not in known_columns:
            continue
        if identifier not in result:
            result.append(identifier)
    return result


def _infer_source_feature(feature: str, method: str, table_columns: set[str]) -> str | None:
    if feature in table_columns:
        return feature
    explicit = {
        "max_device_price": "device_price",
        "max_first_time_duration": "first_time_duration",
        "any_version": "version",
        "total_pay_amount": "pay_amount",
        "avg_pay_amount": "pay_amount",
        "total_push_clicks": "click_cnt",
        "total_push_exposures": "exposure_cnt",
        "avg_push_ctr": "ctr",
        "push_total_clicks": "click_cnt",
        "push_total_exposure": "exposure_cnt",
        "push_avg_ctr": "ctr",
        "n_booked_games": "game_name",
        "booking_game_count": "game_name",
        "n_channels": "channel",
        "booking_channel_count": "channel",
        "n_detail_games": "game_name",
        "detail_game_count": "game_name",
        "n_action_types": "action_type",
        "detail_action_type_count": "action_type",
        "n_install_sources": "install_source",
        "detail_install_source_count": "install_source",
        "booking_entity_flag_count": "entity_flag",
        "booking_status_count": "status",
        "push_app_cn_count": "app_cn_name",
    }
    candidate = explicit.get(feature)
    if candidate in table_columns:
        return candidate

    if "count" in method.lower() and "distinct" not in method.lower():
        return None
    pieces = feature.split("_")
    for suffix in (
        ("_count", ""),
        ("n_", ""),
        ("total_", ""),
        ("avg_", ""),
        ("max_", ""),
    ):
        if suffix[0].startswith("_") and feature.endswith(suffix[0]):
            guess = feature[: -len(suffix[0])]
        elif feature.startswith(suffix[0]):
            guess = feature[len(suffix[0]) :]
        else:
            continue
        guesses = [guess, guess.rstrip("s"), guess.replace("games", "game_name")]
        for item in guesses:
            if item in table_columns:
                return item
    for piece in reversed(pieces):
        if piece in table_columns:
            return piece
    return None


def _aggregation_expression(method: str, source_feature: str | None) -> str | None:
    lowered = method.lower().replace(" ", "")
    if "countif" in lowered:
        return None
    if "countdistinct" in lowered or "uniqexact" in lowered:
        return f"uniqExact({_quote_identifier(source_feature)})" if source_feature else None
    if lowered == "count" or "count" in lowered:
        return "count()"
    for function in ("sum", "avg", "max", "min", "any"):
        if function in lowered:
            return f"{function}({_quote_identifier(source_feature)})" if source_feature else None
    return None


def _merge_lineage_entry(target: dict[str, dict[str, Any]], entry: dict[str, Any]) -> None:
    feature = str(entry.get("feature") or "").strip()
    if not feature or "*" in feature or "{" in feature:
        return
    entry["feature"] = feature
    existing = target.get(feature)
    if existing is None:
        target[feature] = entry
        return
    score = sum(bool(entry.get(key)) for key in ("source_table", "source_feature", "sql_expression"))
    old_score = sum(bool(existing.get(key)) for key in ("source_table", "source_feature", "sql_expression"))
    if score > old_score:
        combined = {**existing, **{k: v for k, v in entry.items() if v not in (None, "", [])}}
        target[feature] = combined


def _sql_scan_boundaries(sql: str, end: int) -> tuple[int, list[tuple[int, int]]]:
    """Return parenthesis depth and projection boundaries before ``end``."""
    depth = 0
    boundaries: list[tuple[int, int]] = []
    quote: str | None = None
    index = 0
    while index < end:
        char = sql[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < end and sql[index + 1] == "'":
                    index += 2
                    continue
                quote = None
            elif char == "\\" and index + 1 < end:
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ",":
            boundaries.append((index + 1, depth))
        elif sql[index : index + 6].upper() == "SELECT" and (
            index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == "_")
        ) and (
            index + 6 >= end
            or not (sql[index + 6].isalnum() or sql[index + 6] == "_")
        ):
            boundaries.append((index + 6, depth))
            index += 5
        index += 1
    return depth, boundaries


def _aggregation_alias_expressions() -> tuple[dict[str, str], str | None]:
    candidates = (
        "step2_3_feature_aggregation_expanded.sql",
        "step2_3_feature_aggregation.sql",
    )
    path = next((OUTPUT_DIR / name for name in candidates if (OUTPUT_DIR / name).is_file()), None)
    if path is None:
        return {}, None
    sql = path.read_text(encoding="utf-8-sig")
    expressions: dict[str, str] = {}
    for match in re.finditer(
        r"\bAS\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
        sql,
        flags=re.I,
    ):
        depth, boundaries = _sql_scan_boundaries(sql, match.start())
        starts = [position for position, boundary_depth in boundaries if boundary_depth == depth]
        start = max(starts, default=0)
        expression = sql[start : match.start()].strip()
        expression = re.sub(r"^SELECT\s+", "", expression, flags=re.I).strip()
        if not expression or expression.upper().startswith(("CREATE ", "WITH ")):
            continue
        # Ignore CAST(x AS Type) and similar nested type aliases. They are not
        # projection aliases and usually end before a closing parenthesis.
        suffix = sql[match.end() :]
        next_text = suffix.lstrip()
        next_non_space = next_text[:1]
        if next_non_space and next_non_space not in {",", "\n", "\r", ")"} and not re.match(
            r"^(?:FROM|WHERE|GROUP|HAVING|ORDER|JOIN|LEFT|RIGHT|INNER|CROSS)\b",
            next_text,
            flags=re.I,
        ):
            continue
        expressions[match.group(1)] = expression
    return expressions, path.name


def _resolve_physical_source(
    feature: str,
    contract: RuntimeContract,
) -> tuple[str, str] | None:
    direct = [
        (table, feature)
        for table, columns in contract.table_columns.items()
        if feature in columns
    ]
    if len(direct) == 1:
        return direct[0]
    for table in sorted(contract.table_columns, key=len, reverse=True):
        prefix = table + "_"
        if not feature.startswith(prefix):
            continue
        source_feature = feature[len(prefix) :]
        if source_feature in contract.table_columns[table]:
            return table, source_feature
    return None


def _high_cardinality_lineage(
    high_cardinality_check: dict[str, Any],
    contract: RuntimeContract,
) -> list[dict[str, Any]]:
    details = high_cardinality_check.get("high_cardinality_check", {})
    if not isinstance(details, dict):
        return []
    findings = details.get("findings_before_binning", [])
    if not isinstance(findings, list):
        return []

    entries: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source_name = str(finding.get("column") or "").strip()
        target_name = str(finding.get("new_column") or "").strip()
        resolved = _resolve_physical_source(source_name, contract)
        if not source_name or not target_name or resolved is None:
            continue
        source_table, source_feature = resolved
        quoted = _quote_identifier(source_feature)
        grouped_value = quoted if source_table == contract.user_table else f"any({quoted})"

        thresholds = finding.get("quantile_thresholds")
        expression: str | None = None
        if isinstance(thresholds, dict) and thresholds:
            ordered = [
                thresholds[key]
                for key in ("p20", "p40", "p60", "p80")
                if key in thresholds
            ]
            if ordered:
                numeric = f"toFloat64OrNull({grouped_value})"
                conditions = [f"{numeric} IS NULL, 0"]
                conditions.extend(
                    f"{numeric} <= {_sql_literal(str(value))}, {index}"
                    for index, value in enumerate(ordered, start=1)
                )
                expression = "multiIf(" + ", ".join([*conditions, str(len(ordered) + 1)]) + ")"
        if expression is None:
            formula = str(finding.get("binning_formula") or "")
            delimiter_match = re.search(r"splitByChar\(\s*'([^']+)'", formula, flags=re.I)
            if delimiter_match:
                delimiter = delimiter_match.group(1).replace("'", "''")
                text_value = f"ifNull({grouped_value}, '')"
                expression = (
                    f"multiIf({text_value} = '', 0, "
                    f"length(splitByChar('{delimiter}', {text_value})))"
                )
        if expression is None:
            continue
        entries.append(
            {
                "feature": target_name,
                "status": "derived",
                "method": str(finding.get("action") or "high_cardinality_transform"),
                "data_type": finding.get("new_type"),
                "source_table": source_table,
                "source_tables": [source_table],
                "source_feature": source_feature,
                "source_columns": [source_feature],
                "sql_expression": expression,
                "null_strategy": "derived expression handles null",
                "section": "step2_3_high_cardinality_check.json",
            }
        )
    return entries


def normalize_feature_derivation(
    contract: RuntimeContract,
    high_cardinality_check: dict[str, Any],
) -> dict[str, Any]:
    markdown_path = OUTPUT_DIR / "step2_3_feature_derivation.md"
    markdown = markdown_path.read_text(encoding="utf-8-sig")
    lines = markdown.splitlines()
    known_tables = list(contract.table_columns)
    aggregation_expressions, aggregation_source = _aggregation_alias_expressions()
    features: dict[str, dict[str, Any]] = {}
    current_heading = ""
    current_tables: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("#"):
            current_heading = stripped.lstrip("#").strip()
            heading_tables = _known_tables_in_text(current_heading, known_tables)
            if heading_tables:
                current_tables = heading_tables
        else:
            source_tables = _known_tables_in_text(stripped, known_tables)
            if source_tables and (
                "source" in stripped.lower()
                or "源表" in stripped
                or "from " in stripped.lower()
                or "聚合" in current_heading
            ):
                current_tables = source_tables

        if (
            "|" in line
            and index + 1 < len(lines)
            and "|" in lines[index + 1]
            and _is_markdown_separator(lines[index + 1])
        ):
            headers = [_canonical_header(cell) for cell in _split_markdown_row(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                cells = _split_markdown_row(lines[index])
                if len(cells) < len(headers):
                    cells.extend([""] * (len(headers) - len(cells)))
                row = {headers[i]: cells[i] for i in range(len(headers))}
                feature = _strip_markup(row.get("feature", row.get("name", row.get("column", ""))))
                if feature and not re.fullmatch(r"[-:]+", feature):
                    source_cell = _strip_markup(row.get("source_table", ""))
                    source_tables = _known_tables_in_text(source_cell, known_tables)
                    if not source_tables:
                        source_tables = list(current_tables)
                    source_table = source_tables[0] if len(source_tables) == 1 else None
                    source_feature = _strip_markup(row.get("source_feature", ""))
                    expression = _strip_markup(row.get("sql_expression", "")) or aggregation_expressions.get(
                        feature, ""
                    )
                    method = _strip_markup(row.get("method", row.get("handling", "")))
                    if source_feature and re.search(r"[()]", source_feature):
                        # Some FE Markdown versions put the aggregate expression
                        # under “原字段”. Treat it as SQL, not as a physical name.
                        expression = expression or source_feature
                        source_feature = ""
                    if source_table and not source_feature and feature in contract.table_columns[source_table]:
                        source_feature = feature
                    source_columns = _sql_source_columns(
                        expression,
                        contract,
                        source_tables,
                    )
                    if source_table and not source_feature:
                        physical_columns = [
                            column
                            for column in source_columns
                            if column in contract.table_columns[source_table]
                        ]
                        if len(physical_columns) == 1:
                            source_feature = physical_columns[0]
                        else:
                            source_feature = _infer_source_feature(
                                feature,
                                method,
                                set(contract.table_columns[source_table]),
                            )
                    if source_feature and source_feature not in source_columns:
                        source_columns.append(source_feature)
                    feature_kind = "direct"
                    if source_tables and all(
                        table in contract.game_dimension_tables for table in source_tables
                    ):
                        feature_kind = "game_dimension"
                    elif source_tables != [contract.user_table]:
                        feature_kind = "user_aggregation"
                    elif expression:
                        feature_kind = "derived"
                    _merge_lineage_entry(
                        features,
                        {
                            "feature": feature,
                            "status": _strip_markup(row.get("status", "kept")) or "kept",
                            "method": method or "direct",
                            "data_type": _strip_markup(row.get("data_type", "")) or None,
                            "source_table": source_table,
                            "source_tables": source_tables,
                            "source_feature": source_feature or None,
                            "source_columns": source_columns,
                            "sql_expression": expression or None,
                            "null_strategy": _strip_markup(row.get("null_strategy", row.get("null_policy", "")))
                            or None,
                            "section": current_heading,
                            "feature_kind": feature_kind,
                            "renderable": True,
                            "diagnostics": [],
                        },
                    )
                index += 1
            continue

        bullet = re.match(r"^\s*-\s*`([^`]+)`\s*:\s*(.+)$", line)
        if bullet and current_tables:
            feature = bullet.group(1).strip()
            description = bullet.group(2).strip()
            method_match = re.search(r"[（(]([^()（）]+)[）)]", description)
            method = method_match.group(1).strip() if method_match else description
            source_table = current_tables[0] if len(current_tables) == 1 else None
            source_feature = None
            expression = None
            if source_table:
                source_feature = _infer_source_feature(feature, method, set(contract.table_columns[source_table]))
                expression = _aggregation_expression(method, source_feature)
            _merge_lineage_entry(
                features,
                {
                    "feature": feature,
                    "status": "derived",
                    "method": method,
                    "data_type": None,
                    "source_table": source_table,
                    "source_tables": list(current_tables),
                    "source_feature": source_feature,
                    "source_columns": [source_feature] if source_feature else [],
                    "sql_expression": expression,
                    "null_strategy": None,
                    "section": current_heading,
                    "description": description,
                    "feature_kind": (
                        "game_dimension"
                        if source_table in contract.game_dimension_tables
                        else "user_aggregation"
                    ),
                    "renderable": True,
                    "diagnostics": [],
                },
            )
        index += 1

    for entry in _high_cardinality_lineage(high_cardinality_check, contract):
        _merge_lineage_entry(features, entry)

    # Physical direct columns are safe fallback lineage, especially for concise
    # Markdown sections that list a category without repeating source_feature.
    for table, columns in contract.table_columns.items():
        for column, data_type in columns.items():
            if column in features:
                continue
            _merge_lineage_entry(
                features,
                {
                    "feature": column,
                    "status": "kept",
                    "method": "direct",
                    "data_type": data_type,
                    "source_table": table,
                    "source_tables": [table],
                    "source_feature": column,
                    "source_columns": [column],
                    "sql_expression": None,
                    "null_strategy": None,
                    "section": "physical_schema_fallback",
                    "feature_kind": (
                        "game_dimension"
                        if table in contract.game_dimension_tables
                        else "direct"
                    ),
                    "renderable": True,
                    "diagnostics": [],
                },
            )

    for entry in features.values():
        entry.setdefault("feature_kind", "derived" if entry.get("sql_expression") else "direct")
        entry.setdefault("renderable", True)
        entry.setdefault("diagnostics", [])

    normalized = {
        "version": 1,
        "source": "step2_3_feature_derivation.md",
        "aggregation_sql_source": aggregation_source,
        "entity": {
            "base_table": contract.user_table,
            "entity_key": contract.user_id,
            "grain": "user",
        },
        "features": [features[name] for name in sorted(features)],
    }
    _write_json(OUTPUT_DIR / "step2_3_feature_derivation.json", normalized)
    return normalized


_CONTRACT_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTRACT_QUALIFIED_COLUMN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.\s*"
    r"`?([A-Za-z_][A-Za-z0-9_]*)`?"
)
_CONTRACT_PARAMETER_PATTERN = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)
def _contract_expression_errors(
    location: str,
    expression: Any,
    aliases: dict[str, str],
    contract: RuntimeContract,
) -> list[str]:
    value = str(expression or "").strip()
    if not value:
        return [f"{location}: expression is required"]
    errors: list[str] = []
    if ";" in value or "--" in value or "/*" in value or "*/" in value:
        errors.append(f"{location}: SQL comments and statement delimiters are forbidden")
    parameters = set(_CONTRACT_PARAMETER_PATTERN.findall(value))
    unsupported = sorted(parameters - {"target_game"})
    if unsupported:
        errors.append(
            f"{location}: unsupported parameters " + ", ".join(unsupported)
        )
    parameter_stripped = _CONTRACT_PARAMETER_PATTERN.sub("", value)
    if "{{" in parameter_stripped or "}}" in parameter_stripped:
        errors.append(f"{location}: malformed runtime parameter")
    code = _strip_sql_comments_and_literals(value)
    for alias, column in _CONTRACT_QUALIFIED_COLUMN_PATTERN.findall(code):
        table = aliases.get(alias)
        if table is None:
            errors.append(f"{location}: undeclared alias {alias!r}")
        elif column not in contract.table_columns.get(table, {}):
            errors.append(f"{location}: unknown source column {table}.{column}")
    return errors


def _deployment_contract_aliases(
    plan_id: str,
    plan: dict[str, Any],
    contract: RuntimeContract,
) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    source = plan.get("source")
    if not isinstance(source, dict):
        return {}, [f"relation_plans.{plan_id}.source must be an object"]
    table = str(source.get("table") or "").strip()
    alias = str(source.get("alias") or "").strip()
    if table not in contract.table_columns:
        errors.append(f"relation_plans.{plan_id}: unknown source table {table!r}")
    if not _CONTRACT_IDENTIFIER_PATTERN.fullmatch(alias):
        errors.append(f"relation_plans.{plan_id}: unsafe source alias {alias!r}")
    aliases = {alias: table} if alias else {}
    joins = plan.get("joins", [])
    if not isinstance(joins, list):
        return aliases, [*errors, f"relation_plans.{plan_id}.joins must be a list"]
    for index, join in enumerate(joins):
        if not isinstance(join, dict):
            errors.append(f"relation_plans.{plan_id}.joins[{index}] must be an object")
            continue
        join_type = str(join.get("type") or "").upper()
        join_table = str(join.get("table") or "").strip()
        join_alias = str(join.get("alias") or "").strip()
        if join_type not in {"LEFT", "INNER", "CROSS"}:
            errors.append(
                f"relation_plans.{plan_id}.joins[{index}]: unsupported type {join_type!r}"
            )
        if join_table not in contract.table_columns:
            errors.append(
                f"relation_plans.{plan_id}.joins[{index}]: unknown table {join_table!r}"
            )
        if not _CONTRACT_IDENTIFIER_PATTERN.fullmatch(join_alias):
            errors.append(
                f"relation_plans.{plan_id}.joins[{index}]: unsafe alias {join_alias!r}"
            )
        elif join_alias in aliases:
            errors.append(f"relation_plans.{plan_id}: duplicate alias {join_alias!r}")
        else:
            aliases[join_alias] = join_table
        on_expression = str(join.get("on") or "").strip()
        if join_type == "CROSS" and on_expression:
            errors.append(
                f"relation_plans.{plan_id}.joins[{index}]: CROSS join must not define on"
            )
        if join_type != "CROSS" and not on_expression:
            errors.append(
                f"relation_plans.{plan_id}.joins[{index}]: non-CROSS join requires on"
            )
    return aliases, errors


def load_deployment_feature_contract(
    contract: RuntimeContract,
) -> dict[str, Any]:
    """Load and validate the machine-readable Step2 deployment feature contract."""
    deployment = _read_json(DEPLOYMENT_FEATURE_CONTRACT)
    errors: list[str] = []
    if deployment.get("contract_version") != 1:
        errors.append("contract_version must equal 1")
    if deployment.get("source_artifact") != "step2_3_feature_aggregation_expanded.sql":
        errors.append("source_artifact must identify the Step2 expanded SQL")
    validation = _structural_validation(deployment)
    if validation.get("passed") is not True:
        errors.append("Step2 structural validation did not pass")
    entity = deployment.get("entity")
    if not isinstance(entity, dict):
        errors.append("entity must be an object")
        entity = {}
    if entity.get("grain") != "user":
        errors.append("entity.grain must equal 'user'")
    if str(entity.get("entity_key") or "") != contract.user_id:
        errors.append("entity.entity_key does not match schema_resolution user id")
    if not str(entity.get("label_column") or "").strip():
        errors.append("entity.label_column is required")
    base_plan_id = str(entity.get("base_relation_plan") or "")

    plans = deployment.get("relation_plans")
    if not isinstance(plans, dict) or not plans:
        errors.append("relation_plans must be a non-empty object")
        plans = {}
    features = deployment.get("features")
    if not isinstance(features, dict) or not features:
        errors.append("features must be a non-empty object")
        features = {}
    if validation:
        if validation.get("source_artifact") != deployment.get("source_artifact"):
            errors.append("validation.source_artifact does not match the contract")
        if validation.get("feature_count") != len(features):
            errors.append("validation.feature_count does not match features")
        if validation.get("relation_plan_count") != len(plans):
            errors.append("validation.relation_plan_count does not match relation_plans")
        for digest_name in ("source_artifact_sha256", "wide_csv_header_sha256"):
            digest = str(validation.get(digest_name) or "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                errors.append(f"validation.{digest_name} is missing or invalid")

    plan_aliases: dict[str, dict[str, str]] = {}
    entity_plan_ids: list[str] = []
    for raw_plan_id, raw_plan in plans.items():
        plan_id = str(raw_plan_id)
        if not _CONTRACT_IDENTIFIER_PATTERN.fullmatch(plan_id):
            errors.append(f"unsafe relation plan id {plan_id!r}")
        if not isinstance(raw_plan, dict):
            errors.append(f"relation_plans.{plan_id} must be an object")
            continue
        kind = str(raw_plan.get("kind") or "")
        if kind not in {"entity", "user_aggregation", "scalar"}:
            errors.append(f"relation_plans.{plan_id}: unsupported kind {kind!r}")
        if kind == "entity":
            entity_plan_ids.append(plan_id)
        aliases, alias_errors = _deployment_contract_aliases(
            plan_id, raw_plan, contract
        )
        plan_aliases[plan_id] = aliases
        errors.extend(alias_errors)
        source = raw_plan.get("source")
        visible_aliases = (
            {str(source.get("alias")): str(source.get("table"))}
            if isinstance(source, dict) and source.get("alias")
            else {}
        )
        for index, join in enumerate(raw_plan.get("joins", [])):
            if isinstance(join, dict) and join.get("alias"):
                visible_aliases[str(join["alias"])] = str(join.get("table") or "")
            if isinstance(join, dict) and join.get("on"):
                errors.extend(
                    _contract_expression_errors(
                        f"relation_plans.{plan_id}.joins[{index}].on",
                        join["on"],
                        visible_aliases,
                        contract,
                    )
                )
        filters = raw_plan.get("filters", [])
        if not isinstance(filters, list):
            errors.append(f"relation_plans.{plan_id}.filters must be a list")
            filters = []
        for index, filter_expression in enumerate(filters):
            errors.extend(
                _contract_expression_errors(
                    f"relation_plans.{plan_id}.filters[{index}]",
                    filter_expression,
                    aliases,
                    contract,
                )
            )
        if kind in {"entity", "user_aggregation"}:
            errors.extend(
                _contract_expression_errors(
                    f"relation_plans.{plan_id}.entity_key_expression",
                    raw_plan.get("entity_key_expression"),
                    aliases,
                    contract,
                )
            )
    if entity_plan_ids != [base_plan_id]:
        errors.append(
            "exactly one entity plan must exist and match entity.base_relation_plan"
        )

    for raw_feature, raw_spec in features.items():
        feature = str(raw_feature)
        if not _CONTRACT_IDENTIFIER_PATTERN.fullmatch(feature):
            errors.append(f"features contains unsafe output name {feature!r}")
        if not isinstance(raw_spec, dict):
            errors.append(f"features.{feature} must be an object")
            continue
        plan_id = str(raw_spec.get("relation_plan") or "")
        if plan_id not in plans:
            errors.append(f"features.{feature}: unknown relation plan {plan_id!r}")
            continue
        expression = str(raw_spec.get("expression") or "")
        errors.extend(
            _contract_expression_errors(
                f"features.{feature}.expression",
                expression,
                plan_aliases.get(plan_id, {}),
                contract,
            )
        )
        refs = set(
            _CONTRACT_QUALIFIED_COLUMN_PATTERN.findall(
                _strip_sql_comments_and_literals(expression)
            )
        )
        raw_source_columns = raw_spec.get("source_columns")
        if not isinstance(raw_source_columns, list):
            errors.append(f"features.{feature}.source_columns must be a list")
            raw_source_columns = []
        declared_refs: set[tuple[str, str]] = set()
        for index, source_column in enumerate(raw_source_columns):
            if not isinstance(source_column, dict):
                errors.append(
                    f"features.{feature}.source_columns[{index}] must be an object"
                )
                continue
            alias = str(source_column.get("alias") or "")
            column = str(source_column.get("column") or "")
            declared_refs.add((alias, column))
            table = plan_aliases.get(plan_id, {}).get(alias)
            if table is None:
                errors.append(f"features.{feature}: undeclared source alias {alias!r}")
            elif column not in contract.table_columns.get(table, {}):
                errors.append(f"features.{feature}: unknown source column {table}.{column}")
        missing_refs = sorted(refs - declared_refs)
        if missing_refs:
            errors.append(
                f"features.{feature}: source_columns does not cover "
                + ", ".join(f"{alias}.{column}" for alias, column in missing_refs)
            )
        extra_refs = sorted(declared_refs - refs)
        if extra_refs:
            errors.append(
                f"features.{feature}: source_columns contains references absent from expression "
                + ", ".join(f"{alias}.{column}" for alias, column in extra_refs)
            )
        null_policy = raw_spec.get("null_policy")
        if not isinstance(null_policy, dict) or null_policy.get("kind") not in {
            "preserve",
            "fill",
        }:
            errors.append(f"features.{feature}: invalid null_policy")
        elif null_policy.get("kind") == "fill" and "value" not in null_policy:
            errors.append(f"features.{feature}: fill null_policy requires value")

    if errors:
        raise SystemExit(
            "Invalid Step2 deployment feature contract: "
            + " | ".join(sorted(set(errors)))
        )

    normalized_features: list[dict[str, Any]] = []
    for feature in sorted(features):
        spec = features[feature]
        plan_id = str(spec["relation_plan"])
        aliases = plan_aliases[plan_id]
        refs = [
            {**item, "table": aliases[str(item["alias"])]}
            for item in spec.get("source_columns", [])
        ]
        normalized_features.append(
            {
                "feature": feature,
                "feature_kind": plans[plan_id]["kind"],
                "relation_plan": plan_id,
                "source_tables": list(dict.fromkeys(item["table"] for item in refs)),
                "source_columns": [str(item["column"]) for item in refs],
                "source_column_refs": refs,
                "sql_expression": spec["expression"],
                "output_type": spec.get("output_type"),
                "null_policy": spec["null_policy"],
                "renderable": True,
                "diagnostics": [],
            }
        )
    normalized = {
        "version": 2,
        "source": DEPLOYMENT_FEATURE_CONTRACT,
        "source_artifact": deployment.get("source_artifact"),
        "entity": entity,
        "relation_plans": plans,
        "features": normalized_features,
        "validation": deployment.get("validation"),
    }
    _write_json(OUTPUT_DIR / "step2_3_feature_derivation.json", normalized)
    return deployment


def _lineage_index(lineage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["feature"]): item
        for item in lineage.get("features", [])
        if isinstance(item, dict) and item.get("feature")
    }


def _quote_identifier(identifier: str | None) -> str:
    if not identifier:
        raise ValueError("Identifier cannot be empty")
    return "`" + str(identifier).replace("`", "``") + "`"


def _qualified_table(database: str, table: str) -> str:
    return f"{_quote_identifier(database)}.{_quote_identifier(table)}"


def _sql_literal(value: str) -> str:
    raw = _strip_markup(value).strip()
    if raw.startswith("'") and raw.endswith("'"):
        raw = raw[1:-1]
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw.upper() == "NULL":
        return "NULL"
    try:
        number = float(raw)
    except ValueError:
        return "'" + raw.replace("\\", "\\\\").replace("'", "''") + "'"
    if math.isfinite(number):
        return raw
    raise ValueError(f"Non-finite numeric SQL literal: {raw}")


def _sql_string_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _is_numeric_literal(value: str) -> bool:
    raw = _strip_markup(value).strip().strip("'\"")
    try:
        return math.isfinite(float(raw))
    except ValueError:
        return False


def _split_and(condition: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"\s+AND\s+", str(condition).strip(), flags=re.I) if piece.strip()]


def parse_tree_atom(
    atom: str,
    alias: str = "features",
    feature_prefix: str = "__tree_",
) -> tuple[str, str]:
    match = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(<=|>=|!=|=|<|>)\s*(.*?)\s*",
        atom,
    )
    if not match:
        raise ValueError(f"Unsupported decision-tree condition: {atom}")
    raw_feature, operator, raw_value = match.groups()
    feature = raw_feature.split(".")[-1]
    reference = f"{alias}.{_quote_identifier(feature_prefix + feature)}"
    value = _strip_markup(raw_value)
    if value.strip("'\"") in {"__MISSING__", ""}:
        return feature, f"{reference} IS NULL"
    comparable = f"toFloat64OrZero(toString({reference}))" if _is_numeric_literal(value) else reference
    return feature, f"{comparable} {operator} {_sql_literal(value)}"


def parse_tree_condition(
    condition: str,
    alias: str = "features",
    feature_prefix: str = "__tree_",
) -> tuple[set[str], str]:
    features: set[str] = set()
    sql_parts: list[str] = []
    for atom in _split_and(condition):
        feature, sql = parse_tree_atom(atom, alias, feature_prefix)
        features.add(feature)
        sql_parts.append(sql)
    if not sql_parts:
        raise ValueError(f"Empty decision-tree condition: {condition}")
    return features, " AND ".join(sql_parts)


def build_tree_candidate(path: Path) -> CandidateSQL:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    required = {"condition", "score"}
    if not required.issubset(frame.columns):
        raise SystemExit(f"{path.name} must contain columns: {sorted(required)}")
    frame = frame.drop_duplicates(subset=["condition", "score"]).reset_index(drop=True)
    branches: list[tuple[str, str]] = []
    features: set[str] = set()
    failures: list[str] = []
    for row_number, row in frame.iterrows():
        try:
            used, sql_condition = parse_tree_condition(str(row["condition"]))
            score = _sql_literal(str(row["score"]))
        except ValueError as exc:
            failures.append(f"row {row_number + 2}: {exc}")
            continue
        features.update(used)
        branches.append((sql_condition, score))
    coverage = len(branches) / len(frame) if len(frame) else 0.0
    if not branches:
        expression = "CAST(0 AS Float64)"
    else:
        pieces: list[str] = ["multiIf("]
        for index, (condition, score) in enumerate(branches):
            comma = "," if index < len(branches) - 1 else ","
            pieces.append(f"      {condition}, toFloat64({score}){comma}")
        pieces.append("      toFloat64(0)")
        pieces.append("    )")
        expression = "\n".join(pieces)
    return CandidateSQL(
        name="decision_tree",
        expression=expression,
        features=features,
        rule_count=len(branches),
        parse_coverage=coverage,
        renderable=coverage == 1.0,
        render_errors=failures,
    )


def parse_scorecard_condition(feature: str, condition: str, alias: str = "features") -> str:
    reference = f"{alias}.{_quote_identifier(feature)}"
    text = str(condition).strip()
    if "__MISSING__" in text.upper() or text.upper() in {"MISSING", "IS NULL", "NULL"}:
        return f"{reference} IS NULL"

    interval = re.fullmatch(r">\s*(.*?)\s+AND\s+<=\s*(.*?)", text, flags=re.I)
    if interval:
        comparable = f"toFloat64OrZero(toString({reference}))"
        return f"{comparable} > {_sql_literal(interval.group(1))} AND {comparable} <= {_sql_literal(interval.group(2))}"

    for operator in ("<=", ">=", "!=", ">", "<", "="):
        if text.startswith(operator):
            value = text[len(operator) :].strip()
            if value.strip("'\"") in {"__MISSING__", ""}:
                return f"{reference} IS NULL"
            comparable = f"toFloat64OrZero(toString({reference}))" if _is_numeric_literal(value) else reference
            return f"{comparable} {operator} {_sql_literal(value)}"
    raise ValueError(f"Unsupported scorecard condition: {condition}")


def build_scorecard_candidate(path: Path) -> CandidateSQL:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    required = {"feature", "condition", "weighted_score"}
    if not required.issubset(frame.columns):
        raise SystemExit(f"{path.name} must contain columns: {sorted(required)}")
    branches: list[str] = []
    features: set[str] = set()
    failures: list[str] = []
    for row_number, row in frame.iterrows():
        feature = str(row["feature"]).strip()
        try:
            condition = parse_scorecard_condition(feature, str(row["condition"]))
            score = _sql_literal(str(row["weighted_score"]))
        except ValueError as exc:
            failures.append(f"row {row_number + 2}: {exc}")
            continue
        features.add(feature)
        branches.append(f"if({condition}, toFloat64({score}), toFloat64(0))")
    coverage = len(branches) / len(frame) if len(frame) else 0.0
    expression = "\n      + ".join(branches) if branches else "CAST(0 AS Float64)"
    return CandidateSQL(
        name="scorecard",
        expression=expression,
        features=features,
        rule_count=len(branches),
        parse_coverage=coverage,
        renderable=coverage == 1.0,
        render_errors=failures,
    )


def apply_deployment_contract(
    tree: CandidateSQL,
    scorecard: CandidateSQL,
    tree_preprocessing: dict[str, Any],
) -> None:
    errors = list(tree.deployment_errors or [])
    validation = tree_preprocessing.get("validation")
    metadata = tree_preprocessing.get("features")
    if not isinstance(validation, dict) or validation.get("passed") is not True:
        errors.append(
            "reconstructed decision-tree preprocessing did not pass validation"
        )
    if not isinstance(metadata, dict):
        errors.append("decision-tree preprocessing feature metadata is missing")
        metadata = {}
    missing = sorted(tree.features - set(metadata))
    if missing:
        errors.append(
            "decision-tree preprocessing metadata lacks: " + ", ".join(missing)
        )
    unsupported = sorted(
        feature
        for feature in tree.features & set(metadata)
        if metadata[feature].get("kind")
        not in {
            "numeric_quantile_ordinal",
            "numeric_identity",
            "categorical_label_encoder",
        }
    )
    if unsupported:
        errors.append(
            "unsupported decision-tree preprocessing kind for: "
            + ", ".join(unsupported)
        )
    if errors:
        tree.renderable = False
        tree.deployment_errors = errors
    if scorecard.parse_coverage != 1.0:
        scorecard.renderable = False


def _find_join_key(table: str, contract: RuntimeContract) -> str | None:
    columns = contract.table_columns.get(table, {})
    candidates = contract.validated_keys.get(table, [])
    for candidate in (
        *candidates,
        *contract.user_id_aliases,
        contract.user_id,
        "usid",
        "rank_flg",
        "dsid",
    ):
        if candidate in columns:
            return candidate
    return None


def _validate_lineage_entry(feature: str, entry: dict[str, Any], contract: RuntimeContract) -> list[str]:
    errors: list[str] = []
    source_tables = entry.get("source_tables") or ([entry["source_table"]] if entry.get("source_table") else [])
    if not source_tables:
        return [f"{feature}: no source table"]
    for table in source_tables:
        if table not in contract.table_columns:
            errors.append(f"{feature}: unknown source table {table}")
    source_columns = entry.get("source_columns") or []
    for column in source_columns:
        if not any(column in contract.table_columns.get(table, {}) for table in source_tables):
            errors.append(f"{feature}: unknown source column {column}")
    game_dimension = bool(source_tables) and all(
        table in contract.game_dimension_tables for table in source_tables
    )
    if game_dimension:
        if not contract.target_game:
            errors.append(f"{feature}: target_game is absent for game dimension feature")
        for table in source_tables:
            if table not in contract.game_keys:
                errors.append(f"{feature}: no validated game key for {table}")
    elif source_tables != [contract.user_table]:
        for table in source_tables:
            if _find_join_key(table, contract) is None:
                errors.append(f"{feature}: no user join key for {table}")
    expression = entry.get("sql_expression")
    source_feature = entry.get("source_feature")
    if not expression and not source_feature and feature not in contract.table_columns.get(contract.user_table, {}):
        errors.append(f"{feature}: no SQL expression or source feature")
    return errors


def _aggregate_expression_for_entry(entry: dict[str, Any]) -> str | None:
    expression = str(entry.get("sql_expression") or "").strip()
    if expression:
        return expression
    source_feature = str(entry.get("source_feature") or "").strip()
    method = str(entry.get("method") or "").lower()
    if source_feature:
        if any(token in method for token in ("sum", "avg", "max", "min", "count")):
            inferred = _aggregation_expression(method, source_feature)
            if inferred:
                return inferred
        return f"any({_quote_identifier(source_feature)})"
    if "count" in method:
        return "count()"
    return None


def _null_wrapped(reference: str, entry: dict[str, Any]) -> str:
    strategy = str(entry.get("null_strategy") or "").lower()
    if any(token in strategy for token in ("fill 0", "as 0", "填充 0", "null as 0")):
        return f"coalesce({reference}, 0)"
    return reference


def _qualify_expression_columns(
    expression: str,
    columns: Iterable[str],
    alias: str,
    source_tables: Iterable[str] = (),
) -> str:
    result = expression
    normalized_columns = {
        str(column) for column in columns if column is not None and str(column)
    }
    for column in sorted(normalized_columns, key=len, reverse=True):
        for table in sorted(set(source_tables), key=len, reverse=True):
            qualified_pattern = re.compile(
                rf"`?{re.escape(table)}`?\s*\.\s*`?{re.escape(column)}`?"
            )
            parts = re.split(r"('(?:''|\\.|[^'])*')", result)
            for index in range(0, len(parts), 2):
                parts[index] = qualified_pattern.sub(
                    f"{alias}.{_quote_identifier(column)}",
                    parts[index],
                )
            result = "".join(parts)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_`.])`?{re.escape(column)}`?(?![A-Za-z0-9_`])"
        )
        parts = re.split(r"('(?:''|\\.|[^'])*')", result)
        for index in range(0, len(parts), 2):
            parts[index] = pattern.sub(f"{alias}.{_quote_identifier(column)}", parts[index])
        result = "".join(parts)
    return result


def _source_relation(
    contract: RuntimeContract,
    table: str,
    *,
    trial_mode: bool,
) -> str:
    qualified = _qualified_table(contract.source_database, table)
    if not trial_mode:
        return qualified
    return f"(SELECT * FROM {qualified} LIMIT {TRIAL_SOURCE_LIMIT_PLACEHOLDER})"


def _render_deployment_expression(value: Any, contract: RuntimeContract) -> str:
    expression = str(value or "").strip()
    expression = re.sub(
        r"\{\{\s*target_game\s*\}\}",
        lambda _match: _sql_string_literal(contract.target_game),
        expression,
    )
    if "{{" in expression or "}}" in expression:
        raise ValueError(f"Unresolved deployment-contract parameter: {value}")
    return expression


def _render_deployment_from(
    plan: dict[str, Any],
    contract: RuntimeContract,
    *,
    trial_mode: bool,
) -> list[str]:
    source = plan["source"]
    lines = [
        "FROM "
        + _source_relation(contract, str(source["table"]), trial_mode=trial_mode)
        + " AS "
        + _quote_identifier(str(source["alias"]))
    ]
    for join in plan.get("joins", []):
        join_type = str(join["type"]).upper()
        line = (
            f"{join_type} JOIN "
            + _source_relation(contract, str(join["table"]), trial_mode=trial_mode)
            + " AS "
            + _quote_identifier(str(join["alias"]))
        )
        if join_type != "CROSS":
            line += " ON " + _render_deployment_expression(join["on"], contract)
        lines.append(line)
    return lines


def _deployment_null_wrapped(expression: str, spec: dict[str, Any]) -> str:
    policy = spec["null_policy"]
    if policy["kind"] == "preserve":
        return expression
    value = policy.get("value")
    if value is None:
        literal = "NULL"
    elif isinstance(value, bool):
        literal = "1" if value else "0"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("Deployment-contract fill value must be finite")
        literal = str(value)
    elif isinstance(value, str):
        literal = _sql_string_literal(value)
    else:
        raise ValueError(f"Unsupported deployment-contract fill value: {value!r}")
    return f"coalesce({expression}, {literal})"


def render_feature_subquery_from_deployment_contract(
    required_features: set[str],
    tree_features: set[str],
    deployment: dict[str, Any],
    contract: RuntimeContract,
    tree_preprocessing: dict[str, Any],
    *,
    trial_mode: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Render required features from the validated Step2 deployment contract."""
    plans = deployment["relation_plans"]
    feature_specs = deployment["features"]
    missing = sorted(required_features - set(feature_specs))
    if missing:
        raise ValueError(
            "features absent from Step2 deployment contract: " + ", ".join(missing)
        )
    base_plan_id = str(deployment["entity"]["base_relation_plan"])
    base_plan = plans[base_plan_id]
    base_key = _render_deployment_expression(
        base_plan["entity_key_expression"], contract
    )
    grouped: dict[str, list[str]] = {}
    for feature in sorted(required_features):
        grouped.setdefault(str(feature_specs[feature]["relation_plan"]), []).append(feature)

    select_lines = [
        f"      {base_key} AS {_quote_identifier('user_id')}"
    ]
    for feature in grouped.get(base_plan_id, []):
        expression = _render_deployment_expression(
            feature_specs[feature]["expression"], contract
        )
        select_lines.append(
            "      "
            + _deployment_null_wrapped(expression, feature_specs[feature])
            + " AS "
            + _quote_identifier(feature)
        )

    join_blocks: list[str] = []
    for plan_id in sorted(set(grouped) - {base_plan_id}):
        plan = plans[plan_id]
        kind = str(plan["kind"])
        plan_alias = "plan_" + plan_id
        plan_features = grouped[plan_id]
        projections: list[str] = []
        if kind == "user_aggregation":
            key_expression = _render_deployment_expression(
                plan["entity_key_expression"], contract
            )
            projections.append(
                f"        {key_expression} AS {_quote_identifier('__join_key')}"
            )
        elif kind != "scalar":
            raise ValueError(
                f"Non-base relation plan {plan_id!r} has unsupported kind {kind!r}"
            )
        for feature in plan_features:
            expression = _render_deployment_expression(
                feature_specs[feature]["expression"], contract
            )
            projections.append(
                "        " + expression + " AS " + _quote_identifier(feature)
            )
            reference = f"{plan_alias}.{_quote_identifier(feature)}"
            select_lines.append(
                "      "
                + _deployment_null_wrapped(reference, feature_specs[feature])
                + " AS "
                + _quote_identifier(feature)
            )
        block = [
            "    " + ("LEFT JOIN (" if kind == "user_aggregation" else "CROSS JOIN ("),
            "      SELECT",
            ",\n".join(projections),
            *["      " + line for line in _render_deployment_from(plan, contract, trial_mode=trial_mode)],
        ]
        filters = plan.get("filters", [])
        if filters:
            block.append(
                "      WHERE "
                + " AND ".join(
                    f"({_render_deployment_expression(item, contract)})"
                    for item in filters
                )
            )
        if kind == "user_aggregation":
            block.append(f"      GROUP BY {key_expression}")
        block.append("    ) AS " + plan_alias)
        if kind == "user_aggregation":
            block.append(
                "      ON toString("
                + base_key
                + ") = toString("
                + plan_alias
                + "."
                + _quote_identifier("__join_key")
                + ")"
            )
        join_blocks.append("\n".join(block))

    feature_sql_lines = [
        "    SELECT",
        ",\n".join(select_lines),
        *["    " + line for line in _render_deployment_from(base_plan, contract, trial_mode=trial_mode)],
    ]
    feature_sql_lines.extend(join_blocks)
    base_filters = base_plan.get("filters", [])
    if base_filters:
        feature_sql_lines.append(
            "    WHERE "
            + " AND ".join(
                f"({_render_deployment_expression(item, contract)})"
                for item in base_filters
            )
        )
    feature_sql = "\n".join(feature_sql_lines)

    lineage_rows: list[dict[str, Any]] = []
    for feature in sorted(required_features):
        spec = feature_specs[feature]
        plan_id = str(spec["relation_plan"])
        aliases, _errors = _deployment_contract_aliases(
            plan_id, plans[plan_id], contract
        )
        refs = spec.get("source_columns", [])
        lineage_rows.append(
            {
                "feature": feature,
                "feature_kind": plans[plan_id]["kind"],
                "relation_plan": plan_id,
                "source_tables": list(
                    dict.fromkeys(
                        aliases[str(item["alias"])] for item in refs
                    )
                ),
                "source_columns": [str(item["column"]) for item in refs],
                "source_column_refs": refs,
                "sql_expression": _render_deployment_expression(
                    spec["expression"], contract
                ),
                "null_policy": spec["null_policy"],
            }
        )
    feature_sql, encoded_report = _wrap_tree_preprocessing(
        feature_sql,
        required_features,
        tree_features,
        tree_preprocessing,
    )
    report = {
        "lineage_source": DEPLOYMENT_FEATURE_CONTRACT,
        "deployment_contract_version": deployment["contract_version"],
        "deployment_contract_validation": deployment["validation"],
        "required_feature_count": len(required_features),
        "resolved_feature_count": len(required_features),
        "feature_coverage": 1.0,
        "features": lineage_rows,
        "tree_encoded_features": encoded_report,
        "tree_preprocessing_validation": tree_preprocessing.get("validation"),
        "trial_mode": trial_mode,
    }
    return feature_sql, report


def _dimension_expression_for_entry(
    entry: dict[str, Any],
    table: str,
) -> str | None:
    expression = _aggregate_expression_for_entry(entry)
    if not expression:
        return None
    qualified = _qualify_expression_columns(
        expression,
        entry.get("source_columns") or [entry.get("source_feature")],
        "dimension_rows",
        [table],
    )
    if re.search(
        r"\b(?:any|anyLast|avg|count|countIf|max|min|sum|sumIf|uniqExact)\s*\(",
        qualified,
        flags=re.I,
    ):
        return qualified
    return f"any({qualified})"


def _tree_preprocessing_expression(
    feature: str,
    metadata: dict[str, Any],
    alias: str,
) -> str:
    reference = f"{alias}.{_quote_identifier(feature)}"
    kind = metadata.get("kind")
    if kind == "numeric_identity":
        missing_value = float(metadata.get("missing_encoded_value", -1.0))
        return (
            f"coalesce(toFloat64OrNull(toString({reference})), "
            f"toFloat64({missing_value:.17g}))"
        )
    if kind == "numeric_quantile_ordinal":
        edges = metadata.get("bin_edges")
        if not isinstance(edges, list) or len(edges) < 2:
            raise ValueError(f"{feature}: invalid reconstructed numeric bin edges")
        numeric = f"toFloat64OrNull(toString({reference}))"
        missing_value = float(metadata.get("missing_encoded_value", -1.0))
        branches = [
            f"{numeric} IS NULL",
            f"toFloat64({missing_value:.17g})",
        ]
        for index, edge in enumerate(edges[1:-1]):
            branches.extend(
                [
                    f"{numeric} < {float(edge):.17g}",
                    f"toFloat64({index})",
                ]
            )
        branches.append(f"toFloat64({len(edges) - 2})")
        return "multiIf(" + ", ".join(branches) + ")"
    if kind == "categorical_label_encoder":
        classes = metadata.get("classes")
        mapping = metadata.get("mapping")
        if not isinstance(classes, list) or not isinstance(mapping, dict):
            raise ValueError(f"{feature}: invalid reconstructed category mapping")
        categorical = (
            f"ifNull(toString({reference}), "
            f"{_sql_string_literal(str(metadata.get('missing_string_value', 'nan')))})"
        )
        branches: list[str] = []
        for value in classes:
            encoded = mapping.get(str(value))
            if encoded is None:
                raise ValueError(f"{feature}: category mapping lacks {value!r}")
            branches.extend(
                [
                    f"{categorical} = {_sql_string_literal(str(value))}",
                    f"toFloat64({int(encoded)})",
                ]
            )
        unknown = float(metadata.get("unknown_encoded_value", -1.0))
        branches.append(f"toFloat64({unknown:.17g})")
        return "multiIf(" + ", ".join(branches) + ")"
    raise ValueError(f"{feature}: unsupported preprocessing kind {kind!r}")


def _wrap_tree_preprocessing(
    feature_sql: str,
    required_features: set[str],
    tree_features: set[str],
    tree_preprocessing: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    if not tree_features:
        return feature_sql, []
    metadata = tree_preprocessing.get("features")
    if not isinstance(metadata, dict):
        raise ValueError("decision-tree preprocessing feature metadata is missing")
    encoded_selects: list[str] = []
    encoded_report: list[dict[str, Any]] = []
    for feature in sorted(tree_features):
        if feature not in metadata:
            raise ValueError(
                f"{feature}: absent from step3_5_preprocessing_reconstructed.json"
            )
        expression = _tree_preprocessing_expression(
            feature,
            metadata[feature],
            "raw_features",
        )
        encoded_name = "__tree_" + feature
        encoded_selects.append(
            f"      {expression} AS {_quote_identifier(encoded_name)}"
        )
        encoded_report.append(
            {
                "feature": feature,
                "encoded_feature": encoded_name,
                "kind": metadata[feature].get("kind"),
            }
        )
    raw_selects = [
        f"      raw_features.{_quote_identifier('user_id')} AS {_quote_identifier('user_id')}",
        *[
            f"      raw_features.{_quote_identifier(feature)} AS {_quote_identifier(feature)}"
            for feature in sorted(required_features)
        ],
    ]
    wrapped = "\n".join(
        [
            "    SELECT",
            ",\n".join([*raw_selects, *encoded_selects]),
            "    FROM (",
            feature_sql,
            "    ) AS raw_features",
        ]
    )
    return wrapped, encoded_report


def render_feature_subquery(
    required_features: set[str],
    tree_features: set[str],
    lineage: dict[str, Any],
    contract: RuntimeContract,
    tree_preprocessing: dict[str, Any],
    *,
    trial_mode: bool = False,
) -> tuple[str, dict[str, Any]]:
    index = _lineage_index(lineage)
    errors: list[str] = []
    direct_selects: list[str] = []
    grouped: dict[tuple[tuple[str, ...], str], list[tuple[str, dict[str, Any]]]] = {}
    dimension_grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    for feature in sorted(required_features):
        entry = index.get(feature)
        if entry is None:
            errors.append(f"{feature}: absent from step2_3_feature_derivation.json")
            continue
        entry_errors = _validate_lineage_entry(feature, entry, contract)
        if entry_errors:
            errors.extend(entry_errors)
            continue
        source_tables = entry.get("source_tables") or [entry.get("source_table")]
        source_tables = [str(table) for table in source_tables if table]
        if source_tables == [contract.user_table]:
            source_feature = str(entry.get("source_feature") or feature)
            if source_feature not in contract.table_columns[contract.user_table]:
                errors.append(f"{feature}: {contract.user_table}.{source_feature} does not exist")
                continue
            raw_expression = str(entry.get("sql_expression") or "").strip()
            if raw_expression:
                reference = _qualify_expression_columns(
                    raw_expression,
                    entry.get("source_columns") or [source_feature],
                    "u",
                    [contract.user_table],
                )
            else:
                reference = f"u.{_quote_identifier(source_feature)}"
            direct_selects.append(f"      {_null_wrapped(reference, entry)} AS {_quote_identifier(feature)}")
            continue

        if source_tables and all(
            table in contract.game_dimension_tables for table in source_tables
        ):
            if len(source_tables) != 1:
                errors.append(
                    f"{feature}: game dimension feature must resolve to exactly one table"
                )
                continue
            dimension_grouped.setdefault(source_tables[0], []).append((feature, entry))
            continue

        first_table = source_tables[0]
        join_key = _find_join_key(first_table, contract)
        if not join_key:
            errors.append(f"{feature}: no join key for {first_table}")
            continue
        if any(join_key not in contract.table_columns[table] for table in source_tables):
            errors.append(f"{feature}: unioned source tables do not share join key {join_key}")
            continue
        grouped.setdefault((tuple(source_tables), join_key), []).append((feature, entry))

    joins: list[str] = []
    aggregate_selects: list[str] = []
    lineage_rows: list[dict[str, Any]] = []
    for group_number, ((source_tables, join_key), entries) in enumerate(sorted(grouped.items()), start=1):
        alias = f"agg_{group_number}"
        aggregate_lines: list[str] = []
        for feature, entry in entries:
            expression = _aggregate_expression_for_entry(entry)
            if not expression:
                errors.append(f"{feature}: cannot derive aggregate SQL expression")
                continue
            aggregate_lines.append(f"        {expression} AS {_quote_identifier(feature)}")
            reference = f"{alias}.{_quote_identifier(feature)}"
            aggregate_selects.append(f"      {_null_wrapped(reference, entry)} AS {_quote_identifier(feature)}")
            lineage_rows.append(
                {
                    "feature": feature,
                    "source_tables": list(source_tables),
                    "source_columns": entry.get("source_columns", []),
                    "join_key": join_key,
                    "sql_expression": expression,
                    "null_strategy": entry.get("null_strategy"),
                }
            )
        if not aggregate_lines:
            continue
        if len(source_tables) == 1:
            source_sql = _source_relation(
                contract,
                source_tables[0],
                trial_mode=trial_mode,
            )
        else:
            union_parts = [
                "SELECT * FROM "
                + _source_relation(contract, table, trial_mode=trial_mode)
                for table in source_tables
            ]
            source_sql = "(\n          " + "\n          UNION ALL\n          ".join(union_parts) + "\n        )"
        qualified_aggregate_lines: list[str] = []
        for feature, entry in entries:
            expression = _aggregate_expression_for_entry(entry)
            if not expression:
                continue
            expression = _qualify_expression_columns(
                expression,
                entry.get("source_columns") or [entry.get("source_feature")],
                "source_rows",
                source_tables,
            )
            qualified_aggregate_lines.append(
                f"        {expression} AS {_quote_identifier(feature)}"
            )
            for row in lineage_rows:
                if row["feature"] == feature:
                    row["sql_expression"] = expression
        aggregate_block = ",\n".join(qualified_aggregate_lines)
        joins.append(
            "\n".join(
                [
                    "    LEFT JOIN (",
                    "      SELECT",
                    "        source_rows."
                    + _quote_identifier(join_key)
                    + f" AS {_quote_identifier('__join_key')},",
                    aggregate_block,
                    f"      FROM {source_sql} AS source_rows",
                    f"      GROUP BY source_rows.{_quote_identifier(join_key)}",
                    f"    ) AS {alias}",
                    "      ON toString(u."
                    + _quote_identifier(contract.user_id)
                    + f") = toString({alias}.{_quote_identifier('__join_key')})",
                ]
            )
        )

    dimension_joins: list[str] = []
    dimension_selects: list[str] = []
    for group_number, (table, entries) in enumerate(
        sorted(dimension_grouped.items()),
        start=1,
    ):
        game_key = contract.game_keys.get(table)
        if not game_key:
            errors.extend(
                f"{feature}: no validated game key for {table}"
                for feature, _entry in entries
            )
            continue
        if not contract.target_game:
            errors.extend(
                f"{feature}: target_game is absent"
                for feature, _entry in entries
            )
            continue
        alias = f"game_dim_{group_number}"
        projections: list[str] = []
        for feature, entry in entries:
            expression = _dimension_expression_for_entry(entry, table)
            if not expression:
                errors.append(f"{feature}: cannot derive game dimension SQL expression")
                continue
            projections.append(f"        {expression} AS {_quote_identifier(feature)}")
            dimension_selects.append(
                f"      {_null_wrapped(f'{alias}.{_quote_identifier(feature)}', entry)} "
                f"AS {_quote_identifier(feature)}"
            )
            lineage_rows.append(
                {
                    "feature": feature,
                    "feature_kind": "game_dimension",
                    "source_tables": [table],
                    "source_columns": entry.get("source_columns", []),
                    "game_key": game_key,
                    "target_game": contract.target_game,
                    "sql_expression": expression,
                    "null_strategy": entry.get("null_strategy"),
                }
            )
        if not projections:
            continue
        source_sql = _source_relation(contract, table, trial_mode=trial_mode)
        dimension_joins.append(
            "\n".join(
                [
                    "    CROSS JOIN (",
                    "      SELECT",
                    ",\n".join(projections),
                    f"      FROM {source_sql} AS dimension_rows",
                    "      WHERE toString(dimension_rows."
                    + _quote_identifier(game_key)
                    + ") = "
                    + _sql_string_literal(contract.target_game),
                    f"    ) AS {alias}",
                ]
            )
        )

    if errors:
        raise ValueError("; ".join(sorted(set(errors))))

    select_lines = [
        f"      u.{_quote_identifier(contract.user_id)} AS {_quote_identifier('user_id')}",
        *direct_selects,
        *aggregate_selects,
        *dimension_selects,
    ]
    feature_sql = "\n".join(
        [
            "    SELECT",
            ",\n".join(select_lines),
            "    FROM "
            + _source_relation(
                contract,
                contract.user_table,
                trial_mode=trial_mode,
            )
            + " AS u",
            *joins,
            *dimension_joins,
        ]
    )
    for feature in sorted(required_features):
        if any(row["feature"] == feature for row in lineage_rows):
            continue
        entry = index[feature]
        lineage_rows.append(
            {
                "feature": feature,
                "feature_kind": entry.get("feature_kind", "direct"),
                "source_tables": entry.get("source_tables") or [entry.get("source_table")],
                "source_columns": entry.get("source_columns", []),
                "join_key": contract.user_id,
                "sql_expression": entry.get("source_feature") or feature,
                "null_strategy": entry.get("null_strategy"),
            }
        )
    feature_sql, encoded_report = _wrap_tree_preprocessing(
        feature_sql,
        required_features,
        tree_features,
        tree_preprocessing,
    )
    report = {
        "required_feature_count": len(required_features),
        "resolved_feature_count": len(required_features),
        "feature_coverage": 1.0,
        "features": sorted(lineage_rows, key=lambda item: item["feature"]),
        "tree_encoded_features": encoded_report,
        "tree_preprocessing_validation": tree_preprocessing.get("validation"),
        "trial_mode": trial_mode,
    }
    return feature_sql, report


def _detect_score_columns(frame: pd.DataFrame, score_name: str) -> tuple[str, str, str]:
    label_candidates = [column for column in frame.columns if column.lower() == "label"]
    score_candidates = [column for column in frame.columns if column == score_name]
    if not label_candidates or not score_candidates:
        raise SystemExit(f"Prediction file must contain label and {score_name}; got {list(frame.columns)}")
    excluded = {label_candidates[0], score_candidates[0]}
    user_candidates = [column for column in frame.columns if column not in excluded]
    if len(user_candidates) != 1:
        raise SystemExit("Prediction file must contain exactly one user id column; got " + ", ".join(user_candidates))
    return user_candidates[0], label_candidates[0], score_candidates[0]


def load_aligned_scores() -> tuple[pd.DataFrame, dict[str, Any]]:
    specifications = (
        ("teacher_score", "step3_4_valid_predictions.csv", "score"),
        ("tree_score", "step3_5_white_box_scores.csv", "white_box_score"),
        ("scorecard_score", "step3_6_white_box_scores.csv", "white_box_score"),
    )
    aligned: pd.DataFrame | None = None
    source_rows: dict[str, int] = {}
    source_user_columns: dict[str, str] = {}
    for target, filename, score_column in specifications:
        frame = pd.read_csv(OUTPUT_DIR / filename, encoding="utf-8-sig")
        user_column, label_column, detected_score = _detect_score_columns(frame, score_column)
        if frame[user_column].isna().any() or frame[user_column].duplicated().any():
            raise SystemExit(f"{filename} has null or duplicate user ids")
        current = frame[[user_column, label_column, detected_score]].rename(
            columns={
                user_column: "user_id",
                label_column: f"label_{target}",
                detected_score: target,
            }
        )
        current["user_id"] = current["user_id"].astype(str)
        current[target] = pd.to_numeric(current[target], errors="coerce")
        if not np.isfinite(current[target].to_numpy(dtype=float)).all():
            raise SystemExit(f"{filename} contains non-finite scores")
        source_rows[filename] = len(current)
        source_user_columns[filename] = user_column
        if aligned is None:
            aligned = current
        else:
            aligned = aligned.merge(current, on="user_id", how="inner", validate="one_to_one")

    assert aligned is not None
    if len(aligned) != min(source_rows.values()) or len(set(source_rows.values())) != 1:
        raise SystemExit("Validation prediction files do not contain the same user set")
    labels = [
        "label_teacher_score",
        "label_tree_score",
        "label_scorecard_score",
    ]
    if not all((aligned[labels[0]] == aligned[column]).all() for column in labels[1:]):
        raise SystemExit("Validation labels differ across prediction files")
    aligned = aligned.rename(columns={labels[0]: "label"}).drop(columns=labels[1:])
    aligned["label"] = pd.to_numeric(aligned["label"], errors="raise").astype(int)
    return aligned, {
        "rows": len(aligned),
        "user_columns": source_user_columns,
        "label_values": sorted(int(value) for value in aligned["label"].unique()),
        "user_sets_aligned": True,
        "labels_aligned": True,
    }


def auc_score(labels: np.ndarray, scores: np.ndarray) -> float:
    labels = np.asarray(labels, dtype=int)
    ranks = pd.Series(scores).rank(method="average").to_numpy(dtype=float)
    positive = labels == 1
    n_positive = int(positive.sum())
    n_negative = int((~positive).sum())
    if not n_positive or not n_negative:
        return float("nan")
    return float((ranks[positive].sum() - n_positive * (n_positive + 1) / 2) / (n_positive * n_negative))


def spearman_score(left: np.ndarray, right: np.ndarray) -> float:
    left_rank = pd.Series(left).rank(method="average").to_numpy(dtype=float)
    right_rank = pd.Series(right).rank(method="average").to_numpy(dtype=float)
    if np.std(left_rank) == 0 or np.std(right_rank) == 0:
        return 0.0
    return float(np.corrcoef(left_rank, right_rank)[0, 1])


def pr_auc_score(labels: np.ndarray, scores: np.ndarray) -> float:
    labels = np.asarray(labels, dtype=int)
    total_positive = int(labels.sum())
    if total_positive == 0:
        return float("nan")
    frame = pd.DataFrame({"label": labels, "score": scores})
    grouped = frame.groupby("score", sort=True)["label"].agg(["sum", "count"]).iloc[::-1]
    tp = grouped["sum"].cumsum().to_numpy(dtype=float)
    selected = grouped["count"].cumsum().to_numpy(dtype=float)
    recall = np.concatenate(([0.0], tp / total_positive))
    precision = np.concatenate(([1.0], tp / selected))
    return float(
        sum(
            (recall[index] - recall[index - 1]) * (precision[index] + precision[index - 1]) / 2
            for index in range(1, len(recall))
        )
    )


def ks_score(labels: np.ndarray, scores: np.ndarray) -> float:
    labels = np.asarray(labels, dtype=int)
    total_positive = int(labels.sum())
    total_negative = len(labels) - total_positive
    if not total_positive or not total_negative:
        return float("nan")
    frame = pd.DataFrame({"label": labels, "score": scores})
    grouped = frame.groupby("score", sort=True)["label"].agg(["sum", "count"]).iloc[::-1]
    tp = grouped["sum"].cumsum().to_numpy(dtype=float) / total_positive
    fp = (grouped["count"] - grouped["sum"]).cumsum().to_numpy(dtype=float) / total_negative
    return float(np.max(np.abs(tp - fp)))


def tie_aware_top_k(labels: np.ndarray, scores: np.ndarray, fraction: float) -> dict[str, float | int]:
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    selected_count = max(1, int(math.ceil(len(labels) * fraction)))
    threshold_index = len(scores) - selected_count
    threshold = float(np.partition(scores, threshold_index)[threshold_index])
    above = scores > threshold
    tied = scores == threshold
    slots = selected_count - int(above.sum())
    positives_above = float(labels[above].sum())
    tied_count = int(tied.sum())
    tied_positive = float(labels[tied].sum())
    expected_hits = positives_above + slots * tied_positive / tied_count
    precision = expected_hits / selected_count
    total_positive = float(labels.sum())
    base_rate = total_positive / len(labels) if len(labels) else float("nan")
    return {
        "fraction": fraction,
        "selected_count": selected_count,
        "threshold": threshold,
        "boundary_tie_count": tied_count,
        "boundary_slots": slots,
        "expected_hits": expected_hits,
        "precision": precision,
        "recall": expected_hits / total_positive if total_positive else float("nan"),
        "lift": precision / base_rate if base_rate else float("nan"),
    }


def candidate_metrics(labels: np.ndarray, scores: np.ndarray, teacher_scores: np.ndarray) -> dict[str, Any]:
    unique_values, counts = np.unique(scores, return_counts=True)
    top = tie_aware_top_k(labels, scores, PRIMARY_K)
    teacher_auc = auc_score(labels, teacher_scores)
    candidate_auc = auc_score(labels, scores)
    return {
        "auc": candidate_auc,
        "pr_auc": pr_auc_score(labels, scores),
        "ks": ks_score(labels, scores),
        "precision_at_k": top["precision"],
        "recall_at_k": top["recall"],
        "lift_at_k": top["lift"],
        "top_k": top,
        "teacher_spearman": spearman_score(teacher_scores, scores),
        "teacher_auc_gap": candidate_auc - teacher_auc,
        "score_unique_count": len(unique_values),
        "largest_tie_count": int(counts.max()),
        "largest_tie_rate": float(counts.max() / len(scores)),
    }


def _bootstrap_difference(
    labels: np.ndarray,
    first: np.ndarray,
    second: np.ndarray,
    *,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    differences: list[float] = []
    for _ in range(max(1, iterations)):
        indices = rng.integers(0, len(labels), size=len(labels))
        sample_labels = labels[indices]
        if sample_labels.min() == sample_labels.max():
            continue
        first_precision = tie_aware_top_k(sample_labels, first[indices], PRIMARY_K)["precision"]
        second_precision = tie_aware_top_k(sample_labels, second[indices], PRIMARY_K)["precision"]
        differences.append(float(first_precision) - float(second_precision))
    if not differences:
        return {"difference": float("nan"), "ci_lower": float("nan"), "ci_upper": float("nan")}
    alpha = (1 - CONFIDENCE_LEVEL) / 2
    values = np.asarray(differences)
    return {
        "difference": float(np.mean(values)),
        "ci_lower": float(np.quantile(values, alpha)),
        "ci_upper": float(np.quantile(values, 1 - alpha)),
        "iterations": len(values),
        "confidence_level": CONFIDENCE_LEVEL,
    }


def _stable_split(user_ids: Iterable[str]) -> np.ndarray:
    values = []
    for user_id in user_ids:
        digest = hashlib.sha256(str(user_id).encode("utf-8")).digest()
        values.append(int.from_bytes(digest[:8], "big") % 2)
    return np.asarray(values, dtype=int)


def _relative_improvement(new_value: float, baseline: float) -> float:
    denominator = max(abs(float(baseline)), 1e-12)
    return (float(new_value) - float(baseline)) / denominator


def _risk_flags(name: str, metrics: dict[str, Any], rule_count: int) -> list[str]:
    flags: list[str] = []
    if metrics["teacher_spearman"] < (0.9 if name == "decision_tree" else 0.8):
        flags.append("low_teacher_spearman")
    if abs(metrics["teacher_auc_gap"]) > 0.05:
        flags.append("large_auc_gap")
    if rule_count > 5000:
        flags.append("high_rule_count")
    if metrics["largest_tie_rate"] > 0.5:
        flags.append("high_score_tie_rate")
    return flags


def choose_strategy(
    aligned: pd.DataFrame,
    tree: CandidateSQL,
    scorecard: CandidateSQL,
) -> tuple[dict[str, Any], dict[str, float]]:
    labels = aligned["label"].to_numpy(dtype=int)
    teacher_scores = aligned["teacher_score"].to_numpy(dtype=float)
    tree_scores = aligned["tree_score"].to_numpy(dtype=float)
    card_scores = aligned["scorecard_score"].to_numpy(dtype=float)

    tree_metrics = candidate_metrics(labels, tree_scores, teacher_scores)
    card_metrics = candidate_metrics(labels, card_scores, teacher_scores)
    tree_metrics["rule_count"] = tree.rule_count
    tree_metrics["rule_parse_coverage"] = tree.parse_coverage
    tree_metrics["rule_parse_errors"] = tree.render_errors or []
    tree_metrics["deployment_renderable"] = tree.renderable
    tree_metrics["deployment_errors"] = tree.deployment_errors or []
    tree_metrics["risk_flags"] = _risk_flags("decision_tree", tree_metrics, tree.rule_count)
    card_metrics["rule_count"] = scorecard.rule_count
    card_metrics["rule_parse_coverage"] = scorecard.parse_coverage
    card_metrics["rule_parse_errors"] = scorecard.render_errors or []
    card_metrics["deployment_renderable"] = scorecard.renderable
    card_metrics["deployment_errors"] = scorecard.deployment_errors or []
    card_metrics["risk_flags"] = _risk_flags("scorecard", card_metrics, scorecard.rule_count)

    if not tree.renderable and not scorecard.renderable:
        raise SystemExit(
            "Neither white-box rule artifact is fully parseable; "
            "this is an invalid technical input contract, not a model-quality gate"
        )
    if tree.renderable != scorecard.renderable:
        strategy = "decision_tree" if tree.renderable else "scorecard"
        return (
            {
                "primary_metric": f"tie_aware_precision_at_{PRIMARY_K:.2%}",
                "configuration": {
                    "primary_k": PRIMARY_K,
                    "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
                    "confidence_level": CONFIDENCE_LEVEL,
                    "minimum_relative_uplift": MIN_RELATIVE_UPLIFT,
                    "random_seed": RANDOM_SEED,
                },
                "candidates": {
                    "decision_tree": tree_metrics,
                    "scorecard": card_metrics,
                },
                "fusion_attempted": False,
                "strategy": strategy,
                "reason": "only_one_strategy_deployable",
            },
            {},
        )

    comparison = _bootstrap_difference(
        labels,
        tree_scores,
        card_scores,
        iterations=BOOTSTRAP_ITERATIONS,
        seed=RANDOM_SEED,
    )
    tree_precision = float(tree_metrics["precision_at_k"])
    card_precision = float(card_metrics["precision_at_k"])
    direct_strategy: str | None = None
    if comparison["ci_lower"] > 0 and _relative_improvement(tree_precision, card_precision) >= MIN_RELATIVE_UPLIFT:
        direct_strategy = "decision_tree"
    elif comparison["ci_upper"] < 0 and _relative_improvement(card_precision, tree_precision) >= MIN_RELATIVE_UPLIFT:
        direct_strategy = "scorecard"

    selection: dict[str, Any] = {
        "primary_metric": f"tie_aware_precision_at_{PRIMARY_K:.2%}",
        "configuration": {
            "primary_k": PRIMARY_K,
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "confidence_level": CONFIDENCE_LEVEL,
            "minimum_relative_uplift": MIN_RELATIVE_UPLIFT,
            "random_seed": RANDOM_SEED,
        },
        "candidates": {
            "decision_tree": tree_metrics,
            "scorecard": card_metrics,
        },
        "paired_bootstrap_tree_minus_scorecard": comparison,
        "fusion_attempted": direct_strategy is None,
    }

    parameters: dict[str, float] = {}
    if direct_strategy:
        selection["strategy"] = direct_strategy
        selection["reason"] = "one_strategy_significantly_better"
        return selection, parameters

    split = _stable_split(aligned["user_id"])
    fit_mask = split == 0
    eval_mask = split == 1
    if not fit_mask.any() or not eval_mask.any():
        fit_mask = np.arange(len(aligned)) % 2 == 0
        eval_mask = ~fit_mask

    tree_mean = float(np.mean(tree_scores[fit_mask]))
    tree_std = float(np.std(tree_scores[fit_mask]))
    card_mean = float(np.mean(card_scores[fit_mask]))
    card_std = float(np.std(card_scores[fit_mask]))
    tree_std = tree_std if tree_std > 0 else 1.0
    card_std = card_std if card_std > 0 else 1.0
    tree_z = (tree_scores - tree_mean) / tree_std
    card_z = (card_scores - card_mean) / card_std

    weight_results: list[dict[str, float]] = []
    for weight in np.linspace(0.05, 0.95, 19):
        fused = weight * tree_z + (1 - weight) * card_z
        fit_precision = float(tie_aware_top_k(labels[fit_mask], fused[fit_mask], PRIMARY_K)["precision"])
        weight_results.append({"tree_weight": float(round(weight, 2)), "fit_precision_at_k": fit_precision})
    best = max(weight_results, key=lambda item: (item["fit_precision_at_k"], -item["tree_weight"]))
    weight = float(best["tree_weight"])
    fused = weight * tree_z + (1 - weight) * card_z
    fusion_metrics = candidate_metrics(labels[eval_mask], fused[eval_mask], teacher_scores[eval_mask])
    eval_tree_metrics = candidate_metrics(labels[eval_mask], tree_scores[eval_mask], teacher_scores[eval_mask])
    eval_card_metrics = candidate_metrics(labels[eval_mask], card_scores[eval_mask], teacher_scores[eval_mask])
    fusion_vs_tree = _bootstrap_difference(
        labels[eval_mask],
        fused[eval_mask],
        tree_scores[eval_mask],
        iterations=BOOTSTRAP_ITERATIONS,
        seed=RANDOM_SEED + 1,
    )
    fusion_vs_card = _bootstrap_difference(
        labels[eval_mask],
        fused[eval_mask],
        card_scores[eval_mask],
        iterations=BOOTSTRAP_ITERATIONS,
        seed=RANDOM_SEED + 2,
    )
    fusion_precision = float(fusion_metrics["precision_at_k"])
    fusion_accepted = (
        0 < weight < 1
        and fusion_vs_tree["ci_lower"] > 0
        and fusion_vs_card["ci_lower"] > 0
        and _relative_improvement(fusion_precision, float(eval_tree_metrics["precision_at_k"])) >= MIN_RELATIVE_UPLIFT
        and _relative_improvement(fusion_precision, float(eval_card_metrics["precision_at_k"])) >= MIN_RELATIVE_UPLIFT
    )

    selection["fusion"] = {
        "split": {
            "method": "sha256(user_id) modulo 2",
            "fit_rows": int(fit_mask.sum()),
            "evaluation_rows": int(eval_mask.sum()),
        },
        "normalization": {
            "tree_mean": tree_mean,
            "tree_std": tree_std,
            "scorecard_mean": card_mean,
            "scorecard_std": card_std,
        },
        "weight_grid": weight_results,
        "selected_tree_weight": weight,
        "evaluation_metrics": fusion_metrics,
        "evaluation_tree_metrics": eval_tree_metrics,
        "evaluation_scorecard_metrics": eval_card_metrics,
        "fusion_minus_tree": fusion_vs_tree,
        "fusion_minus_scorecard": fusion_vs_card,
        "accepted": fusion_accepted,
    }
    if fusion_accepted:
        selection["strategy"] = "decision_tree_scorecard_fusion"
        selection["reason"] = "no_direct_winner_and_fusion_improved_both"
        parameters = {
            "tree_weight": weight,
            "tree_mean": tree_mean,
            "tree_std": tree_std,
            "scorecard_mean": card_mean,
            "scorecard_std": card_std,
        }
        return selection, parameters

    def fallback_key(name: str, metrics: dict[str, Any], candidate: CandidateSQL) -> tuple[Any, ...]:
        return (
            float(metrics["precision_at_k"]),
            float(metrics["pr_auc"]),
            float(metrics["auc"]),
            float(metrics["teacher_spearman"]),
            -candidate.rule_count,
            -len(candidate.expression),
            # Stable final tie-break: scorecard sorts after decision_tree.
            name,
        )

    choices = [
        ("decision_tree", tree_metrics, tree),
        ("scorecard", card_metrics, scorecard),
    ]
    fallback = max(choices, key=lambda item: fallback_key(*item))[0]
    selection["strategy"] = fallback
    selection["reason"] = "fusion_not_accepted_deterministic_single_strategy_fallback"
    return selection, parameters


def render_final_sql(
    strategy: str,
    tree: CandidateSQL,
    scorecard: CandidateSQL,
    fusion_parameters: dict[str, float],
    feature_subquery: str,
) -> str:
    if strategy == "decision_tree":
        scoring_projection = tree.expression + " AS final_score"
        scoring_sql = "\n".join(
            [
                "  SELECT",
                f"    features.{_quote_identifier('user_id')} AS {_quote_identifier('user_id')},",
                "    " + scoring_projection.replace("\n", "\n    "),
                "  FROM (",
                feature_subquery,
                "  ) AS features",
            ]
        )
    elif strategy == "scorecard":
        scoring_projection = scorecard.expression + " AS final_score"
        scoring_sql = "\n".join(
            [
                "  SELECT",
                f"    features.{_quote_identifier('user_id')} AS {_quote_identifier('user_id')},",
                "    " + scoring_projection.replace("\n", "\n    "),
                "  FROM (",
                feature_subquery,
                "  ) AS features",
            ]
        )
    elif strategy == "decision_tree_scorecard_fusion":
        weight = fusion_parameters["tree_weight"]
        tree_mean = fusion_parameters["tree_mean"]
        tree_std = fusion_parameters["tree_std"]
        card_mean = fusion_parameters["scorecard_mean"]
        card_std = fusion_parameters["scorecard_std"]
        scoring_sql = "\n".join(
            [
                "  SELECT",
                f"    candidate_scores.{_quote_identifier('user_id')} AS {_quote_identifier('user_id')},",
                "    (",
                f"      {weight:.12g} * ((candidate_scores.tree_score - {tree_mean:.17g}) / {tree_std:.17g})",
                f"      + {1 - weight:.12g} * ((candidate_scores.scorecard_score - {card_mean:.17g}) / {card_std:.17g})",
                "    ) AS final_score",
                "  FROM (",
                "    SELECT",
                f"      features.{_quote_identifier('user_id')} AS {_quote_identifier('user_id')},",
                "      " + tree.expression.replace("\n", "\n      ") + " AS tree_score,",
                "      " + scorecard.expression.replace("\n", "\n      ") + " AS scorecard_score",
                "    FROM (",
                feature_subquery,
                "    ) AS features",
                "  ) AS candidate_scores",
            ]
        )
    else:
        raise ValueError(f"Unsupported final strategy: {strategy}")

    return "\n".join(
        [
            "SELECT",
            f"  scored.{_quote_identifier('user_id')} AS {_quote_identifier('user_id')},",
            "  scored.final_score AS final_score",
            "FROM (",
            scoring_sql,
            ") AS scored",
            "ORDER BY scored.final_score DESC",
            "",
        ]
    )


def _apply_trial_source_row_budget(trial_scoring_sql: str) -> tuple[str, dict[str, int]]:
    source_relation_count = trial_scoring_sql.count(TRIAL_SOURCE_LIMIT_PLACEHOLDER)
    if TRIAL_ROWS_PER_TABLE <= 0:
        raise ValueError("NL2SQL_TRIAL_ROWS_PER_TABLE must be positive")
    if TRIAL_MAX_ROWS_TO_READ <= 0:
        raise ValueError("NL2SQL_TRIAL_MAX_ROWS_TO_READ must be positive")
    if TRIAL_READ_SAFETY_FACTOR <= 0:
        raise ValueError("NL2SQL_TRIAL_READ_SAFETY_FACTOR must be positive")

    safe_total_rows = max(1, TRIAL_MAX_ROWS_TO_READ // TRIAL_READ_SAFETY_FACTOR)
    if source_relation_count > safe_total_rows:
        raise ValueError(
            "Source-trial relation count exceeds the safe ClickHouse row budget: "
            f"relations={source_relation_count}, budget={safe_total_rows}"
        )
    rows_per_relation = min(
        TRIAL_ROWS_PER_TABLE,
        max(1, safe_total_rows // max(1, source_relation_count)),
    )
    bounded_sql = trial_scoring_sql.replace(
        TRIAL_SOURCE_LIMIT_PLACEHOLDER,
        str(rows_per_relation),
    )
    return bounded_sql, {
        "source_relation_count": source_relation_count,
        "rows_per_relation": rows_per_relation,
        "nominal_source_rows": source_relation_count * rows_per_relation,
        "safe_total_rows": safe_total_rows,
    }


def render_source_trial_sql(trial_scoring_sql: str) -> str:
    """Wrap the scoring SQL into a resource-bounded ClickHouse trial query.

    Allocates a safe total row budget across every physical source relation,
    wraps the scoring SQL as ``__nl2sql_trial``, and applies ClickHouse limits.
    Reaching the read cap returns a partial smoke-test result instead of failing.
    """
    bounded_scoring_sql, budget = _apply_trial_source_row_budget(trial_scoring_sql)
    max_block_size = max(1, budget["rows_per_relation"])
    settings = (
        f"max_execution_time = {TRIAL_MAX_EXECUTION_TIME}, "
        f"max_threads = 2, "
        f"max_block_size = {max_block_size}, "
        f"max_rows_to_read = {TRIAL_MAX_ROWS_TO_READ}, "
        "read_overflow_mode = 'break', "
        f"max_bytes_to_read = {TRIAL_MAX_BYTES_TO_READ}, "
        f"max_memory_usage = {TRIAL_MAX_MEMORY_USAGE}"
    )
    return "\n".join(
        [
            "SELECT *",
            (
                "/* bounded source trial: "
                f"relations={budget['source_relation_count']}, "
                f"rows_per_relation={budget['rows_per_relation']}, "
                f"nominal_rows={budget['nominal_source_rows']}, "
                f"safe_budget={budget['safe_total_rows']} */"
            ),
            "FROM (",
            bounded_scoring_sql.strip().rstrip(";"),
            ") AS __nl2sql_trial",
            f"LIMIT {TRIAL_OUTPUT_ROWS}",
            f"SETTINGS {settings}",
            "",
        ]
    )


def _strip_sql_comments_and_literals(sql: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    without_line = re.sub(r"--[^\r\n]*", " ", without_block)
    without_strings = re.sub(r"'(?:''|\\.|[^'])*'", "''", without_line)
    return without_strings


def validate_final_sql(
    sql: str,
    strategy: str,
    contract: RuntimeContract,
    feature_report: dict[str, Any],
    tree: CandidateSQL,
    scorecard: CandidateSQL,
) -> dict[str, Any]:
    code = _strip_sql_comments_and_literals(sql)
    forbidden: list[str] = []
    for pattern, label in FORBIDDEN_SQL_PATTERNS:
        if pattern.search(code):
            forbidden.append(label)
    unresolved = sorted(set(re.findall(r"<[A-Za-z0-9_]+>", code)))
    database_references = re.findall(r"`([^`]+)`\.`([^`]+)`", code)
    unknown_tables = sorted(
        {
            f"{database}.{table}"
            for database, table in database_references
            if database != contract.source_database or table not in contract.table_columns
        }
    )
    contains_sampling_database = bool(
        contract.sampling_database and re.search(rf"\b{re.escape(contract.sampling_database)}\b", code)
    )
    contains_label = bool(re.search(r"(?<![A-Za-z0-9_])`?label`?(?![A-Za-z0-9_])", code, re.I))
    semicolons = code.count(";")
    single_query = code.lstrip().upper().startswith("SELECT") and semicolons <= 1
    unknown_columns: list[str] = []
    for item in feature_report.get("features", []):
        if not isinstance(item, dict):
            continue
        source_tables = [str(value) for value in item.get("source_tables", []) if value]
        for column in item.get("source_columns", []):
            if not any(column in contract.table_columns.get(table, {}) for table in source_tables):
                unknown_columns.append(str(column))
    report = {
        "artifact": "sql/step4_1_final.sql",
        "strategy": strategy,
        "single_query": single_query,
        "source_database_only": not unknown_tables and bool(database_references),
        "contains_sampling_database": contains_sampling_database,
        "contains_label": contains_label,
        "unresolved_placeholders": unresolved,
        "unknown_tables": unknown_tables,
        "unknown_columns": sorted(set(unknown_columns)),
        "forbidden_constructs": forbidden,
        "feature_coverage": feature_report["feature_coverage"],
        "tree_rule_parse_coverage": tree.parse_coverage,
        "scorecard_rule_parse_coverage": scorecard.parse_coverage,
        "full_database_execution_performed": False,
        "full_database_execution_expected": False,
    }
    report["passed"] = all(
        (
            single_query,
            report["source_database_only"],
            not contains_sampling_database,
            not contains_label,
            not unresolved,
            not unknown_tables,
            not unknown_columns,
            not forbidden,
            feature_report["feature_coverage"] == 1.0,
        )
    )
    if not report["passed"]:
        raise SystemExit(
            "Generated final SQL failed static validation: " + json.dumps(_json_safe(report), ensure_ascii=False)
        )
    return report


_RESOURCE_LIMIT_PATTERNS = (
    "max_rows_to_read",
    "max_bytes_to_read",
    "max_execution_time",
    "max_memory_usage",
    "memory limit",
    "timeout exceeded",
    "too many rows",
    "too many bytes",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_source_validation_request(trial_sql: str) -> dict[str, Any]:
    if not re.match(r"^\s*SELECT\b", trial_sql, flags=re.IGNORECASE):
        raise SystemExit(
            "source_trial SQL must start with SELECT to satisfy the ClickHouse MCP whitelist"
        )
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    trial_path = RUNTIME_DIR / "source_trial.sql"
    # Remove the obsolete EXPLAIN artifact when rerunning in a workspace prepared
    # by the previous two-query validation contract.
    (RUNTIME_DIR / "source_explain.sql").unlink(missing_ok=True)
    trial_path.write_text(trial_sql, encoding="utf-8")
    request = {
        "version": 2,
        "resource_id": "clickhouse",
        "task_type": "sql_query",
        "queries": {
            "source_trial": {
                "path": str(trial_path.relative_to(OUTPUT_DIR)),
                "sha256": _sha256_text(trial_sql),
                "source_limited": True,
                "executes_final_query": False,
            },
        },
        "result_path": str(SOURCE_VALIDATION_RESULT_PATH.relative_to(OUTPUT_DIR)),
    }
    _write_json(RUNTIME_DIR / "source_validation_request.json", request)
    return request


def _result_text(payload: dict[str, Any]) -> str:
    parts = [
        str(payload.get("status") or ""),
        str(payload.get("summary") or ""),
        str(payload.get("error") or ""),
        str(payload.get("message") or ""),
    ]
    return "\n".join(part for part in parts if part).strip()


def _source_validation_item(
    name: str,
    raw: Any,
    expected_sha256: str,
) -> dict[str, Any]:
    base = {
        "performed": False,
        "passed": False,
        "query_sha256": expected_sha256,
        "job_id": None,
        "status": "missing",
        "returned_rows": None,
        "resource_limit_reached": False,
        "error": None,
    }
    if not isinstance(raw, dict):
        return base
    supplied_sha256 = str(raw.get("query_sha256") or raw.get("sha256") or "").strip()
    if supplied_sha256 != expected_sha256:
        base["status"] = "stale_or_mismatched_query"
        base["error"] = f"{name} query_sha256 does not match current generated SQL"
        return base
    payload = raw.get("result", raw.get("collect", raw.get("payload")))
    if not isinstance(payload, dict):
        base["status"] = "invalid_result_payload"
        base["error"] = f"{name} must contain the raw collect_job payload"
        return base
    status = str(payload.get("status") or "").strip().lower()
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    exit_code = metrics.get("exit_code")
    error_text = _result_text(payload)
    outputs = payload.get("outputs") if isinstance(payload.get("outputs"), list) else []
    returned_rows = sum(
        int(item.get("row_count") or 0)
        for item in outputs
        if isinstance(item, dict) and item.get("kind") == "clickhouse_result"
    )
    completed = status in {"completed", "success", "succeeded"}
    passed = completed and exit_code in (None, 0) and not str(payload.get("error") or "").strip()
    lowered_error = error_text.lower()
    return {
        "performed": status not in {"", "queued", "pending", "running", "submitted"},
        "passed": passed,
        "query_sha256": expected_sha256,
        "job_id": payload.get("job_id"),
        "status": status or "unknown",
        "returned_rows": returned_rows if completed else None,
        "resource_limit_reached": any(
            pattern in lowered_error for pattern in _RESOURCE_LIMIT_PATTERNS
        ),
        "error": None if passed else error_text or "ClickHouse validation did not complete",
    }


def evaluate_source_validation(
    request: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Evaluate the source-validation result file against expected query digests.

    When the result file is absent, returns a pending report (not passed).
    Otherwise parses it and reports whether the source-trial query matched its
    expected ``sha256`` and completed successfully.
    """
    queries = request["queries"]
    if not SOURCE_VALIDATION_RESULT_PATH.is_file():
        pending = {
            "source_trial_validation": _source_validation_item(
                "source_trial",
                None,
                queries["source_trial"]["sha256"],
            ),
        }
        return pending, False
    try:
        raw = json.loads(SOURCE_VALIDATION_RESULT_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read source validation result: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("source_validation_result.json must contain one JSON object")
    report = {
        "source_trial_validation": _source_validation_item(
            "source_trial",
            raw.get("source_trial"),
            queries["source_trial"]["sha256"],
        ),
    }
    passed = all(item["passed"] for item in report.values())
    return report, passed


def main() -> None:
    if not 0 < PRIMARY_K <= 1:
        raise SystemExit("NL2SQL_PRIMARY_K must be in (0, 1]")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = OUTPUT_DIR / "receipt.json"
    receipt_path.unlink(missing_ok=True)
    _require_inputs()

    _ensure_executed_generator_artifact()
    contract = load_runtime_contract()
    generator_provenance = _generator_provenance()
    high_cardinality_path = OUTPUT_DIR / "step2_3_high_cardinality_check.json"
    high_cardinality_check = (
        _read_json(
            "step2_3_high_cardinality_check.json",
            allow_safe_repairs=True,
        )
        if high_cardinality_path.is_file()
        else {"status": "not_transferred_when_deployment_contract_is_available"}
    )
    upstream_reports = {
        "lightgbm_teacher": _read_json("step3_4_model_report.json"),
        "decision_tree": _read_json("step3_5_model_report.json"),
        "scorecard": _read_json("step3_6_model_report.json"),
    }
    tree_preprocessing = _read_json("step3_5_preprocessing_reconstructed.json")
    deployment_contract: dict[str, Any] | None = None
    if _has_usable_deployment_feature_contract():
        deployment_contract = load_deployment_feature_contract(contract)
        lineage = None
    else:
        if (OUTPUT_DIR / DEPLOYMENT_FEATURE_CONTRACT).is_file():
            INPUT_NORMALIZATION_WARNINGS.append(
                "step2_3_deployment_feature_contract.json: structural validation "
                "did not pass; used legacy lineage fallback"
            )
        lineage = normalize_feature_derivation(contract, high_cardinality_check)

    def render_features(
        required_features: set[str],
        tree_features: set[str],
        *,
        trial_mode: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        if deployment_contract is not None:
            return render_feature_subquery_from_deployment_contract(
                required_features,
                tree_features,
                deployment_contract,
                contract,
                tree_preprocessing,
                trial_mode=trial_mode,
            )
        assert lineage is not None
        return render_feature_subquery(
            required_features,
            tree_features,
            lineage,
            contract,
            tree_preprocessing,
            trial_mode=trial_mode,
        )
    tree = build_tree_candidate(OUTPUT_DIR / "step3_5_rule_card.csv")
    scorecard = build_scorecard_candidate(OUTPUT_DIR / "step3_6_score_rule.csv")
    apply_deployment_contract(tree, scorecard, tree_preprocessing)
    aligned, alignment_report = load_aligned_scores()

    candidate_renderings: dict[
        str,
        tuple[tuple[str, dict[str, Any]], tuple[str, dict[str, Any]]],
    ] = {}
    for candidate in (tree, scorecard):
        if not candidate.renderable:
            continue
        candidate_tree_features = (
            set(candidate.features) if candidate.name == "decision_tree" else set()
        )
        try:
            final_rendering = render_features(
                set(candidate.features),
                candidate_tree_features,
            )
            trial_rendering = render_features(
                set(candidate.features),
                candidate_tree_features,
                trial_mode=True,
            )
        except ValueError as exc:
            candidate.renderable = False
            candidate.deployment_errors = [
                *(candidate.deployment_errors or []),
                str(exc),
            ]
            continue
        candidate_renderings[candidate.name] = (final_rendering, trial_rendering)

    selection, fusion_parameters = choose_strategy(aligned, tree, scorecard)
    strategy = str(selection["strategy"])
    if strategy in candidate_renderings:
        (feature_subquery, feature_report), (
            trial_feature_subquery,
            _trial_feature_report,
        ) = candidate_renderings[strategy]
    else:
        required_features = set(tree.features) | set(scorecard.features)
        try:
            feature_subquery, feature_report = render_features(
                required_features,
                set(tree.features),
            )
            trial_feature_subquery, _trial_feature_report = render_features(
                required_features,
                set(tree.features),
                trial_mode=True,
            )
        except ValueError as exc:
            alternatives = sorted(
                (candidate for candidate in (tree, scorecard) if candidate.name in candidate_renderings),
                key=lambda item: (
                    selection["candidates"][item.name]["precision_at_k"],
                    selection["candidates"][item.name]["pr_auc"],
                    selection["candidates"][item.name]["auc"],
                    -item.rule_count,
                    item.name,
                ),
                reverse=True,
            )
            if not alternatives:
                raise SystemExit(
                    "Cannot render a deployable white-box SQL after lineage normalization: "
                    + str(exc)
                ) from exc
            fallback = alternatives[0]
            strategy = fallback.name
            selection["initial_strategy"] = "decision_tree_scorecard_fusion"
            selection["strategy"] = strategy
            selection["reason"] = "fusion_not_renderable_used_best_renderable_single_strategy"
            selection["render_warning"] = str(exc)
            fusion_parameters = {}
            (feature_subquery, feature_report), (
                trial_feature_subquery,
                _trial_feature_report,
            ) = candidate_renderings[strategy]

    sql = render_final_sql(strategy, tree, scorecard, fusion_parameters, feature_subquery)
    trial_scoring_sql = render_final_sql(
        strategy,
        tree,
        scorecard,
        fusion_parameters,
        trial_feature_subquery,
    )
    sql_path = SQL_DIR / "step4_1_final.sql"
    sql_path.write_text(sql, encoding="utf-8")

    selection["validation_alignment"] = alignment_report
    selection["upstream_reports"] = upstream_reports
    selection["teacher_model_deployment_candidate"] = False
    selection["final_strategy"] = strategy
    selection["full_database_execution_performed"] = False
    selection["generator_provenance"] = generator_provenance
    selection["tree_preprocessing"] = {
        "artifact": "step3_5_preprocessing_reconstructed.json",
        "validation": tree_preprocessing.get("validation"),
        "script_provenance": tree_preprocessing.get("script_provenance"),
    }
    selection["input_normalization_warnings"] = list(INPUT_NORMALIZATION_WARNINGS)
    _write_json(OUTPUT_DIR / "step4_1_strategy_selection.json", selection)
    feature_report["high_cardinality_check"] = high_cardinality_check
    feature_report["input_normalization_warnings"] = list(INPUT_NORMALIZATION_WARNINGS)
    _write_json(OUTPUT_DIR / "step4_1_feature_lineage_report.json", feature_report)

    static_validation = validate_final_sql(
        sql,
        strategy,
        contract,
        feature_report,
        tree,
        scorecard,
    )
    source_trial_sql = render_source_trial_sql(trial_scoring_sql)
    source_request = _write_source_validation_request(source_trial_sql)
    source_validation, source_validation_passed = evaluate_source_validation(
        source_request
    )
    validation_report = {
        "artifact": "sql/step4_1_final.sql",
        "strategy": strategy,
        "passed": bool(static_validation["passed"] and source_validation_passed),
        "static_validation": static_validation,
        **source_validation,
        "full_database_execution_performed": False,
        "full_database_execution_expected": False,
        "source_database_limited_trial_performed": source_validation[
            "source_trial_validation"
        ]["performed"],
        "generator_provenance": generator_provenance,
        "tree_preprocessing_validation": tree_preprocessing.get("validation"),
        "input_normalization_warnings": list(INPUT_NORMALIZATION_WARNINGS),
    }
    _write_json(
        OUTPUT_DIR / "step4_1_sql_validation_report.json",
        validation_report,
    )

    if not SOURCE_VALIDATION_RESULT_PATH.is_file():
        print(f"Generated: {sql_path}")
        print("Static validation passed. Source validation is pending.")
        print(f"Validation request: {RUNTIME_DIR / 'source_validation_request.json'}")
        return
    if not source_validation_passed:
        failures = [
            f"{name}: {details.get('error') or details.get('status')}"
            for name, details in source_validation.items()
            if not details.get("passed")
        ]
        raise SystemExit("Source database validation failed: " + " | ".join(failures))

    receipt = {
        "summary": (
            f"NL2SQL completed with {strategy}; generated one final SQL targeting "
            f"{contract.source_database}; source-limited SELECT trial passed without "
            f"executing the full query. Generator working copy modified: "
            f"{str(generator_provenance['working_copy_modified']).lower()}."
        ),
        "artifacts": [
            {
                "kind": "file",
                "path": "step2_3_feature_derivation.json",
                "type": "json",
            },
            {
                "kind": "file",
                "path": "sql/step4_1_final.sql",
                "type": "sql",
            },
            {
                "kind": "file",
                "path": "step4_1_strategy_selection.json",
                "type": "json",
            },
            {
                "kind": "file",
                "path": "step4_1_feature_lineage_report.json",
                "type": "json",
            },
            {
                "kind": "file",
                "path": "step4_1_sql_validation_report.json",
                "type": "json",
            },
            {
                "kind": "file",
                "path": "step3_5_preprocessing_reconstructed.json",
                "type": "json",
            },
            {
                "kind": "file",
                "path": "scripts/step4_0_reconstruct_tree_preprocessing.py",
                "type": "python",
            },
            {
                "kind": "file",
                "path": "scripts/step4_1_generate_sql.py",
                "type": "python",
            },
        ],
    }
    _write_json(receipt_path, receipt)
    print(f"Generated: {sql_path}")
    print(f"Final strategy: {strategy}")
    print("Source limited trial passed: true")
    print("Full database execution performed: false")


if __name__ == "__main__":
    main()
