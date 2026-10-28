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
"""独立数据验证工具（dataops_validate_data）。

给一条 **现成的 INSERT 语句**，直接对它写入的**真实目标表**做数据正确性校验
（空值覆盖率 / 单一值比率 / 异常值占比）。

与交付后 post-hook 那条链路的**区别**（这两点是本工具存在的理由）：

| | 独立数据验证（本工具） | 交付后 post-hook |
|---|---|---|
| 建表 | **不建表** | 按 ``create_*.sql`` 建临时表 |
| 插入 | **不插入** | 试算 INSERT 到临时表 |
| 校验对象 | **真实目标表** ``db.table`` | ``adhoctemp.tmp_*`` 临时表 |
| 临时表替换 | **不做** | 做（目标改写成临时表名） |

所以本工具是**只读**的：不建表、不插入、不删表、不改真实数据。它只是从 INSERT
里解析出「目标表 + 投影字段」，然后对那张真实表跑统计。

对应 skill：``de_agent/skill/dataops_validate_data/SKILL.md``。

Best-effort 契约：永不抛异常，所有失败都在返回值里表达。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    render_metrics_table,
    resolve_config,
    run_quality_check,
)
from dataagent.actions.tools.local_tool.dataops_validate_tool import (
    _extract_fields_from_insert_sql,
    _is_insert_sql,
    extract_insert_partition_filter,
    extract_insert_target_table,
)
from dataagent.utils.log import logger

# 报告前缀：让前端一眼看出这是「数据验证」的结果，而不是 agent 自己的叙述。
_REPORT_PREFIX = "【数据验证】"

# 优雅结束时的提示语（没有 SQL / 不是 INSERT / 解析不出目标表）。
_NO_SQL_HINT = (
    "未检测到可用的 INSERT 语句，已跳过数据验证。"
    "请提供一条完整的 INSERT 语句（形如 "
    "`INSERT OVERWRITE TABLE db.table SELECT ...`），"
    "或用 `sql_file_path` 指向工作区里的 insert_*.sql 文件。"
)


async def validate_data(
    sql: str = "",
    *,
    sql_file_path: str | None = None,
    target_table: str = "",
    partition_filter: str = "",
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """对一条 INSERT 写入的真实目标表做数据正确性校验。

    **只读**：不建表、不插入、不用临时表替换 —— 直接查真实目标表。

    Args:
        sql: INSERT 语句文本。``sql_file_path`` 存在时优先用文件内容。
        sql_file_path: 工作区内 ``insert_*.sql`` 的路径。避免大段 SQL 进 chat
            history —— 推荐用法。
        target_table: 目标表 ``db.table``。留空时从 INSERT 语句里解析。
        partition_filter: 分区过滤条件（形如 ``pt_d = '20261020'``）。留空时从
            INSERT 语句里解析；再留空则用默认分区列 + 一周前的日期。

    Returns:
        ``{"original_msg", "frontend_msg", "data"}``。``data`` 含 ``ok``、
        ``skipped``、``reason``、``warnings``、``details``、``target_table`` 等。
    """
    try:
        resolved_sql = _resolve_sql(sql, sql_file_path)
    except OSError as exc:
        return _fmt(
            f"{_REPORT_PREFIX}读取 SQL 文件失败：{exc}",
            {"ok": False, "skipped": True, "reason": "sql_read_failed", "error": str(exc)},
        )

    # --- 优雅结束：没有 SQL --------------------------------------------
    if not resolved_sql.strip():
        return _fmt(
            f"{_REPORT_PREFIX}{_NO_SQL_HINT}",
            {"ok": True, "skipped": True, "reason": "no_sql", "warnings": [], "details": {}},
        )

    # --- 优雅结束：不是 INSERT ------------------------------------------
    if not _is_insert_sql(resolved_sql):
        return _fmt(
            f"{_REPORT_PREFIX}提供的不是 INSERT 语句（只有 INSERT 写入的数据才需要验证），已跳过。",
            {"ok": True, "skipped": True, "reason": "not_insert_sql", "warnings": [], "details": {}},
        )

    # --- 解析目标表 -----------------------------------------------------
    resolved_table = (target_table or "").strip() or extract_insert_target_table(resolved_sql)
    if not resolved_table:
        return _fmt(
            f"{_REPORT_PREFIX}无法从 INSERT 语句中解析出目标表，已跳过。"
            "请确认语句形如 `INSERT OVERWRITE TABLE db.table SELECT ...`，"
            "或显式传入 target_table。",
            {"ok": True, "skipped": True, "reason": "no_target_table", "warnings": [], "details": {}},
        )

    # --- 解析分区条件 ---------------------------------------------------
    resolved_filter = (partition_filter or "").strip() or extract_insert_partition_filter(resolved_sql)

    # --- 解析可校验字段 -------------------------------------------------
    config = resolve_config(_tool_context)
    fields = _extract_fields_from_insert_sql(resolved_sql, config.exclude_fields)
    if not fields:
        return _fmt(
            f"{_REPORT_PREFIX}无法从 INSERT 的 SELECT 投影中解析出可校验字段，已跳过。"
            "请确认 SELECT 里有具名的输出列（`col` 或 `expr AS alias`）。",
            {
                "ok": True,
                "skipped": True,
                "reason": "no_checkable_fields",
                "target_table": resolved_table,
                "warnings": [],
                "details": {},
            },
        )

    # --- 跑校验（直连真实目标表） ----------------------------------------
    result = await run_quality_check(
        resolved_sql,
        _tool_context=_tool_context,
        config=config,
        direct_table=resolved_table,
        partition_filter=resolved_filter or None,
    )
    if result is None:
        return _fmt(
            f"{_REPORT_PREFIX}数据验证未能执行（校验不可用或目标表无法访问），本次未产出质量结论。",
            {
                "ok": False,
                "skipped": True,
                "reason": "check_not_run",
                "target_table": resolved_table,
                "warnings": [],
                "details": {},
            },
        )

    warnings = result.get("warnings") or []
    details = result.get("details") or {}
    ok = bool(result.get("ok"))
    failed_checks = result.get("failed_checks") or []
    completed_checks = result.get("completed_checks") or []

    if not result.get("sampled", True):
        # 校验没跑起来 —— 不能报成「全部通过」，也不能报成「0 处告警」。
        reason = result.get("error") or "未知原因"
        return _fmt(
            f"{_REPORT_PREFIX}数据验证未能执行（{reason}），本次未产出质量结论。",
            {
                "ok": False,
                "skipped": False,
                "reason": "check_not_run",
                "target_table": resolved_table,
                "error": reason,
                "warnings": [],
                "details": {},
            },
        )

    table_md = render_metrics_table(details, warnings, result.get("failed_metrics") or {})
    if ok:
        summary = (
            f"{_REPORT_PREFIX}数据验证通过（目标表 `{resolved_table}`，"
            f"校验项：{'、'.join(result.get('checks') or [])}，采样 {result.get('sample_size')} 行）。"
        )
    elif failed_checks:
        # 部分完成：跑出来的指标照常给结论，失败的校验项单独列出。
        # 早先这里因为「一条挂了 = 整个校验没跑成」而丢弃全部结果。
        done = "、".join(completed_checks) or "无"
        summary = (
            f"{_REPORT_PREFIX}数据验证**部分完成**（目标表 `{resolved_table}`，"
            f"已完成的校验项：{done}，采样 {result.get('sample_size')} 行）。"
        )
    else:
        summary = (
            f"{_REPORT_PREFIX}数据验证发现 {len(warnings)} 处告警"
            f"（目标表 `{resolved_table}`，采样 {result.get('sample_size')} 行）。"
        )

    body = "\n".join([summary, "", table_md])
    if failed_checks:
        body += "\n\n" + "未完成的校验项：\n" + "\n".join(
            f"- {item.get('name')}：{item.get('reason') or item.get('status')}"
            for item in failed_checks
        )
        body += "\n\n以上校验项未产出结果（超时或执行失败），其余结果如上表所示。"
    if warnings:
        body += "\n\n" + "\n".join(f"- {warning}" for warning in warnings[:20])
        body += "\n\n请检查对应字段的口径（是否漏 JOIN、INNER JOIN 丢数据、分组粒度错误等）。"

    return _fmt(
        body,
        {
            "ok": ok,
            "skipped": False,
            "reason": "",
            "target_table": resolved_table,
            "partition_filter": resolved_filter,
            "checks": result.get("checks") or [],
            "completed_checks": completed_checks,
            "failed_checks": failed_checks,
            "failed_metrics": result.get("failed_metrics") or {},
            "partial": bool(failed_checks),
            "sample_size": result.get("sample_size"),
            "warnings": warnings,
            "details": details,
            "markdown": body,
        },
    )


# ============================================================================
# Helpers
# ============================================================================


def _resolve_sql(sql: str, sql_file_path: str | None) -> str:
    """从 ``sql_file_path`` / ``sql`` 收敛出 SQL 文本。"""
    if sql_file_path:
        path = Path(sql_file_path).expanduser()
        return path.read_text(encoding="utf-8").strip()
    return str(sql or "").strip()


def _fmt(original_msg: str, data: dict[str, Any]) -> dict[str, Any]:
    """统一工具返回结构（``original_msg`` / ``frontend_msg`` / ``data``）。

    ``frontend_msg`` 与 ``original_msg`` 相同 —— Markdown 表格在前端直接渲染，
    agent 侧看到的也是同一份内容。
    """
    logger.debug("[validate_data] ok={} skipped={} reason={}", data.get("ok"), data.get("skipped"), data.get("reason"))
    return {"original_msg": original_msg, "frontend_msg": original_msg, "data": data}
