# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Independent tool for DataOps adHoc SQL validation.

de_agent should call this tool AFTER it has run the 8-category quality
self-check on the generated SQL, not as a hook on the NL2SQL pipeline tool.

Wire in YAML::

    TOOLS:
      local_functions:
        - module: "dataagent.actions.tools.local_tool.dataops_validate_tool"
          function: "dataops_validate_sql"

The tool reads the ``dataops`` resource from
``runtime.ensure_resource_coordinator().catalog``. If the resource is absent,
the tool returns ``{"passed": true, "skipped": true, "reason": "..."}``
so that callers can no-op cleanly.

Environment routing
-------------------

Set ``DATAOPS_ENV=prod`` to route validation through the official DataOps MCP
(2-step ``execute_sql`` -> ``get_query_result`` flow). Defaults to
``integration`` (the original 3-step ``submit -> poll -> collect`` flow). Unknown
values fall back to ``integration`` with a warning log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.managers import llm_manager
from dataagent.utils.log import logger

if TYPE_CHECKING:
    pass


# ============================================================================
# Retry / best-effort constants (shared by dataops_validate_sql_with_log_analysis)
# ============================================================================
MAX_VALIDATE_ATTEMPTS = 3
_VALIDATION_REPORT_FILENAME = "validate_{table}.json"
_DELIVERY_WARNING_FILENAME = "delivery_warning.md"
_DELIVERY_WARNING_HEADER = (
    "⚠️ 本次交付物未通过 DataOps 校验（重试 {n} 次仍失败），仅作 best-effort，下游使用前必须人工复核"
)


def _get_dataops_resource(runtime):
    try:
        coordinator = runtime.ensure_resource_coordinator()
    except Exception:
        return None
    if coordinator is None:
        return None
    return coordinator.catalog.get("dataops")


def _check_skip_conditions(sql: str, runtime) -> tuple[dict | None, Any]:
    """Check skip conditions for dataops validation.

    Returns:
        (skip_result, None) if should skip: skip_result is the skip dict to return
        (error_result, None) if coordinator unavailable: error_result is the error dict to return
        (None, (coordinator, resource)) if should proceed
    """
    sql = (sql or "").strip()
    if not sql:
        return {"passed": True, "skipped": True, "reason": "empty sql"}, None

    try:
        coordinator = runtime.ensure_resource_coordinator()
    except Exception:
        logger.debug("[dataops_validate_sql] skipped: no dataops resource")
        return {"passed": True, "skipped": True, "reason": "no_dataops_resource"}, None

    if coordinator is None:
        logger.debug("[dataops_validate_sql] skipped: no dataops resource")
        return {"passed": True, "skipped": True, "reason": "no_dataops_resource"}, None

    resource = coordinator.catalog.get("dataops")
    if resource is None:
        logger.debug("[dataops_validate_sql] skipped: no dataops resource")
        return {"passed": True, "skipped": True, "reason": "no_dataops_resource"}, None

    if not bool(resource.metadata.get("enabled", True)):
        logger.debug("[dataops_validate_sql] skipped: dataops resource disabled")
        return {"passed": True, "skipped": True, "reason": "dataops_disabled"}, None

    transport_url = str((resource.transport or {}).get("url") or "").strip()
    if not transport_url:
        logger.debug("[dataops_validate_sql] skipped: empty transport url")
        return {"passed": True, "skipped": True, "reason": "dataops_url_empty"}, None

    return None, coordinator


async def _poll_until_done(
    coordinator,
    job_id: str,
    timeout_sec: int,
    poll_interval: float,
) -> tuple[str, dict | None]:
    """Poll job until completion, timeout, or error.

    Returns:
        (result_status, result_dict_or_None)
        result_dict is from coordinator.collect() if status is terminal.
        result_dict is None on timeout.
    """
    deadline = time.monotonic() + timeout_sec
    result_status = "queued"
    while time.monotonic() < deadline:
        poll_result = coordinator.poll(job_id=job_id)
        logger.debug(f"[dataops_validate_sql] poll_result job_id={job_id} result={poll_result}")
        result_status = str(poll_result.get("status") or "").strip().lower()
        if result_status in {"completed", "failed", "cancelled", "timed_out"}:
            break
        await asyncio.sleep(poll_interval)
    else:
        try:
            coordinator.cancel(job_id=job_id)
        except Exception as cancel_exc:
            logger.debug(f"[dataops_validate_sql] cancel failed for {job_id}: {cancel_exc}")
        return "timed_out", None

    result = coordinator.collect(job_id=job_id)
    logger.debug(
        f"[dataops_validate_tool] resource.collect received job_id={job_id} "
        f"payload={json.dumps(result, ensure_ascii=False, default=str)[:8000]}"
    )
    logger.debug(
        "[dataops_validate_sql] collect result keys={} logFileInfo={}".format(
            list(result.keys()),
            result.get("logFileInfo"),
        )
    )
    return result_status, result


def _build_failure_response(
    result_status: str,
    job_id: str,
    result: dict,
    sql: str,
) -> dict:
    """Build failure response from collect result."""
    error_msg = result.get("error") or result.get("summary") or result.get("message") or ""
    log_file_info = result.get("logFileInfo") or result.get("log_file_info") or {}

    error_excerpt = (error_msg or "")[:300]
    log_info_keys = list(log_file_info.keys()) if isinstance(log_file_info, dict) else []
    logger.debug(
        f"[dataops_validate_sql] FAILED job_id={job_id!r} status={result_status!r} "
        f"error={error_excerpt!r} log_file_info_keys={log_info_keys} sql_excerpt={sql[:200]!r}",
    )

    response_data = {
        "passed": False,
        "error": error_msg or f"dataops rejected SQL (status={result_status})",
        "job_id": job_id,
    }

    if log_file_info and isinstance(log_file_info, dict):
        logger.debug(
            f"[dataops_validate_sql] Validation failed job_id={job_id!r}, returning log_file_info "
            "for log analysis to consume",
        )
        response_data["log_file_info"] = log_file_info
    else:
        logger.debug(
            f"[dataops_validate_sql] No log_file_info available job_id={job_id!r}",
        )

    return response_data


def _get_user_account(runtime) -> str:
    """Extract user account from runtime context or environment.

    Priority:
    1. DATAOPS_EXEC_USER environment variable (set by MCP server)
    2. runtime.user_id
    3. "anonymous" (default fallback)
    """
    # First check environment variable (set by the MCP server)
    exec_user = os.environ.get("DATAOPS_EXEC_USER", "").strip()
    if exec_user:
        return exec_user

    # Fall back to runtime user_id
    user_id = getattr(runtime, "user_id", None)
    if user_id:
        return str(user_id)
    return "anonymous"


def _sanitize_table_name(name: str) -> str:
    """Replace dots and hyphens in table name with underscores."""
    return name.replace(".", "_").replace("-", "_")


_MAX_FULL_TABLE_LEN = 100


def _get_temp_table_name(original_table: str, user_account: str, today: str, table_suffix: str) -> str:
    """Build the adhoctemp.tmp_{user}_{date}_{table} name, truncated to _MAX_FULL_TABLE_LEN."""
    sanitized = _sanitize_table_name(original_table)
    if "." in original_table:
        sanitized = sanitized.split("_")[-1]
    prefix = f"adhoctemp.tmp_{_sanitize_table_name(user_account)}_{today}_"
    max_suffix = _MAX_FULL_TABLE_LEN - len(prefix)
    if len(sanitized) > max_suffix:
        sanitized = sanitized[:max_suffix]
    suffix = _sanitize_table_name(table_suffix) if table_suffix else ""
    return f"{prefix}{sanitized}" if not suffix else f"{prefix}{sanitized}_{suffix}"


_INSERT_RE = re.compile(r"\binsert\s+(?:overwrite\s+|into\s+)?(?:table\s+)?", re.IGNORECASE)
_CREATE_RE = re.compile(r"\bcreate\s+(?:external\s+)?table\b", re.IGNORECASE)


def _is_create_sql(sql: str) -> bool:
    """Return True if sql is a CREATE TABLE statement."""
    return bool(_CREATE_RE.search(sql))


def _build_drop_sql(original_sql: str, user_account: str, table_suffix: str) -> str | None:
    """Build DROP TABLE IF EXISTS statement for the temporary table.

    This is used when submitting CREATE TABLE to ensure clean table creation
    by dropping any existing table with the same name first.

    Args:
        original_sql: The original CREATE TABLE statement (before rewriting)
        user_account: User account for temp table naming
        table_suffix: Table suffix from resource metadata

    Returns:
        DROP TABLE IF EXISTS statement for the temp table, or None if extraction fails.
    """
    # First rewrite to get the temp table name
    rewritten = _replace_target_table(original_sql, user_account, table_suffix)

    # Extract the temp table name from the rewritten CREATE statement
    # Pattern: CREATE [EXTERNAL] TABLE [IF NOT EXISTS] temp_table (...)
    create_pattern = r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.`_-]+)"
    match = re.search(create_pattern, rewritten, re.IGNORECASE)
    if not match:
        return None

    temp_table = match.group(1).strip()
    temp_table = temp_table.strip("`").strip("'").strip('"')
    if not temp_table:
        return None

    return f"DROP TABLE IF EXISTS {temp_table}"


def _replace_target_table(sql: str, user_account: str, table_suffix: str) -> str:
    """Replace target table names (CREATE TABLE, INSERT OVERWRITE) with temporary table format.

    Temporary table format: adhoctemp.tmp_{user_account}_{date}_{table_suffix}

    Also replaces pt_d = '$date' (and other partition columns) with one year from today.

    Examples:
        CREATE TABLE biads.ads_xxx → CREATE TABLE adhoctemp.tmp_user_date_ads_xxx
        INSERT OVERWRITE TABLE biads.ads_yyy → INSERT OVERWRITE TABLE adhoctemp.tmp_user_date_ads_yyy
        INSERT OVERWRITE EXTERNAL TABLE xxx → INSERT OVERWRITE TABLE adhoctemp.tmp_user_date_xxx
        pt_d = '$date' → pt_d = 'YYYYMMDD'
    """
    one_year_later = (date.today() + timedelta(days=365)).strftime("%Y%m%d")
    sql = re.sub(
        r"(\w+)\s*=\s*'\$date'",
        lambda m: f"{m.group(1)} = '{one_year_later}'",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"(\w+)\s*=\s*'\$\{date\}'",
        lambda m: f"{m.group(1)} = '{one_year_later}'",
        sql,
        flags=re.IGNORECASE,
    )

    def _find_matching_paren(s: str, start: int) -> int:
        """Find the matching closing parenthesis, handling nested parens and quoted strings."""
        depth = 1
        i = start
        while i < len(s) and depth > 0:
            char = s[i]
            if char == "'":
                i += 1
                while i < len(s) and s[i] != "'":
                    if s[i] == "\\":
                        i += 2
                    else:
                        i += 1
                i += 1
            elif char == "(":
                depth += 1
                i += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return i
                i += 1
            else:
                i += 1
        return i

    # Process CREATE TABLE statements
    # Pattern: CREATE [EXTERNAL] TABLE [IF NOT EXISTS] [db.]table_name (...)
    def replace_create(m: re.Match) -> str:
        full_match = m.group(0)

        # Find the table name
        create_pattern = r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)"
        create_match = re.search(create_pattern, full_match, re.IGNORECASE)
        if not create_match:
            return full_match

        # Check if IF NOT EXISTS was present
        has_if_not_exists = bool(
            re.search(
                r"IF\s+NOT\s+EXISTS",
                full_match[: create_match.end()],
                re.IGNORECASE,
            )
        )

        table_name = create_match.group(2)
        temp_table = _get_temp_table_name(table_name, user_account, one_year_later, table_suffix)

        # Find parentheses content
        paren_start = full_match.find("(", create_match.end())
        if paren_start == -1:
            return full_match
        paren_end = _find_matching_paren(full_match, paren_start)
        content = full_match[paren_start : paren_end + 1]

        # Preserve IF NOT EXISTS
        if_not_exists = " IF NOT EXISTS" if has_if_not_exists else ""
        return f"CREATE TABLE{if_not_exists} {temp_table}{content}"

    # Process INSERT OVERWRITE/INTO statements
    def replace_insert(m: re.Match) -> str:
        full_match = m.group(0)

        # Remove EXTERNAL keyword if present
        full_match = re.sub(r"\bEXTERNAL\b", "", full_match, flags=re.IGNORECASE).strip()

        # Find the table name - support db.table-name format
        # Note: TABLE keyword is optional (some SQL dialects omit it)
        # Table name can contain dots and hyphens
        insert_pattern = r"INSERT\s+(?:OVERWRITE|INTO)\s+(?:EXTERNAL\s+)?(?:TABLE\s+)?(?:(\w+(?:\.\w+)*)\.)?([\w\-]+)"
        insert_match = re.search(insert_pattern, full_match, re.IGNORECASE)
        if not insert_match:
            return full_match

        db_name = insert_match.group(1)
        table_name = insert_match.group(2)
        temp_table = _get_temp_table_name(table_name, user_account, one_year_later, table_suffix)

        # Replace original table name
        original = f"{db_name}.{table_name}" if db_name else table_name

        return full_match.replace(original, temp_table, 1)

    result = sql

    # Match and replace CREATE TABLE statements
    result = re.sub(
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[^\s(]+\s*\([^)]*\)",
        replace_create,
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Match and replace INSERT OVERWRITE/INTO TABLE statements
    # Support table names with dots, hyphens, and underscores
    # Note: TABLE keyword is optional (some SQL dialects omit it)
    result = re.sub(
        r"INSERT\s+(?:OVERWRITE|INTO)\s+(?:EXTERNAL\s+)?(?:TABLE\s+)?(?:(\w+(?:\.\w+)*)\.)?[\w\-]+",
        replace_insert,
        result,
        flags=re.IGNORECASE,
    )

    return result


_CTE_SYSTEM_PROMPT = """Transform SQL for adHoc engines that do NOT support WITH AS (CTE) before INSERT.
Rules:
1. If no WITH AS clause → return unchanged.
2. If WITH AS appears AFTER INSERT (INSERT ... WITH ...) → return unchanged.
3. If WITH AS appears BEFORE INSERT (WITH ... INSERT ...) → inline the CTEs.
4. Return ONLY the transformed SQL, no explanations, no markdown.
Examples:
Input:
WITH t1 AS (SELECT * FROM src1),
     t2 AS (SELECT * FROM src2)
SELECT a.id, b.name FROM t1 a JOIN t2 b ON a.id = b.id

Output:
SELECT a.id, b.name FROM (SELECT * FROM src1) AS t1 JOIN (SELECT * FROM src2) AS t2 ON a.id = b.id
"""


def _has_cte(sql: str) -> bool:
    """Check if the SQL contains a WITH AS clause before an INSERT."""
    upper = sql.upper()
    with_pos = upper.find("WITH")
    insert_pos = upper.find("INSERT")
    return with_pos != -1 and (insert_pos == -1 or with_pos < insert_pos)


async def _expand_cte_for_dml(sql: str, timeout_sec: int = 30, runtime=None) -> str:
    """Expand WITH AS (CTE) in DML INSERT statements to equivalent inline subqueries.

    Only processes DML INSERT statements that contain WITH AS clauses.
    Returns the original SQL unchanged if no CTE is detected or if an error occurs.
    Does not modify the original saved file.
    """
    if not _has_cte(sql):
        return sql

    try:
        if runtime is not None:
            try:
                llm = runtime.llm("planner")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[_expand_cte_for_dml] runtime.llm('planner') failed: {}", exc)
                llm = llm_manager.get_default_llm()
        else:
            llm = llm_manager.get_default_llm()
        messages = [
            {"role": "system", "content": _CTE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Transform this SQL:\n{sql}"},
        ]
        logger.debug(
            f"[_expand_cte_for_dml] expanding CTE, original[:120]={sql[:120]!r}",
        )
        response = await asyncio.to_thread(llm.invoke, messages)
        expanded = str(response.content).strip()

        # Strip markdown code fences if LLM wrapped output
        expanded = re.sub(r"^```sql\s*", "", expanded, flags=re.IGNORECASE).strip()
        expanded = re.sub(r"^```\s*", "", expanded).strip()
        expanded = re.sub(r"\s*```$", "", expanded).strip()

        if expanded and expanded != sql:
            logger.debug(
                f"[_expand_cte_for_dml] expanded original[:80]={sql[:80]!r} → expanded[:80]={expanded[:80]!r}",
            )
            return expanded
        return sql
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[_expand_cte_for_dml] LLM expansion failed: {exc}, falling back to original")
        return sql


# ---------------------------------------------------------------------------
# Helpers for post-validate SELECT count(*) second-phase check
# ---------------------------------------------------------------------------

_POST_VALIDATE_PARTITION_COL = "pt_d"


def _is_insert_sql(sql: str) -> bool:
    """Return True if sql is an INSERT statement."""
    return bool(_INSERT_RE.search(sql))


def _extract_temp_table_name(
    sql: str,
    user_account: str,
    table_suffix: str,
) -> str | None:
    """Extract the adhoctemp target table name from any rewritten SQL (CREATE or INSERT).

    Unified extractor for cleanup purposes. Returns None for SELECT-like SQL
    that doesn't materialize a temp table.
    """
    if _is_create_sql(sql):
        # CREATE: parse the rewritten CREATE statement
        rewritten = _replace_target_table(sql, user_account, table_suffix)
        match = re.search(
            r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.`_-]+)",
            rewritten,
            re.IGNORECASE,
        )
        if not match:
            return None
        return match.group(1).strip().strip("`").strip("'").strip('"') or None

    return _extract_insert_target_table(sql, user_account, table_suffix)


def _extract_insert_target_table(
    sql: str,
    user_account: str,
    table_suffix: str,
) -> str | None:
    """Extract the adhoctemp target table name from a rewritten INSERT sql.

    Runs _replace_target_table internally to get the rewritten INSERT line,
    then extracts the table identifier that was substituted.
    Returns None if no INSERT clause or table name is found.
    """
    rewritten = _replace_target_table(sql, user_account, table_suffix)
    # The rewritten INSERT has the temp table in the INSERT clause.
    # Pattern: INSERT ... INTO <adhoctemp.tmp_...>
    m = re.search(
        r"INSERT\s+(?:OVERWRITE|INTO)\s+(?:EXTERNAL\s+)?(?:TABLE\s+)?([\w.`_-]+)",
        rewritten,
        re.IGNORECASE,
    )
    if not m:
        return None
    table_ref = m.group(1).strip()
    # Remove backtick/quotes if any
    table_ref = table_ref.strip("`").strip("'").strip('"')
    return table_ref if table_ref else None


def _build_count_sql(rewritten_insert_sql: str, partition_col: str = _POST_VALIDATE_PARTITION_COL) -> str | None:
    """Build SELECT count(*) FROM <adhoctemp tmp table> WHERE partition_col='<today>'.

    The adhoctemp table name is extracted directly from the INSERT clause
    (between INSERT and SELECT), not from the SELECT source.
    """
    one_year_later = (date.today() + timedelta(days=365)).strftime("%Y%m%d")
    # Strip any trailing semicolon and whitespace
    sql = rewritten_insert_sql.rstrip("; \t\r\n")

    # Match the target table: between "INSERT [OVERWRITE|INTO] ..." and "SELECT"
    # Use a greedy match for the table name (may contain dots/underscores/hyphens)
    m = re.search(
        r"INSERT\s+(?:OVERWRITE|INTO)\s+(?:EXTERNAL\s+)?(?:TABLE\s+)?([\w.`_-]+)",
        sql,
        re.IGNORECASE,
    )
    if not m:
        return None
    target_table = m.group(1).strip().strip("`").strip("'").strip('"')
    if not target_table:
        return None
    return f"SELECT count(*) FROM {target_table} WHERE {partition_col} = '{one_year_later}'"


def _parse_count_from_collect(result: dict[str, Any]) -> int | None:
    """Extract integer count from a completed collect result.

    Tries ``data`` first (list of row dicts from CSV parse), then falls back
    to trying to read the first numeric value found.
    Returns None if parsing fails.
    """
    # 1. Try explicit data rows
    data = result.get("data")
    if isinstance(data, list) and len(data) > 0:
        first_row = data[0]
        if isinstance(first_row, dict):
            for v in first_row.values():
                try:
                    val = float(str(v).strip())
                    if val == int(val):
                        return int(val)
                    return int(val)
                except (ValueError, TypeError):
                    continue
        elif first_row is not None:
            try:
                return int(float(str(first_row).strip()))
            except (ValueError, TypeError):
                pass

    # 2. Fall back to data_meta.row_count
    meta = result.get("data_meta")
    if isinstance(meta, dict):
        row_count = meta.get("row_count")
        if isinstance(row_count, int):
            return row_count
        if isinstance(row_count, str):
            try:
                return int(float(row_count.strip()))
            except (ValueError, TypeError):
                pass

    return None


async def _analyze_count_zero_with_llm(
    original_query: str,
    generated_sql: str,
    runtime=None,
) -> dict[str, Any]:
    """Ask LLM whether the INSERT SQL semantically matches the original NL query.

    Returns ``{"has_mismatch": bool, "mismatch_reason": str, "fix_suggestion": str}``.
    """
    system_prompt = (
        "You are a Spark SQL / NL2SQL analyst. Given:\n"
        "1. The original natural language query from the user\n"
        "2. A generated INSERT SQL statement\n"
        "Determine whether the SQL faithfully implements the user's intent.\n\n"
        "Return EXACTLY this JSON object (no markdown, no prose):\n"
        "{\n"
        '  "has_mismatch": true|false,\n'
        '  "mismatch_reason": "<one sentence, <= 300 chars, empty string if no mismatch>",\n'
        '  "fix_suggestion": "<actionable SQL correction, <= 200 chars, empty string if no mismatch>"\n'
        "}\n\n"
        "Common mismatch patterns to detect:\n"
        "- WHERE/ON condition is too restrictive (filters out all rows)\n"
        "- Wrong source or target table\n"
        "- Incorrect join keys causing empty result\n"
        "- Wrong date/partition value\n"
        "- Logic is inverted (e.g., WHERE NOT condition when it should be WHERE condition)\n"
        "- Missing GROUP BY or JOIN causing empty aggregation\n"
        "- SELECT columns don't match what user asked for"
    )
    user_prompt = (
        f"=== Original Natural Language Query ===\n{original_query}\n\n"
        f"=== Generated INSERT SQL ===\n{generated_sql}\n\n"
        f"=== Task ===\n"
        f"Does the SQL faithfully implement the user's intent? If the SQL inserts 0 rows,\n"
        f"what is the most likely cause?\n"
    )

    llm = None
    llm_source = "none"
    if runtime is not None:
        runtime_llm_getter = getattr(runtime, "llm", None)
        if callable(runtime_llm_getter):
            with contextlib.suppress(Exception):  # noqa: BLE001
                llm = runtime_llm_getter("planner")
            llm_source = "runtime.planner"

    if llm is None:
        try:
            llm = llm_manager.get_default_llm()
        except Exception:  # noqa: BLE001
            llm = None
        else:
            llm_source = "llm_manager"

    if llm is None:
        logger.debug("[_analyze_count_zero_with_llm] no LLM available, defaulting to no mismatch")
        return {"has_mismatch": False, "mismatch_reason": "", "fix_suggestion": ""}

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        logger.debug(
            f"[_analyze_count_zero_with_llm] invoking LLM source={llm_source} "
            f"query_len={len(original_query)} sql_len={len(generated_sql)}"
        )
        response = await asyncio.to_thread(llm.invoke, messages)
        raw_text = str(getattr(response, "content", response) or "").strip()
        logger.debug(f"[_analyze_count_zero_with_llm] LLM response[:200]={raw_text[:200]!r}")

        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE).strip()
        raw_text = re.sub(r"\s*```$", "", raw_text).strip()
        parsed = json.loads(raw_text)
        return {
            "has_mismatch": bool(parsed.get("has_mismatch", False)),
            "mismatch_reason": str(parsed.get("mismatch_reason", "") or ""),
            "fix_suggestion": str(parsed.get("fix_suggestion", "") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[_analyze_count_zero_with_llm] LLM call failed: {exc}, defaulting to no mismatch")
        return {"has_mismatch": False, "mismatch_reason": "", "fix_suggestion": ""}


async def _run_post_validate(
    coordinator,
    count_sql: str,
    original_sql: str = "",
    *,
    timeout_sec: int,
    poll_interval: float,
    runtime=None,
    original_query: str = "",
) -> dict[str, Any]:
    """Submit SELECT count(*) → poll → collect → parse count.

    When count is 0 and original_query is available, also runs a semantic mismatch
    check via LLM.

    Returns ``{"ok": bool, "count": int|None, "error": str|None, "job_id": str|None,
    "has_mismatch": bool, "mismatch_reason": str, "fix_suggestion": str}``.
    """
    count_timeout = min(timeout_sec, 120)
    try:
        submit_result = coordinator.submit_job(
            resource_id="dataops",
            command=count_sql,
            task_type="sql_validate",
            timeout_sec=count_timeout,
        )
        count_job_id = submit_result.get("job_id")
        if submit_result.get("status") == "ERROR" or not count_job_id:
            return {
                "ok": False,
                "count": None,
                "error": submit_result.get("message") or "count submit failed",
                "job_id": None,
            }
    except Exception as exc:
        return {"ok": False, "count": None, "error": f"count submit error: {exc}", "job_id": None}

    result_status, result = await _poll_until_done(coordinator, count_job_id, count_timeout, poll_interval)

    if result_status == "timed_out":
        return {
            "ok": False,
            "count": None,
            "error": f"count query timed out after {count_timeout}s",
            "job_id": count_job_id,
        }
    if result is None:
        return {"ok": False, "count": None, "error": "count collect returned no result", "job_id": count_job_id}
    if result.get("status") != "completed":
        error_msg = result.get("error") or result.get("summary") or f"count job status={result.get('status')}"
        return {"ok": False, "count": None, "error": error_msg, "job_id": count_job_id}

    count = _parse_count_from_collect(result)
    if count is None:
        return {
            "ok": False,
            "count": None,
            "error": "count result parse failed",
            "job_id": count_job_id,
            "has_mismatch": False,
            "mismatch_reason": "",
            "fix_suggestion": "",
        }

    if count == 0 and original_query:
        analysis = await _analyze_count_zero_with_llm(original_query, original_sql or count_sql, runtime=runtime)
        return {
            "ok": True,
            "count": 0,
            "error": None,
            "job_id": count_job_id,
            **analysis,
        }

    return {
        "ok": True,
        "count": count,
        "error": None,
        "job_id": count_job_id,
        "has_mismatch": False,
        "mismatch_reason": "",
        "fix_suggestion": "",
    }


# ============================================================================
# Lifecycle helpers
# ============================================================================


async def _submit_and_poll(
    coordinator,
    sql_for_submit: str,
    timeout_sec: int,
    poll_interval: float,
) -> tuple[str, str | None, dict | None]:
    """Submit SQL to DataOps and poll until done.

    Returns:
        (result_status, job_id, result)
        job_id is None if submission failed.
    """
    try:
        submit_result = coordinator.submit_job(
            resource_id="dataops",
            command=sql_for_submit,
            task_type="sql_validate",
            timeout_sec=timeout_sec,
        )
        job_id = submit_result.get("job_id")
        if submit_result.get("status") == "ERROR":
            return "error", None, {"error": submit_result.get("message") or "dataops submit_job failed"}
        if not job_id:
            return "error", None, {"error": "dataops submit_job returned no job_id"}

        result_status, result = await _poll_until_done(coordinator, job_id, timeout_sec, poll_interval)

        if result_status == "timed_out":
            return "timed_out", job_id, None

        if result is None:
            return "error", job_id, None

        return result_status, job_id, result

    except Exception as exc:  # noqa: BLE001
        return "error", None, {"error": f"dataops MCP call failed: {exc}"}


async def _handle_post_validate(
    coordinator,
    sql_for_submit: str,
    original_sql: str,
    user_account: str,
    table_suffix: str,
    resource: Any,
    runtime: Any,
    timeout_sec: int,
    poll_interval: float,
) -> dict[str, Any] | None:
    """Run post-validation for INSERT SQL: SELECT count(*) against temp table.

    Returns None if not an INSERT or if temp table extraction failed.
    Returns the post-validate dict with 'ok', 'count', 'error', etc.
    """
    # 统一门控:环境变量 DATAOPS_ENABLE_POST_VALIDATE=false 时完全跳过 count 校验
    if not _is_post_validate_enabled():
        logger.debug("[dataops_validate_sql] post-validate disabled by DATAOPS_ENABLE_POST_VALIDATE")
        return None

    if not _is_insert_sql(original_sql):
        return None

    original_query = ""
    if runtime is not None:
        original_query = str(getattr(runtime, "user_query", "") or "").strip()
        if not original_query:
            original_query = str(getattr(runtime, "parent_user_query", "") or "").strip()

    temp_table = _extract_insert_target_table(original_sql, user_account, table_suffix)
    if not temp_table:
        logger.debug("[dataops_validate_sql] post-validate skipped: could not extract target table")
        return None

    count_sql = _build_count_sql(sql_for_submit, _POST_VALIDATE_PARTITION_COL)
    if not count_sql:
        return None

    return await _run_post_validate(
        coordinator,
        count_sql,
        sql_for_submit,
        timeout_sec=timeout_sec,
        poll_interval=poll_interval,
        runtime=runtime,
        original_query=original_query,
    )


# ============================================================================
# Main validation entry points
# ============================================================================


async def dataops_validate_sql(
    sql: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate one SQL statement against the DataOps adHoc API.

    All SQL types (DDL, DML) are routed through the ``submit_job`` lifecycle:
    submit → poll → collect. The DataOps backend distinguishes CREATE TABLE
    vs INSERT/UPDATE/DELETE internally via the SQL text itself.

    de_agent is expected to call this AFTER it has finished the 8-category
    quality self-check on the generated SQL.

    Args:
        sql: SQL statement to validate.

    Returns:
        A dict with at least ``passed`` (bool).
        On failure, also includes:
        - ``error``: The error message from DataOps
        - ``job_id``: The DataOps job ID for this validation
        - ``log_file_info``: OBS log access info (url + headers) if available

        NOTE: This tool does NOT auto-fetch OBS log to avoid polluting context.
        Use ``dataops_validate_sql_with_log_analysis`` if you want log analysis.
    """
    runtime = _tool_context.runtime

    # --- Environment routing -------------------------------------------------
    env = _get_dataops_env()
    logger.debug(f"[dataops_validate_sql] env routing: DATAOPS_ENV={env!r} (set={bool(os.environ.get('DATAOPS_ENV'))})")
    if env == "prod":
        logger.debug("[dataops_validate_sql] routing to PROD path (official MCP flat flow)")
        return await _dataops_validate_sql_prod(sql, _tool_context=_tool_context)
    logger.debug("[dataops_validate_sql] routing to INTEGRATION path (resource-coordinator flow)")

    # --- Skip / setup ---
    skip_result, coordinator = _check_skip_conditions(sql, runtime)
    if skip_result is not None:
        return skip_result

    # --- Config ---
    resource = coordinator.catalog.get("dataops")
    POLL_INTERVAL_SEC = 30.0
    MAX_TIMEOUT_SEC = 30 * 60
    timeout_sec = min(int(resource.metadata.get("timeout_s", MAX_TIMEOUT_SEC)), MAX_TIMEOUT_SEC)
    poll_interval = max(POLL_INTERVAL_SEC, float(resource.metadata.get("poll_interval_ms", 30_000)) / 1000.0)

    user_account = resource.metadata.get("exec_user") or _get_user_account(runtime) or "anonymous"
    table_suffix = resource.metadata.get("table_suffix", "")

    # --- SQL rewrite ---
    sql_for_submit = _replace_target_table(sql, user_account, table_suffix)
    sql_for_submit = await _expand_cte_for_dml(sql_for_submit, timeout_sec=min(timeout_sec, 60), runtime=runtime)

    # --- Pre-submit: DROP temp table for CREATE TABLE (when enabled) ---
    drop_job_id: str | None = None
    if bool(resource.metadata.get("drop_before_create", False)):
        drop_job_id = await _pre_drop_for_create(
            coordinator, sql, user_account, table_suffix, timeout_sec, poll_interval
        )

    # --- Lifecycle ---
    result_status, job_id, result = await _submit_and_poll(coordinator, sql_for_submit, timeout_sec, poll_interval)

    if result_status == "timed_out":
        return {
            "passed": False,
            "error": f"dataops execution timed out after {timeout_sec}s, skipped check",
            "job_id": job_id,
            "timed_out": True,
        }

    if result_status == "error":
        err = result or {}
        return {"passed": False, "error": err.get("error", "unknown error"), "job_id": job_id}

    # --- Outcome ---
    if result_status == "completed":
        final_result: dict[str, Any] = {"passed": True, "job_id": job_id}
        if drop_job_id:
            final_result["drop_job_id"] = drop_job_id

        pv = await _handle_post_validate(
            coordinator,
            sql_for_submit,
            sql,
            user_account,
            table_suffix,
            resource,
            runtime,
            timeout_sec,
            poll_interval,
        )
        if pv is not None:
            _apply_post_validate_result(final_result, pv, resource)

        # NOTE: temp-table cleanup is NOT done here. Single-attempt
        # entry point must leave the adhoctemp.tmp_* table in place
        # so the corresponding DML (INSERT OVERWRITE) on the same table
        # can run. Cleanup is owned by the multi-attempt wrapper
        # (dataops_validate_sql_with_log_analysis), which knows whether
        # this is the last attempt and whether the SQL is DDL or DML.
        return final_result

    # Failed — return the failure as-is, no cleanup here either (see note above).
    return _build_failure_response(result_status, job_id, result, sql)


async def _pre_drop_for_create(
    coordinator,
    sql: str,
    user_account: str,
    table_suffix: str,
    timeout_sec: int,
    poll_interval: float,
) -> str | None:
    """For CREATE TABLE, drop existing temp table first to ensure clean creation.

    Returns the drop job_id if submitted, None otherwise.
    """
    if not _is_create_sql(sql):
        return None

    drop_sql = _build_drop_sql(sql, user_account, table_suffix)
    if not drop_sql:
        return None

    try:
        logger.debug(f"[dataops_validate_sql] Submitting DROP for CREATE TABLE: {drop_sql}")
        drop_result = coordinator.submit_job(
            resource_id="dataops",
            command=drop_sql,
            task_type="sql_validate",
            timeout_sec=min(timeout_sec, 60),
        )
        drop_job_id = drop_result.get("job_id")
        if drop_result.get("status") == "ERROR" or not drop_job_id:
            logger.warning(
                f"[dataops_validate_sql] DROP failed, continuing with CREATE anyway: "
                f"{drop_result.get('message', 'no job_id')}"
            )
            return None

        drop_status, _ = await _poll_until_done(coordinator, drop_job_id, min(timeout_sec, 60), poll_interval)
        if drop_status == "completed":
            logger.debug(f"[dataops_validate_sql] DROP completed successfully job_id={drop_job_id}")
        else:
            logger.warning(
                f"[dataops_validate_sql] DROP ended with status={drop_status}, "
                f"proceeding with CREATE anyway job_id={drop_job_id}"
            )
        return drop_job_id

    except Exception as drop_exc:
        logger.warning(f"[dataops_validate_sql] DROP submission failed, continuing: {drop_exc}")
        return None


async def _cleanup_temp_table(
    coordinator,
    sql: str,
    user_account: str,
    table_suffix: str,
    timeout_sec: int,
    poll_interval: float,
    *,
    context_label: str = "dataops_validate_sql",
) -> str | None:
    """Drop the adhoctemp temporary table after a validation attempt completes.

    Fire-and-forget: this runs on every attempt outcome (passed / failed /
    timed_out) to ensure the temp table doesn't accumulate in adhoctemp.
    Cleanup failures are logged but NEVER affect the validation result —
    the user has already received their pass/fail verdict.

    Gated by DATAOPS_DISABLE_TEMP_CLEANUP: when set to a truthy value
    (true/1/yes/on/y/t, case-insensitive), cleanup is skipped so the temp
    table is preserved for debugging. Default behavior (var unset or any
    other value) is cleanup ON.

    Returns the drop job_id if a DROP was submitted, None if no temp table
    was created (e.g. SELECT-only), extraction failed, or cleanup disabled.
    """
    if _is_temp_cleanup_disabled():
        logger.debug(
            f"[{context_label}] cleanup skipped: DATAOPS_DISABLE_TEMP_CLEANUP is set "
            f"(preserving adhoctemp table for inspection)"
        )
        return None

    temp_table = _extract_temp_table_name(sql, user_account, table_suffix)
    if not temp_table:
        logger.debug(f"[{context_label}] cleanup skipped: no temp table in SQL (SELECT / non-DML); sql={sql[:80]!r}")
        return None

    drop_sql = f"DROP TABLE IF EXISTS {temp_table}"
    try:
        logger.debug(f"[{context_label}] cleanup DROP submitting: {drop_sql}")
        # Cap cleanup timeout so it can't block validation indefinitely
        cleanup_timeout = min(timeout_sec, 60)
        submit_result = coordinator.submit_job(
            resource_id="dataops",
            command=drop_sql,
            task_type="sql_validate",
            timeout_sec=cleanup_timeout,
        )
        if submit_result.get("status") == "ERROR" or not submit_result.get("job_id"):
            logger.warning(
                f"[{context_label}] cleanup DROP submission failed: {submit_result.get('message', 'no job_id')}"
            )
            return None
        drop_job_id = submit_result["job_id"]
        drop_status, _ = await _poll_until_done(coordinator, drop_job_id, cleanup_timeout, poll_interval)
        if drop_status == "completed":
            logger.debug(f"[{context_label}] cleanup DROP completed table={temp_table} job_id={drop_job_id}")
        else:
            logger.warning(
                f"[{context_label}] cleanup DROP ended with status={drop_status} "
                f"table={temp_table} job_id={drop_job_id}"
            )
        return drop_job_id
    except Exception as cleanup_exc:
        logger.warning(f"[{context_label}] cleanup DROP exception table={temp_table}: {cleanup_exc}")
        return None


async def _cleanup_temp_table_prod(
    sql: str,
    user_account: str,
    table_suffix: str,
    timeout_sec: int,
) -> str | None:
    """Production counterpart of :func:`_cleanup_temp_table` — drops the adhoctemp
    temp table via official MCP after each validation attempt. Fire-and-forget.

    Same gating as the integration path: DATAOPS_DISABLE_TEMP_CLEANUP=true
    preserves the temp table for debugging.
    """
    if _is_temp_cleanup_disabled():
        logger.debug(
            "[dataops_validate_sql_prod] cleanup skipped: DATAOPS_DISABLE_TEMP_CLEANUP is set "
            "(preserving adhoctemp table for inspection)"
        )
        return None

    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_collect_result,
        prod_execute_sql,
        prod_get_query_result,
    )

    temp_table = _extract_temp_table_name(sql, user_account, table_suffix)
    if not temp_table:
        logger.debug(
            f"[dataops_validate_sql_prod] cleanup skipped: no temp table in SQL (SELECT / non-DML); sql={sql[:80]!r}"
        )
        return None

    drop_sql = f"DROP TABLE IF EXISTS {temp_table}"
    try:
        logger.debug(f"[dataops_validate_sql_prod] cleanup DROP submitting: {drop_sql}")
        submit_result = await prod_execute_sql(drop_sql)
        if not submit_result.get("success"):
            logger.warning(
                f"[dataops_validate_sql_prod] cleanup DROP submission failed: {submit_result.get('error', 'unknown')}"
            )
            return None
        drop_job_id = submit_result["job_id"]
        cleanup_timeout = min(timeout_sec, 60)
        deadline = asyncio.get_event_loop().time() + cleanup_timeout
        while asyncio.get_event_loop().time() < deadline:
            polled = await prod_get_query_result(drop_job_id)
            status = polled.get("status")
            if status in {"completed", "failed"}:
                await prod_collect_result(drop_job_id)
                if status == "completed":
                    logger.debug(
                        f"[dataops_validate_sql_prod] cleanup DROP completed table={temp_table} job_id={drop_job_id}"
                    )
                else:
                    logger.warning(
                        f"[dataops_validate_sql_prod] cleanup DROP failed table={temp_table} job_id={drop_job_id}"
                    )
                return drop_job_id
            await asyncio.sleep(5.0)
        logger.warning(f"[dataops_validate_sql_prod] cleanup DROP timed out table={temp_table} job_id={drop_job_id}")
        return drop_job_id
    except Exception as cleanup_exc:
        logger.warning(f"[dataops_validate_sql_prod] cleanup DROP exception table={temp_table}: {cleanup_exc}")
        return None


async def _cleanup_temp_table_wrapper(
    sql: str,
    *,
    _tool_context: ToolExecutionContext,
) -> str | None:
    """Cleanup dispatcher for ``dataops_validate_sql_with_log_analysis``.

    Routes to the integration- or production-cleanup helper based on
    DATAOPS_ENV, with shared gating (DATAOPS_DISABLE_TEMP_CLEANUP) and
    identical fire-and-forget semantics. Returns the drop job_id if a
    DROP was submitted, otherwise None.

    Note: this is only invoked by the wrapper, AFTER the wrapper has
    already determined that this is the final outcome (passed, or
    attempt >= MAX_VALIDATE_ATTEMPTS) and the SQL is DML (not DDL).
    """
    runtime = _tool_context.runtime
    if _get_dataops_env() == "prod":
        user_account = _get_user_account(runtime)
        table_suffix = os.environ.get("DATAOPS_TABLE_SUFFIX", "")
        timeout_sec = 30 * 60
        return await _cleanup_temp_table_prod(sql, user_account, table_suffix, timeout_sec)

    # Integration path
    resource = _get_dataops_resource(runtime)
    coordinator = runtime.ensure_resource_coordinator("dataops") if hasattr(runtime, "ensure_resource_coordinator") else None
    if coordinator is None:
        logger.debug(
            "[dataops_validate_sql_with_log_analysis] cleanup skipped: "
            "no resource coordinator available"
        )
        return None
    POLL_INTERVAL_SEC = 30.0
    MAX_TIMEOUT_SEC = 30 * 60
    timeout_sec = min(int(resource.metadata.get("timeout_s", MAX_TIMEOUT_SEC)), MAX_TIMEOUT_SEC)
    poll_interval = max(
        POLL_INTERVAL_SEC,
        float(resource.metadata.get("poll_interval_ms", 30_000)) / 1000.0,
    )
    user_account = resource.metadata.get("exec_user") or _get_user_account(runtime) or "anonymous"
    table_suffix = resource.metadata.get("table_suffix", "")
    return await _cleanup_temp_table(
        coordinator,
        sql,
        user_account,
        table_suffix,
        timeout_sec,
        poll_interval,
        context_label="dataops_validate_sql_with_log_analysis",
    )


def _apply_post_validate_result(final_result: dict[str, Any], pv: dict[str, Any], resource: Any) -> None:
    """Merge post-validate result into final_result, mutating final_result in place."""
    if not pv["ok"]:
        final_result["passed"] = False
        final_result["error"] = f"post_validate error: {pv['error']}"
        return

    if pv["count"] == 0:
        skip_count_zero = bool(resource.metadata.get("skip_post_validate_count_zero", False))
        if skip_count_zero:
            final_result["passed"] = True
            return

        if pv.get("has_mismatch") and pv.get("fix_suggestion"):
            final_result["passed"] = False
            final_result["error"] = (
                f"INSERT validated but target table is empty (0 rows on "
                f"{_POST_VALIDATE_PARTITION_COL}=<today>): "
                f"{pv['mismatch_reason']}"
            )
            final_result["count_zero_analysis"] = {
                "has_mismatch": True,
                "mismatch_reason": pv["mismatch_reason"],
                "fix_suggestion": pv["fix_suggestion"],
            }
        else:
            final_result["passed"] = False
            final_result["error"] = (
                f"INSERT validated but target table is empty (0 rows on {_POST_VALIDATE_PARTITION_COL}=<today>)"
            )


async def dataops_validate_sql_with_log_analysis(
    sql: str,
    *,
    attempt: int = 1,
    prior_attempts: list[dict[str, Any]] | None = None,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate SQL and fetch + analyze OBS log on failure in a single tool call.

    This is a convenience wrapper that combines ``dataops_validate_sql`` with
    in-process log analysis. Use this when you want structured error analysis
    without polluting the main context with raw log content.

    The in-process analyzer handles:
    1. Fetching OBS log using log_file_info
    2. Analyzing log content with LLM
    3. Returning structured error analysis (error_type, location, suggestions)

    **Retry contract (de_agent invariant):**
    - Per SQL, the caller may invoke this at most ``MAX_VALIDATE_ATTEMPTS`` (3)
      times (including the first call). Each invocation must pass
      ``attempt`` = 1, 2, or 3, and ``prior_attempts`` = the ``attempts`` list
      returned by the previous call (empty list on first call).
    - When ``attempt > MAX_VALIDATE_ATTEMPTS`` the call is rejected without
      touching DataOps; the caller must not initiate a 4th call.
    - When the final attempt (``attempt == MAX_VALIDATE_ATTEMPTS``) still
      returns ``passed: false``, this wrapper auto-dumps
      ``validate_<table>.json`` + ``delivery_warning.md`` to the workspace
      and adds ``failed_after_max_retries: true`` to the response.

    Args:
        sql: SQL statement to validate.
        attempt: 1-indexed attempt number for this call (caller-managed).
        prior_attempts: Returned ``attempts`` list from the previous call
            (empty/None on first call).

    Returns:
        Same as ``dataops_validate_sql``, but on failure also includes:
        - ``log_analysis``: Structured error analysis (error_type, error_message,
          location, suggestions)
        - ``log_analysis_summary``: Short summary for display (not full log)
        - ``attempts``: Running list of attempt summaries (always returned;
          caller must echo this back as ``prior_attempts`` on the next call)
        - On max retries: ``failed_after_max_retries: true``,
          ``validation_report_path``: report file path,
          ``delivery_warning_path``: warning file path
    """
    start_time = time.time()
    prior_attempts = list(prior_attempts or [])

    # --- Retry guard: reject attempts beyond MAX_VALIDATE_ATTEMPTS -------------
    if not isinstance(attempt, int) or attempt < 1:
        return {
            "passed": False,
            "failed_after_max_retries": True,
            "error": f"attempt must be a positive int (got {attempt!r})",
            "attempts": prior_attempts,
        }
    if attempt > MAX_VALIDATE_ATTEMPTS:
        logger.warning(
            f"[dataops_validate_sql_with_log_analysis] attempt={attempt} exceeds "
            f"MAX_VALIDATE_ATTEMPTS={MAX_VALIDATE_ATTEMPTS}; refusing. "
            f"Caller must not initiate a 4th call."
        )
        return {
            "passed": False,
            "failed_after_max_retries": True,
            "error": (f"exceeded MAX_VALIDATE_ATTEMPTS={MAX_VALIDATE_ATTEMPTS}; caller must not initiate a 4th call"),
            "attempts": prior_attempts,
        }

    logger.debug(
        f"[dataops_validate_sql_with_log_analysis] === START attempt={attempt} "
        f"prior_attempts={len(prior_attempts)} sql={sql[:100]}"
    )

    # Step 1: Validate SQL
    logger.debug("[dataops_validate_sql_with_log_analysis] Calling dataops_validate_sql...")
    validate_result = await dataops_validate_sql(sql, _tool_context=_tool_context)
    logger.debug(
        "[dataops_validate_sql_with_log_analysis] Validate done passed={} job_id={}".format(
            validate_result.get("passed"),
            validate_result.get("job_id"),
        ),
    )

    # If passed, return immediately
    if validate_result.get("passed"):
        elapsed = time.time() - start_time
        logger.debug(
            f"[dataops_validate_sql_with_log_analysis] === END (passed) attempt={attempt} elapsed={elapsed:.2f}s ==="
        )
        # Cleanup: only drop the temp table when the SQL is DML (not DDL)
        # and this is the final outcome. CREATE TABLE leaves the temp
        # table in place so the corresponding INSERT OVERWRITE can target it.
        if not _is_create_sql(sql):
            cleanup_job_id = await _cleanup_temp_table_wrapper(
                sql, _tool_context=_tool_context
            )
            if cleanup_job_id:
                validate_result["cleanup_job_id"] = cleanup_job_id
        return validate_result

    # If failed but neither log_file_info (integration) nor inline errorLog (prod)
    # is available, return as-is — there's nothing to analyze.
    log_file_info = validate_result.get("log_file_info")
    inline_error_log = validate_result.get("errorLog") or ""
    if not log_file_info and not inline_error_log:
        logger.debug(
            "[dataops_validate_sql_with_log_analysis] Validation failed but no log source "
            "job_id={} log_file_info_present={} errorLog_present={}".format(
                validate_result.get("job_id"),
                bool(log_file_info),
                bool(inline_error_log),
            ),
        )
        elapsed = time.time() - start_time
        logger.debug(
            f"[dataops_validate_sql_with_log_analysis] === END (no log_info) "
            f"attempt={attempt} elapsed={elapsed:.2f}s ==="
        )
        return validate_result

    job_id = validate_result.get("job_id")
    raw_error = validate_result.get("error", "")

    logger.debug(
        "[dataops_validate_sql_with_log_analysis] Validation FAILED job_id={!r} error={!r} ".format(
            job_id,
            (raw_error or "")[:200],
        ),
    )

    # Step 2: Analyze error context with LLM in-process. Two source modes:
    #   - integration: log_file_info (url + headers) → fetch OBS log → analyze
    #   - prod: inline errorLog (already on the response) → analyze directly
    # The raw log and LLM messages stay private to this tool invocation, so the
    # main agent's chat history is never polluted with raw log content. Only the
    # structured analysis (error_type, error_message, location, suggestions) is returned.
    fetch_and_analyze = await _analyze_log_directly(
        log_file_info if log_file_info else None,
        job_id,
        raw_error,
        raw_log=inline_error_log if (not log_file_info and inline_error_log) else None,
        _tool_context=_tool_context,
    )
    direct_status = fetch_and_analyze.get("status")
    logger.debug(
        f"[dataops_validate_sql_with_log_analysis] In-process analysis job_id={job_id!r} "
        f"status={direct_status!r} elapsed_tool={time.time() - start_time:.2f}s",
    )

    if direct_status == "ok":
        analysis = fetch_and_analyze["analysis"]
        validate_result["log_analysis"] = analysis
        logger.debug(
            "[dataops_validate_sql_with_log_analysis] Analysis extracted job_id={!r} "
            "error_type={!r} suggestions_count={}".format(
                job_id,
                analysis.get("error_type"),
                len(analysis.get("suggestions", [])),
            ),
        )
        validate_result["log_analysis_summary"] = _make_analysis_summary(analysis, raw_error)
    else:
        # fetch_error or llm_error — record but don't fail the validation result itself
        validate_result["log_analysis_error"] = fetch_and_analyze.get("error", "unknown_analysis_error")
        logger.debug(
            "[dataops_validate_sql_with_log_analysis] In-process analysis failed job_id={!r} error={!r}".format(
                job_id, validate_result["log_analysis_error"]
            ),
        )

    # --- Append this attempt's summary to the running history -----------------
    # Strip bulky fields (log_file_info / log analysis raw text) before storing;
    # the caller will echo this back as prior_attempts, so keep it compact.
    attempt_summary = _summarize_attempt(attempt, validate_result, start_time)
    attempts = prior_attempts + [attempt_summary]
    validate_result["attempts"] = attempts

    # --- Max-retries reached → dump validate_<table>.json + delivery_warning.md
    if attempt >= MAX_VALIDATE_ATTEMPTS:
        validate_result["failed_after_max_retries"] = True
        report_paths = _dump_validation_artifacts(
            sql=sql,
            attempts=attempts,
            _tool_context=_tool_context,
        )
        if report_paths.get("validation_report_path"):
            validate_result["validation_report_path"] = report_paths["validation_report_path"]
        if report_paths.get("delivery_warning_path"):
            validate_result["delivery_warning_path"] = report_paths["delivery_warning_path"]
        logger.warning(
            f"[dataops_validate_sql_with_log_analysis] MAX_VALIDATE_ATTEMPTS reached "
            f"({attempt}/{MAX_VALIDATE_ATTEMPTS}); best-effort artifacts written. "
            f"report={report_paths.get('validation_report_path')} "
            f"warning={report_paths.get('delivery_warning_path')}"
        )
        # Cleanup: after exhausting all retries on a DML, drop the temp table.
        # DDL is NEVER dropped here — its temp table must remain for the
        # matching DML (which may run on a separate wrapper invocation).
        if not _is_create_sql(sql):
            cleanup_job_id = await _cleanup_temp_table_wrapper(
                sql, _tool_context=_tool_context
            )
            if cleanup_job_id:
                validate_result["cleanup_job_id"] = cleanup_job_id

    # Reorder fields so analysis comes before log_file_info (the latter is bulky and
    # easily truncated when the tool result is displayed in chat history). Putting
    # log_analysis_summary first keeps the actionable info visible.
    job_id = validate_result.pop("job_id", None)
    passed = validate_result.pop("passed", None)
    error = validate_result.pop("error", None)
    skipped = validate_result.pop("skipped", None)
    reason = validate_result.pop("reason", None)

    ordered_result: dict[str, Any] = {
        "passed": passed,
        "error": error,
        "job_id": job_id,
    }
    if skipped is not None:
        ordered_result["skipped"] = skipped
    if reason is not None:
        ordered_result["reason"] = reason

    # attempts / failed_after_max_retries / paths must surface before log_file_info
    for key in (
        "attempts",
        "failed_after_max_retries",
        "validation_report_path",
        "delivery_warning_path",
    ):
        if key in validate_result:
            ordered_result[key] = validate_result.pop(key)

    # log_analysis / log_analysis_summary / log_analysis_error sit before the
    # verbose log_file_info block so they remain visible even if the result is truncated.
    for key in ("log_analysis", "log_analysis_summary", "log_analysis_error"):
        if key in validate_result:
            ordered_result[key] = validate_result.pop(key)

    # log_file_info last (may be large; truncates first when chat scrolls)
    for key, value in validate_result.items():
        ordered_result[key] = value

    elapsed = time.time() - start_time
    logger.debug(
        f"[dataops_validate_sql_with_log_analysis] === END (failed with analysis) "
        f"attempt={attempt} elapsed={elapsed:.2f}s ==="
    )
    return ordered_result


def _parse_analysis_from_text(text: str) -> dict[str, Any]:
    """Parse structured analysis from text response."""
    logger.debug(f"[_parse_analysis_from_text] text len={len(text)}")
    analysis = {"error_type": "unknown", "error_message": "", "location": {}, "suggestions": []}

    # Try to extract error type
    for et in [
        "AnalysisException",
        "ParseException",
        "TableNotFoundException",
        "ColumnNotFoundException",
        "SemanticException",
    ]:
        if et in text:
            analysis["error_type"] = et
            logger.debug(f"[_parse_analysis_from_text] Detected error_type={et!r}")
            break

    # Extract suggestions from numbered lists or bullet points
    suggestion_pattern = re.compile(r"(?:^|\n)\s*(?:[-*]|\d+\.)\s*(.+?)(?=\n|$)", re.MULTILINE)
    matches = suggestion_pattern.findall(text)
    if matches:
        analysis["suggestions"] = [s.strip() for s in matches[:10]]
        logger.debug(f"[_parse_analysis_from_text] Extracted {len(matches)} suggestions")

    # Use first few lines as error message
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if lines:
        analysis["error_message"] = lines[0][:500]
        logger.debug(f"[_parse_analysis_from_text] First line (error_message): {lines[0][:100]!r}")

    return analysis


async def _analyze_log_directly(
    log_file_info: dict[str, Any] | None,
    job_id: str,
    raw_error: str,
    *,
    raw_log: str | None = None,
    _tool_context: ToolExecutionContext | None = None,
) -> dict[str, Any]:
    """Analyze an error log with LLM in-process.

    Two source modes (mutually exclusive):
      - integration: ``log_file_info`` is set → fetch OBS log via URL+headers.
      - prod: ``raw_log`` is set → use the inline ``errorLog`` returned by the
        official MCP directly (no OBS hop).

    Runs inside the same tool invocation so the raw log never enters chat history.
    Only the structured analysis (error_type, error_message, location, suggestions)
    is returned to the caller.

    LLM resolution order (first non-None wins):
      1. ``_tool_context.runtime.llm("planner")`` — reuse the main agent's LLM
      2. ``llm_manager.get_default_llm()``        — module-level singleton
      3. ``_parse_analysis_from_text(...)``        — pure-regex fallback

    Returns:
        dict with keys: status, analysis (optional), error (optional).
    """
    logger.debug(f"[_analyze_log_directly] START job_id={job_id!r} source={'inline_error_log' if raw_log else 'obs'}")

    # 1) Resolve raw log content (private to this function — never returned to main agent)
    if raw_log:
        # prod path: official MCP returned errorLog inline, skip OBS fetch entirely
        log_content = raw_log
        logger.debug(
            f"[_analyze_log_directly] using inline errorLog job_id={job_id!r} log_len={len(log_content)}",
        )
    else:
        # integration path: fetch OBS log via URL+headers
        fetch_result = await dataops_fetch_obs_log(log_file_info, _tool_context=None)  # type: ignore[arg-type]
        status = fetch_result.get("status")
        if status != "success":
            logger.debug(
                f"[_analyze_log_directly] OBS fetch failed job_id={job_id!r} "
                f"fetch_status={status!r} error={fetch_result.get('error')!r}",
            )
            return {
                "status": "fetch_error",
                "error": fetch_result.get("error", "failed to fetch log from OBS"),
            }
        log_content = fetch_result.get("log_content", "") or ""
        logger.debug(
            f"[_analyze_log_directly] OBS fetched job_id={job_id!r} log_len={len(log_content)}",
        )

    # Truncate to keep LLM prompt bounded. Spark logs are usually < 50KB; cap at 32KB.
    truncated_log = log_content[:32_000]
    if len(log_content) > 32_000:
        logger.debug(
            f"[_analyze_log_directly] Log truncated job_id={job_id!r} original_len={len(log_content)} kept=32000",
        )

    # 2) LLM analysis — run in thread to avoid blocking event loop
    system_prompt = (
        "You are a Spark SQL / DataOps log analyzer. Given an execution log and an original "
        "error message, produce a structured JSON object with EXACTLY these keys:\n"
        "{\n"
        '  "error_type": "<AnalysisException|ParseException|TableNotFoundException|'
        'ColumnNotFoundException|SemanticException|unknown>",\n'
        '  "error_message": "<one sentence, <= 200 chars>",\n'
        '  "location": {"tables": ["..."], "columns": ["..."], "functions": ["..."], "line": "<n> or null"},\n'
        '  "suggestions": ["<actionable fix>", ...]  // max 5 items, each <= 120 chars\n'
        "}\n"
        "Output ONLY the JSON object, no prose, no markdown fences."
    )
    user_prompt = (
        f"job_id={job_id}\nraw_error={raw_error[:500]}\n=== EXECUTION LOG ===\n{truncated_log}\n=== END LOG ==="
    )

    # Resolve LLM: prefer the main agent's LLM (same model, no extra instantiation)
    llm = None
    llm_source = "none"
    runtime = getattr(_tool_context, "runtime", None) if _tool_context is not None else None
    if runtime is not None:
        runtime_llm_getter = getattr(runtime, "llm", None)
        if callable(runtime_llm_getter):
            try:
                llm = runtime_llm_getter("planner")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[_analyze_log_directly] runtime.llm('planner') failed: {exc}")
            llm_source = "runtime.planner"

    if llm is None:
        try:
            llm = llm_manager.get_default_llm()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"[_analyze_log_directly] llm_manager.get_default_llm() failed: {exc}",
            )
        llm_source = "llm_manager"

    if llm is None:
        logger.debug(
            f"[_analyze_log_directly] No LLM available job_id={job_id!r}, falling back to regex",
        )
        analysis = _parse_analysis_from_text(truncated_log)
        return {"status": "ok", "analysis": analysis}

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        logger.debug(
            f"[_analyze_log_directly] Invoking LLM job_id={job_id!r} source={llm_source} prompt_len={len(user_prompt)}",
        )
        response = await asyncio.to_thread(llm.invoke, messages)
        raw_text = str(getattr(response, "content", response) or "").strip()
        logger.debug(
            f"[_analyze_log_directly] LLM responded job_id={job_id!r} response_len={len(raw_text)}",
        )

        # Strip code fences if present
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE).strip()
        raw_text = re.sub(r"\s*```$", "", raw_text).strip()

        # Try to parse JSON
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.debug(
                f"[_analyze_log_directly] LLM output not JSON, falling back to text parse "
                f"job_id={job_id!r} text[:200]={raw_text[:200]!r}",
            )
            parsed = _parse_analysis_from_text(raw_text)

        # Normalize into the schema consumed by _make_analysis_summary
        analysis = {
            "error_type": parsed.get("error_type", "unknown"),
            "error_message": str(parsed.get("error_message", ""))[:500],
            "location": parsed.get("location", {}) if isinstance(parsed.get("location"), dict) else {},
            "suggestions": [str(s)[:120] for s in (parsed.get("suggestions") or [])[:5]],
        }
        logger.debug(
            f"[_analyze_log_directly] Analysis completed job_id={job_id!r} "
            f"error_type={analysis['error_type']!r} suggestions_count={len(analysis['suggestions'])}",
        )
        return {"status": "ok", "analysis": analysis}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[_analyze_log_directly] LLM analysis failed job_id={job_id!r}: {exc}")
        return {"status": "llm_error", "error": str(exc)}


def _make_analysis_summary(analysis: dict[str, Any], raw_error: str = "") -> str:
    """Create a short summary from analysis for display purposes."""
    logger.debug("[_make_analysis_summary] building summary for analysis")
    parts = []

    error_type = analysis.get("error_type", "")
    if error_type and error_type != "unknown":
        parts.append(f"错误类型: {error_type}")

    error_message = analysis.get("error_message", "")
    if error_message:
        parts.append(f"错误信息: {error_message[:200]}")

    location = analysis.get("location", {})
    if location:
        locations = []
        if location.get("tables"):
            locations.append(f"表: {', '.join(location['tables'][:3])}")
        if location.get("columns"):
            locations.append(f"字段: {', '.join(location['columns'][:3])}")
        if locations:
            parts.append(" | ".join(locations))

    suggestions = analysis.get("suggestions", [])
    if suggestions:
        parts.append(f"建议: {suggestions[0][:100]}")

    if not parts:
        parts.append(f"原始错误: {raw_error[:200]}" if raw_error else "未知错误")

    summary = " | ".join(parts)
    logger.debug(f"[_make_analysis_summary] Final summary: {summary[:200]!r}")
    return summary


async def dataops_fetch_obs_log(
    log_file_info: dict[str, Any],
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Fetch execution log directly from OBS storage using pre-signed URL.

    This tool directly calls OBS with the pre-signed URL and headers provided
    in the log_file_info. No MCP call needed.

    Args:
        log_file_info: A dict containing 'url' and 'headers' from the poll result.
            Example:
            {
                "url": "https://obs.cn-north-4.myhuaweicloud.cn/...",
                "headers": {"Authorization": "AWS4-HMAC-SHA256 ...", ...}
            }

    Returns:
        A dict with at least ``status``. On success, ``log_content`` contains
        the full execution log. On failure, ``error`` explains why.
    """
    import httpx

    # Resolve URL first, then resolve the real HTTP headers (preferring the
    # inner ``headers.headers`` so synthetic keys like "url"/"method" don't
    # leak into the request).
    outer_headers = log_file_info.get("headers") or {}
    if not isinstance(outer_headers, dict):
        outer_headers = {}
    inner_headers = outer_headers.get("headers")
    if not isinstance(inner_headers, dict):
        inner_headers = None

    url = str(log_file_info.get("url") or "").strip() or str(outer_headers.get("url") or "").strip()
    if not url:
        logger.debug(
            f"[dataops_fetch_obs_log] missing 'url' in log_file_info "
            f"top_level_keys={list(log_file_info.keys()) if isinstance(log_file_info, dict) else None} "
            f"headers_keys={list(outer_headers.keys()) if isinstance(outer_headers, dict) else None}",
        )
        return {
            "status": "error",
            "error": "log_file_info missing 'url' (no top-level 'url' and no headers.url)",
        }

    # Decide which dict carries the real HTTP headers: inner (V3) > outer (V1/V2)
    headers = inner_headers if inner_headers is not None else outer_headers
    if not headers:
        logger.debug(f"[dataops_fetch_obs_log] missing 'headers' in log_file_info url={url}")
        return {"status": "error", "error": "log_file_info missing 'headers'"}

    # Mask the Authorization header value when logging. Also drop synthetic
    # metadata keys (e.g. nested-scheme's "url", "method") so we don't forward
    # them as HTTP headers.
    _NON_HTTP_HEADER_KEYS = {"url", "method"}
    safe_headers = {
        k: ("***MASKED***" if k.lower() in {"authorization", "x-auth-token"} else v)
        for k, v in headers.items()
        if k.lower() not in _NON_HTTP_HEADER_KEYS
    }
    forward_headers = {k: v for k, v in headers.items() if k.lower() not in _NON_HTTP_HEADER_KEYS}
    logger.debug(
        f"[dataops_fetch_obs_log] GET obs url={url} headers={safe_headers} timeout=60s",
    )
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.get(url, headers=forward_headers)
            content_len = len(response.text or "")
            logger.debug(
                f"[dataops_fetch_obs_log] OBS responded status={response.status_code}"
                f" content_length={content_len} url={url}",
            )
            if response.status_code >= 400:
                logger.debug(
                    f"[dataops_fetch_obs_log] OBS HTTP error status={response.status_code}"
                    f" body_excerpt={response.text[:500]!r} url={url}",
                )
                return {
                    "status": "error",
                    "error": f"OBS returned HTTP {response.status_code}: {response.text[:500]}",
                }
            return {
                "status": "success",
                "log_content": response.text,
            }
    except httpx.HTTPError as exc:
        logger.debug(
            f"[dataops_fetch_obs_log] HTTP error url={url}]",
            exc_info=True,
        )
        return {"status": "error", "error": f"Failed to fetch log from OBS: {exc}"}


# ============================================================================
# Production environment (扁平路径,不走 resource-coordinator)
# ============================================================================

_PROD_ENV = "prod"
_VALID_DATAOPS_ENVS = frozenset({"prod", "integration"})

# 控制是否在 INSERT 校验通过后再跑一次 SELECT count(*) 后置校验。
# 默认 True（保持现状,向后兼容）。关闭后,联调和生产两条路径都不再触发 count SQL。
# 接受 (case-insensitive) true/false/1/0/yes/no/on/off,其它值按 True 处理。
_DISABLE_POST_VALIDATE_VALUES = frozenset({"false", "0", "no", "off", "n", "f"})


def _is_post_validate_enabled() -> bool:
    """读取 DATAOPS_ENABLE_POST_VALIDATE,默认 True(向后兼容)。

    Returns:
        True  -> 跑 SELECT count(*) 后置校验(默认)
        False -> 完全跳过 count SQL 的 submit/poll/collect
    """
    raw = os.environ.get("DATAOPS_ENABLE_POST_VALIDATE", "").strip().lower()
    if not raw:
        return True
    return raw not in _DISABLE_POST_VALIDATE_VALUES


# 控制是否在每次校验后自动 DROP adhoctemp 临时表。
# 默认 False（保持现状,自动清理开启）。开启后会保留临时表方便调试,但会在 adhoctemp 库累积。
# 接受 (case-insensitive) true/false/1/0/yes/no/on/off,其它值按 False 处理。
_ENABLE_KEEP_TEMP_VALUES = frozenset({"true", "1", "yes", "on", "y", "t"})


def _is_temp_cleanup_disabled() -> bool:
    """读取 DATAOPS_DISABLE_TEMP_CLEANUP,默认 False(自动清理开启,默认行为)。

    Returns:
        True  -> 保留临时表(关闭自动清理,方便调试 adhoctemp 残留)
        False -> 自动 DROP(默认)
    """
    raw = os.environ.get("DATAOPS_DISABLE_TEMP_CLEANUP", "").strip().lower()
    if not raw:
        return False
    return raw in _ENABLE_KEEP_TEMP_VALUES


def _get_dataops_env() -> str:
    """读取 DATAOPS_ENV,默认 integration(保持向后兼容).

    Returns:
        "prod" or "integration"
    """
    raw = os.environ.get("DATAOPS_ENV", "integration").strip().lower()
    if raw not in _VALID_DATAOPS_ENVS:
        logger.warning(f"[dataops_validate_sql] Unknown DATAOPS_ENV={raw!r}, falling back to 'integration'")
        return "integration"
    return raw


async def _dataops_validate_sql_prod(
    sql: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Production-environment validation entry point.

    与联调路径完全独立:不走 submit -> poll -> collect,直接调官方 MCP 工具。
    """
    runtime = _tool_context.runtime

    # --- Empty SQL shortcut ---
    sql = (sql or "").strip()
    if not sql:
        return {"passed": True, "skipped": True, "reason": "empty sql"}

    # --- Config ---
    POLL_INTERVAL_SEC = 5.0  # 比联调(30s)短,官方 MCP 无 per-poll 开销
    MAX_TIMEOUT_SEC = 30 * 60
    timeout_sec = MAX_TIMEOUT_SEC
    poll_interval = POLL_INTERVAL_SEC

    user_account = _get_user_account(runtime)
    table_suffix = os.environ.get("DATAOPS_TABLE_SUFFIX", "")

    # --- SQL rewrite (与联调相同) ---
    sql_for_submit = _replace_target_table(sql, user_account, table_suffix)
    sql_for_submit = await _expand_cte_for_dml(sql_for_submit, timeout_sec=min(timeout_sec, 60), runtime=runtime)

    # --- Execute -> poll -> collect (扁平函数) ---
    result_status, job_id, result = await _submit_and_poll_prod(sql_for_submit, timeout_sec, poll_interval)

    # --- Outcome dispatch ---
    if result_status == "timed_out":
        return {
            "passed": False,
            "error": f"dataops(prod) execution timed out after {timeout_sec}s",
            "job_id": job_id,
            "timed_out": True,
        }
    if result_status == "error":
        err = result or {}
        return {"passed": False, "error": err.get("error", "unknown error"), "job_id": job_id}

    if result_status == "completed":
        final_result: dict[str, Any] = {"passed": True, "job_id": job_id}
        pv = await _run_post_validate_prod(
            sql_for_submit,
            sql,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
            runtime=runtime,
        )
        if pv is not None:
            _apply_post_validate_result_prod(final_result, pv)
        # NOTE: temp-table cleanup is NOT done here. Single-attempt
        # entry point must leave the adhoctemp.tmp_* table in place
        # so the corresponding DML on the same table can run.
        # Cleanup is owned by the multi-attempt wrapper.
        return final_result

    # failed — return as-is, no cleanup here either (see note above).
    return _build_failure_response_prod(result_status, job_id, result, sql)


async def _submit_and_poll_prod(
    sql_for_submit: str,
    timeout_sec: int,
    poll_interval: float,
) -> tuple[str, str | None, dict | None]:
    """Execute -> poll-until-done for production (扁平实现).

    与联调版本的核心区别:
        - 联调:submit_job -> 循环 poll -> collect(走 HMAC-SHA256 OpenAPI)
        - 生产:execute_sql -> 循环 get_query_result -> 直接返回结果(走官方 MCP)
    """
    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_collect_result,
        prod_execute_sql,
        prod_get_query_result,
    )

    # Step 1: 提交 SQL
    try:
        submit_result = await prod_execute_sql(sql_for_submit)
        if not submit_result["success"]:
            return "error", None, {"error": submit_result.get("error") or "execute_sql failed"}
        job_id = submit_result["job_id"]
    except Exception as exc:
        return "error", None, {"error": f"dataops(prod) MCP execute_sql failed: {exc}"}

    # Step 2: 轮询直到 terminal 状态
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        polled = await prod_get_query_result(job_id)
        status = polled["status"]
        if status in {"completed", "failed"}:
            # Step 3: 到达 terminal,取最终结果(含 inline data 或 errorLog)
            result = await prod_collect_result(job_id)
            return status, job_id, result
        if status == "error":
            return "error", job_id, {"error": polled.get("error", "poll error")}
        await asyncio.sleep(poll_interval)

    return "timed_out", job_id, None


async def _run_post_validate_prod(
    sql_for_submit: str,
    original_sql: str,
    *,
    timeout_sec: int,
    poll_interval: float,
    runtime: Any,
) -> dict[str, Any] | None:
    """生产端后置验证:count 校验 + 0 行 LLM 分析。

    与联调版 _run_post_validate 逻辑对齐,但执行层走生产 MCP 扁平函数(不耦合 coordinator)。
    """
    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_collect_result,
        prod_execute_sql,
        prod_get_query_result,
    )

    # 统一门控:环境变量 DATAOPS_ENABLE_POST_VALIDATE=false 时完全跳过 count 校验
    # 与联调路径读取同一个开关,避免联调/生产行为分叉
    if not _is_post_validate_enabled():
        logger.debug("[dataops_validate_sql_prod] post-validate disabled by DATAOPS_ENABLE_POST_VALIDATE")
        return None

    if not _is_insert_sql(original_sql):
        return None

    count_sql = _build_count_sql(sql_for_submit, _POST_VALIDATE_PARTITION_COL)
    if not count_sql:
        return None

    count_timeout = min(timeout_sec, 120)

    # Submit count query
    try:
        submit_result = await prod_execute_sql(count_sql)
        if not submit_result["success"]:
            return {
                "ok": False,
                "count": None,
                "error": submit_result.get("error") or "count submit failed",
                "job_id": None,
            }
        count_job_id = submit_result["job_id"]
    except Exception as exc:
        return {"ok": False, "count": None, "error": f"count submit error: {exc}", "job_id": None}

    # Poll count query
    deadline = asyncio.get_event_loop().time() + count_timeout
    while asyncio.get_event_loop().time() < deadline:
        polled = await prod_get_query_result(count_job_id)
        if polled["status"] == "completed":
            count_result = await prod_collect_result(count_job_id)
            break
        if polled["status"] == "error":
            return {
                "ok": False,
                "count": None,
                "error": polled.get("error", "count poll error"),
                "job_id": count_job_id,
            }
        await asyncio.sleep(poll_interval)
    else:
        return {
            "ok": False,
            "count": None,
            "error": f"count query timed out after {count_timeout}s",
            "job_id": count_job_id,
        }

    # Parse count
    count = _parse_count_from_collect(count_result)
    if count is None:
        return {
            "ok": False,
            "count": None,
            "error": "count result parse failed",
            "job_id": count_job_id,
            "has_mismatch": False,
            "mismatch_reason": "",
            "fix_suggestion": "",
        }

    # 0 行 LLM 分析(复用联调版 LLM 调用)
    if count == 0:
        original_query = ""
        if runtime is not None:
            original_query = str(getattr(runtime, "user_query", "") or "").strip()
            if not original_query:
                original_query = str(getattr(runtime, "parent_user_query", "") or "").strip()
        if original_query:
            analysis = await _analyze_count_zero_with_llm(original_query, original_sql or count_sql, runtime=runtime)
            return {
                "ok": True,
                "count": 0,
                "error": None,
                "job_id": count_job_id,
                **analysis,
            }

    return {
        "ok": True,
        "count": count,
        "error": None,
        "job_id": count_job_id,
        "has_mismatch": False,
        "mismatch_reason": "",
        "fix_suggestion": "",
    }


def _apply_post_validate_result_prod(
    final_result: dict[str, Any],
    pv: dict[str, Any],
) -> None:
    """将后置验证结果合并到生产路径的 final_result(与联调版镜像,不需要 resource)。"""
    if pv.get("error") and not pv.get("ok"):
        # 验证出错时,不阻塞上层(参考联调版逻辑,只 attach 字段)
        final_result["post_validate"] = {
            "ok": False,
            "error": pv.get("error"),
            "job_id": pv.get("job_id"),
        }
        return

    final_result["post_validate"] = {
        "ok": pv.get("ok", True),
        "count": pv.get("count"),
        "job_id": pv.get("job_id"),
    }
    if pv.get("has_mismatch"):
        final_result["post_validate"]["has_mismatch"] = True
        final_result["post_validate"]["mismatch_reason"] = pv.get("mismatch_reason", "")
        final_result["post_validate"]["fix_suggestion"] = pv.get("fix_suggestion", "")


def _build_failure_response_prod(
    result_status: str,
    job_id: str,
    result: dict,
    sql: str,
) -> dict[str, Any]:
    """生产端失败响应(参考联调版 _build_failure_response,但不依赖 OBS logFileInfo)。

    errorLog 直接从响应里取(生产端内联,无需 OBS)。
    """
    error_msg = (result or {}).get("error") or f"dataops(prod) rejected SQL (status={result_status})"
    error_log = (result or {}).get("errorLog") or ""
    response_data: dict[str, Any] = {
        "passed": False,
        "error": error_msg,
        "job_id": job_id,
    }
    if error_log:
        response_data["errorLog"] = error_log
        response_data["log_source"] = "inline"
    return response_data


# ============================================================================
# Retry helpers (shared by dataops_validate_sql_with_log_analysis)
# ============================================================================


def _extract_original_target_table(sql: str) -> str | None:
    """Extract the target table reference (db.table) from a SQL statement.

    Used purely to derive a filename for ``validate_<table>.json``. Falls back
    to ``None`` when no INSERT/CREATE clause is found; the caller must then
    use a generic filename like ``validate_unknown.json``.

    NOTE: This parses the *original* (pre-rewrite) SQL so the report filename
    matches the user-visible table, not the adhoctemp rewriting.
    """
    if not sql:
        return None
    sql_no_comment = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    patterns = [
        # INSERT OVERWRITE TABLE db.tbl PARTITION ...
        r"INSERT\s+(?:OVERWRITE|INTO)\s+(?:EXTERNAL\s+)?(?:TABLE\s+)?([`\w.-]+(?:\.[`\w.-]+)+)",
        # CREATE [EXTERNAL] TABLE [IF NOT EXISTS] db.tbl
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\w.-]+(?:\.[`\w.-]+)+)",
    ]
    for pat in patterns:
        m = re.search(pat, sql_no_comment, re.IGNORECASE)
        if m:
            ref = m.group(1).strip().strip("`").strip("'").strip('"')
            # Take last segment after a dot if multi-segment, otherwise full
            return ref.replace(".", "_").replace("-", "_") if ref else None
    return None


def _summarize_attempt(
    attempt: int,
    validate_result: dict[str, Any],
    start_time: float,
) -> dict[str, Any]:
    """Build a compact summary of one attempt for storage in ``attempts`` list.

    The summary is intentionally stripped of bulky fields (raw log_file_info,
    full log analysis, raw errorLog) — those are not needed for the caller to
    decide what to fix on the next attempt; ``log_analysis_summary`` is enough.
    """
    elapsed = max(0.0, time.time() - start_time)
    analysis = validate_result.get("log_analysis") or {}
    return {
        "attempt": attempt,
        "passed": bool(validate_result.get("passed")),
        "job_id": validate_result.get("job_id"),
        "elapsed_sec": round(elapsed, 2),
        "error_type": analysis.get("error_type"),
        "error_message": analysis.get("error_message") or validate_result.get("error"),
        "suggestions": list(analysis.get("suggestions") or []),
        "log_analysis_summary": validate_result.get("log_analysis_summary"),
    }


def _resolve_workspace_dir(_tool_context: ToolExecutionContext | None) -> Path | None:
    """Pick the best workspace directory we can write validation artifacts to.

    Priority:
      1. ``runtime.workspace_dir`` (preferred — same dir the agent writes SQL into)
      2. ``DATAOPS_VALIDATION_REPORT_DIR`` env var (manual override)
      3. ``None`` (caller falls back to logging-only mode)
    """
    env_dir: Path | None = None
    raw = os.environ.get("DATAOPS_VALIDATION_REPORT_DIR", "").strip()
    if raw:
        env_dir = Path(raw).expanduser().resolve()
    runtime = getattr(_tool_context, "runtime", None) if _tool_context is not None else None
    runtime_ws = getattr(runtime, "workspace_dir", None) if runtime is not None else None
    if runtime_ws:
        return Path(runtime_ws).expanduser().resolve()
    return env_dir


def _dump_validation_artifacts(
    sql: str,
    attempts: list[dict[str, Any]],
    _tool_context: ToolExecutionContext | None,
) -> dict[str, str | None]:
    """Best-effort write of ``validate_<table>.json`` + ``delivery_warning.md``.

    Never raises — failures are logged and returned as None paths so the
    main flow keeps moving. Returns:
        {"validation_report_path": str|None, "delivery_warning_path": str|None}
    """
    result: dict[str, str | None] = {
        "validation_report_path": None,
        "delivery_warning_path": None,
    }
    workspace = _resolve_workspace_dir(_tool_context)
    if workspace is None:
        logger.warning(
            "[_dump_validation_artifacts] no workspace_dir available; "
            "skipping validate_<table>.json / delivery_warning.md dump"
        )
        return result

    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"[_dump_validation_artifacts] cannot create workspace {workspace}: {exc}")
        return result

    table_token = _extract_original_target_table(sql) or "unknown"
    report_path = workspace / _VALIDATION_REPORT_FILENAME.format(table=table_token)
    warning_path = workspace / _DELIVERY_WARNING_FILENAME

    report = {
        "validation_status": "failed_after_max_retries",
        "max_attempts": MAX_VALIDATE_ATTEMPTS,
        "sql_excerpt": (sql or "")[:500],
        "attempts": attempts,
        "generated_at": time.time(),
    }
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        result["validation_report_path"] = str(report_path)
        logger.debug(f"[_dump_validation_artifacts] wrote validation report to {report_path}")
    except Exception as exc:
        logger.warning(f"[_dump_validation_artifacts] failed to write report {report_path}: {exc}")

    # delivery_warning.md — short, scannable, fixed format
    suggestion_lines: list[str] = []
    for att in attempts:
        att_no = att.get("attempt")
        et = att.get("error_type") or "unknown"
        em = (att.get("error_message") or "")[:200]
        sgs = att.get("suggestions") or []
        suggestion_lines.append(f"- attempt {att_no}: `{et}` — {em}")
        for sg in sgs[:3]:
            suggestion_lines.append(f"  - {sg}")

    warning_body = (
        f"# {_DELIVERY_WARNING_HEADER.format(n=MAX_VALIDATE_ATTEMPTS)}\n\n"
        f"## 失败摘要\n\n" + "\n".join(suggestion_lines) + f"\n\n## 详细报告\n\n"
        f"见 `{report_path.name}`（含每次调用的 job_id、elapsed、error_type、suggestions）。\n"
        f"下游使用本批 SQL 前必须人工复核。\n"
    )
    try:
        warning_path.write_text(warning_body, encoding="utf-8")
        result["delivery_warning_path"] = str(warning_path)
        logger.debug(f"[_dump_validation_artifacts] wrote delivery warning to {warning_path}")
    except Exception as exc:
        logger.warning(f"[_dump_validation_artifacts] failed to write warning {warning_path}: {exc}")

    return result
