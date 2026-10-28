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
"""Lightweight data probe tool for schema linking (SELECT LIMIT N).

Public entry point: :func:`dataops_probe_table`.

Design contract: see ``docs/zh/design_doc/dataops_probe_tool_design.md``.
Highlights:
  - Never raises; all failures are returned as ``{"ok": False, "skipped": True, "reason": ...}``.
  - SELECT-only — refuses any non-SELECT-like table/column identifiers via whitelist.
  - Dual-path: env ``DATAOPS_ENV=prod`` uses official DataOps MCP (flat),
    otherwise uses self-hosted MCP via resource-coordinator (3-step).
  - Hard caps: ``sample_size <= 50``; default ``sample_size=5``; ``timeout_sec<=300``.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, timedelta
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext

# Reused, do not redeclare.
from dataagent.actions.tools.local_tool.dataops_log_utils import (
    bound_error_excerpt,
    bound_error_fields,
)
from dataagent.actions.tools.local_tool.dataops_validate_tool import (
    _check_skip_conditions,
    _get_dataops_env,
)
from dataagent.utils.log import logger

# ============================================================================
# Constants
# ============================================================================

_DEFAULT_SAMPLE_SIZE = 5
_MAX_SAMPLE_SIZE = 50
_DEFAULT_TIMEOUT_SEC = 60
_MAX_TIMEOUT_SEC = 300
# poll 间隔:prod 路径用 2.0s(probe 任务小、官方 MCP 无 per-poll 开销,稍激进
# 一点让 sample 查询更快返回);integration 路径用 1.0s(本机自建 MCP,响应快)
_PROD_POLL_INTERVAL_SEC = 2.0
_INTEGRATION_POLL_INTERVAL_SEC = 1.0

# 表分区过滤白名单正则(只接受已落地的字面日期;**严禁** $date/$hour/$minute
# 等占位符,DataOps 后端不替换它们,只会被原样透传到底层查询引擎,导致
# `pt_d = 'literal_$date'` 永远查不到数据)。
#
# 接受的格式(整串匹配):
#   pt_d = 'YYYYMMDD'
# 只接受天分区 pt_d —— 小时/分钟分区(pt_h / pt_i)一律拒绝,与数据探查、
# 质量校验链路的 pt_d 口径保持一致。
# 默认值自动生成规则(``partition_filter=None`` 时):
#   - 当前日期为 ``dt``,``target = dt - 7 days``(默认探查近一周,数据已落盘)
#   - 所有表统一只约束 pt_d,不区分 _dm/_hm/_im 等后缀。
_PARTITION_FILTER_RE = re.compile(
    r"^\s*pt_d\s*=\s*'(?P<d>\d{8})'\s*$"
)

# Identifier whitelist: starts with letter, then letters/digits/underscores.
# Multi-segment identifiers (e.g. "db.tbl") are validated per-segment.
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# ============================================================================
# Public entry
# ============================================================================


async def dataops_probe_table(
    table: str,
    columns: list[str] | None = None,
    *,
    partition_filter: str | None = None,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """对单表拉 LIMIT sample_size 行样例数据.

    用于 schema linking 阶段验证表/列是否存在、了解取值范围与空值情况。
    不做排序/聚合/去重/子查询/JOIN,纯 SELECT ... LIMIT N.

    Args:
        table: 目标表名,形如 "db.tbl"。仅允许 ``[A-Za-z][A-Za-z0-9_]*``,
               多段(用 . 分隔)时每段都需通过白名单。
        columns: 可选列名清单。``None`` 或空列表 → ``SELECT *``;
                 非空时按白名单校验后用反引号引用。
        partition_filter: 分区过滤字面串,**必须**是 ``pt_d = 'yyyyMMdd'`` 形式
            (由调用方通过 skill 知识生成,如"今天=20261012");格式白名单见
            ``_PARTITION_FILTER_RE``。**只接受天分区 pt_d**,小时/分钟分区
            (``pt_h`` / ``pt_i``)一律拒绝。DataOps SQL 校验器要求
            WHERE/ON 出现分区列(readbook 第 13-15 节 + verify.py L107-108);
            DataOps 后端**不**替换 $date/$hour/$minute 等占位符,工具也
            拒绝接受它们。``None`` 时自动生成 ``pt_d``(系统前 7 天)。
        sample_size: 抽样行数,默认 5,上限 50。超出会自动夹紧到上限。
        timeout_sec: 整个 submit+poll+collect 硬超时,默认 60s,上限 300s。

    Returns:
        ``{"ok": True, "table": ..., "rows": [...], "row_count": N, ...}``
        或
        ``{"ok": False, "skipped": True, "table": ..., "reason": "...", ...}``

        永远不会抛异常给 ToolWrapper — 最外层 try/except 把所有 Exception
        转成 ``reason="unknown_error"``。
    """
    start = time.monotonic()
    common: dict[str, Any] = {"table": table}

    try:
        # --- 1. 参数校验 ---
        if not isinstance(table, str) or not table.strip():
            return _err(common, "empty_table", start=start)
        if not _is_safe_identifier(table):
            return _err(common, "invalid_table_name", start=start,
                        error=f"table={table!r} 含有非法字符")

        # 清洗 sample_size / timeout_sec(防御性夹紧)
        sample_size = _clamp_int(sample_size, _DEFAULT_SAMPLE_SIZE, 1, _MAX_SAMPLE_SIZE)
        timeout_sec = _clamp_int(timeout_sec, _DEFAULT_TIMEOUT_SEC, 1, _MAX_TIMEOUT_SEC)

        # partition_filter: 缺省时按"系统前一周 + 表后缀"自动生成(避免 LLM 漏传导致
        # DataOps 平台报 partition condition not found);显式传则做整串白名单 +
        # 逐 token 合法性校验。$date / ; / 换行任何偏移一律拒绝,直接返回
        # invalid_partition_filter(让 LLM 看到错误重写,而不是默默执行出错 SQL)。
        auto_default_filter: str | None = None
        if partition_filter is None:
            auto_default_filter = _default_partition_filter(table)
            partition_filter = auto_default_filter
        else:
            if not isinstance(partition_filter, str):
                return _err(common, "invalid_partition_filter", start=start,
                            error="partition_filter must be str or None")
            m = _PARTITION_FILTER_RE.match(partition_filter)
            if not m:
                return _err(common, "invalid_partition_filter", start=start,
                            error=f"partition_filter={partition_filter!r} 不符合白名单 "
                                  f"pt_d = 'YYYYMMDD'（只支持天分区 pt_d，不支持 pt_h/pt_i 等小时/分钟分区）")
            # 进一步校验日期数字合法(避免 20261301 这种字符串合法但语义非法的值)
            try:
                date(int(m["d"][:4]), int(m["d"][4:6]), int(m["d"][6:8]))
            except ValueError:
                return _err(common, "invalid_partition_filter", start=start,
                            error=f"partition_filter 日期段 {m['d']!r} 不合法")

        # 校验 columns(不允许 None —— DataOps 平台禁止 SELECT *,LLM 必须先通过
        # metadata_recall 拿到表的列名列表再显式传入)。空 list [] 按 None 处理。
        validated_columns: list[str] | None
        if columns is None or (isinstance(columns, list) and len(columns) == 0):
            return _err(common, "invalid_columns", start=start,
                        error="columns 不允许为空或 None。请先通过 metadata_recall/get_table_schema "
                              "获取表的所有列名,然后选取你要探查的列显式传入 columns=[...]。"
                              "示例:columns=['col_a','col_b','col_c']")
        else:
            if not isinstance(columns, list):
                return _err(common, "invalid_columns", start=start,
                            error=f"columns 必须是 list[str] 或 None,实际 {type(columns).__name__}")
            bad = [c for c in columns if not (isinstance(c, str) and _is_safe_identifier(c))]
            if bad:
                return _err(common, "invalid_columns", start=start,
                            error=f"非法列名: {bad!r}")
            validated_columns = [str(c) for c in columns]

        # --- 2. 路由:prod / integration ---
        sql = _build_probe_sql(table, validated_columns, sample_size, partition_filter)
        env = _get_dataops_env()

        if env == "prod":
            return await _probe_prod(
                sql=sql, common=common, timeout_sec=timeout_sec, start=start,
                auto_partition_filter=auto_default_filter,
            )
        # integration
        return await _probe_integration(
            sql=sql, common=common, timeout_sec=timeout_sec, start=start,
            _tool_context=_tool_context,
            auto_partition_filter=auto_default_filter,
        )

    except Exception as exc:
        # catch-all:不抛给 ToolWrapper
        logger.warning(
            f"[dataops_probe_table] unknown_error table={table!r} exc={exc!r}",
            exc_info=True,
        )
        return _err(common, "unknown_error", start=start, error=str(exc))


# ============================================================================
# Path 1: prod — official DataOps MCP (flat 2-step)
# ============================================================================


async def _probe_prod(
    *,
    sql: str,
    common: dict[str, Any],
    timeout_sec: int,
    start: float,
    auto_partition_filter: str | None = None,
) -> dict[str, Any]:
    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_collect_result,
        prod_execute_sql,
        prod_get_query_result,
    )

    # 1) submit
    try:
        submit = await prod_execute_sql(sql)
    except Exception as exc:
        return _err(common, "execute_failed", start=start, error=f"prod_execute_sql exception: {exc}")

    if not submit.get("success") or not submit.get("job_id"):
        return _err(common, "execute_failed", start=start,
                    error=submit.get("error") or "execute_sql returned no job_id")

    job_id = str(submit["job_id"])

    # 2) poll
    deadline = time.monotonic() + timeout_sec
    while True:
        if time.monotonic() >= deadline:
            return _err(common, "timed_out", start=start, job_id=job_id)
        try:
            polled = await prod_get_query_result(job_id)
        except Exception as exc:
            return _err(common, "poll_error", start=start, job_id=job_id, error=str(exc))

        st = polled.get("status")
        if st in {"completed", "failed"}:
            break
        if st == "error":
            return _err(common, "poll_error", start=start, job_id=job_id,
                        error=polled.get("error") or "poll returned error")
        # st == "running" 或未知值 → 等下一次轮询,未知值打 debug 便于排查
        if st != "running":
            logger.debug(
                f"[dataops_probe_table] poll got unexpected status={st!r} "
                f"job_id={job_id!r}, will retry"
            )
        await asyncio.sleep(_PROD_POLL_INTERVAL_SEC)

    # 3) collect
    try:
        collected = await prod_collect_result(job_id)
    except Exception as exc:
        return _err(common, "execute_failed", start=start, job_id=job_id,
                    error=f"prod_collect_result exception: {exc}")

    if collected.get("status") != "completed":
        return _err(common, "execute_failed", start=start, job_id=job_id,
                    error=collected.get("error") or f"status={collected.get('status')}",
                    extra={"errorLog": collected.get("errorLog")})

    rows = collected.get("data") or []
    if not isinstance(rows, list):
        return _err(common, "execute_failed", start=start, job_id=job_id,
                    error=f"prod_collect_result.data is {type(rows).__name__}, expected list")

    result = {
        "ok": True,
        **common,
        "rows": rows,
        "row_count": len(rows),
        "job_id": job_id,
        "sql": sql,
        "elapsed_sec": round(time.monotonic() - start, 3),
    }
    if auto_partition_filter is not None:
        # 显式告诉 LLM:"工具用了什么默认值",方便后续需要时(查"今天"/其他日期)
        # 在对话里复述并显式覆盖。
        result["auto_partition_filter"] = auto_partition_filter
    return result


# ============================================================================
# Path 2: integration — self-hosted DataOps MCP via resource-coordinator (3-step)
# ============================================================================


async def _probe_integration(
    *,
    sql: str,
    common: dict[str, Any],
    timeout_sec: int,
    start: float,
    _tool_context: ToolExecutionContext,
    auto_partition_filter: str | None = None,
) -> dict[str, Any]:
    runtime = _tool_context.runtime

    # 0) skip 条件(复用 dataops_validate_tool 的 resource-coordinator 检查)
    skip_result, coordinator_or_none = _check_skip_conditions(sql="SELECT 1", runtime=runtime)
    if skip_result is not None:
        return {
            "ok": False,
            "skipped": True,
            **common,
            "reason": skip_result.get("reason", "no_dataops_resource"),
            "elapsed_sec": round(time.monotonic() - start, 3),
        }
    coordinator = coordinator_or_none

    # 1) submit
    try:
        submit = coordinator.submit_job(
            resource_id="dataops",
            command=sql,
            task_type="sql_validate",
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        return _err(common, "execute_failed", start=start, error=f"coordinator.submit_job exception: {exc}")

    if not isinstance(submit, dict) or submit.get("status") == "ERROR" or not submit.get("job_id"):
        return _err(common, "execute_failed", start=start,
                    error=(submit or {}).get("message") or "submit_job returned no job_id")

    job_id = str(submit["job_id"])

    # 2) poll-until-done(直接走 coordinator,不调 _poll_until_done 以避免与 validate
    #    共享取消副作用 — probe 不取消,让 DataOps 自身清理)
    deadline = time.monotonic() + timeout_sec
    result_status = "queued"
    while time.monotonic() < deadline:
        try:
            poll_result = coordinator.poll(job_id=job_id)
        except Exception as exc:
            return _err(common, "poll_error", start=start, job_id=job_id,
                        error=f"coordinator.poll exception: {exc}")
        result_status = str(poll_result.get("status") or "").strip().lower()
        if result_status in {"completed", "failed", "cancelled", "timed_out"}:
            break
        await asyncio.sleep(_INTEGRATION_POLL_INTERVAL_SEC)
    else:
        return _err(common, "timed_out", start=start, job_id=job_id)

    if result_status != "completed":
        # 仍尝试 collect 看是否有 error 信息
        try:
            result = coordinator.collect(job_id=job_id)
            err_msg = (result or {}).get("error") or (result or {}).get("summary") or f"status={result_status}"
        except Exception:
            err_msg = f"status={result_status}"
        return _err(common, "execute_failed", start=start, job_id=job_id, error=err_msg)

    # 3) collect
    try:
        result = coordinator.collect(job_id=job_id)
    except Exception as exc:
        return _err(common, "execute_failed", start=start, job_id=job_id,
                    error=f"coordinator.collect exception: {exc}")

    if not isinstance(result, dict) or result.get("status") != "completed":
        err_msg = (result or {}).get("error") or (result or {}).get("summary") \
            or (f"status={result.get('status')}" if isinstance(result, dict) else "no result")
        return _err(common, "execute_failed", start=start, job_id=job_id, error=err_msg)

    rows = result.get("data")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        return _err(common, "execute_failed", start=start, job_id=job_id,
                    error=f"collect.data is {type(rows).__name__}, expected list")

    result = {
        "ok": True,
        **common,
        "rows": rows,
        "row_count": len(rows),
        "job_id": job_id,
        "sql": sql,
        "elapsed_sec": round(time.monotonic() - start, 3),
    }
    if auto_partition_filter is not None:
        result["auto_partition_filter"] = auto_partition_filter
    return result


# ============================================================================
# Helpers
# ============================================================================


def _is_safe_identifier(s: str) -> bool:
    """表名/列名白名单校验。

    支持多段(``db.tbl`` 或 ``db.schema.tbl``),每段都必须匹配
    ``^[A-Za-z][A-Za-z0-9_]*$``。空段直接拒绝(避免 ``"db."``)。
    """
    if not isinstance(s, str) or not s.strip():
        return False
    parts = s.split(".")
    if any(not p for p in parts):  # 拒绝空段
        return False
    return all(_SAFE_ID_RE.match(p) for p in parts)


def _clamp_int(v: Any, default: int, lo: int, hi: int) -> int:
    """把任意值夹紧到 [lo, hi];非法值回退 default。"""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))


def _default_partition_filter(table: str) -> str:
    """``partition_filter`` 默认值自动生成器:系统前7天 + 统一 pt_d。

    Args:
        table: 形如 ``db.tbl`` 的表名(后缀仅用于日志记录,不再区分行为)。

    Returns:
        整串 ``pt_d = 'YYYYMMDD'``,可直接喂给 ``_PARTITION_FILTER_RE`` 校验。

    注意:不再按表后缀(_dm/_hm/_im/_od 等)区分分区列组合,所有表统一只
    约束 pt_d。如此可避免:表后缀命名与实际分区策略不一致时探查失败,
    或业务需要跨小时/跨分钟扫描时必须手动传 partition_filter 的心智负担。

    Examples:
        >>> _default_partition_filter("dws.user_action_dm")
        "pt_d = '20261013'"
        >>> _default_partition_filter("ads.click_hm")
        "pt_d = '20261013'"
    """
    target = date.today() - timedelta(days=7)
    d = target.strftime("%Y%m%d")
    return f"pt_d = '{d}'"


def _build_probe_sql(
    table: str,
    columns: list[str] | None,
    sample_size: int,
    partition_filter: str | None = None,
) -> str:
    """生成 ``SELECT ... FROM `db`.`tbl` [WHERE partition_filter] LIMIT N`` 形式的 SQL。

    反引号策略:
      - 表名按 ``.`` 拆段,每段独立加反引号 → ``FROM `db`.`tbl``` 而 **非**
        ``FROM `db.tbl```。后者在 Hive/ClickHouse 中会被解析为单个标识符
        ``db.tbl`` 而非 ``db`` 库下的 ``tbl``。ClickHouse 库名/表名都支持点号,
        所以这种"整体加反引号"实际跑不通。
      - 列名单元素分别加反引号(单段名,不存在歧义)。

    ``sample_size`` 已被 _clamp_int 夹紧到 [1, 50],这里是整数路径,
    不接受字符串注入。

    ``partition_filter`` 已被 ``_PARTITION_FILTER_RE`` 白名单校验过(整串匹配,
    只可能由 ``pt_d = 'YYYYMMDD'`` 组成,不含任何用户控制字符),所以这里直接
    拼接是安全的。
    """
    cols_clause = ", ".join(f"`{c}`" for c in columns) if columns else "*"
    # 拆段:每段独立加反引号
    table_quoted = ".".join(f"`{part}`" for part in table.split("."))
    where_clause = f" WHERE {partition_filter}" if partition_filter else ""
    return f"SELECT {cols_clause} FROM {table_quoted}{where_clause} LIMIT {sample_size}"


def _err(
    common: dict[str, Any],
    reason: str,
    *,
    start: float,
    error: str | None = None,
    job_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一的失败响应。``common`` 通常是 ``{"table": table}``。

    错误文本在这里统一收敛：DataOps 失败时 ``errorLog`` 可能是几百 KB 的
    Spark 日志（驱动栈 + 每个失败 task 的栈），原样返回会淹没 agent 上下文。
    头尾保留 + 中间省略，根因（异常抛出处，多在头部）不会丢。
    """
    payload: dict[str, Any] = {
        "ok": False,
        "skipped": True,
        **common,
        "reason": reason,
        "elapsed_sec": round(time.monotonic() - start, 3),
    }
    if error is not None:
        payload["error"] = bound_error_excerpt(error, label="error")
    if job_id is not None:
        payload["job_id"] = job_id
    if extra:
        payload.update(extra)
    # ``extra`` 可能带 errorLog（见 prod 路径），统一收敛
    return bound_error_fields(payload)
