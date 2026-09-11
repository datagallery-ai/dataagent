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
import hashlib
import re
from typing import Any

from dataagent.agents.bird.constants import DEFAULT_BIRD_CELL_TRUNCATE_LENGTH
from dataagent.agents.bird.errors import LLMOutputParseError
from dataagent.core.errors import DataAgentError

# 匹配未被单引号包裹的 ${...} 模板（如 ${starttime, -5, yyyyMMdd}）
_PLACEHOLDER_BRACE = re.compile(r"(?<!\')\$\{[^}]+\}(?!')")
# 匹配未被单引号包裹的 $var 模板（如 $date）；排除 ${...} 中的 $
_PLACEHOLDER_SIMPLE = re.compile(r"(?<!\')\$(?!\{)[a-zA-Z_][a-zA-Z0-9_]*(?!')")


def sql_sha256(sql: str) -> str:
    """Return a stable SHA-256 fingerprint for normalized SQL."""
    normalized = re.sub(r"\s+", " ", (sql or "").strip())
    return hashlib.sha256(normalized.encode()).hexdigest()


def quote_sql_placeholders(sql: str) -> str:
    """Wrap unquoted $ / ${} template placeholders in single quotes."""
    sql = _PLACEHOLDER_BRACE.sub(lambda m: f"'{m.group(0)}'", sql)
    sql = _PLACEHOLDER_SIMPLE.sub(lambda m: f"'{m.group(0)}'", sql)
    return sql


def sql_parser(content: str) -> list[str]:
    """Extract SQL statements from model output text."""
    m = re.findall(r"```sql\s*(.*?)\s*```", content, re.S | re.I)
    if not m:
        raise DataAgentError(source="internal", fact="No SQL found", component="bird")
    sqls = []
    for sql in m:
        sql = sql.replace("\xa0", " ").strip().rstrip(";")
        sql = re.sub(r"/\*.*?\*/|--.*?$", "", sql, flags=re.S | re.M)
        sql = re.sub(r"\s+", " ", sql).strip()
        sql = quote_sql_placeholders(sql)
        sqls.append(sql)
    return sqls


def json_parser(content: str, *, allow_unfenced: bool = False) -> str:
    """Extract a JSON object/array string from model output."""
    m = re.search(r"```json\s*(.*?)\s*```", content, re.S | re.I)
    if m:
        return m.group(1).strip()
    if allow_unfenced:
        stripped = content.strip()
        opening_fence = re.match(r"```json\s*(.*)\Z", stripped, re.S | re.I)
        candidate = opening_fence.group(1).strip() if opening_fence else stripped
        if candidate.startswith(("{", "[")):
            return candidate
    raise LLMOutputParseError(detail="No JSON found")


def truncate(v: Any) -> str:
    """Truncate a value for prompt-friendly display."""
    MAX_LEN = DEFAULT_BIRD_CELL_TRUNCATE_LENGTH
    s = str(v)
    return f"{s[:MAX_LEN]}..." if len(s) > MAX_LEN else v


def _normalize_type(value_type: str):
    if not value_type:
        return "TEXT"
    vt = value_type.lower()
    if "int" in vt or any(k in vt for k in ["id", "count", "num", "decimal(24,0)"]):
        return "INTEGER"
    if any(k in vt for k in ["float", "double", "price", "amount"]):
        return "REAL"
    if any(k in vt for k in ["date", "time"]):
        return "TEXT"
    return "TEXT"


def format_col(col_name: str):
    """Format a column name for DDL/prompt output."""
    return f"`{col_name}`"


def schema_to_ddl(schema_ir, joins=None, *, include_primary_keys: bool = False):
    """Convert schema IR (and joins) into a DDL-like prompt string."""
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
            if include_primary_keys and col_info.get("is_primary_key"):
                line += " PRIMARY KEY"
            comments = []
            if col_desc:
                comments.append(col_desc)
            vals = col_info.get("example_values")
            if vals:
                comments.append(f"example: {vals}")
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
