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
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import date
from typing import TYPE_CHECKING, Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.managers import llm_manager
from dataagent.utils.log import logger

if TYPE_CHECKING:
    pass


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


def _replace_target_table(sql: str, user_account: str, table_suffix: str) -> str:
    """Replace target table names (CREATE TABLE, INSERT OVERWRITE) with temporary table format.

    Temporary table format: adhoctemp.tmp_{user_account}_{date}_{table_suffix}

    Also replaces pt_d = '$date' (and other partition columns) with the current date.

    Examples:
        CREATE TABLE biads.ads_xxx → CREATE TABLE adhoctemp.tmp_user_date_ads_xxx
        INSERT OVERWRITE TABLE biads.ads_yyy → INSERT OVERWRITE TABLE adhoctemp.tmp_user_date_ads_yyy
        INSERT OVERWRITE EXTERNAL TABLE xxx → INSERT OVERWRITE TABLE adhoctemp.tmp_user_date_xxx
        pt_d = '$date' → pt_d = 'YYYYMMDD'
    """
    today = date.today().strftime("%Y%m%d")

    # Replace partition date placeholders like pt_d = '$date' with actual date
    sql = re.sub(
        r"(\w+)\s*=\s*'\$date'",
        lambda m: f"{m.group(1)} = '{today}'",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"(\w+)\s*=\s*'\$\{date\}'",
        lambda m: f"{m.group(1)} = '{today}'",
        sql,
        flags=re.IGNORECASE,
    )

    # Maximum length for the full temp table name (Hive limit is 128)
    MAX_FULL_TABLE_LEN = 100

    def _sanitize_table_name(name: str) -> str:
        return name.replace(".", "_").replace("-", "_")

    def _get_temp_table_name(original_table: str) -> str:
        # Remove dots and hyphens, then split by dots and take the last part
        sanitized = original_table.replace(".", "_").replace("-", "_")
        # If there were dots, take just the table name (last part)
        if "." in original_table:
            sanitized = sanitized.split("_")[-1]

        # Truncate if too long, keeping the last part which is usually the table name
        prefix = f"adhoctemp.tmp_{user_account}_{today}_"
        max_suffix = MAX_FULL_TABLE_LEN - len(prefix)
        if len(sanitized) > max_suffix:
            sanitized = sanitized[:max_suffix]

        return f"{prefix}{sanitized}"

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
        temp_table = _get_temp_table_name(table_name)

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
        temp_table = _get_temp_table_name(table_name)

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
    logger.debug(
        f"[dataops_validate_sql] preparing submit user_account={user_account!r} "
        f"table_suffix={table_suffix!r} timeout_sec={timeout_sec} poll_interval={poll_interval}s",
    )

    # --- SQL rewrite ---
    sql_for_submit = _replace_target_table(sql, user_account, table_suffix)
    if sql_for_submit != sql:
        logger.debug(
            f"[dataops_validate_sql] replaced table names original[:120]={sql[:120]!r}"
            f" → rewritten[:120]={sql_for_submit[:120]!r}",
        )
    else:
        logger.debug("[dataops_validate_sql] no table name rewrite applied")

    sql_for_submit = await _expand_cte_for_dml(sql_for_submit, timeout_sec=min(timeout_sec, 60), runtime=runtime)

    # --- Lifecycle ---
    try:
        submit_result = coordinator.submit_job(
            resource_id="dataops",
            command=sql_for_submit,
            task_type="sql_validate",
            timeout_sec=timeout_sec,
        )
        job_id = submit_result.get("job_id")
        if submit_result.get("status") == "ERROR":
            return {"passed": False, "error": submit_result.get("message") or "dataops submit_job failed"}
        if not job_id:
            return {"passed": False, "error": "dataops submit_job returned no job_id"}

        result_status, result = await _poll_until_done(coordinator, job_id, timeout_sec, poll_interval)

        if result_status == "timed_out":
            return {
                "passed": False,
                "error": f"dataops execution timed out after {timeout_sec}s, skipped check",
                "job_id": job_id,
                "timed_out": True,
            }

        if result is None:
            return {"passed": False, "error": "dataops collect returned no result", "job_id": job_id}

    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"dataops MCP call failed: {exc}"}

    # --- Outcome ---
    if result_status == "completed":
        return {"passed": True, "job_id": job_id}

    return _build_failure_response(result_status, job_id, result, sql)


async def dataops_validate_sql_with_log_analysis(
    sql: str,
    *,
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

    Args:
        sql: SQL statement to validate.

    Returns:
        Same as ``dataops_validate_sql``, but on failure also includes:
        - ``log_analysis``: Structured error analysis (error_type, error_message,
          location, suggestions)
        - ``log_analysis_summary``: Short summary for display (not full log)
    """
    logger.debug(f"[dataops_validate_sql_with_log_analysis] === START === sql={sql[:100]}")
    start_time = time.time()

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
        logger.debug(f"[dataops_validate_sql_with_log_analysis] === END (passed) elapsed={elapsed:.2f}s ===")
        return validate_result

    # If failed but no log_file_info, return as-is
    log_file_info = validate_result.get("log_file_info")
    if not log_file_info:
        logger.debug(
            "[dataops_validate_sql_with_log_analysis] Validation failed but no log_file_info job_id={}".format(
                validate_result.get("job_id"),
            ),
        )
        elapsed = time.time() - start_time
        logger.debug(f"[dataops_validate_sql_with_log_analysis] === END (no log_info) elapsed={elapsed:.2f}s ===")
        return validate_result

    job_id = validate_result.get("job_id")
    raw_error = validate_result.get("error", "")

    logger.debug(
        "[dataops_validate_sql_with_log_analysis] Validation FAILED job_id={!r} error={!r} ".format(
            job_id,
            (raw_error or "")[:200],
        ),
    )

    # Step 2: Fetch OBS log and analyze with LLM in-process. The raw log and LLM
    # messages stay private to this tool invocation, so the main agent's chat
    # history is never polluted with raw log content. Only the structured
    # analysis (error_type, error_message, location, suggestions) is returned.
    fetch_and_analyze = await _analyze_log_directly(log_file_info, job_id, raw_error, _tool_context=_tool_context)
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

    # log_analysis / log_analysis_summary / log_analysis_error sit before the
    # verbose log_file_info block so they remain visible even if the result is truncated.
    for key in ("log_analysis", "log_analysis_summary", "log_analysis_error"):
        if key in validate_result:
            ordered_result[key] = validate_result.pop(key)

    # log_file_info last (may be large; truncates first when chat scrolls)
    for key, value in validate_result.items():
        ordered_result[key] = value

    elapsed = time.time() - start_time
    logger.debug(f"[dataops_validate_sql_with_log_analysis] === END (failed with analysis) elapsed={elapsed:.2f}s ===")
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
    log_file_info: dict[str, Any],
    job_id: str,
    raw_error: str,
    _tool_context: ToolExecutionContext | None = None,
) -> dict[str, Any]:
    """Fetch OBS log and analyze with LLM in-process — keeps main-agent chat history clean.

    Runs inside the same tool invocation so:
      - The raw log content stays a local variable and never enters chat history.
      - Only the structured analysis (error_type, error_message, location, suggestions)
        is returned to the caller.
      - LLM I/O is wrapped in asyncio.to_thread (same pattern as _expand_cte_for_dml).

    LLM resolution order (first non-None wins):
      1. ``_tool_context.runtime.llm("planner")`` — reuse the main agent's LLM
      2. ``llm_manager.get_default_llm()``        — module-level singleton
      3. ``_parse_analysis_from_text(...)``        — pure-regex fallback

    Returns:
        dict with keys: status, analysis (optional), error (optional).
    """
    logger.debug(f"[_analyze_log_directly] START job_id={job_id!r}")

    # 1) Fetch raw log (private to this function — never returned to main agent)
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
