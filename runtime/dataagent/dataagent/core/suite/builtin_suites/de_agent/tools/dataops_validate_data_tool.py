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

给一个**目标表**做数据正确性校验（空值覆盖率 / 单一值比率 / 异常值占比 /
数据行数）。目标表有三种给法，对应三条字段来源：

=====================  ==========================================
给法                   字段从哪来
=====================  ==========================================
表名 ``db.table``      semantic-service 查表的列元数据
DDL（``CREATE TABLE``）从 DDL 的列清单里解析
DML（``INSERT ...``）  从 INSERT 的 SELECT 投影里解析
=====================  ==========================================

三种给法最终都收敛成同一件事：**直连真实目标表做只读统计**。

与交付后 post-hook 那条链路的**区别**（这两点是本工具存在的理由）：

    | | 独立数据验证（本工具） | 交付后 post-hook |
    |---|---|---|
    | 建表 | **不建表** | 按 ``create_*.sql`` 建临时表 |
    | 插入 | **不插入** | 试算 INSERT 到临时表 |
    | 校验对象 | **真实目标表** ``db.table`` | ``adhoctemp.tmp_*`` 临时表 |
    | 临时表替换 | **不做** | 做（目标改写成临时表名） |

所以本工具是**只读**的：不建表、不插入、不删表、不改真实数据。

对应 skill：``de_agent/skill/dataops_validate_data/SKILL.md``。

Best-effort 契约：永不抛异常，所有失败都在返回值里表达。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    _SCOPE_TABLE,
    render_metrics_table,
    render_table_metrics,
    resolve_config,
    run_quality_check,
)
from dataagent.actions.tools.local_tool.dataops_table_schema import (
    SchemaResolutionError,
    exclude_columns,
    extract_ddl_target_table,
    fetch_semantic_columns,
    looks_like_ddl,
    parse_ddl_columns,
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

# 优雅结束时的提示语（没有可校验的输入 / 目标表解析不出来）。
_NO_SQL_HINT = (
    "未检测到可用的校验输入，已跳过数据验证。"
    "请提供以下任一：目标表名（如 `biads.ads_user_profile_ds`）、"
    "一条完整的 INSERT 语句（形如 `INSERT OVERWRITE TABLE db.table SELECT ...`），"
    "或建表 DDL；也可用 `sql_file_path` 指向工作区里的 SQL 文件。"
)


async def validate_data(
    sql: str = "",
    *,
    sql_file_path: str | None = None,
    target_table: str = "",
    partition_filter: str = "",
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """对一张真实表做数据正确性校验。

    **只读**：不建表、不插入、不用临时表替换 —— 直接查真实目标表。

    Args:
        sql: SQL 语句（INSERT / DDL 皆可）。``sql_file_path`` 存在时优先用文件内容。
        sql_file_path: 工作区内 SQL 文件的路径。避免大段 SQL 进 chat
            history —— 推荐用法。
        target_table: 目标表 ``db.table``。留空时从 SQL 里解析（INSERT / DDL）。
            只给表名、没给 SQL 时，字段走 semantic-service。
        partition_filter: 分区过滤条件（形如 ``pt_d = '20261020'``）。留空时从
            INSERT 语句里解析；再留空则用默认分区列 + 一周前的日期。
        _tool_context: 工具执行上下文。

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

    # --- 优雅结束：既没有 SQL 也没有表名 --------------------------------
    resolved_table = (target_table or "").strip()
    if not resolved_sql.strip() and not resolved_table:
        return _fmt(
            f"{_REPORT_PREFIX}{_NO_SQL_HINT}",
            {"ok": True, "skipped": True, "reason": "no_sql", "warnings": [], "details": {}},
        )

    config = resolve_config(_tool_context)

    # --- 解析目标表 + 字段来源 -------------------------------------------
    try:
        resolution = _resolve_target_and_fields(
            resolved_sql,
            resolved_table,
            partition_filter=partition_filter,
            config=config,
            _tool_context=_tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        # 本工具的契约是**永不抛异常** —— 解析阶段出问题也要变成友好跳过，
        # 而不是把堆栈甩给 agent。
        logger.warning(f"[validate_data] target/field resolution failed: {exc!r}")
        return _fmt(
            f"{_REPORT_PREFIX}解析校验输入时发生未预期的错误：{exc}。本次未产出质量结论。",
            {
                "ok": False,
                "skipped": True,
                "reason": "resolution_failed",
                "error": repr(exc),
                "warnings": [],
                "details": {},
            },
        )
    if resolution.get("skipped"):
        return _fmt(f"{_REPORT_PREFIX}{resolution['message']}", resolution["data"])

    resolved_table = resolution["target_table"]
    resolved_filter = resolution["partition_filter"]
    fields = resolution["fields"]
    column_types = resolution["column_types"]

    # --- 跑校验（直连真实目标表） ----------------------------------------
    result = await run_quality_check(
        resolved_sql,
        _tool_context=_tool_context,
        config=config,
        direct_table=resolved_table,
        partition_filter=resolved_filter or None,
        # 注意：传**列表**而不是 ``list or None`` —— 空的字段列表也是「显式指定」，
        # 意味着「字段一个都没有，但表级校验项（如数据行数）仍要跑」。
        fields=fields,
        column_types=column_types,
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
    # 表级指标（数据行数）—— 单独一张表。
    table_metrics_md = render_table_metrics(
        result.get("table_metrics") or {},
        bool(result.get("row_count_truncated")),
    )
    if table_metrics_md:
        body += "\n\n" + table_metrics_md
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
            "field_source": resolution["field_source"],
            "checks": result.get("checks") or [],
            "completed_checks": completed_checks,
            "failed_checks": failed_checks,
            "failed_metrics": result.get("failed_metrics") or {},
            "partial": bool(failed_checks),
            "sample_size": result.get("sample_size"),
            "warnings": warnings,
            "details": details,
            "table_metrics": result.get("table_metrics") or {},
            "markdown": body,
        },
    )


# ============================================================================
# 目标表 / 字段解析
# ============================================================================


def _fetch_column_types_best_effort(
    table_name: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str] | None:
    """查表的列类型（semantic 元数据），拿不到就返回 ``None``。

    **Best-effort**：semantic-service 不可用 / 表不在元数据里 / 网络异常，
    全部静默降级为 ``None`` —— 调用方会退回 workspace 的 DDL，再退回
    ``IS NOT NULL``。这里**绝不能抛异常**，否则整个数据验证会挂掉。

    只用来补充**类型**，不改变字段清单 —— 字段清单由各自的来源决定。
    """
    if not table_name:
        return None
    try:
        _fields, column_types = fetch_semantic_columns(
            table_name, _tool_context=_tool_context
        )
    except SchemaResolutionError as exc:
        logger.debug(f"[validate_data] semantic column types unavailable for {table_name}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[validate_data] semantic column types lookup failed ({exc})")
        return None
    return column_types or None


def _resolve_target_and_fields(
    sql: str,
    target_table: str,
    *,
    partition_filter: str,
    config: Any,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """解析出 ``(目标表, 字段, 字段类型, 字段来源)``。

    字段来源按以下优先级：

    1. **DDL**（``CREATE TABLE ...``）—— 从列清单解析，带类型。
    2. **DML**（``INSERT ... SELECT ...``）—— 从 SELECT 投影解析（别名即列名），
       类型从 workspace 的 DDL 读；读不到就退回 ``IS NOT NULL``。
    3. **只有表名** —— 走 semantic-service 查列元数据，带类型。

    无法解析时返回 ``{"skipped": True, ...}``，由调用方转成友好提示。
    """
    is_insert = bool(sql) and _is_insert_sql(sql)
    is_ddl = bool(sql) and looks_like_ddl(sql)

    # --- 目标表 ---------------------------------------------------------
    resolved_table = target_table
    if not resolved_table and is_insert:
        resolved_table = extract_insert_target_table(sql) or ""
    if not resolved_table and is_ddl:
        resolved_table = extract_ddl_target_table(sql) or ""
    if not resolved_table:
        return {
            "skipped": True,
            "message": (
                "无法确定要校验的目标表，已跳过。"
                "请显式传入 `target_table`（形如 `db.table`），"
                "或提供一条完整的 INSERT / 建表 DDL。"
            ),
            "data": {
                "ok": True,
                "skipped": True,
                "reason": "no_target_table",
                "warnings": [],
                "details": {},
            },
        }

    # --- 字段 -----------------------------------------------------------
    field_source = ""
    fields: list[str] = []
    # ``None`` 表示「类型未知，去 workspace 的 DDL 里找」——
    # DML 投影路径就是这种情况（``None`` → run_quality_check 回退到 DDL）。
    column_types: dict[str, str] | None = {}

    if is_ddl and not is_insert:
        try:
            fields, column_types = parse_ddl_columns(sql)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[validate_data] DDL column parsing failed: {exc}")
        field_source = "ddl"
    elif is_insert:
        fields = _extract_fields_from_insert_sql(sql, config.exclude_fields)
        # DML 只给出列名（投影别名），类型未知 —— 但**目标表的列元数据是已知的**。
        # 用 semantic 查一次目标表的列类型：判空写法（STRING → IsEmpty vs
        # 数值 → IS NOT NULL）直接决定 null_rate 的口径准不准。
        # 查不到就留 None —— run_quality_check 会退回 workspace 的 DDL。
        column_types = _fetch_column_types_best_effort(
            resolved_table, _tool_context=_tool_context
        )
        field_source = "dml"
    else:
        # 只有表名 —— 走 semantic-service。
        try:
            fields, column_types = fetch_semantic_columns(
                resolved_table, _tool_context=_tool_context
            )
            field_source = "semantic"
        except SchemaResolutionError as exc:
            return {
                "skipped": True,
                "message": (
                    f"{exc} 请改用一条 INSERT 语句或建表 DDL 来提供字段清单。"
                ),
                "data": {
                    "ok": True,
                    "skipped": True,
                    "reason": "schema_unavailable",
                    "target_table": resolved_table,
                    "error": str(exc),
                    "warnings": [],
                    "details": {},
                },
            }
        except Exception as exc:  # noqa: BLE001
            # 兜底：本工具的契约是**永不抛异常**。``fetch_semantic_columns``
            # 承诺只抛 ``SchemaResolutionError``，但万一有形态异常的元数据
            # 打穿了那个承诺，也不能让整轮验证炸掉 —— 一律降级成友好跳过。
            logger.warning(f"[validate_data] semantic column lookup failed: {exc!r}")
            return {
                "skipped": True,
                "message": (
                    f"查询表 `{resolved_table}` 的字段元数据时发生未预期的错误：{exc}。"
                    "请改用一条 INSERT 语句或建表 DDL 来提供字段清单。"
                ),
                "data": {
                    "ok": True,
                    "skipped": True,
                    "reason": "schema_unavailable",
                    "target_table": resolved_table,
                    "error": repr(exc),
                    "warnings": [],
                    "details": {},
                },
            }

    # 排除分区列 / 时间戳 / 主键 ID（DDL / semantic 来的字段需要排除；
    # DML 来的字段 ``_extract_fields_from_insert_sql`` 已经排除了）。
    fields = exclude_columns(fields, config.exclude_fields)

    # 表级校验（数据行数）不依赖字段 —— 字段解析不出来时它仍然可以跑。
    has_table_checker = any(c.scope == _SCOPE_TABLE for c in config.checkers)
    if not fields and not has_table_checker:
        return {
            "skipped": True,
            "message": (
                "无法解析出可校验的字段，已跳过。"
                "请确认输入里有具名的输出列（`col` 或 `expr AS alias`），"
                "或换一张字段更明确的表。"
            ),
            "data": {
                "ok": True,
                "skipped": True,
                "reason": "no_checkable_fields",
                "target_table": resolved_table,
                "warnings": [],
                "details": {},
            },
        }

    # --- 分区条件 -------------------------------------------------------
    resolved_filter = (partition_filter or "").strip()
    if not resolved_filter and is_insert:
        resolved_filter = extract_insert_partition_filter(sql)

    return {
        "skipped": False,
        "target_table": resolved_table,
        "partition_filter": resolved_filter,
        "fields": fields,
        "column_types": column_types,
        "field_source": field_source,
    }


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
    logger.debug(
        "[validate_data] ok={} skipped={} reason={}",
        data.get("ok"),
        data.get("skipped"),
        data.get("reason"),
    )
    return {"original_msg": original_msg, "frontend_msg": original_msg, "data": data}
