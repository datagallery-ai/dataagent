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
import re
from typing import Any

from dataagent.agents.nl2sql.errors import LLMOutputParseError
from dataagent.utils.constants import DEFAULT_NL2SQL_CELL_TRUNCATE_LENGTH

# 匹配未被单引号包裹的 ${...} 模板（如 ${starttime, -5, yyyyMMdd}）
_PLACEHOLDER_BRACE = re.compile(r"(?<!\')\$\{[^}]+\}(?!')")
# 匹配未被单引号包裹的 $var 模板（如 $date）；排除 ${...} 中的 $
_PLACEHOLDER_SIMPLE = re.compile(r"(?<!\')\$(?!\{)[a-zA-Z_][a-zA-Z0-9_]*(?!')")


def iter_semantic_column_payloads(raw: Any) -> list[dict]:
    if raw is None:
        return []
    outer = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for item in outer:
        if isinstance(item, dict) and item:
            payload = next(iter(item.values()))
            if isinstance(payload, dict):
                out.append(payload)
    return out


def quote_sql_placeholders(sql: str) -> str:
    """Wrap unquoted $ / ${} template placeholders in single quotes."""
    sql = _PLACEHOLDER_BRACE.sub(lambda m: f"'{m.group(0)}'", sql)
    sql = _PLACEHOLDER_SIMPLE.sub(lambda m: f"'{m.group(0)}'", sql)
    return sql


def sql_parser(content: str) -> list[str]:
    m = re.findall(r"```sql\s*(.*?)\s*```", content, re.S | re.I)
    if not m:
        raise LLMOutputParseError(detail="No SQL found")
    sqls = []
    for sql in m:
        sql = sql.replace("\xa0", " ").strip().rstrip(";")
        sql = re.sub(r"/\*.*?\*/|--.*?$", "", sql, flags=re.S | re.M)
        sql = re.sub(r"\s+", " ", sql).strip()
        sql = quote_sql_placeholders(sql)
        sqls.append(sql)
    return sqls


def json_parser(content: str) -> str:
    m = re.search(r"```json\s*(.*?)\s*```", content, re.S | re.I)
    if not m:
        raise LLMOutputParseError(detail="No JSON found")
    return m.group(1).strip()


def metadata_parser(text: str) -> list[dict[str, set[str]]]:
    blocks = re.findall(r"<res>\s*(.*?)\s*</res>", text, flags=re.S | re.I)
    if not blocks:
        raise LLMOutputParseError(detail="No metadata result found")
    out: list[dict[str, set[str]]] = []
    try:
        for b in blocks:
            m: dict[str, set[str]] = {}
            for line in b.splitlines():
                line = line.strip()
                if not line or "=>" not in line:
                    continue
                col, rhs = map(str.strip, line.split("=>", 1))
                if "." not in col:
                    raise ValueError(f"Invalid metadata column: {col}")
                t, c = (x.strip('"') for x in col.split(".", 1))
                vals = {v.strip() for v in rhs.split("|") if v.strip()} if rhs else set()
                m[f"{t}.{c}"] = vals
            out.append(m)
    except Exception as exc:
        raise LLMOutputParseError(detail=str(exc)) from exc
    return out


def flatten_schema(schema: dict) -> set[str]:
    res = set()
    for t, t_meta in schema.items():
        for c in t_meta["columns"]:
            res.add(f"{t}.{c}")
    return res


def filter_schema(schema: dict, used: set[str]) -> dict:
    res = {}
    for t, c in (u.split(".", 1) for u in used):
        if t in schema and c in schema[t]["columns"]:
            res.setdefault(
                t,
                {"description": schema[t].get("description"), "columns": {}},
            )["columns"][c] = schema[t]["columns"][c]
    return res


def snippets_to_str(sql_snippets: list[dict[str, str]]) -> str:
    return "\n".join(f"{snip['description']}\n```sql\n{snip['content']}\n```\n" for snip in sql_snippets)


def truncate(v: Any) -> str:
    MAX_LEN = DEFAULT_NL2SQL_CELL_TRUNCATE_LENGTH
    s = str(v)
    return f"{s[:MAX_LEN]}..." if len(s) > MAX_LEN else v


def _normalize_type(value_type: str):
    if not value_type:
        return "TEXT"
    vt = value_type.lower()
    if any(k in vt for k in ["int", "id", "count", "num"]):
        return "INTEGER"
    if any(k in vt for k in ["float", "double", "price", "amount"]):
        return "REAL"
    if any(k in vt for k in ["date", "time"]):
        return "TEXT"
    return "TEXT"


def format_col(col_name: str):
    return f"`{col_name}`"


_FORMULA_COLUMN = re.compile(
    r"(?:\b(?:avg|count|max|min|sum)\s*\(\s*|^\s*)([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _formula_column_names(formula: str) -> list[str]:
    """Return concrete column identifiers in formula order."""
    return list(dict.fromkeys(_FORMULA_COLUMN.findall(formula)))


def _assign_relation_formulas(schema_ir, relation_catalog) -> dict[tuple[str, str], list[str]]:
    """Assign each unique derived formula to one referenced schema column."""
    if not relation_catalog:
        return {}

    table_columns = {table_name: set(table_info["columns"]) for table_name, table_info in schema_ir.items()}

    assignments: dict[tuple[str, str], list[str]] = {}
    seen_formulas: set[str] = set()
    for metric_id, metadata in relation_catalog.items():
        if not isinstance(metadata, dict):
            continue
        formula = str(metadata.get("column_value_profile") or "").strip()
        if not formula or formula in seen_formulas:
            continue
        seen_formulas.add(formula)

        identifiers = _formula_column_names(formula)
        required_columns = set(identifiers)
        eligible_tables = [
            table_name
            for table_name, columns in table_columns.items()
            if required_columns and required_columns.issubset(columns)
        ]
        candidates: list[tuple[str, str]] = []
        for identifier in identifiers:
            for table_name in eligible_tables:
                candidates.append((table_name, identifier))
        if not candidates:
            continue

        target = min(candidates, key=lambda location: len(assignments.get(location, [])))
        metric_name = str(metadata.get("column_short_description") or "").strip()
        if not metric_name:
            metric_name = str(metric_id).rsplit(".", 1)[-1]
        assignments.setdefault(target, []).append(f"{metric_name} = {formula}")
    return assignments


def schema_to_ddl(schema_ir, joins=None, relation_catalog=None):
    relation_formulas = _assign_relation_formulas(schema_ir, relation_catalog)
    fk_map = {}
    if joins:
        for left, right in joins:
            l_tbl, l_col = left.split(".")
            r_tbl, r_col = right.split(".")
            fk_map.setdefault(l_tbl, []).append((l_col, r_tbl, r_col))
    ddl_blocks = []
    for table_name, table_info in schema_ir.items():
        lines = []
        table_stmt = f"CREATE TABLE `{table_name}` (\n"
        for col_name, col_info in table_info["columns"].items():
            col_type = _normalize_type(col_info.get("value_type"))
            col_desc = col_info.get("description", "").strip()
            line = f"    {format_col(col_name)} {col_type}"
            comments = []
            if col_desc:
                comments.append(col_desc)
            vals = col_info.get("example_values")
            if vals:
                comments.append(f"example: {vals}")
            for formula in relation_formulas.get((table_name, col_name), []):
                comments.append(f"relation_formula: {formula}")
            if comments:
                line += f", -- {'; '.join(comments)}"
            else:
                line += ","
            lines.append(line)
        fk_lines = []
        for fk_col, ref_table, ref_col in fk_map.get(table_name, []):
            fk_lines.append(f"    FOREIGN KEY ({format_col(fk_col)}) REFERENCES `{ref_table}`({format_col(ref_col)}),")
        all_lines = lines + fk_lines
        if all_lines:
            all_lines[-1] = all_lines[-1].rstrip(",")
        table_stmt += "\n".join(all_lines)
        table_stmt += "\n);"
        if table_info.get("description"):
            table_stmt = f"-- {table_info['description']}\n" + table_stmt
        ddl_blocks.append(table_stmt)
    return "\n\n".join(ddl_blocks)
