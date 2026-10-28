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
"""trial_compute_table — run a delivery's DML against a scratch table, then verify its data.

Purpose: "数据试算" — actually execute the INSERT the agent just wrote, then check
that the produced data is sane, *without* touching the real target table.

Flow (single pass, no retry):

    0) re-create the scratch table   (from the deliverable's create_*.sql —
                                    the syntax-validation chain already dropped
                                    its own scratch table, and it used a
                                    different partition date anyway)
      └─ execute  (shared dataops_validate_sql: prod MCP or
                   resource-coordinator, incl. CTE expansion + drop-before-create)
          ├─ data quality check   (shared run_quality_check, see
          │                         dataops_quality_check) — runs while the
          │                         scratch table still exists
          └─ drop scratch table   (shared _cleanup_temp_table_wrapper) —
                                  always, success or failure

Because the scratch-table naming and execution routing are shared with the
DataOps validation path, a trial run and a validation run cannot drift apart:
same rewrite, same execution routing.

**Partition date differs from the static validation chain on purpose.**
The static validation chain (``validate_deliverables``) only asks "does this SQL
run?", so it writes to a partition **one year out** — nothing there can collide
with real data. This chain wants to inspect the *real* produced data, so it
writes to a partition **one week back**, which has already settled. Within this
chain the trial run, the quality check and the cleanup all use that one date, so
the scratch table name always matches the table that was actually written.

This is the **only** place the data quality check runs — the static validation
chain (``validate_deliverables``) deliberately does not run it, so the agent
iterates on broken SQL quickly and the expensive sampling statistics happen
once, after delivery.

Best-effort contract: failures are returned in the payload and never raised.
``quality_check`` warnings do **not** flip ``passed`` — the trial run succeeded,
the data merely has something worth reporting.

See skill: ``de_agent/skill/dataops_quality_check/SKILL.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.dataops_log_utils import bound_error_excerpt
from dataagent.utils.log import logger

# ============================================================================
# Public entry
# ============================================================================


async def trial_compute_table(
    target_table: str = "",
    *,
    dml_path: str | None = None,
    query: str = "",
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Run ``dml_path`` into a scratch table and validate the resulting data.

    Args:
        target_table: The real target table, ``"db.table"``. Used for reporting
            and for the ``dry_run=false`` escape hatch; the scratch rewrite is
            derived from the DML itself.
        dml_path: Workspace path to the INSERT SQL. Required — the SQL is read
            from disk so large statements stay out of chat history.
        query: The user's original request. Kept for diagnostics only.
        _tool_context: Tool execution context.

    Returns:
        ``{"passed", "skipped", "reason"?, "summary", "quality_check"?, "job_id"?}``
    """
    if not dml_path:
        return _skipped("no_dml_path", "未提供 DML 文件路径，无法试算。")

    try:
        with open(dml_path, encoding="utf-8") as fh:
            sql = fh.read().strip()
    except OSError as exc:
        return {
            "passed": False,
            "skipped": False,
            "summary": f"failed to read {dml_path}: {exc}",
            "trigger": "trial_compute",
        }

    if not sql:
        return _skipped("empty_sql", "DML 文件内容为空，没有可试算的 SQL。")

    # Lazy imports: these pull in the dataops backend, which should not be
    # imported at module load for tests that mock them out.
    from dataagent.actions.tools.local_tool.dataops_validate_tool import (
        _is_create_sql,
        dataops_validate_sql,
        partition_date_for_quality,
    )

    # 本链路是「交付后质量校验」，分区日期用**一周之前**（真实产出已稳定落盘）。
    # 语法校验链路用的是「一年之后」；两者刻意不同。试算、校验、DROP 三步必须
    # 用同一个日期，否则临时表名对不上，校验会静默读到空表。
    partition_date = partition_date_for_quality()

    payload: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "trigger": "trial_compute",
        "target_table": (target_table or "").strip() or None,
        "partition_date": partition_date,
    }

    # --- 0) Re-create the scratch table ------------------------------------
    # 语法校验链路已经把 adhoctemp.tmp_* 删掉了（而且它用的是「一年之后」的
    # 日期，跟本链路的临时表压根不是同一张表）。本链路要往一张**存在**的临时表
    # 里写数据才能做质量校验，所以先按交付物里的 DDL 再建一次。
    ddl_path = _find_ddl(_tool_context)
    if ddl_path is not None:
        create_result = await dataops_validate_sql(
            ddl_path.read_text(encoding="utf-8"),
            _tool_context=_tool_context,
            _partition_date=partition_date,
        )
        payload["create_job_id"] = create_result.get("job_id")
        payload["create_passed"] = bool(create_result.get("passed"))
        if not create_result.get("passed"):
            # 建表失败 → 试算必然失败（目标表不存在）。不往下走，直接收敛报错。
            payload["passed"] = False
            payload["error"] = bound_error_excerpt(
                create_result.get("log_analysis_summary")
                or create_result.get("error")
                or "failed to create scratch table for quality check",
                label="summary",
            )
            if create_result.get("log_analysis"):
                payload["log_analysis"] = create_result["log_analysis"]
            # 清理：建表失败也可能留下半成品临时表。
            await _drop_scratch_table(sql, _tool_context, partition_date, payload)
            payload["summary"] = _build_summary(payload)
            return payload

    # --- 1) Execute against the scratch table ------------------------------
    # dataops_validate_sql rewrites the target to adhoctemp.tmp_* itself, so the
    # scratch name, the partition date and the execution routing all stay in
    # lock-step — as long as we hand it the same partition_date we later use for
    # the quality check and the cleanup.
    try:
        validate_result = await dataops_validate_sql(
            sql,
            _tool_context=_tool_context,
            _partition_date=partition_date,
            # 质量校验链路专有：把源表上的 pt_d **范围**过滤收敛成单日。
            # 交付物常用范围分区表达时间窗口（pt_d <= '$date' AND pt_d >=
            # '${start_time,-30,yyyyMMdd}'），而 DataOps 后端不替换这些占位符，
            # 源表会被过滤成空集 → 试算扫不到任何行。质量校验只关心产出数据
            # 长什么样，不需要保留时间窗口宽度，收敛成单日即可。
            _collapse_partition_range=True,
        )

        payload["passed"] = bool(validate_result.get("passed"))
        payload["job_id"] = validate_result.get("job_id")
        if not payload["passed"]:
            payload["skipped"] = bool(validate_result.get("skipped"))
            # ``error`` 可能是一整份 Spark 日志(生产端内联),summary 必须收敛,
            # 否则这一行就会把几十万字塞进 agent 上下文。
            payload["error"] = bound_error_excerpt(
                validate_result.get("log_analysis_summary")
                or validate_result.get("error")
                or "trial compute execution failed",
                label="summary",
            )
            # Surface the structured log analysis so the agent can fix the SQL
            # without a second round-trip.
            if validate_result.get("log_analysis"):
                payload["log_analysis"] = validate_result["log_analysis"]

        # --- 2) Data quality check, while the scratch table still exists ---
        # DDL is skipped: there is no data to check yet.
        if payload["passed"] and not _is_create_sql(sql):
            from dataagent.actions.tools.local_tool.dataops_quality_check import run_quality_check

            quality_result = await run_quality_check(
                sql,
                _tool_context=_tool_context,
                _partition_date=partition_date,
            )
            if quality_result is not None:
                payload["quality_check"] = quality_result
                if not quality_result.get("ok"):
                    logger.warning(
                        f"[trial_compute] data quality check reported warnings "
                        f"(non-blocking): {quality_result.get('warnings')}"
                    )
    finally:
        # --- 3) Drop the scratch table -------------------------------------
        # 不管成功与否都删。DDL/DML 都删 —— 本链路的临时表是我们自己刚建的，
        # 不像语法校验链路那样需要留给后续 DML 使用。
        await _drop_scratch_table(sql, _tool_context, partition_date, payload)

    payload["summary"] = _build_summary(payload)
    return payload


async def _drop_scratch_table(
    sql: str,
    _tool_context: ToolExecutionContext,
    partition_date: str,
    payload: dict[str, Any],
) -> None:
    """Drop the adhoctemp scratch table; never raises."""
    from dataagent.actions.tools.local_tool.dataops_validate_tool import (
        _cleanup_temp_table_wrapper,
    )

    try:
        cleanup_job_id = await _cleanup_temp_table_wrapper(
            sql,
            _tool_context=_tool_context,
            partition_date=partition_date,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[trial_compute] scratch table cleanup failed (ignored): {exc}")
        return
    if cleanup_job_id:
        payload["cleanup_job_id"] = cleanup_job_id


def _find_ddl(_tool_context: ToolExecutionContext) -> Path | None:
    """Locate the deliverable ``create_*.sql`` in the workspace.

    Mirrors ``verify._locate_sql``: the DDL defines the scratch table's
    schema, so the quality chain must create it with exactly that DDL before
    running the INSERT into it.
    """
    runtime = getattr(_tool_context, "runtime", None)
    raw = getattr(runtime, "workspace_dir", None) if runtime is not None else None
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return None
    try:
        workspace = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not workspace.is_dir():
        return None

    # Prefer ``sql/`` (organized location) over the workspace root.
    for directory in (workspace / "sql", workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            name = path.name.lower()
            if name.startswith("create_") and name.endswith(".sql") and path.is_file():
                return path
    return None


def _skipped(reason: str, message: str = "") -> dict[str, Any]:
    """跳过的返回值 —— **优雅结束**，不是错误。

    ``message`` 是给用户看的人话；缺省时退回到 ``reason``，保证前端至少
    有一个可展示的说明，而不是一句机器码。
    """
    return {
        "passed": True,
        "skipped": True,
        "reason": reason,
        "summary": f"[trial_compute] skipped: {reason}",
        "message": message or reason,
        "trigger": "trial_compute",
    }


def _build_summary(payload: dict[str, Any]) -> str:
    """Render a one-line, scannable summary of the trial run."""
    status = "PASSED" if payload.get("passed") else "FAILED"
    if payload.get("skipped"):
        status = "SKIPPED"
    summary = f"[trial_compute] {status}"
    if payload.get("job_id"):
        summary += f" job_id={payload['job_id']}"

    error = payload.get("error")
    if error:
        summary += f" | {error}"

    quality = payload.get("quality_check")
    if quality is not None:
        if quality.get("ok"):
            summary += " | quality_check=ok"
        elif not quality.get("sampled", True):
            # 校验没跑起来（提交失败/超时）—— 不能报成「0 warning(s)」，
            # 那会让读日志的人以为数据已经校验通过。
            reason = quality.get("error") or "check did not run"
            summary += f" | quality_check=not-run ({reason})"
        elif quality.get("failed_checks"):
            # 部分完成：跑出来的指标有效，没跑成的项单独说明。
            failed = quality.get("failed_checks") or []
            names = ",".join(str(item.get("name")) for item in failed)
            summary += f" | quality_check=partial ({names} failed)"
        else:
            warnings = quality.get("warnings") or []
            summary += f" | quality_check={len(warnings)} warning(s)"
    return summary
