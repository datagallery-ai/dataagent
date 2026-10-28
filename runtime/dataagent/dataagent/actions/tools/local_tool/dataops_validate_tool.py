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
from dataagent.actions.tools.local_tool.dataops_log_utils import (
    bound_error_excerpt,
    bound_error_fields,
    bound_text,
)
from dataagent.core.managers import llm_manager
from dataagent.core.managers.llm_manager.stream_utils import collect_llm_stream
from dataagent.utils.log import logger

if TYPE_CHECKING:
    pass


# ============================================================================
# Retry / best-effort constants (shared by dataops_validate_sql_with_log_analysis)
# ============================================================================
MAX_VALIDATE_ATTEMPTS = 10
_VALIDATION_REPORT_FILENAME = "validate_{table}.json"
_DELIVERY_WARNING_FILENAME = "delivery_warning.md"
_DELIVERY_WARNING_HEADER = (
    "⚠️ 本次交付物未通过 DataOps 校验（重试 {n} 次仍失败），仅作 best-effort，下游使用前必须人工复核"
)

# OBS 调度日志的防御性长度上限。``_analyze_log_directly`` 只会把日志头部
# (异常抛出处)截到 32KB 再喂 LLM,所以这里留一个更宽松的上限即可。
_MAX_OBS_LOG_CHARS = 256_000


# ============================================================================
# 分区日期策略
#
# 两条链路刻意使用**不同**的分区日期，互不共享：
#
#   - 语法校验链路（``validate_deliverables`` → ``dataops_validate_sql_with_log_analysis``）
#     用「一年之后」的日期。校验只关心 SQL 能不能跑通；写入的分区在未来，
#     不会与真实分区 / 真实数据发生任何冲突。
#   - 交付后质量校验链路（post-hook → ``trial_compute_table``）
#     用「一周之前」的日期。质量校验要看真实产出，一周前的分区已经稳定落盘。
#
# 注意：``_replace_target_table`` 只替换 ``= '$date'`` 这种**等值**形态，
# ``<=`` / ``>=`` 形态和 ``${start_time,-30,yyyyMMdd}`` 这类宏它都匹配不到。
# DataOps 后端也不替换这些占位符（见 dataops_probe_tool 的说明），所以
# 源表上的范围过滤在质量校验链路里由 ``_collapse_pt_d_range`` 收敛成单日。
# ============================================================================
_VALIDATION_PARTITION_OFFSET_DAYS = 365
_QUALITY_PARTITION_OFFSET_DAYS = -7


def partition_date_for_validation() -> str:
    """语法校验链路的分区日期：一年之后（``today + 365``）。"""
    return (date.today() + timedelta(days=_VALIDATION_PARTITION_OFFSET_DAYS)).strftime("%Y%m%d")


def partition_date_for_quality() -> str:
    """交付后质量校验链路的分区日期：一周之前（``today - 7``）。"""
    return (date.today() + timedelta(days=_QUALITY_PARTITION_OFFSET_DAYS)).strftime("%Y%m%d")


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
        "error": bound_error_excerpt(
            error_msg or f"dataops rejected SQL (status={result_status})",
            label="error",
        ),
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


def _build_drop_sql(
    original_sql: str,
    user_account: str,
    table_suffix: str,
    partition_date: str | None = None,
) -> str | None:
    """Build DROP TABLE IF EXISTS statement for the temporary table.

    This is used when submitting CREATE TABLE to ensure clean table creation
    by dropping any existing table with the same name first.

    Args:
        original_sql: The original CREATE TABLE statement (before rewriting)
        user_account: User account for temp table naming
        table_suffix: Table suffix from resource metadata
        partition_date: 分区日期（``YYYYMMDD``）；``None`` 时用语法校验链路的默认日期。

    Returns:
        DROP TABLE IF EXISTS statement for the temp table, or None if extraction fails.
    """
    # First rewrite to get the temp table name
    rewritten = _replace_target_table(original_sql, user_account, table_suffix, partition_date)

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


def _replace_target_table(
    sql: str,
    user_account: str,
    table_suffix: str,
    partition_date: str | None = None,
) -> str:
    """Replace target table names (CREATE TABLE, INSERT OVERWRITE) with temporary table format.

    Temporary table format: adhoctemp.tmp_{user_account}_{date}_{table_suffix}

    Also replaces pt_d = '$date' (and other partition columns) with ``partition_date``.

    Examples:
        CREATE TABLE biads.ads_xxx → CREATE TABLE adhoctemp.tmp_user_date_ads_xxx
        INSERT OVERWRITE TABLE biads.ads_yyy → INSERT OVERWRITE TABLE adhoctemp.tmp_user_date_ads_yyy
        INSERT OVERWRITE EXTERNAL TABLE xxx → INSERT OVERWRITE TABLE adhoctemp.tmp_user_date_xxx
        pt_d = '$date' → pt_d = 'YYYYMMDD'

    Args:
        partition_date: 分区日期（``YYYYMMDD``）。``None`` 时用语法校验链路的默认日期
            （一年之后）。质量校验链路必须显式传入「一周之前」的日期。
    """
    if not partition_date:
        partition_date = partition_date_for_validation()
    sql = re.sub(
        r"(\w+)\s*=\s*'\$date'",
        lambda m: f"{m.group(1)} = '{partition_date}'",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"(\w+)\s*=\s*'\$\{date\}'",
        lambda m: f"{m.group(1)} = '{partition_date}'",
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
        temp_table = _get_temp_table_name(table_name, user_account, partition_date, table_suffix)

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
        temp_table = _get_temp_table_name(table_name, user_account, partition_date, table_suffix)

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


# ---------------------------------------------------------------------------
# pt_d 范围过滤 → 单日收敛（**仅**交付后质量校验链路）
# ---------------------------------------------------------------------------
#
# 背景：交付物里的 DML 常用**范围**分区过滤来表达时间窗口：
#
#     WHERE pt_d <= '$date' AND pt_d >= '${start_time,-30,yyyyMMdd}'
#
# 这类写法在两条链路上都有问题：
#   1. ``_replace_target_table`` 只替换 ``= '$date'``（正则要求 ``=``），
#      ``<=`` / ``>=`` 匹配不到；``${start_time,...}`` 更是没人解析。
#   2. DataOps 后端**不**替换这些占位符（见 dataops_probe_tool 的说明），
#      于是比较的是字符串字面量：``'20261021' <= '$date'`` 恒为 False
#      （``'2' > '$'``），源表被过滤成**空集** —— 试算扫不到任何行。
#
# 质量校验只关心「产出的数据长什么样」，不关心时间窗口的宽度，所以把范围
# 收敛成**某一天**即可，既能扫到数据，又不用做宏解析。
#
# 收敛策略：取范围里的**上界**（``<=`` / ``<`` 的右值）作为那一天。
#  - ``pt_d <= '20261021'``           → ``pt_d = '20261021'``
#  - ``pt_d <  '20261021'``           → ``pt_d = '20261020'``（不含上界，前一天）
#  - 上界是宏/占位符时用 ``partition_date``（质量校验链路的基准日）
#
# 只在**质量校验链路**启用：语法校验链路要保留原始范围语义（它只验证
# 「SQL 能不能跑通」），而且它用的是「一年之后」的日期，收敛反而会掩盖问题。

#: 识别 ``pt_d`` 与比较运算符 + 引号值（含 ``$date`` / ``${start_time,...}`` 宏）
_RANGE_PARTITION_RE = re.compile(
    r"(?P<col>\bpt_d\b)\s*(?P<op><=|>=|<|>|=)\s*'(?P<val>[^']*)'",
    re.IGNORECASE,
)

#: 形如 ``${start_time,-30,yyyyMMdd}`` / ``${date}`` 的调度宏
_SCHEDULER_MACRO_RE = re.compile(r"\$\{[^{}]*\}|\$\w+")


def _is_scheduler_placeholder(value: str) -> bool:
    """判断一个分区值是否是调度占位符（``$date`` / ``${start_time,-30,yyyyMMdd}``）。"""
    return bool(_SCHEDULER_MACRO_RE.search(value or ""))


def _shift_one_day_before(value: str) -> str:
    """把 ``YYYYMMDD`` 往前挪一天；解析失败时原样返回。"""
    try:
        return (date(int(value[:4]), int(value[4:6]), int(value[6:8])) - timedelta(days=1)).strftime("%Y%m%d")
    except (ValueError, IndexError):
        return value


def _collapse_pt_d_range(sql: str, partition_date: str) -> str:
    """把 ``pt_d`` 的**范围**过滤收敛成 ``pt_d = '<某一天>'``。

    只处理**范围**形态（``<=`` / ``>=`` / ``<`` / ``>``）的 ``pt_d`` 条件；
    已经是 ``=`` 的单日条件保持原样。

    **就地改写**，不删除任何条件：把范围内的每一条 ``pt_d`` 比较都换成同一天的
    等值条件。这样不会出现「删掉条件后 ``AND``/``OR`` 链断裂」的语法问题，
    也不会误伤 INSERT 目标表自己的 ``PARTITION(pt_d = ...)`` 声明。

    收敛到哪一天：取范围的**上界**（``<=`` / ``<`` 的右值）。
      - 上界是字面日期 → 用它；``<``（不含上界）→ 往前挪一天，仍落在原范围内；
      - 上界是占位符/宏（``$date`` / ``${start_time,...}``）→ 用 ``partition_date``。

    Args:
        sql: 原始 SQL。
        partition_date: 质量校验链路的基准日（``YYYYMMDD``），作为上界是占位符时的兜底。

    Returns:
        收敛后的 SQL。没有范围条件时原样返回。
    """
    if not sql or "pt_d" not in sql:
        return sql

    matches = list(_RANGE_PARTITION_RE.finditer(sql))
    if not matches:
        return sql

    # --- 1) 先定下收敛到哪一天 -------------------------------------------
    target: str | None = None
    for match in matches:
        op = match.group("op")
        if op not in ("<=", "<"):
            continue
        value = match.group("val").strip()
        if value and not _is_scheduler_placeholder(value):
            target = _shift_one_day_before(value) if op == "<" else value
            break
        if target is None:
            # 上界是占位符/宏 → 用质量校验链路的基准日
            target = partition_date
    if target is None:
        return sql

    logger.debug(
        f"[_collapse_pt_d_range] collapsed pt_d range to pt_d = '{target}' (partition_date={partition_date})",
    )

    # --- 2) 就地把每条范围条件换成等值条件 --------------------------------
    def _replace(match: re.Match) -> str:
        if match.group("op") == "=":
            return match.group(0)  # 单日条件不动
        return f"{match.group('col')} = '{target}'"

    result = _RANGE_PARTITION_RE.sub(_replace, sql)

    # 收敛后可能出现重复的同一条条件（``pt_d <= X AND pt_d >= Y`` → 两遍
    # ``pt_d = 'Z'``）。语义没错，但读着别扭，顺手去重。
    result = re.sub(
        rf"(\bpt_d\s*=\s*'{re.escape(target)}')"
        rf"(\s+AND\s+\bpt_d\s*=\s*'{re.escape(target)}')+",
        r"\1",
        result,
        flags=re.IGNORECASE,
    )
    return result


# CTE 展开（LLM 内联）的等待上限（秒）。
# 60s 偏紧：一次大 SQL 的 CTE 内联经常跑更久，超时后回退原始 SQL（仍带 CTE），
# 提交给 DataOps 就会报 "table(s) agapdw._src do not exist"。
# 300s 与非流式 LLM 调用默认超时（DEFAULT_LLM_NON_STREAM_TIMEOUT）一致。
_CTE_EXPANSION_MAX_TIMEOUT_SEC = 300

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


async def _expand_cte_for_dml(sql: str, timeout_sec: int = _CTE_EXPANSION_MAX_TIMEOUT_SEC, runtime=None) -> str:
    """Expand WITH AS (CTE) in DML INSERT statements to equivalent inline subqueries.

    Only processes DML INSERT statements that contain WITH AS clauses.
    Returns the original SQL unchanged if no CTE is detected or if an error occurs.
    Does not modify the original saved file.

    ``timeout_sec`` 上限为 :data:`_CTE_EXPANSION_MAX_TIMEOUT_SEC`（300s），并**真正生效**：
    超时/失败都回退原始 SQL（仍带 CTE），提交给 DataOps 会报
    ``table(s) agapdw._src do not exist``，所以这个上限要留足。
    """
    if not _has_cte(sql):
        return sql

    timeout_sec = max(1, min(int(timeout_sec), _CTE_EXPANSION_MAX_TIMEOUT_SEC))

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
            f"[_expand_cte_for_dml] expanding CTE (timeout={timeout_sec}s), original[:120]={sql[:120]!r}",
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(llm.invoke, messages),
            timeout=timeout_sec,
        )
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
# Constants for reasonable-check
# ---------------------------------------------------------------------------

_POST_VALIDATE_PARTITION_COL = "pt_d"


def _is_insert_sql(sql: str) -> bool:
    """Return True if sql is an INSERT statement."""
    return bool(_INSERT_RE.search(sql))


def _extract_temp_table_name(
    sql: str,
    user_account: str,
    table_suffix: str,
    partition_date: str | None = None,
) -> str | None:
    """Extract the adhoctemp target table name from any rewritten SQL (CREATE or INSERT).

    Unified extractor for cleanup purposes. Returns None for SELECT-like SQL
    that doesn't materialize a temp table.

    ``partition_date`` must match the value used when the SQL was rewritten,
    otherwise the derived temp table name will not exist.
    """
    if _is_create_sql(sql):
        # CREATE: parse the rewritten CREATE statement
        rewritten = _replace_target_table(sql, user_account, table_suffix, partition_date)
        match = re.search(
            r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.`_-]+)",
            rewritten,
            re.IGNORECASE,
        )
        if not match:
            return None
        return match.group(1).strip().strip("`").strip("'").strip('"') or None

    return _extract_insert_target_table(sql, user_account, table_suffix, partition_date)


def _extract_insert_target_table(
    sql: str,
    user_account: str,
    table_suffix: str,
    partition_date: str | None = None,
) -> str | None:
    """Extract the adhoctemp target table name from a rewritten INSERT sql.

    Runs _replace_target_table internally to get the rewritten INSERT line,
    then extracts the table identifier that was substituted.
    Returns None if no INSERT clause or table name is found.
    """
    rewritten = _replace_target_table(sql, user_account, table_suffix, partition_date)
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


# ============================================================================
# Main validation entry points
# ============================================================================


async def dataops_validate_sql(
    sql: str | None = None,
    *,
    sql_file_path: str | os.PathLike[str] | None = None,
    _partition_date: str | None = None,
    _collapse_partition_range: bool = False,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate one SQL statement against the DataOps adHoc API.

    All SQL types (DDL, DML) are routed through the ``submit_job`` lifecycle:
    submit → poll → collect. The DataOps backend distinguishes CREATE TABLE
    vs INSERT/UPDATE/DELETE internally via the SQL text itself.

    de_agent is expected to call this AFTER it has finished the 8-category
    quality self-check on the generated SQL.

    Args:
        sql: SQL statement to validate. 可选,提供时与 ``sql_file_path`` 至少一个非空。
        sql_file_path: 工作区内 SQL 文件路径。可选,提供时优先于 ``sql``（推荐用法,
            避免大段 SQL 进 chat history）。
        _partition_date: 内部参数。分区日期（``YYYYMMDD``），同时用于临时表命名与
            ``$date`` 占位符替换。``None`` 时用语法校验链路的默认日期（一年之后）；
            交付后的质量校验链路需要显式传入「一周之前」，以对齐它自己写的临时表。
        _collapse_partition_range: 内部参数。``True`` 时把源表上的 ``pt_d`` **范围**
            过滤（``<=`` / ``>=`` / ``<`` / ``>``）收敛成单日等值条件 —— 只在
            **交付后质量校验链路**开启（试算只关心产出数据长什么样，不需要保留
            时间窗口宽度）。语法校验链路保持原样。

    Returns:
        A dict with at least ``passed`` (bool).
        On failure, also includes:
        - ``error``: The error message from DataOps
        - ``job_id``: The DataOps job ID for this validation
        - ``log_file_info``: OBS log access info (url + headers) if available

        NOTE: This tool does NOT auto-fetch OBS log to avoid polluting context.
        Use ``dataops_validate_sql_with_log_analysis`` if you want log analysis.
    """
    partition_date = _partition_date or partition_date_for_validation()

    # --- Resolve SQL input (file path preferred) ----------------------------
    try:
        sql = _resolve_sql_input(sql, sql_file_path)
    except ValueError as exc:
        return {"passed": False, "error": f"dataops_validate_sql: {exc}"}

    runtime = _tool_context.runtime

    # --- Environment routing -------------------------------------------------
    env = _get_dataops_env()
    logger.debug(f"[dataops_validate_sql] env routing: DATAOPS_ENV={env!r} (set={bool(os.environ.get('DATAOPS_ENV'))})")
    if env == "prod":
        logger.debug("[dataops_validate_sql] routing to PROD path (official MCP flat flow)")
        return await _dataops_validate_sql_prod(
            sql,
            _tool_context=_tool_context,
            sql_file_path=sql_file_path,
            partition_date=partition_date,
            collapse_partition_range=_collapse_partition_range,
        )
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
    sql_for_submit = _replace_target_table(sql, user_account, table_suffix, partition_date)
    # 质量校验链路：把源表上的 pt_d **范围**过滤收敛成单日。
    # 必须在 CTE 展开之前做 —— 展开后的 SQL 交给 LLM 改写，范围条件容易被改花。
    if _collapse_partition_range:
        sql_for_submit = _collapse_pt_d_range(sql_for_submit, partition_date)
    sql_for_submit = await _expand_cte_for_dml(
        sql_for_submit,
        timeout_sec=min(timeout_sec, _CTE_EXPANSION_MAX_TIMEOUT_SEC),
        runtime=runtime,
    )

    # --- Pre-submit: DROP temp table for CREATE TABLE (default enabled) ---
    # 默认开启：DDL 重跑时同名 adhoctemp 表可能已存在（上次 CREATE 失败留下），
    # 不先 DROP 会报 "Table already exists"。
    # 生产路径 (_dataops_validate_sql_prod) 无条件执行同样的预清理。
    drop_job_id: str | None = None
    if _is_drop_before_create_enabled(resource.metadata):
        drop_job_id = await _pre_drop_for_create(
            coordinator, sql, user_account, table_suffix, timeout_sec, poll_interval, partition_date
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
    partition_date: str | None = None,
) -> str | None:
    """For CREATE TABLE, drop existing temp table first to ensure clean creation.

    Returns the drop job_id if submitted, None otherwise.
    """
    if not _is_create_sql(sql):
        return None

    drop_sql = _build_drop_sql(sql, user_account, table_suffix, partition_date)
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
    partition_date: str | None = None,
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

    temp_table = _extract_temp_table_name(sql, user_account, table_suffix, partition_date)
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
    partition_date: str | None = None,
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

    temp_table = _extract_temp_table_name(sql, user_account, table_suffix, partition_date)
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
    partition_date: str | None = None,
) -> str | None:
    """Cleanup dispatcher for ``dataops_validate_sql_with_log_analysis``.

    Routes to the integration- or production-cleanup helper based on
    DATAOPS_ENV, with shared gating (DATAOPS_DISABLE_TEMP_CLEANUP) and
    identical fire-and-forget semantics. Returns the drop job_id if a
    DROP was submitted, otherwise None.

    Args:
        partition_date: 分区日期（``YYYYMMDD``），必须与当初改写 SQL 时用的值一致，
            否则推导出的临时表名不匹配，DROP 会落空。``None`` 时用语法校验链路的
            默认日期（一年之后）。

    Note: this is only invoked by the wrapper, AFTER the wrapper has
    already determined that this is the final outcome (passed, or
    attempt >= MAX_VALIDATE_ATTEMPTS) and the SQL is DML (not DDL).
    """
    runtime = _tool_context.runtime
    if _get_dataops_env() == "prod":
        user_account = _get_user_account(runtime)
        table_suffix = os.environ.get("DATAOPS_TABLE_SUFFIX", "")
        timeout_sec = 30 * 60
        return await _cleanup_temp_table_prod(sql, user_account, table_suffix, timeout_sec, partition_date)

    # Integration path
    resource = _get_dataops_resource(runtime)
    coordinator = runtime.ensure_resource_coordinator() if hasattr(runtime, "ensure_resource_coordinator") else None
    if coordinator is None:
        logger.debug("[dataops_validate_sql_with_log_analysis] cleanup skipped: no resource coordinator available")
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
        partition_date=partition_date,
    )


async def dataops_validate_sql_with_log_analysis(
    sql: str | None = None,
    *,
    sql_file_path: str | os.PathLike[str] | None = None,
    attempt: int = 1,
    prior_attempts: list[dict[str, Any]] | None = None,
    _partition_date: str | None = None,
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
    - Per SQL, the caller may invoke this at most ``MAX_VALIDATE_ATTEMPTS`` (10)
      times (including the first call). Each invocation must pass
      ``attempt`` = 1..10, and ``prior_attempts`` = the ``attempts`` list
      returned by the previous call (empty list on first call).
    - When ``attempt > MAX_VALIDATE_ATTEMPTS`` the call is rejected without
      touching DataOps; the caller must not initiate an 11th call.
    - When the final attempt (``attempt == MAX_VALIDATE_ATTEMPTS``) still
      returns ``passed: false``, this wrapper auto-dumps
      ``validate_<table>.json`` + ``delivery_warning.md`` to the workspace
      and adds ``failed_after_max_retries: true`` to the response.

    **数据正确性校验（quality check）说明：**
    - 本链路**不**执行数据正确性校验（空值率 / 单一值比率）。
    - 校验只在交付后的 post-hook 中跑一次
      （``de_agent/hooks/dataops_quality_check_post.py`` → ``trial_compute_table``），
      因此静态校验阶段保持"快"，不拖慢 agent 迭代修 SQL 的循环。
    - 响应中不再包含 ``reasonable_check`` 字段。

    **分区日期：**
    本链路是**语法校验**，用「一年之后」的日期（``today + 365``）。写入的分区在未来，
    不会与真实分区/真实数据冲突；这里只关心 SQL 能不能跑通。交付后的质量校验链路
    用「一周之前」的日期（见 ``partition_date_for_quality``），两者刻意不同。

    Args:
        sql: SQL statement to validate. 可选,与 ``sql_file_path`` 至少一个非空。
        sql_file_path: 工作区内 SQL 文件路径。推荐用法,避免大段 SQL 进 chat history。
        attempt: 1-indexed attempt number for this call (caller-managed).
        prior_attempts: Returned ``attempts`` list from the previous call
            (empty/None on first call).
        _partition_date: 内部参数。分区日期（``YYYYMMDD``）；``None`` 时用语法校验链路的
            默认日期（一年之后）。

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
            f"Caller must not initiate an 11th call."
        )
        return {
            "passed": False,
            "failed_after_max_retries": True,
            "error": (f"exceeded MAX_VALIDATE_ATTEMPTS={MAX_VALIDATE_ATTEMPTS}; caller must not initiate an 11th call"),
            "attempts": prior_attempts,
        }

    partition_date = _partition_date or partition_date_for_validation()

    # --- Resolve SQL input (file path preferred) ----------------------------
    try:
        sql = _resolve_sql_input(sql, sql_file_path)
    except ValueError as exc:
        return {
            "passed": False,
            "failed_after_max_retries": True,
            "error": f"dataops_validate_sql_with_log_analysis: {exc}",
            "attempts": prior_attempts,
        }

    logger.debug(
        f"[dataops_validate_sql_with_log_analysis] === START attempt={attempt} "
        f"prior_attempts={len(prior_attempts)} partition_date={partition_date} sql={sql[:100]}"
    )

    # Step 1: Validate SQL
    logger.debug("[dataops_validate_sql_with_log_analysis] Calling dataops_validate_sql...")
    validate_result = await dataops_validate_sql(
        sql,
        _tool_context=_tool_context,
        sql_file_path=sql_file_path,
        _partition_date=partition_date,
    )
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

        # NOTE: 数据正确性校验（空值率 / 单一值比率）**不在本链路执行**。
        # 它只在交付后的 post-hook（``dataops_quality_check_post`` →
        # ``trial_compute_table``）里跑一次。这样静态校验阶段保持"快"，
        # 昂贵的采样统计不拖慢 agent 的迭代修 SQL 循环。

        # Cleanup: only drop the temp table when the SQL is DML (not DDL)
        # and this is the final outcome. CREATE TABLE leaves the temp
        # table in place so the corresponding INSERT OVERWRITE can target it.
        if not _is_create_sql(sql):
            cleanup_job_id = await _cleanup_temp_table_wrapper(
                sql, _tool_context=_tool_context, partition_date=partition_date
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
        # NOTE: 这里也要收敛错误文本 —— 提前 return 会绕过末尾的 errorLog 截断,
        # 而生产端的 ``error`` 可能就是整份 Spark 日志。
        return bound_error_fields(validate_result)

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
                sql, _tool_context=_tool_context, partition_date=partition_date
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

    MAX_LEN_ERROR_LOG = 250
    if len(ordered_result.get("errorLog", "")) > MAX_LEN_ERROR_LOG:
        ordered_result["errorLog"] = bound_text(
            ordered_result["errorLog"],
            max_chars=MAX_LEN_ERROR_LOG,
            label="errorLog",
        )

    MAX_LEN_ERROR = 500
    if len(ordered_result.get("error", "")) > MAX_LEN_ERROR:
        ordered_result["error"] = bound_text(
            ordered_result["error"],
            max_chars=MAX_LEN_ERROR,
            label="error",
        )

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

    # 2) LLM analysis — collect the complete asynchronous stream before parsing JSON
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
        raw_text = (await collect_llm_stream(llm, messages)).strip()
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
                # 防御性上限:OBS 上的调度日志可能有几百 KB。调用方
                # ``_analyze_log_directly`` 只需要头部(异常在头部),且它自己
                # 还会截到 32KB 再喂 LLM。这里留一个宽松的上限,避免异常大的
                # 响应占满内存/工具结果。
                "log_content": bound_text(
                    response.text,
                    max_chars=_MAX_OBS_LOG_CHARS,
                    label="log_content",
                ),
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


_DISABLE_DROP_BEFORE_CREATE_VALUES = frozenset({"false", "0", "no", "off", "n", "f"})


def _is_drop_before_create_enabled(metadata: dict | None = None) -> bool:
    """Whether to DROP the scratch table before submitting a CREATE TABLE.

    默认开启（True）。DDL 重跑时同名 adhoctemp 表可能已存在（上次 CREATE
    失败留下），不先 DROP 会报 "Table already exists"。

    可以用两种方式关闭：
        - resource metadata: ``drop_before_create: false``
        - 环境变量: ``DATAOPS_ENABLE_DROP_BEFORE_CREATE=false``

    接受 (case-insensitive) true/false/1/0/yes/no/on/off；metadata 优先于
    环境变量；两者都未设置时返回 True（默认开启）。
    """
    raw_meta = (metadata or {}).get("drop_before_create")
    if raw_meta is not None:
        return str(raw_meta).strip().lower() not in _DISABLE_DROP_BEFORE_CREATE_VALUES

    raw_env = os.environ.get("DATAOPS_ENABLE_DROP_BEFORE_CREATE", "").strip().lower()
    if not raw_env:
        return True
    return raw_env not in _DISABLE_DROP_BEFORE_CREATE_VALUES


async def _dataops_validate_sql_prod(
    sql: str | None = None,
    *,
    sql_file_path: str | os.PathLike[str] | None = None,
    partition_date: str | None = None,
    collapse_partition_range: bool = False,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Production-environment validation entry point.

    与联调路径完全独立:不走 submit -> poll -> collect,直接调官方 MCP 工具。
    """
    if not partition_date:
        partition_date = partition_date_for_validation()

    # --- Resolve SQL input (file path preferred) ----------------------------
    try:
        sql = _resolve_sql_input(sql, sql_file_path)
    except ValueError as exc:
        return {"passed": False, "error": f"dataops_validate_sql(prod): {exc}"}

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
    logger.debug(f"[_dataops_validate_sql_prod] user_account={user_account!r} table_suffix={table_suffix!r}")

    # --- SQL rewrite (与联调相同) ---
    sql_for_submit = _replace_target_table(sql, user_account, table_suffix, partition_date)
    # 质量校验链路：把源表上的 pt_d **范围**过滤收敛成单日（与联调一致）。
    if collapse_partition_range:
        sql_for_submit = _collapse_pt_d_range(sql_for_submit, partition_date)
    sql_for_submit = await _expand_cte_for_dml(
        sql_for_submit,
        timeout_sec=min(timeout_sec, _CTE_EXPANSION_MAX_TIMEOUT_SEC),
        runtime=runtime,
    )

    # --- Pre-submit: DROP temp table for CREATE TABLE (默认开启,DROP 失败仅记录警告) ---
    # 生产环境下 DROP-before-CREATE 为硬约束:即使 adhoctemp 已存在残留 tmp_*,CREATE
    # 也会再次失败,因此必须先尝试清理。DROP 自身出错时静默继续 CREATE,把责任
    # 交给 CREATE 阶段的 DataOps 报错。
    # 与联调路径共用同一个开关(默认开启);生产路径没有 resource metadata,
    # 因此这里只读环境变量 DATAOPS_ENABLE_DROP_BEFORE_CREATE。
    drop_job_id: str | None = None
    if _is_create_sql(sql) and _is_drop_before_create_enabled(None):
        drop_job_id = await _pre_drop_for_create_prod(
            sql, user_account, table_suffix, timeout_sec, poll_interval, partition_date
        )

    # --- Execute -> poll -> collect (扁平函数) ---
    logger.debug(f"[_dataops_validate_sql_prod] submitting SQL (first 200 chars): {sql_for_submit[:200]}")
    result_status, job_id, result = await _submit_and_poll_prod(sql_for_submit, timeout_sec, poll_interval)
    logger.debug(
        f"[_dataops_validate_sql_prod] _submit_and_poll_prod returned result_status={result_status!r} "
        f"job_id={job_id!r} raw result={result}"
    )

    # --- Outcome dispatch ---
    if result_status == "timed_out":
        return {
            "passed": False,
            "error": f"dataops(prod) execution timed out after {timeout_sec}s",
            "job_id": job_id,
            "timed_out": True,
            "drop_job_id": drop_job_id,
        }
    if result_status == "error":
        err = result or {}
        return {
            "passed": False,
            "error": err.get("error", "unknown error"),
            "job_id": job_id,
            "drop_job_id": drop_job_id,
        }

    if result_status == "completed":
        final_result: dict[str, Any] = {"passed": True, "job_id": job_id}
        # NOTE: temp-table cleanup is NOT done here. Single-attempt
        # entry point must leave the adhoctemp.tmp_* table in place
        # so the corresponding DML on the same table can run.
        # Cleanup is owned by the multi-attempt wrapper.
        return final_result

    # failed — return as-is, no cleanup here either (see note above).
    failed = _build_failure_response_prod(result_status, job_id, result, sql)
    if drop_job_id:
        failed["drop_job_id"] = drop_job_id
    return failed


async def _pre_drop_for_create_prod(
    original_sql: str,
    user_account: str,
    table_suffix: str,
    timeout_sec: int,
    poll_interval: float,
    partition_date: str | None = None,
) -> str | None:
    """生产环境下的 pre-drop:对 CREATE TABLE 先 DROP TABLE IF EXISTS adhoctemp 临时表。

    与联调版 (_pre_drop_for_create) 行为对齐:
        - 仅当 SQL 为 CREATE TABLE 时触发
        - DROP 失败时仅记录 warning,返回 None,不抛错
        - DROP 成功 / 失败 / 超时均不阻塞主流程,CREATE 阶段会自行报回真实问题

    Returns:
        drop_job_id (str) 如果 DROP 已提交(含后续轮询);否则 None。
    """
    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_execute_sql,
        prod_get_query_result,
    )

    if not _is_create_sql(original_sql):
        return None

    drop_sql = _build_drop_sql(original_sql, user_account, table_suffix, partition_date)
    if not drop_sql:
        return None

    drop_timeout = min(timeout_sec, 60)
    try:
        logger.debug(f"[_pre_drop_for_create_prod] Submitting DROP for CREATE TABLE: {drop_sql}")
        submit_result = await prod_execute_sql(drop_sql)
        logger.debug(f"[_pre_drop_for_create_prod] DROP submit_result={submit_result}")
        if not submit_result.get("success") or not submit_result.get("job_id"):
            logger.warning(
                f"[_pre_drop_for_create_prod] DROP submit failed, continuing with CREATE anyway: "
                f"{submit_result.get('error', 'no job_id')}"
            )
            return None
        drop_job_id = submit_result["job_id"]
    except Exception as drop_exc:
        logger.warning(f"[_pre_drop_for_create_prod] DROP submission raised, continuing with CREATE: {drop_exc}")
        return None

    deadline = asyncio.get_event_loop().time() + drop_timeout
    while asyncio.get_event_loop().time() < deadline:
        polled = await prod_get_query_result(drop_job_id)
        status = polled["status"]
        logger.debug(f"[_pre_drop_for_create_prod] DROP job_id={drop_job_id} polled status={status!r}")
        if status in {"completed", "failed", "error"}:
            if status == "completed":
                logger.debug(f"[_pre_drop_for_create_prod] DROP completed job_id={drop_job_id}")
            else:
                logger.warning(
                    f"[_pre_drop_for_create_prod] DROP ended with status={status}, "
                    f"proceeding with CREATE anyway job_id={drop_job_id} "
                    f"error={polled.get('error')!r}"
                )
            return drop_job_id
        await asyncio.sleep(poll_interval)

    logger.warning(
        f"[_pre_drop_for_create_prod] DROP poll timed out after {drop_timeout}s, "
        f"proceeding with CREATE anyway job_id={drop_job_id}"
    )
    return drop_job_id


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
    logger.debug("[_submit_and_poll_prod] Step1: calling prod_execute_sql")
    try:
        submit_result = await prod_execute_sql(sql_for_submit)
        logger.debug(f"[_submit_and_poll_prod] Step1 raw submit_result={submit_result}")
        if not submit_result["success"]:
            return "error", None, {"error": submit_result.get("error") or "execute_sql failed"}
        job_id = submit_result["job_id"]
    except Exception as exc:
        return "error", None, {"error": f"dataops(prod) MCP execute_sql failed: {exc}"}

    # Step 2: 轮询直到 terminal 状态
    deadline = asyncio.get_event_loop().time() + timeout_sec
    poll_count = 0
    logger.debug(f"[_submit_and_poll_prod] job_id={job_id} Step2: start polling deadline={deadline}")
    while asyncio.get_event_loop().time() < deadline:
        poll_count += 1
        polled = await prod_get_query_result(job_id)
        status = polled["status"]
        logger.debug(f"[_submit_and_poll_prod] job_id={job_id} poll_count={poll_count} raw polled={polled}")
        if status in {"completed", "failed"}:
            # Step 3: 到达 terminal,取最终结果(含 inline data 或 errorLog)
            logger.debug(
                f"[_submit_and_poll_prod] job_id={job_id} Step3: terminal reached ({status}), "
                "calling prod_collect_result"
            )
            result = await prod_collect_result(job_id)
            logger.debug(f"[_submit_and_poll_prod] job_id={job_id} Step3 raw result={result}")
            return status, job_id, result
        if status == "error":
            return "error", job_id, {"error": polled.get("error", "poll error")}
        await asyncio.sleep(poll_interval)

    return "timed_out", job_id, None


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
        "error": bound_error_excerpt(error_msg, label="error"),
        "job_id": job_id,
    }
    if error_log:
        # NOTE: 这里**不能**截断 errorLog —— 下游 ``_analyze_log_directly`` 需要
        # 完整日志才能分类错误。截断发生在 agent 边界(见
        # ``dataops_validate_sql_with_log_analysis`` 末尾),原始日志不会进对话。
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


def extract_insert_target_table(sql: str) -> str | None:
    """提取 INSERT 的**真实目标表**（``db.table``），不做任何临时表替换。

    与 :func:`_extract_insert_target_table` 的区别：后者会把目标改写成
    ``adhoctemp.tmp_*`` 临时表名；本函数只做**只读解析**，返回原始目标表 ——
    独立「数据验证」skill 需要直接对真实表做统计。

    Args:
        sql: INSERT 语句文本。

    Returns:
        ``db.table`` 形式的目标表名；解析不出时返回 ``None``。
    """
    if not sql:
        return None
    sql_no_comment = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    pattern = (
        r"\bINSERT\s+(?:OVERWRITE|INTO)\s+(?:EXTERNAL\s+)?(?:TABLE\s+)?"
        r"([`\w.-]+(?:\.[`\w.-]+)+)"
    )
    m = re.search(pattern, sql_no_comment, re.IGNORECASE)
    if not m:
        return None
    ref = m.group(1).strip().strip("`").strip("'").strip('"')
    return ref or None


def extract_insert_partition_filter(sql: str) -> str:
    """提取 INSERT 语句里**写死的**天分区过滤条件（形如 ``pt_d = '20261020'``）。

    独立数据验证 skill 拿到的 INSERT 往往已经带了具体的分区条件（而不是
    ``$date`` 占位符），直连真实表统计时必须沿用它 —— 否则会扫到别的分区。

    **只认天分区 ``pt_d``**：小时/分钟分区（``pt_h`` / ``pt_i``）会把统计
    落到某一个具体时刻上，与本仓库其它链路（数据探查、质量校验统一用
    ``pt_d``）的口径不一致；这里统一归一化成 ``pt_d``。

    Args:
        sql: INSERT 语句文本。

    Returns:
        分区过滤条件文本（可直接放进 WHERE）；找不到时返回 ``""``。
    """
    if not sql:
        return ""
    sql_no_comment = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)

    # 只认已经写成字面日期的条件 —— ``$date`` 占位符由调度时替换，
    # 这里拿不到真实日期，交给调用方兜底。
    #
    # 分区列优先级：``pt_d`` > ``pt`` > 其它 ``pt_*``。
    # 命中任何一个都归一化成 ``pt_d``：小时分区是 10 位、分钟分区是 12 位，
    # 取前 8 位当天分区日期。
    for col_pattern in (r"pt_d", r"pt", r"pt_\w+"):
        m = re.search(
            rf"\b({col_pattern})\s*=\s*'(\d{{8,12}})'",
            sql_no_comment,
            re.IGNORECASE,
        )
        if not m:
            continue
        value = m.group(2)
        # 只接受真正的分区列，避免误抓业务字段的等值条件。
        if not m.group(1).lower().startswith("pt"):
            return ""
        return f"pt_d = '{value[:8]}'"
    return ""


def _summarize_attempt(
    attempt: int,
    validate_result: dict[str, Any],
    start_time: float,
) -> dict[str, Any]:
    """Build a compact summary of one attempt for storage in ``attempts`` list.

    The summary is intentionally stripped of bulky fields (raw log_file_info,
    full log analysis, raw errorLog) — those are not needed for the caller to
    decide what to fix on the next attempt; ``log_analysis_summary`` is enough.

    NOTE: ``attempts`` is echoed back to the agent as ``prior_attempts`` on the
    next attempt, so every byte here is multiplied by the retry count. The
    ``error`` fallback is therefore bounded — without this, a single huge
    Spark ``errorLog`` is replayed into the conversation up to 10 times.
    """
    elapsed = max(0.0, time.time() - start_time)
    analysis = validate_result.get("log_analysis") or {}
    return {
        "attempt": attempt,
        "passed": bool(validate_result.get("passed")),
        "job_id": validate_result.get("job_id"),
        "elapsed_sec": round(elapsed, 2),
        "error_type": analysis.get("error_type"),
        "error_message": bound_error_excerpt(
            analysis.get("error_message") or validate_result.get("error"),
            label="error_message",
        ),
        "suggestions": list(analysis.get("suggestions") or []),
        "log_analysis_summary": bound_error_excerpt(
            validate_result.get("log_analysis_summary"),
            label="log_analysis_summary",
        ),
    }


def _resolve_workspace_dir(_tool_context: ToolExecutionContext | None) -> Path | None:
    """Pick the best workspace directory we can write validation artifacts to.

    Priority:
      1. ``runtime.workspace_dir`` (preferred — same dir the agent writes SQL into)
      2. ``DATAOPS_VALIDATION_REPORT_DIR`` env var (manual override)
      3. ``None`` (caller falls back to logging-only mode)

    ``runtime.workspace_dir`` is only honoured when it is a real ``str``/``Path``.
    Test doubles (``MagicMock``) auto-generate a truthy, path-like attribute whose
    ``__fspath__`` yields a junk relative path (``MagicMock/mock.workspace_dir/<id>``);
    accepting it would scatter validation artifacts into a bogus directory instead
    of the intended workspace.
    """
    env_dir: Path | None = None
    raw = os.environ.get("DATAOPS_VALIDATION_REPORT_DIR", "").strip()
    if raw:
        env_dir = Path(raw).expanduser().resolve()
    runtime = getattr(_tool_context, "runtime", None) if _tool_context is not None else None
    runtime_ws = getattr(runtime, "workspace_dir", None) if runtime is not None else None
    if isinstance(runtime_ws, (str, Path)) and str(runtime_ws).strip():
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


def _load_sql_from_path(sql_file_path: str | os.PathLike[str] | None) -> str:
    """从工作区文件加载 SQL 文本。

    设计意图：避免上游 LLM 在 chat history 中把整段 SQL 重复一遍（可能很长，
    还会被中间 token 截断/改写），统一以文件路径作为入参，由工具侧一次性读完。
    行为：
      - 路径不存在 / 读失败 -> 抛 ``FileNotFoundError`` / ``OSError``，由调用方决定
        是否包装为 ``{"passed": False, "error": ...}`` 返回。
      - 读到的 bytes 用 ``utf-8`` 解析（DataOps 平台默认 utf-8）。
      - 自动 strip 首尾空白。
    """
    if sql_file_path is None:
        raise ValueError("sql_file_path must not be None when called")
    p = Path(sql_file_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"sql_file_path not found: {p}")
    if not p.is_file():
        raise IsADirectoryError(f"sql_file_path is not a regular file: {p}")
    text = p.read_text(encoding="utf-8")
    return text.strip()


def _resolve_sql_input(
    sql: str | None,
    sql_file_path: str | os.PathLike[str] | None,
) -> str:
    """把 ``sql`` / ``sql_file_path`` 两种入参统一收敛为 SQL 文本。

    优先用 ``sql_file_path``（避免大 SQL 重复进 chat history）；
    若两者都为空字符串则返回 ``""``（让调用方的"empty SQL → skipped"逻辑接管，
    而不是抛 ValueError）。
    """
    if sql_file_path:
        try:
            loaded = _load_sql_from_path(sql_file_path)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            raise ValueError(f"failed to load SQL from sql_file_path: {exc}") from exc
        return loaded  # 允许空文件 → 走 skipped
    if sql is not None and str(sql).strip():
        return str(sql).strip()
    # 都没给 → 返回空串,沿用 _check_skip_conditions / _dataops_validate_sql_prod 的 skipped 路径
    return ""


def _split_top_level_commas(text: str) -> list[str]:
    """按**顶层**逗号切分 SELECT 投影列表。

    括号内、字符串字面量里的逗号不是分隔符 ——
    ``SUM(IF(a = '1', 1, 0)) AS cnt`` 必须整体作为一个投影项。
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _select_projection(sql: str) -> str | None:
    """取出一条 SELECT 语句的投影列表文本（``SELECT`` 与顶层 ``FROM`` 之间）。

    只认**顶层**的 FROM —— 投影里的标量子查询自带 FROM，不能当成语句的 FROM。
    """
    m = re.search(r"\bSELECT\b", sql, re.IGNORECASE)
    if not m:
        return None
    start = m.end()
    depth = 0
    quote: str | None = None
    i = start
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and sql[i : i + 4].upper() == "FROM":
            after = sql[i + 4 : i + 5]
            if not after or not (after.isalnum() or after == "_"):
                return sql[start:i].strip()
        i += 1
    return None


def _extract_fields_from_insert_sql(sql: str, exclude_fields: frozenset) -> list[str]:
    """从 INSERT ... SELECT ... 解析出要校验的字段列表。

    返回的是**目标表里真实存在的列名**（即 INSERT 投影的别名），因为质量校验
    是去查这些列的统计值。

    支持：
      - 普通字段：``col1`` -> ``col1``
      - 字段别名：``col1 AS c1`` -> ``c1``；``col1 c1`` -> ``c1``
      - 表达式别名：``SUM(IF(a = '1', 1, 0)) AS cnt`` -> ``cnt``（聚合列同样要校验）
      - 无别名表达式：``col1 + col2`` -> 跳过（推不出列名）
      - 常量：``1 AS dummy`` -> 跳过（无统计意义）
      - ``SELECT *`` -> 返回空（无法在 SQL 文本中知道列名，调用方会跳过）
      - 重复列：去重保序

    注意（CTE 场景）：字段必须取自 **INSERT 自己的 SELECT 投影**，也就是
    临时表里真正落地的列。语句可能写成
    ``WITH cte AS (SELECT ... FROM src) INSERT OVERWRITE TABLE t SELECT ... FROM cte``，
    此时第一个 ``SELECT`` 属于 CTE，拿到的是 CTE 的列（临时表里根本没有），
    而 INSERT 的投影才是要校验的列。所以先定位 INSERT，再从它后面找 SELECT。
    """
    if not sql:
        return []
    text = sql.rstrip("; \t\r\n")
    # 先定位 INSERT 的位置，再在其后找 SELECT —— 否则会命中 CTE 里的 SELECT。
    insert_m = re.search(r"\bINSERT\b", text, re.IGNORECASE)
    tail = text[insert_m.end() :] if insert_m else text
    select_list = _select_projection(tail)
    if not select_list:
        return []
    parts = _split_top_level_commas(select_list)
    fields: list[str] = []
    seen: set[str] = set()
    excluded = {c.lower() for c in exclude_fields}
    identifier_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    # 常量字面量：数字 / 字符串 / NULL / TRUE / FALSE —— 不落成有意义的列
    literal_re = re.compile(
        r"^(?:[-+]?\d+(?:\.\d+)?|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|NULL|TRUE|FALSE)$",
        re.IGNORECASE,
    )
    for part in parts:
        # 取出列名 / 别名
        name = None
        # 形式：``expr AS alias`` / ``expr alias`` / ``expr``
        # 先去掉尾部 AS 别名
        as_match = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", part, re.IGNORECASE)
        if as_match:
            before_as = re.sub(r"\bAS\s+[A-Za-z_][A-Za-z0-9_]*\s*$", "", part, flags=re.IGNORECASE).strip()
            if literal_re.match(before_as):
                # 常量：``1 AS dummy`` —— 没有统计意义，跳过
                continue
            # 带 AS 时别名就是目标表里的列名：
            #   ``col1 AS c1``       -> c1
            #   ``SUM(...) AS cnt``  -> cnt（聚合列同样要校验）
            name = as_match.group(1)
        else:
            # 无 AS 的形式只有两种：
            #   ``col1``      -> col1（单个标识符）
            #   ``col1 c1``   -> c1（表达式 + 裸别名）
            # 其它（``col1 + col2`` 等）推不出列名，跳过。
            # 注意不能简单取最后一个 token —— 那样 ``col1 + col2`` 会被误判成
            # 别名 ``col2``，而目标表里根本没有这一列。
            tokens = part.split()
            if len(tokens) == 1:
                if identifier_re.match(tokens[0]):
                    name = tokens[0]
            elif len(tokens) == 2 and identifier_re.match(tokens[0]) and identifier_re.match(tokens[1]):
                # ``col1 c1`` —— 简单字段 + 裸别名
                name = tokens[1]
            else:
                continue
        if not name or name in seen:
            continue
        if name.lower() in excluded:
            continue
        seen.add(name)
        fields.append(name)
    return fields


# ============================================================================
# Data quality check (null-value / single-value coverage)
# ============================================================================
#
# The implementation lives in ``dataops_quality_check``, which owns an
# extensible checker registry. It is **not** invoked from this validation
# chain any more: the data quality check runs only in the delivery post-hook
# (``de_agent/hooks/dataops_quality_check_post.py`` → ``trial_compute_table``).
# See ``dataops_quality_check`` for the checker registry and configuration.


# ============================================================================
# Backward-compatible lazy re-exports
# ============================================================================
#
# These names now live in ``dataops_quality_check``. They are re-exported via
# PEP 562 module ``__getattr__`` rather than a module-level import, because
# ``dataops_quality_check`` imports *from this module* at import time — a
# module-level import here would be circular. Resolved values are cached in
# ``globals()`` so the lookup cost is paid only once.

_COMPAT_REEXPORTS = frozenset({
    "_build_reasonable_check_sql",
    "_parse_reasonable_check_result",
    "_is_reasonable_check_enabled",
    "_REASONABLE_CHECK_DEFAULT_EXCLUDE_COLS",
    "run_quality_check",
    "resolve_config",
})


def __getattr__(name: str):
    if name not in _COMPAT_REEXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from dataagent.actions.tools.local_tool import dataops_quality_check as _qc

    value = getattr(_qc, name)
    globals()[name] = value
    return value
