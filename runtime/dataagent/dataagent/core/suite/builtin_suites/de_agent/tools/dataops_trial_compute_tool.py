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
                   resource-coordinator, incl. CTE expansion + drop-before-create
                   + row limit — see below)
          ├─ data quality check   (shared run_quality_check, see
          │                         dataops_quality_check) — runs while the
          │                         scratch table still exists
          └─ drop scratch table   (shared _cleanup_temp_table_wrapper) —
                                  always, success or failure

Because the scratch-table naming and execution routing are shared with the
DataOps validation path, a trial run and a validation run cannot drift apart:
same rewrite, same execution routing.

**The trial write is row-limited.**
The quality check only reads the first ``quality_check.sample_size`` rows of
the produced data (default 10000), so rows past that are never seen. The trial
INSERT is therefore capped at the sample size (see
``dataops_validate_tool._limit_insert_rows``): the sampled rows are identical
to a full write's, only the elapsed time changes.

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

import asyncio
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
    from dataagent.actions.tools.local_tool.dataops_quality_check import resolve_config
    from dataagent.actions.tools.local_tool.dataops_validate_tool import (
        _clamp_row_limit,
        _is_create_sql,
        dataops_validate_sql,
        partition_date_for_quality,
    )

    # 本链路是「交付后质量校验」，分区日期用**一周之前**（真实产出已稳定落盘）。
    # 语法校验链路用的是「一年之后」；两者刻意不同。试算、校验、DROP 三步必须
    # 用同一个日期，否则临时表名对不上，校验会静默读到空表。
    partition_date = partition_date_for_quality()

    # --- 试算写入行数上限 -------------------------------------------------
    # 质量校验只对产出数据做 **LIMIT sample_size** 的采样统计 —— 写入超过
    # sample_size 的那部分数据**永远不会被校验看到**。所以试算时把写入量截断
    # 到采样量级：校验读到的行与全量写入时读到的行完全一致（前 N 行），
    # 结论不变，但试算耗时大幅下降。
    #
    # 截断量跟随 ``quality_check.sample_size``（默认 10000）；两者必须一致，
    # 否则「采样量 > 写入量」会让校验看到的样本不完整。
    # ``sample_size <= 0`` 表示不采样（全量扫描），此时不能截断。
    quality_config = resolve_config(_tool_context)
    row_limit = _clamp_row_limit(quality_config.sample_size) if quality_config.sample_size > 0 else 0
    # DDL 没有数据可写 —— 不截断（``CREATE TABLE ... LIMIT`` 是语法错误）。
    if _is_create_sql(sql):
        row_limit = 0

    payload: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "trigger": "trial_compute",
        "target_table": (target_table or "").strip() or None,
        "partition_date": partition_date,
        # 试算写入行数上限（= 校验采样量）。写进 payload 便于排查时知道
        # 「这次试算只写了前 N 行」。
        "row_limit": row_limit,
    }

    # --- 0) Re-create the scratch table ------------------------------------
    # 语法校验链路已经把 adhoctemp.tmp_* 删掉了（而且它用的是「一年之后」的
    # 日期，跟本链路的临时表压根不是同一张表）。本链路要往一张**存在**的临时表
    # 里写数据才能做质量校验，所以先按交付物里的 DDL 再建一次。
    ddl_path = _find_ddl(_tool_context)
    ddl_sql = ""
    if ddl_path is not None:
        ddl_sql = ddl_path.read_text(encoding="utf-8")
        create_result = await dataops_validate_sql(
            ddl_sql,
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
            # **DDL 与 DML 都要清** —— 本链路的建表是我们自己刚做的，
            # 不像语法校验链路那样要把表留给后续 DML 用。
            await _drop_scratch_tables(
                [sql, ddl_sql], _tool_context, partition_date, payload
            )
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
            # 质量校验链路专有：把试算的写入量截断到采样量级（= row_limit）。
            # 校验只读前 N 行，写入更多行既看不到也白等。
            _row_limit=row_limit,
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
                # 试算写入被截断到 row_limit 行 —— 数据行数只是下界，
                # 校验结果里会带 row_count_truncated 标记。
                row_limit=row_limit,
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
        # 不管成功与否都删。**DDL 与 DML 都删** —— 本链路的临时表是我们自己
        # 刚建的，不像语法校验链路那样需要留给后续 DML 使用。
        await _drop_scratch_tables(
            [sql, ddl_sql], _tool_context, partition_date, payload
        )

    payload["summary"] = _build_summary(payload)
    return payload


async def _drop_scratch_tables(
    sqls: list[str],
    _tool_context: ToolExecutionContext,
    partition_date: str,
    payload: dict[str, Any],
) -> None:
    """Drop every adhoctemp scratch table these SQLs created; never raises.

    **两条 SQL 都要清**：DDL 建的那张 + DML 写的那张。早先这里只清 DML 推导
    出来的那一张 —— 而 DDL 与 DML 指向**不同**目标表时（并非罕见），
    DDL 建出来的临时表就永远留在 adhoctemp 里。

    **绝不抛异常。** 这里在 ``trial_compute_table`` 的 ``finally`` 里被调用 ——
    抛异常会替换掉已经算好的试算结论（``payload["passed"]`` / ``summary``）。

    ``except Exception`` 接不住 ``asyncio.CancelledError``（继承自
    ``BaseException``）—— 所以这里显式捕获并**重新抛出**它。

    注意：这里是 ``await`` 在调用方的任务里的，所以**不能吞掉**取消信号 ——
    吞掉会让任务「正常结束」，``task.cancelled()`` 变成 ``False``，
    被取消的作业于是假装跑完了。
    """
    from dataagent.actions.tools.local_tool.dataops_validate_tool import (
        cleanup_temp_tables,
    )

    try:
        cleanup_job_ids = await cleanup_temp_tables(
            [text for text in sqls if (text or "").strip()],
            _tool_context=_tool_context,
            partition_date=partition_date,
            context_label="trial_compute",
        )
    except asyncio.CancelledError:
        # 取消必须继续往外传 —— 吞掉它会让「被取消的作业」假装正常结束。
        logger.warning(
            "[trial_compute] scratch table cleanup cancelled; "
            "adhoctemp scratch tables may remain"
        )
        raise
    except Exception as exc:  # noqa: BLE001
        # 兜底：清理失败**绝不能**影响试算结论。
        logger.warning(
            f"[trial_compute] scratch table cleanup failed (ignored): {exc!r}; "
            "adhoctemp scratch tables may remain"
        )
        return
    if cleanup_job_ids:
        payload["cleanup_job_ids"] = cleanup_job_ids
        payload["cleanup_job_id"] = cleanup_job_ids[0]


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
