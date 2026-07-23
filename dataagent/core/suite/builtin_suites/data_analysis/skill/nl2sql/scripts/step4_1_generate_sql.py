"""Generate one deployable white-box audience-selection SQL.

The step is deliberately local-only:

* LightGBM is a teacher/reference model, never a deployment candidate.
* The only deployment candidates are the distilled decision tree, scorecard,
  or a deterministic blend of those two.
* Feature lineage is normalized from the existing step2 Markdown artifact into
  ``step2_3_feature_derivation.json`` inside the current NL2SQL workspace.
* The emitted SQL targets the full ``source_database`` but is not executed.
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

REQUIRED_INPUTS = (
    "step1_0_table_schema.json",
    "step1_output_meta.json",
    "schema_resolution.json",
    "step2_3_feature_derivation.md",
    "step2_3_high_cardinality_check.json",
    "step3_4_valid_predictions.csv",
    "step3_4_model_report.json",
    "step3_5_rule_card.csv",
    "step3_5_white_box_scores.csv",
    "step3_5_model_report.json",
    "step3_6_score_rule.csv",
    "step3_6_white_box_scores.csv",
    "step3_6_model_report.json",
)

FORBIDDEN_SQL_PATTERNS = (
    (re.compile(r"\bWITH\b", re.I), "CTE/WITH"),
    (re.compile(r"MODE\s*\(\s*\)\s*WITHIN\s+GROUP", re.I), "MODE() WITHIN GROUP"),
    (re.compile(r"\bTRY_TO_NUMERIC\b", re.I), "TRY_TO_NUMERIC"),
    (re.compile(r"\bINTERVAL\b", re.I), "INTERVAL"),
    (re.compile(r"\bLIMIT\b", re.I), "LIMIT"),
)

SQL_FUNCTIONS = {
    "abs",
    "avg",
    "case",
    "cast",
    "coalesce",
    "count",
    "countdistinct",
    "countif",
    "date",
    "datetime",
    "else",
    "end",
    "float32",
    "float64",
    "if",
    "ifnull",
    "int16",
    "int32",
    "int64",
    "int8",
    "isnull",
    "length",
    "max",
    "min",
    "multiif",
    "nullif",
    "replace",
    "round",
    "sum",
    "then",
    "tostring",
    "uint16",
    "uint32",
    "uint64",
    "uint8",
    "uniqexact",
    "when",
}


@dataclass(frozen=True)
class RuntimeContract:
    source_database: str
    sampling_database: str
    user_table: str
    user_id: str
    table_columns: dict[str, dict[str, str]]
    validated_keys: dict[str, list[str]]
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


def _require_inputs() -> None:
    missing = [name for name in REQUIRED_INPUTS if not (OUTPUT_DIR / name).is_file()]
    if missing:
        raise SystemExit("Missing NL2SQL input artifacts: " + ", ".join(missing))


def _read_json(name: str) -> dict[str, Any]:
    path = OUTPUT_DIR / name
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {name}: {exc}") from exc
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
        "状态": "status",
        "处理方式": "method",
        "来源表": "source_table",
        "来源字段": "source_feature",
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
    for table in table_schema.get("tables", []):
        if not isinstance(table, dict) or not table.get("name"):
            continue
        columns: dict[str, str] = {}
        for column in table.get("columns", []):
            if not isinstance(column, dict) or not column.get("name"):
                continue
            columns[str(column["name"])] = str(column.get("valueType", column.get("type", "Unknown")))
        result[str(table["name"])] = columns
    if not result:
        raise SystemExit("step1_0_table_schema.json contains no tables")
    return result


def load_runtime_contract() -> RuntimeContract:
    table_schema = _read_json("step1_0_table_schema.json")
    output_meta = _read_json("step1_output_meta.json")
    schema_resolution = _read_json("schema_resolution.json")

    source_database = str(output_meta.get("source_database") or table_schema.get("source_database") or "").strip()
    if not source_database:
        raise SystemExit("source_database is absent from step1 artifacts")
    schema_source = str(table_schema.get("source_database") or "").strip()
    if schema_source and schema_source != source_database:
        raise SystemExit("source_database mismatch between step1_output_meta.json and step1_0_table_schema.json")

    sampling_database = str(
        output_meta.get("output_database") or schema_resolution.get("output_database") or ""
    ).strip()
    roles = schema_resolution.get("roles", {})
    if not isinstance(roles, dict):
        raise SystemExit("schema_resolution.roles must be an object")
    user_table = str(roles.get("<user_table>") or roles.get("user_table") or "").strip()
    user_id = str(roles.get("<user_id>") or roles.get("user_id") or "").strip()
    if not user_table or not user_id:
        raise SystemExit("schema_resolution must resolve <user_table> and <user_id>")

    table_columns = _table_schema_map(table_schema)
    if user_table not in table_columns:
        raise SystemExit(f"Resolved user table does not exist in source schema: {user_table}")
    if user_id not in table_columns[user_table]:
        raise SystemExit(f"Resolved user id {user_id} does not exist in source table {user_table}")

    validated_keys: dict[str, list[str]] = {}
    key_validation = schema_resolution.get("key_validation", {})
    if isinstance(key_validation, dict):
        for item in key_validation.get("candidate_keys", []):
            if not isinstance(item, dict) or not item.get("validated"):
                continue
            table = str(item.get("table") or "")
            column = str(item.get("column") or "")
            if table and column:
                validated_keys.setdefault(table, []).append(column)

    classification = schema_resolution.get("table_classification", {})
    one_to_one: set[str] = {user_table}
    if isinstance(classification, dict):
        values = classification.get("1:1_tables", classification.get("one_to_one_tables", []))
        if isinstance(values, list):
            one_to_one.update(str(item) for item in values)

    return RuntimeContract(
        source_database=source_database,
        sampling_database=sampling_database,
        user_table=user_table,
        user_id=user_id,
        table_columns=table_columns,
        validated_keys=validated_keys,
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


def _sql_source_columns(expression: str) -> list[str]:
    if not expression:
        return []
    identifiers = re.findall(r"(?<!['\"])\b[A-Za-z_][A-Za-z0-9_]*\b", expression)
    result: list[str] = []
    for identifier in identifiers:
        lowered = identifier.lower()
        if lowered in SQL_FUNCTIONS or lowered in {"and", "or", "not", "null", "as"}:
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


def normalize_feature_derivation(contract: RuntimeContract) -> dict[str, Any]:
    markdown_path = OUTPUT_DIR / "step2_3_feature_derivation.md"
    markdown = markdown_path.read_text(encoding="utf-8-sig")
    lines = markdown.splitlines()
    known_tables = list(contract.table_columns)
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
                    expression = _strip_markup(row.get("sql_expression", ""))
                    method = _strip_markup(row.get("method", row.get("handling", "")))
                    if source_table and not source_feature and feature in contract.table_columns[source_table]:
                        source_feature = feature
                    source_columns = _sql_source_columns(expression)
                    if source_feature and source_feature not in source_columns:
                        source_columns.append(source_feature)
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
                },
            )
        index += 1

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
                },
            )

    normalized = {
        "version": 1,
        "source": "step2_3_feature_derivation.md",
        "entity": {
            "base_table": contract.user_table,
            "entity_key": contract.user_id,
            "grain": "user",
        },
        "features": [features[name] for name in sorted(features)],
    }
    _write_json(OUTPUT_DIR / "step2_3_feature_derivation.json", normalized)
    return normalized


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


def _is_numeric_literal(value: str) -> bool:
    raw = _strip_markup(value).strip().strip("'\"")
    try:
        return math.isfinite(float(raw))
    except ValueError:
        return False


def _split_and(condition: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"\s+AND\s+", str(condition).strip(), flags=re.I) if piece.strip()]


def parse_tree_atom(atom: str, alias: str = "features") -> tuple[str, str]:
    match = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(<=|>=|!=|=|<|>)\s*(.*?)\s*",
        atom,
    )
    if not match:
        raise ValueError(f"Unsupported decision-tree condition: {atom}")
    raw_feature, operator, raw_value = match.groups()
    feature = raw_feature.split(".")[-1]
    reference = f"{alias}.{_quote_identifier(feature)}"
    value = _strip_markup(raw_value)
    if value.strip("'\"") in {"__MISSING__", ""}:
        return feature, f"{reference} IS NULL"
    comparable = f"toFloat64OrZero(toString({reference}))" if _is_numeric_literal(value) else reference
    return feature, f"{comparable} {operator} {_sql_literal(value)}"


def parse_tree_condition(condition: str, alias: str = "features") -> tuple[set[str], str]:
    features: set[str] = set()
    sql_parts: list[str] = []
    for atom in _split_and(condition):
        feature, sql = parse_tree_atom(atom, alias)
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


def _find_join_key(table: str, contract: RuntimeContract) -> str | None:
    columns = contract.table_columns.get(table, {})
    candidates = contract.validated_keys.get(table, [])
    for candidate in (contract.user_id, *candidates, "usid", "rank_flg", "dsid"):
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
    if source_tables != [contract.user_table]:
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


def render_feature_subquery(
    required_features: set[str],
    lineage: dict[str, Any],
    contract: RuntimeContract,
) -> tuple[str, dict[str, Any]]:
    index = _lineage_index(lineage)
    errors: list[str] = []
    direct_selects: list[str] = []
    grouped: dict[tuple[tuple[str, ...], str], list[tuple[str, dict[str, Any]]]] = {}

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
            reference = f"u.{_quote_identifier(source_feature)}"
            direct_selects.append(f"      {_null_wrapped(reference, entry)} AS {_quote_identifier(feature)}")
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
            source_sql = _qualified_table(contract.source_database, source_tables[0])
        else:
            union_parts = [
                f"SELECT * FROM {_qualified_table(contract.source_database, table)}" for table in source_tables
            ]
            source_sql = "(\n          " + "\n          UNION ALL\n          ".join(union_parts) + "\n        )"
        aggregate_block = ",\n".join(aggregate_lines)
        joins.append(
            "\n".join(
                [
                    "    LEFT JOIN (",
                    "      SELECT",
                    f"        {_quote_identifier(join_key)} AS {_quote_identifier('__join_key')},",
                    aggregate_block,
                    f"      FROM {source_sql}",
                    f"      GROUP BY {_quote_identifier(join_key)}",
                    f"    ) AS {alias}",
                    "      ON toString(u."
                    + _quote_identifier(contract.user_id)
                    + f") = toString({alias}.{_quote_identifier('__join_key')})",
                ]
            )
        )

    if errors:
        raise ValueError("; ".join(sorted(set(errors))))

    select_lines = [
        f"      u.{_quote_identifier(contract.user_id)} AS {_quote_identifier('user_id')}",
        *direct_selects,
        *aggregate_selects,
    ]
    feature_sql = "\n".join(
        [
            "    SELECT",
            ",\n".join(select_lines),
            f"    FROM {_qualified_table(contract.source_database, contract.user_table)} AS u",
            *joins,
        ]
    )
    for feature in sorted(required_features):
        if any(row["feature"] == feature for row in lineage_rows):
            continue
        entry = index[feature]
        lineage_rows.append(
            {
                "feature": feature,
                "source_tables": entry.get("source_tables") or [entry.get("source_table")],
                "source_columns": entry.get("source_columns", []),
                "join_key": contract.user_id,
                "sql_expression": entry.get("source_feature") or feature,
                "null_strategy": entry.get("null_strategy"),
            }
        )
    report = {
        "required_feature_count": len(required_features),
        "resolved_feature_count": len(required_features),
        "feature_coverage": 1.0,
        "features": sorted(lineage_rows, key=lambda item: item["feature"]),
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
    tree_metrics["risk_flags"] = _risk_flags("decision_tree", tree_metrics, tree.rule_count)
    card_metrics["rule_count"] = scorecard.rule_count
    card_metrics["rule_parse_coverage"] = scorecard.parse_coverage
    card_metrics["rule_parse_errors"] = scorecard.render_errors or []
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
                "reason": "only_one_rule_artifact_fully_parseable",
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
    report = {
        "artifact": "sql/step4_1_final.sql",
        "strategy": strategy,
        "single_query": single_query,
        "source_database_only": not unknown_tables and bool(database_references),
        "contains_sampling_database": contains_sampling_database,
        "contains_label": contains_label,
        "unresolved_placeholders": unresolved,
        "unknown_tables": unknown_tables,
        "unknown_columns": [],
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
            not forbidden,
            feature_report["feature_coverage"] == 1.0,
        )
    )
    if not report["passed"]:
        raise SystemExit(
            "Generated final SQL failed static validation: " + json.dumps(_json_safe(report), ensure_ascii=False)
        )
    return report


def main() -> None:
    if not 0 < PRIMARY_K <= 1:
        raise SystemExit("NL2SQL_PRIMARY_K must be in (0, 1]")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    _require_inputs()

    contract = load_runtime_contract()
    high_cardinality_check = _read_json("step2_3_high_cardinality_check.json")
    upstream_reports = {
        "lightgbm_teacher": _read_json("step3_4_model_report.json"),
        "decision_tree": _read_json("step3_5_model_report.json"),
        "scorecard": _read_json("step3_6_model_report.json"),
    }
    lineage = normalize_feature_derivation(contract)
    tree = build_tree_candidate(OUTPUT_DIR / "step3_5_rule_card.csv")
    scorecard = build_scorecard_candidate(OUTPUT_DIR / "step3_6_score_rule.csv")
    aligned, alignment_report = load_aligned_scores()

    selection, fusion_parameters = choose_strategy(aligned, tree, scorecard)
    strategy = str(selection["strategy"])
    if strategy == "decision_tree":
        required_features = set(tree.features)
    elif strategy == "scorecard":
        required_features = set(scorecard.features)
    else:
        required_features = set(tree.features) | set(scorecard.features)

    try:
        feature_subquery, feature_report = render_feature_subquery(required_features, lineage, contract)
    except ValueError as exc:
        # If a selected single candidate is not technically renderable, use the
        # other candidate before failing the fixed input contract.
        alternative = "scorecard" if strategy == "decision_tree" else "decision_tree"
        if strategy != "decision_tree_scorecard_fusion":
            alternative_features = scorecard.features if alternative == "scorecard" else tree.features
            try:
                feature_subquery, feature_report = render_feature_subquery(set(alternative_features), lineage, contract)
            except ValueError:
                raise SystemExit(f"Cannot render selected white-box strategy: {exc}") from exc
            selection["initial_strategy"] = strategy
            selection["strategy"] = alternative
            selection["reason"] = "selected_strategy_not_renderable_used_other_strategy"
            selection["render_warning"] = str(exc)
            strategy = alternative
            fusion_parameters = {}
        else:
            # Try the deterministic single-strategy ordering used after a
            # rejected blend.
            alternatives = sorted(
                (tree, scorecard),
                key=lambda item: (
                    selection["candidates"][item.name]["precision_at_k"],
                    selection["candidates"][item.name]["pr_auc"],
                    selection["candidates"][item.name]["auc"],
                    -item.rule_count,
                    item.name,
                ),
                reverse=True,
            )
            rendered = None
            errors = [str(exc)]
            for candidate in alternatives:
                try:
                    rendered = render_feature_subquery(set(candidate.features), lineage, contract)
                except ValueError as candidate_exc:
                    errors.append(str(candidate_exc))
                    continue
                strategy = candidate.name
                feature_subquery, feature_report = rendered
                selection["initial_strategy"] = "decision_tree_scorecard_fusion"
                selection["strategy"] = strategy
                selection["reason"] = "fusion_not_renderable_used_best_renderable_single_strategy"
                selection["render_warning"] = str(exc)
                fusion_parameters = {}
                break
            if rendered is None:
                raise SystemExit("Cannot render either white-box strategy: " + " | ".join(errors)) from exc

    sql = render_final_sql(strategy, tree, scorecard, fusion_parameters, feature_subquery)
    sql_path = SQL_DIR / "step4_1_final.sql"
    sql_path.write_text(sql, encoding="utf-8")

    selection["validation_alignment"] = alignment_report
    selection["upstream_reports"] = upstream_reports
    selection["teacher_model_deployment_candidate"] = False
    selection["final_strategy"] = strategy
    selection["full_database_execution_performed"] = False
    _write_json(OUTPUT_DIR / "step4_1_strategy_selection.json", selection)
    feature_report["high_cardinality_check"] = high_cardinality_check
    _write_json(OUTPUT_DIR / "step4_1_feature_lineage_report.json", feature_report)

    validation_report = validate_final_sql(sql, strategy, contract, feature_report, tree, scorecard)
    _write_json(OUTPUT_DIR / "step4_1_sql_validation_report.json", validation_report)

    receipt = {
        "summary": (
            f"NL2SQL completed with {strategy}; generated one final SQL targeting "
            f"{contract.source_database} without executing it."
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
        ],
    }
    _write_json(OUTPUT_DIR / "receipt.json", receipt)
    print(f"Generated: {sql_path}")
    print(f"Final strategy: {strategy}")
    print("Full database execution performed: false")


if __name__ == "__main__":
    main()
