"""Delivery contract writer and deterministic checker for de_agent.

The contract is the machine-readable part of the calibration freeze table.  It
is written by the main Agent after freezing the calibration, then enforced by
``check_and_repair_sql``.  Only decidable checks live here; the LLM is never
used by this module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from dataagent.actions.tools.context import ToolExecutionContext

CONTRACT_FILE = "contract.json"
_FILTER_OPERATORS = {"not_empty", "empty"}


def _workspace(_tool_context: ToolExecutionContext) -> Path:
    runtime = _tool_context.runtime
    if runtime is None or runtime.workspace_dir is None:
        raise RuntimeError("A session workspace is required")
    return Path(runtime.workspace_dir)


def validate_contract(value: Any) -> dict[str, Any]:
    """Validate the JSON shape; never invent or repair business values."""
    if not isinstance(value, dict):
        raise ValueError("contract must be a JSON object")
    allowed = {"output_columns", "required_filters", "joins", "status", "notes"}
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"unsupported contract keys: {sorted(extra)}")
    columns = value.get("output_columns", [])
    if not isinstance(columns, list) or not all(isinstance(item, str) and item.strip() for item in columns):
        raise ValueError("output_columns must be a list of non-empty strings")
    if len(columns) > 200:
        raise ValueError("output_columns is too large")
    filters = value.get("required_filters", [])
    if not isinstance(filters, list):
        raise ValueError("required_filters must be a list")
    for item in filters:
        if not isinstance(item, dict) or set(item) - {"field", "operator", "scope"}:
            raise ValueError("each required_filter must be {field, operator, scope?}")
        if not isinstance(item.get("field"), str) or not item.get("field", "").strip():
            raise ValueError("required_filter.field is required")
        if item.get("operator") not in _FILTER_OPERATORS:
            raise ValueError(f"required_filter.operator must be one of {sorted(_FILTER_OPERATORS)}")
    joins = value.get("joins", [])
    if not isinstance(joins, list):
        raise ValueError("joins must be a list")
    for item in joins:
        if not isinstance(item, dict) or set(item) - {"right", "type"}:
            raise ValueError("each join must be {right, type}")
        if not isinstance(item.get("right"), str) or not item.get("right", "").strip():
            raise ValueError("join.right is required")
        if str(item.get("type", "")).upper() not in {"LEFT", "INNER", "RIGHT", "FULL", "CROSS"}:
            raise ValueError("join.type is invalid")
    return value


def _metadata_join_types(workspace: Path) -> dict[str, str]:
    """Read metadata join relations from schema_schemair.md, if present."""
    path = workspace / "schema_schemair.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    marker = "join_relations"
    index = text.find(marker)
    if index < 0:
        return {}
    tail = text[index + len(marker) :].lstrip()
    if tail.startswith("="):
        tail = tail[1:].lstrip()
    try:
        relations = json.loads(tail)
    except (ValueError, TypeError):
        return {}
    result: dict[str, str] = {}
    for item in relations if isinstance(relations, list) else []:
        if not isinstance(item, dict):
            continue
        rel_type = str(item.get("rel_type") or "").upper()
        for key in ("src", "target"):
            value = str(item.get(key) or "")
            table = value.rsplit(".", 1)[0] if "." in value else value
            if table:
                result[table.lower()] = rel_type
                result[table.rsplit(".", 1)[-1].lower()] = rel_type
    return result


def validate_contract_joins(contract: dict[str, Any], workspace: Path) -> str | None:
    """Reject a contract join type that contradicts metadata join_relations."""
    metadata = _metadata_join_types(workspace)
    if not metadata:
        return None
    for item in contract.get("joins", []):
        right = str(item.get("right", "")).replace("`", "").lower()
        expected = str(item.get("type", "")).upper()
        actual = (metadata.get(right) or metadata.get(right.rsplit(".", 1)[-1]) or "").replace(" JOIN", "")
        if actual and expected and actual != expected:
            return f"contract join {right} 写的是 {expected}，但 metadata join_relations 是 {actual}。"
    return None


async def write_delivery_contract(contract: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Write the delivery contract JSON to the workspace.

    Args:
        contract: JSON object with output_columns, required_filters and joins.
    """
    workspace = _workspace(_tool_context)
    try:
        parsed = json.loads(contract)
        validated = validate_contract(parsed)
    except (ValueError, TypeError) as exc:
        message = f"交付契约校验失败：{exc}"
        return {"status": "ERROR", "original_msg": message, "frontend_msg": message}
    join_error = validate_contract_joins(validated, workspace)
    if join_error:
        return {"status": "ERROR", "original_msg": join_error, "frontend_msg": join_error}
    path = workspace / CONTRACT_FILE
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    summary = (
        f"交付契约已写入 {path}："
        f"{len(validated.get('output_columns', []))} 个输出列，"
        f"{len(validated.get('required_filters', []))} 个必填过滤，"
        f"{len(validated.get('joins', []))} 个 JOIN 约束。"
    )
    return {
        "status": "OK",
        "contract_path": str(path),
        "original_msg": summary,
        "frontend_msg": summary,
    }


def load_contract(workspace: Path) -> dict[str, Any] | None:
    """Read an explicit contract if present; report invalid JSON without inventing defaults."""
    path = workspace / CONTRACT_FILE
    if not path.is_file():
        return None
    try:
        return validate_contract(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError, TypeError):
        return {"invalid": True}


def _partition_columns(tree: exp.Expression) -> set[str]:
    partition = tree.find(exp.PartitionedByProperty)
    if partition is None:
        return set()
    return {item.name.lower() for item in partition.find_all(exp.ColumnDef)}


def _ddl_columns(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read="hive")
    partition = _partition_columns(tree)
    return [item.name for item in tree.find_all(exp.ColumnDef) if item.name.lower() not in partition]


def _select_columns(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read="spark")
    select = tree.find(exp.Select)
    return [] if select is None else [item.alias_or_name or "" for item in select.expressions]


def _filter_present(sql: str, field: str, operator: str) -> bool:
    if operator == "not_empty":
        return bool(
            re.search(rf"(?i)NOT\s+bicoredata\.IsEmpty\s*\(\s*{re.escape(field)}\s*\)", sql)
            or re.search(rf"(?i){re.escape(field)}\s+IS\s+NOT\s+NULL", sql)
            # An equality/range/IN predicate on the field already excludes NULL/empty.
            or re.search(rf"(?i)\b{re.escape(field)}\b\s*(?:=|<=|>=|<|>|BETWEEN\b|IN\b)", sql)
        )
    if operator == "empty":
        return bool(re.search(rf"(?i)bicoredata\.IsEmpty\s*\(\s*{re.escape(field)}\s*\)", sql))
    return True


def _cte_table_map(sql: str) -> dict[str, str]:
    """Map simple CTE names to their first physical FROM table."""
    result: dict[str, str] = {}
    for match in re.finditer(r"(?is)(\w+)\s+AS\s*\(", sql):
        name = match.group(1)
        start = match.end()
        depth = 1
        index = start
        while index < len(sql) and depth:
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
            index += 1
        block = sql[start:index]
        source = re.search(r"(?i)\bFROM\s+([\w.`]+)", block)
        if source:
            result[name.lower()] = source.group(1).replace("`", "").lower()
    return result


def _resolve_source(name: str, cte_map: dict[str, str]) -> str:
    seen: set[str] = set()
    current = name
    while current in cte_map and current not in seen:
        seen.add(current)
        current = cte_map.get(current, current)
    return current


def _sql_join_types(sql: str, cte_map: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r"(?i)(?:(LEFT|INNER|RIGHT|FULL|CROSS)\s+(?:OUTER\s+)?)?JOIN\s+([\w.`]+)", sql):
        kind = (match.group(1) or "INNER").upper()
        name = match.group(2).replace("`", "").lower()
        table = cte_map.get(name, name)
        result[table] = kind
        result[name] = kind
    return result


def check_static_semantics(ddl: str, dml: str, workspace: Path) -> list[dict[str, str]]:
    """Check explicit metadata JOIN evidence, without inferring policy from table/column names."""
    metadata = _metadata_join_types(workspace)
    findings: list[dict[str, str]] = []
    cte_map = _cte_table_map(dml)
    for table, actual in _sql_join_types(dml, cte_map).items():
        resolved = _resolve_source(table, cte_map)
        expected = (
            (
                metadata.get(table)
                or metadata.get(resolved)
                or metadata.get(table.rsplit(".", 1)[-1])
                or metadata.get(resolved.rsplit(".", 1)[-1])
                or ""
            )
            .replace(" JOIN", "")
            .replace(" OUTER", "")
        )
        if expected and actual != expected:
            findings.append(
                {
                    "code": "SQL_JOIN_TYPE_MISMATCH",
                    "field": table,
                    "message": f"metadata join_relations 要求 {expected}，SQL 使用了 {actual} JOIN。",
                }
            )
            break
    return findings


def check_contract(ddl: str, dml: str, contract: dict[str, Any]) -> list[dict[str, str]]:
    """Return findings for explicit output, predicate and JOIN requirements only."""
    findings: list[dict[str, str]] = []
    if contract.get("invalid"):
        return [{"code": "CONTRACT_INVALID", "field": "contract", "message": "contract.json 无法解析。"}]
    expected = [str(item) for item in contract.get("output_columns", [])]
    ddl_columns = _ddl_columns(ddl)
    select_columns = _select_columns(dml)
    if expected and len(ddl_columns) != len(expected):
        findings.append(
            {
                "code": "OUTPUT_COLUMN_COUNT",
                "field": "output_columns",
                "message": f"DDL 非分区列 {len(ddl_columns)} 个，contract 期望 {len(expected)} 个。",
            }
        )
    if expected and len(select_columns) != len(expected):
        findings.append(
            {
                "code": "SELECT_COLUMN_COUNT",
                "field": "output_columns",
                "message": f"INSERT SELECT 输出 {len(select_columns)} 列，contract 期望 {len(expected)} 列。",
            }
        )
    combined = ddl + "\n" + dml
    for item in contract.get("required_filters", []):
        field = str(item.get("field"))
        operator = str(item.get("operator"))
        if not _filter_present(combined, field, operator):
            findings.append(
                {
                    "code": "REQUIRED_FILTER_MISSING",
                    "field": field,
                    "message": f"contract 要求 {field} {operator}，但 SQL 中未找到。",
                }
            )
    cte_map = _cte_table_map(dml)
    joins = _sql_join_types(dml, cte_map)
    for item in contract.get("joins", []):
        right = str(item.get("right", "")).replace("`", "").lower()
        expected_type = str(item.get("type", "")).upper()
        actual_type = joins.get(right)
        if actual_type != expected_type:
            findings.append(
                {
                    "code": "CONTRACT_JOIN_MISMATCH",
                    "field": right,
                    "message": f"contract 要求 {right} 使用 {expected_type} JOIN，实际 {actual_type or '未找到'}。",
                }
            )
    return findings
