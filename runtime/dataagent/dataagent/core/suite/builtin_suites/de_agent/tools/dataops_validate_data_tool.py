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
"""数据校验工具（dataops_validate_data）。

**用户输入「执行数据校验」时由 agent 调用**。两条路径，自动选：

=====================  ====================================================
路径                   什么时候走 / 做什么
=====================  ====================================================
①试算链路              workspace 里有交付物（``insert_*.sql``）时走这条
                       建临时表 → 试算 INSERT → 全部校验项 → 流程型校验
                       → error 级违规自修正闭环 → 落盘
②直连兜底              workspace 里**没有**交付物、只给了表名时走这条
                       只读查真实目标表 → 同一套校验项 → 报告
                       **不建表、不插入、不改真实数据、不做自修正**
=====================  ====================================================

**为什么不再挂 post-hook 自动跑**：试算要连 DataOps 跑一遍（可能几分钟），
自动挂在交付后会拖住每一轮交付 —— 用户先看到交付、再等几分钟看校验结论。
改成**用户显式触发**（输入「执行数据校验」）：交付立刻返回，校验按需触发。

**校验对象是「这份交付物跑出来的数据」** —— 走试算链路（真实目标表里的数据
可能是上一版 SQL 写的，校验它没有意义）。只有在**没有交付物**、用户只想
体检存量数据时，才走直连兜底路径。

对应 skill：``de_agent/skill/dataops_validate_data/SKILL.md``。

Best-effort 契约：永不抛异常，所有失败都在返回值里表达。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.dataops_correctness_flow import resolve_llm
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    _SCOPE_TABLE,
    format_flow_note,
    resolve_config,
    run_quality_check,
    violations_from_dicts,
)
from dataagent.actions.tools.local_tool.dataops_quality_report_format import (
    render_fixed_report,
)
from dataagent.actions.tools.local_tool.dataops_self_correct import run_self_correct
from dataagent.actions.tools.local_tool.dataops_table_schema import (
    SchemaResolutionError,
    exclude_columns,
    fetch_semantic_columns,
)
from dataagent.utils.log import logger

# 报告前缀：让前端一眼看出这是「数据校验」的结果，而不是 agent 自己的叙述。
_REPORT_PREFIX = "【数据校验】"

#: 自修正后备份文件的扩展名（``insert_x.sql.bak.1``）。
_BACKUP_SUFFIX = ".bak"

#: 摘要区里每项抽样展示多少行 —— 摘要区要能塞进 500 字符的截断窗口。
_SUMMARY_SAMPLE_ROWS = 3

#: 摘要区里每个单元格最多保留多少字符。
_SUMMARY_CELL_CHARS = 40

#: 流程型校验的中文名（摘要区用中文，不用内部英文 key）。
_FLOW_CHECK_LABELS = {
    "common_sense": "常识违规检测",
    "source_reconciliation": "抽样回源比对",
}


async def validate_data(
    sql: str = "",
    *,
    sql_file_path: str | None = None,
    target_table: str = "",
    dml_path: str | None = None,
    partition_filter: str = "",
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """对一份交付物做**试算 + 数据正确性校验**（必要时自修正）。

    **用户输入「执行数据校验」时调用本工具。**

    Args:
        sql: SQL 语句。``sql_file_path`` / ``dml_path`` 存在时优先用文件内容。
        sql_file_path: 工作区内 SQL 文件的路径。避免大段 SQL 进 chat history
            —— 推荐用法。
        target_table: 目标表 ``db.table``。留空时从交付物里猜（仅用于报告）。
        dml_path: 工作区里 INSERT SQL 的路径。留空时**自动在 workspace 里找**
            （``insert_*.sql``，优先最新修改的那个）。
        partition_filter: 分区过滤条件（形如 ``pt_d = '20261020'``）。**只对
            直连真实目标表的兜底路径生效** —— 试算链路查的是临时表，里面
            只有本次试算的数据，不需要（也不能）加分区过滤。留空时直连模式
            查「一周之前」那一天。
        _tool_context: 工具执行上下文。

    Returns:
        ``{"original_msg", "frontend_msg", "data"}``。``data`` 含 ``ok``、
        ``skipped``、``reason``、``quality_check``、``markdown`` 等。
    """
    try:
        return await _run_validation(
            sql=sql,
            sql_file_path=sql_file_path,
            target_table=target_table,
            dml_path=dml_path,
            partition_filter=partition_filter,
            _tool_context=_tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        # 本工具的契约是**永不抛异常** —— 校验挂了也要变成友好结论。
        logger.warning(f"[validate_data] validation failed: {exc!r}")
        return _fmt(
            f"{_REPORT_PREFIX}数据校验执行失败：{exc}。本次未产出质量结论。",
            {
                "ok": False,
                "skipped": False,
                "reason": "validation_failed",
                "error": repr(exc),
                "warnings": [],
                "details": {},
            },
        )


async def _run_validation(
    *,
    sql: str,
    sql_file_path: str | None,
    target_table: str,
    dml_path: str | None,
    partition_filter: str,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """试算 + 校验 + 自修正的主流程。"""
    # --- 0) 定位交付物 ---------------------------------------------------
    resolved_path, inline_sql = _resolve_dml_path(
        dml_path=dml_path,
        sql_file_path=sql_file_path,
        sql=sql,
        runtime=getattr(_tool_context, "runtime", None),
    )

    if resolved_path is not None:
        try:
            sql_text = resolved_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return _fmt(
                f"{_REPORT_PREFIX}读取交付物失败：{exc}",
                {
                    "ok": False,
                    "skipped": True,
                    "reason": "sql_read_failed",
                    "error": str(exc),
                    "warnings": [],
                    "details": {},
                },
            )
        if not sql_text:
            return _fmt(
                f"{_REPORT_PREFIX}交付物内容为空，没有可校验的 SQL。",
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "empty_sql",
                    "warnings": [],
                    "details": {},
                },
            )
    else:
        sql_text = inline_sql
        if not sql_text:
            # --- 兜底：workspace 里没有交付物，但用户给了表名 ----------------
            # 只知道表名 → 没有「这份交付物」可试算 → 走**直连真实目标表**
            # 的校验路径（只读，不建表不插入）。
            if target_table.strip():
                return await _validate_direct_table(
                    target_table=target_table.strip(),
                    partition_filter=partition_filter,
                    _tool_context=_tool_context,
                )
            return _fmt(
                f"{_REPORT_PREFIX}未找到可校验的交付物，已跳过。"
                "请先完成 SQL 交付（workspace 里要有 `insert_*.sql`），"
                "或显式传入 `dml_path` / `sql_file_path` / `target_table`。",
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_dml",
                    "warnings": [],
                    "details": {},
                },
            )

    # --- 1) 试算 + 校验 --------------------------------------------------
    result = await _invoke_trial_compute(
        sql=sql_text,
        target_table=target_table or _guess_target_table(sql_text),
        _tool_context=_tool_context,
    )

    # --- 2) 自修正闭环 ---------------------------------------------------
    # 自修正要**写回交付文件** —— 没有文件就没有底稿可改，自修正会被跳过
    # （报告里会注明「自修正未执行」，不会伪装成修好了）。
    if resolved_path is not None:
        result = await _maybe_self_correct(
            dml_path=resolved_path,
            original_sql=sql_text,
            result=result,
            _tool_context=_tool_context,
        )
    else:
        _mark_self_correct_unavailable(result)

    # --- 3) 落盘 ---------------------------------------------------------
    workspace = _resolve_workspace(getattr(_tool_context, "runtime", None))
    persisted = (
        _persist_quality_report(workspace=workspace, result=result)
        if workspace is not None
        else None
    )

    # --- 4) 渲染 ---------------------------------------------------------
    report = _render_report(result=result)
    if persisted is not None:
        report = f"{report}\n\n（校验结论已落盘：`{persisted}`，可据此复核）"

    return _fmt(
        report,
        {
            "ok": bool(result.get("passed")) and not _has_error_violations(result),
            "skipped": bool(result.get("skipped")),
            "reason": result.get("reason") or "",
            "target_table": result.get("target_table"),
            "passed": bool(result.get("passed")),
            "quality_check": result.get("quality_check") or {},
            "violations": (result.get("quality_check") or {}).get("violations") or [],
            "warnings": (result.get("quality_check") or {}).get("warnings") or [],
            "details": (result.get("quality_check") or {}).get("details") or {},
            "table_metrics": (result.get("quality_check") or {}).get("table_metrics") or {},
            "skipped_checks": (result.get("quality_check") or {}).get("skipped_checks") or [],
            "failed_checks": (result.get("quality_check") or {}).get("failed_checks") or [],
            # 空表 → 字段级校验是空跑 → 必须要求人工复核（阻断交付）。
            "needs_manual_review": bool(result.get("needs_manual_review"))
            or _has_error_violations(result),
            "persisted_report": str(persisted) if persisted else None,
            "markdown": report,
        },
    )


# ============================================================================
# 兜底路径：直连真实目标表
# ============================================================================
#
# **什么时候走这条**：workspace 里**没有** DDL / DML 可试算，但用户只给了
# 表名（``target_table``）—— 「这张表现在存的数据对不对」。
#
# **与试算链路的区别**：
#
#   |                    | 试算链路（主）          | 直连（兜底）              |
#   |--------------------|-------------------------|---------------------------|
#   | 校验对象            | 临时表 tmp_*（本次产出） | 真实表 db.table（存量数据） |
#   | 建表 / 插入         | 建临时表 + 试算 INSERT   | **都不做**（只读）         |
#   | 自修正闭环          | 做（改 SQL → 重跑全流程） | **不做**（改不了存量数据）  |
#   | 字段来源            | 交付物投影              | semantic 元数据            |
#
# **为什么不做自修正**：直连模式下没有「交付物」可改 —— 真实表里的数据是
# 历史产出，改它需要重跑上游任务，不是校验器该做的事。error 级违规如实报出，
# 由用户决定怎么办。
#
# **口径差异必须写清**：主键重复率在试算链路里算的是「临时表那 N 行」，
# 在直连模式里算的是**真实表全量** —— 报告里要标明。


async def _validate_direct_table(
    *,
    target_table: str,
    partition_filter: str,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """直连真实目标表做只读校验（兜底路径）。

    **只读**：不建表、不插入、不删表、不改真实数据。
    """
    config = resolve_config(_tool_context)

    # --- 字段解析：只有表名 → 走 semantic 元数据 -------------------------
    try:
        fields, column_types = fetch_semantic_columns(
            target_table, _tool_context=_tool_context
        )
    except SchemaResolutionError as exc:
        return _fmt(
            f"{_REPORT_PREFIX}{exc} 请提供 `insert_*.sql` 或建表 DDL 以便校验交付物。",
            {
                "ok": True,
                "skipped": True,
                "reason": "schema_unavailable",
                "target_table": target_table,
                "error": str(exc),
                "warnings": [],
                "details": {},
            },
        )
    except Exception as exc:  # noqa: BLE001
        # 本工具的契约是**永不抛异常**。
        logger.warning(f"[validate_data] semantic column lookup failed: {exc!r}")
        return _fmt(
            f"{_REPORT_PREFIX}查询表 `{target_table}` 的字段元数据时发生未预期的错误：{exc}。",
            {
                "ok": True,
                "skipped": True,
                "reason": "schema_unavailable",
                "target_table": target_table,
                "error": repr(exc),
                "warnings": [],
                "details": {},
            },
        )

    # 排除名单只影响「覆盖率统计」的字段集 —— **主键匹配要用未排除的字段**
    # （``udid`` / ``id`` 这些恰恰经常就是主键本身）。
    unexcluded_fields = list(fields)
    fields = exclude_columns(fields, config.exclude_fields)
    # 表级校验（数据行数）不依赖字段 —— 字段解析不出来时它仍然可以跑。
    has_table_checker = any(c.scope == _SCOPE_TABLE for c in config.checkers)
    if not fields and not has_table_checker:
        return _fmt(
            f"{_REPORT_PREFIX}无法解析出可校验的字段，已跳过。"
            "请确认输入里有具名的输出列，或换一张字段更明确的表。",
            {
                "ok": True,
                "skipped": True,
                "reason": "no_checkable_fields",
                "target_table": target_table,
                "warnings": [],
                "details": {},
            },
        )

    # --- 直连校验（只读） -------------------------------------------------
    result = await run_quality_check(
        "",
        _tool_context=_tool_context,
        config=config,
        direct_table=target_table,
        partition_filter=partition_filter or None,
        # 空的字段列表也是「显式指定」—— 表级校验项（数据行数）仍要跑。
        fields=fields,
        column_types=column_types,
        # 主键匹配用「未排除」的字段集（与试算链路同一口径）。
        unexcluded_fields=unexcluded_fields,
        # semantic 元数据里的主键标记 —— 直连模式没有 DDL、没有 IR 声明，
        # 这是**唯一**能拿到主键的来源（不传 → 主键重复率与回源比对整项跳过）。
        # 查不到时由校验器内部兜底（按 ``direct_table`` 再查一次）。
    )
    if result is None:
        return _fmt(
            f"{_REPORT_PREFIX}数据验证未能执行（校验不可用或目标表无法访问），"
            "本次未产出质量结论。",
            {
                "ok": False,
                "skipped": False,
                "reason": "check_not_run",
                "target_table": target_table,
                "warnings": [],
                "details": {},
            },
        )

    if not result.get("sampled", True):
        # 校验没跑起来 —— 不能报成「全部通过」，也不能报成「0 处告警」。
        reason = result.get("error") or "未知原因"
        return _fmt(
            f"{_REPORT_PREFIX}数据验证未能执行（{reason}），本次未产出质量结论。",
            {
                "ok": False,
                "skipped": False,
                "reason": "check_not_run",
                "target_table": target_table,
                "error": reason,
                "warnings": [],
                "details": {},
            },
        )

    report = _render_direct_report(result=result, target_table=target_table)

    return _fmt(
        report,
        {
            "ok": bool(result.get("ok")) and not _has_error_violations(result),
            "skipped": False,
            "reason": "",
            "target_table": target_table,
            "mode": "direct_table",
            "passed": bool(result.get("ok")),
            "quality_check": result,
            "violations": result.get("violations") or [],
            "warnings": result.get("warnings") or [],
            "details": result.get("details") or {},
            "table_metrics": result.get("table_metrics") or {},
            "skipped_checks": result.get("skipped_checks") or [],
            "failed_checks": result.get("failed_checks") or [],
            "needs_manual_review": _has_error_violations(result),
            "markdown": report,
        },
    )


def _render_direct_report(*, result: dict[str, Any], target_table: str) -> str:
    """渲染直连模式的报告。

    与试算链路的报告**形状一致**（同样的指标表 / 违规明细 / 交付结论），
    只有摘要句和口径说明不同 —— 让用户切换两种模式时不需要重新学怎么看。
    """
    warnings = result.get("warnings") or []
    failed_checks = result.get("failed_checks") or []
    completed_checks = result.get("completed_checks") or []
    checks = "、".join(result.get("checks") or []) or "—"
    sample_size = result.get("sample_size")

    if result.get("ok"):
        summary = (
            f"{_REPORT_PREFIX}数据验证通过（目标表 `{target_table}`，"
            f"校验项：{checks}，采样 {sample_size} 行）。"
        )
    elif failed_checks:
        done = "、".join(completed_checks) or "无"
        missing = "、".join(
            str(item.get("name") or "?") for item in failed_checks if isinstance(item, dict)
        )
        summary = (
            f"{_REPORT_PREFIX}数据验证**未完整完成**（目标表 `{target_table}`，"
            f"已完成：{done}；**未跑完：{missing}**；采样 {sample_size} 行）。"
        )
    else:
        if _is_empty_table(result):
            summary = (
                f"{_REPORT_PREFIX}目标表 `{target_table}` **0 行** —— "
                f"**本次校验是空跑，没有任何数据被校验过**（采样 {sample_size} 行）。"
            )
        else:
            summary = (
                f"{_REPORT_PREFIX}数据验证发现 {len(warnings)} 处告警"
                f"（目标表 `{target_table}`，采样 {sample_size} 行）。"
            )

    lines: list[str] = [summary, ""]

    # 抽样数据紧跟摘要 —— 结论后面必须有数据行，否则用户看到的只是一句
    # 没有内容支撑的结论。
    sample_block = _render_summary_samples(result)
    if sample_block:
        lines.extend(sample_block)
        lines.append("")

    # 固定形状的报告（与试算链路完全一致）—— 十个小节永远按同一顺序出现，
    # 内容为空时写明「（无内容）」。
    body = render_fixed_report(
        result,
        scope=f"目标表 `{target_table}`",
        mode="direct",
    )
    if body:
        lines.append(body)

    # 口径说明：直连模式下主键重复率算的是**真实表全量**（不是临时表的 N 行）。
    if not result.get("pk_duplicate_truncated"):
        lines.append("")
        lines.append(
            "注：本次为**直连真实目标表**的只读校验 —— 主键重复率对真实表全量计算"
            "（非采样），数据行数亦为全量 COUNT。"
        )
    return "\n".join(lines)


# ============================================================================
# 交付物定位
# ============================================================================


def _resolve_dml_path(
    *,
    dml_path: str | None,
    sql_file_path: str | None,
    sql: str,
    runtime: Any,
) -> tuple[Path | None, str]:
    """定位要校验的交付物。

    返回 ``(文件路径, SQL 文本)``：

    - 找到文件 → ``(path, path 里的 SQL)`` —— 自修正**可以**写回它。
    - 只给了 ``sql`` 文本 → ``(None, sql)`` —— 能试算、能校验，但自修正
      **没法写回**（没有底稿可改、没法备份、没法留痕），所以自修正会被跳过。

    优先级：``dml_path`` > ``sql_file_path`` > workspace 里最新的 ``insert_*.sql``。

    **为什么优先用文件而不是 ``sql`` 参数**：大段 SQL 进 chat history 既费
    token 又容易在转述中失真；自修正还要**写回**这个文件 —— 没有文件路径就
    没法改、没法备份、没法留痕。
    """
    for candidate in (dml_path, sql_file_path):
        if candidate and str(candidate).strip():
            path = Path(str(candidate)).expanduser()
            if path.is_file():
                return path, ""
            logger.warning(f"[validate_data] file not found: {path}")

    workspace = _resolve_workspace(runtime)
    if workspace is not None:
        found = _find_dml(workspace)
        if found is not None:
            return found, ""

    # 没有文件可写 —— 退到 ``sql`` 文本本身。
    return None, (sql or "").strip()


def _resolve_workspace(runtime: Any) -> Path | None:
    """取 workspace 目录（只认真正的 ``str`` / ``Path``）。

    测试替身（``MagicMock``）会暴露一个 truthy 的 path-like 属性，其
    ``__fspath__`` 产出垃圾相对路径（``MagicMock/mock.workspace_dir/<id>``）——
    不做类型校验就会去扫一个不存在的目录。
    """
    raw = getattr(runtime, "workspace_dir", None)
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError as exc:
        logger.debug(f"[validate_data] unusable workspace_dir {raw}: {exc}")
        return None


def _find_dml(workspace: Path) -> Path | None:
    """找 workspace 里的交付 INSERT。

    **同名文件优先取 workspace 根目录那份**（不是 ``sql/`` 里的镜像）。

    ``organize_workspace`` 这个 agent 级 post-hook 会把根目录的 ``insert_*.sql``
    **复制**一份到 ``sql/``（``shutil.copy`` 不保留 mtime → 副本的 mtime 反而
    更新）。于是按 mtime 取「最新」会挑到 ``sql/`` 里的**影子副本**。

    这个 bug 的后果很隐蔽：自修正把修好的 SQL **写回影子副本**，而根目录的
    交付物原封不动 —— 语法校验链路读的是**根目录**那份（``verify._locate_sql``），
    于是它看到的还是**改坏之前的 SQL**。agent 明明被告知「已修好」，
    交付物却没变。

    所以这里的优先级是：

    1. **同名**时根目录优先（根目录才是交付物本体，``sql/`` 只是备份）；
    2. 不同名之间按 mtime 取最新（``sql/`` 里可能残留上一轮的其它文件）。

    Args:
        workspace: workspace 根目录。

    Returns:
        交付 INSERT 的路径；找不到返回 ``None``。
    """
    candidates: list[Path] = []
    for directory in (workspace / "sql", workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            name = path.name.lower()
            if name.startswith("insert_") and name.endswith(".sql") and path.is_file():
                candidates.append(path)

    if not candidates:
        return None

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    # --- 1) 按文件名分组，同名时根目录优先 ------------------------------
    by_name: dict[str, list[Path]] = {}
    for path in candidates:
        by_name.setdefault(path.name.lower(), []).append(path)

    preferred: list[Path] = []
    for paths in by_name.values():
        root_hit = next((p for p in paths if p.parent == workspace), None)
        preferred.append(root_hit if root_hit is not None else max(paths, key=_mtime))

    # --- 2) 不同名之间取最新修改的 ---------------------------------------
    return max(preferred, key=_mtime)


def _guess_target_table(sql: str) -> str:
    """从 SQL 里猜目标表（仅用于报告展示，不影响执行）。

    优先看 DDL 的 ``CREATE TABLE db.tbl``，其次看 INSERT 的
    ``INSERT OVERWRITE TABLE db.tbl``。
    """
    for pattern in (
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\w.-]+(?:\.[`\w.-]+)+)",
        r"INSERT\s+(?:OVERWRITE|INTO)\s+(?:TABLE\s+)?([`\w.-]+(?:\.[`\w.-]+)+)",
    ):
        match = re.search(pattern, sql, re.IGNORECASE)
        if match:
            return ".".join(part.strip("`") for part in match.group(1).split("."))
    return ""


# ============================================================================
# 试算
# ============================================================================


async def _invoke_trial_compute(
    *,
    sql: str,
    target_table: str,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """跑试算链路（建临时表 → 试算 INSERT → 全部校验项）。

    复用 ``trial_compute_table`` —— 临时表命名、执行路由、CTE 展开、
    分区日期都与静态校验链路保持一致，不会各自漂移。
    """
    from dataagent.core.suite.builtin_suites.de_agent.tools.dataops_trial_compute_tool import (
        trial_compute_table,
    )

    return await trial_compute_table(
        target_table=target_table,
        # SQL 直接给（不落盘再读）—— 自修正后的 SQL 已经在内存里了，
        # 再写回磁盘读一遍既慢又可能读到旧内容（写回失败时）。
        _sql_text=sql,
        _tool_context=_tool_context,
    )


# ============================================================================
# 自修正闭环
# ============================================================================


async def _maybe_self_correct(
    *,
    dml_path: Path,
    original_sql: str,
    result: dict[str, Any],
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """校验产出 error 级违规时跑自修正闭环。

    **只在有 error 级违规时触发** —— 仅 warning 级时跳过修正。
    任何异常都被吞掉（自修正失败不能影响校验结论），并把失败原因写进结果里。

    **每一轮自修正后都要重新走整个流程** —— 校验读的是临时表，不重新试算
    就看到的还是旧数据。而且复检必须是**完整**的：只把 ``violations`` 交给
    闭环不够 —— 复检时可能出现「违规清零了，但这次复检本身只跑了一半」
    （某个 checker 超时、常识判定降级）。那种情况下「没有违规」不等于
    「校验通过」，所以复检回调返回**整个 quality_check 字典**。
    """
    if not result or not result.get("passed"):
        # 试算本身没过 —— 那是执行问题，不是数据正确性问题，不走自修正。
        return result or {}
    quality = result.get("quality_check")
    if not isinstance(quality, dict):
        return result

    violations = violations_from_dicts(quality.get("violations"))
    error_violations = [v for v in violations if v.is_error]
    if not error_violations:
        quality.setdefault("self_correct", {"status": "not_needed", "reason": "没有 error 级违规"})
        return result

    try:
        from dataagent.core.managers.llm_manager.thinking import validation_llm_kwargs

        config = resolve_config(_tool_context)
        if not config.self_correct_enabled:
            quality["self_correct"] = {
                "status": "disabled",
                "reason": "自修正已关闭（quality_check.self_correct_enabled=false）",
                "needs_manual_review": True,
                "remaining_violations": [v.to_dict() for v in error_violations],
            }
            return result

        llm = resolve_llm(getattr(_tool_context, "runtime", None))
        try:
            llm_kwargs = validation_llm_kwargs(llm, enable_thinking=False)
        except Exception:  # noqa: BLE001
            llm_kwargs = {}

        # 每一轮自修正后的**完整**复检结果。
        last_recheck_quality: dict[str, Any] = {}

        async def _recheck(new_sql: str):
            """重新试算 + 重新校验，返回**完整的**校验结论。

            **复检没跑成 ≠ 校验通过** —— 必须把「没跑成」如实标出来。
            早先这里返回空字典 ``{}``，而 :func:`_extract_recheck` 把空字典
            当成「复检完整且无违规」→ 判成**收敛** → 报告写「✅ 自修正已
            收敛，当前 SQL 已通过复检」。实际上复检根本没跑到校验那一步
            （试算失败 / 校验不可用）—— 报告在撒谎。

            所以这里显式带上 ``failed_checks`` 标记，让复检**一定**被认成
            「未完成」，本轮不算修好。
            """
            # 写回后的 SQL 必须重新走一遍试算 —— 校验读的是临时表。
            _write_sql(dml_path, new_sql)
            recheck_result = await _invoke_trial_compute(
                sql=new_sql,
                target_table=result.get("target_table") or "",
                _tool_context=_tool_context,
            )
            recheck_quality = (recheck_result or {}).get("quality_check")
            if not isinstance(recheck_quality, dict) or not recheck_quality:
                # 复检没产出质量结论（试算失败 / 校验不可用）—— 明确标记为
                # 「未完成」，绝不能让它看起来像「查过了、没问题」。
                reason = (
                    (recheck_result or {}).get("error")
                    or (recheck_result or {}).get("summary")
                    or "复检未产出质量结论"
                )
                recheck_quality = {
                    "ok": False,
                    "sampled": False,
                    "violations": [],
                    "warnings": [],
                    "details": {},
                    "failed_checks": [
                        {
                            "name": "recheck",
                            "status": "not_run",
                            "reason": str(reason),
                        }
                    ],
                    "partial": True,
                }
            last_recheck_quality.clear()
            last_recheck_quality.update(recheck_quality)
            return recheck_quality

        corrected = await run_self_correct(
            dml_path=dml_path,
            original_sql=original_sql,
            violations=violations,
            recheck=_recheck,
            llm=llm,
            max_rounds=config.self_correct_max_rounds,
            enabled=True,
            llm_kwargs=llm_kwargs,
        )
        quality["self_correct"] = corrected.to_dict()

        # --- 最终结论必须来自「一次真实完整的复检」，不是推断 ---------------
        #
        # **复检结果要带着 ``self_correct`` 一起替换** —— 复检返回的是一份
        # 全新的 quality_check（重新试算 + 重新校验的产物），里面**没有**
        # ``self_correct`` 字段。直接整个替换掉的话，报告里的「自修正结果」
        # 小节会凭空消失 —— 用户看到「校验通过」却不知道刚才自动改过 SQL。
        self_correct_payload = corrected.to_dict()
        if last_recheck_quality:
            merged = dict(last_recheck_quality)
            merged["self_correct"] = self_correct_payload
            result["quality_check"] = merged
        elif corrected.converged:
            # 兜底：收敛了但没拿到复检结果（极少见）—— 再跑一次完整流程，
            # 绝不能拿「收敛」这个标志当作「校验通过」来展示。
            recheck_result = await _invoke_trial_compute(
                sql=_read_sql(dml_path),
                target_table=result.get("target_table") or "",
                _tool_context=_tool_context,
            )
            recheck_quality = (recheck_result or {}).get("quality_check")
            if isinstance(recheck_quality, dict):
                merged = dict(recheck_quality)
                merged["self_correct"] = self_correct_payload
                result["quality_check"] = merged

        if not corrected.converged:
            quality.setdefault("self_correct", {})["needs_manual_review"] = True
            result["needs_manual_review"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[validate_data] self-correct failed (ignored): {exc!r}")
        quality["self_correct"] = {
            "status": "failed",
            "reason": f"自修正执行失败：{exc!r}",
            "needs_manual_review": True,
        }
        result["needs_manual_review"] = True
    return result


def _mark_self_correct_unavailable(result: dict[str, Any]) -> None:
    """标记「自修正未执行」—— 没有交付文件可写回。

    **必须显式标记，不能静默跳过** —— 否则用户看到「发现 N 处 error 级违规」
    却看不到任何自修正痕迹，会以为闭环跑过了。
    """
    quality = result.get("quality_check")
    if not isinstance(quality, dict):
        return
    violations = violations_from_dicts(quality.get("violations"))
    error_violations = [v for v in violations if v.is_error]
    if not error_violations:
        quality.setdefault("self_correct", {"status": "not_needed", "reason": "没有 error 级违规"})
        return
    quality["self_correct"] = {
        "status": "unavailable",
        "reason": "没有交付文件可供写回，自修正无法执行（请传入 dml_path）",
        "needs_manual_review": True,
        "remaining_violations": [v.to_dict() for v in error_violations],
    }
    result["needs_manual_review"] = True


def _read_sql(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_sql(path: Path, sql: str) -> None:
    try:
        path.write_text(sql, encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[validate_data] write back failed: {exc!r}")


# ============================================================================
# 落盘
# ============================================================================


def _persist_quality_report(*, workspace: Path, result: dict[str, Any]) -> Path | None:
    """把质量校验结论落盘到 workspace（供事后复核）。

    **为什么要落盘**：结论只在聊天记录里 —— 一旦对话被压缩、或者别人想复核
    「当时到底查了什么、结论是什么」，就没有任何可追溯的产物。

    落盘文件名带内容哈希 —— 同一份结论重复落盘不会产生一堆重复文件，而内容
    变了就是新文件（能看出「改了几版」）。

    落盘失败**不阻断**任何流程 —— 只是少了复核依据。
    """
    if not result:
        return None
    try:
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning(f"[validate_data] quality report not serializable: {exc!r}")
        return None

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    out_dir = workspace / ".runtime"
    out_path = out_dir / f"data_quality_{digest}.json"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[validate_data] quality report not persisted: {exc!r}")
        return None
    logger.info(f"[validate_data] quality report persisted: {out_path}")
    return out_path


# ============================================================================
# 渲染
# ============================================================================


def _has_error_violations(result: dict[str, Any] | None) -> bool:
    """结果里有没有未解决的 error 级违规。

    兼容两种形态：试算链路的 ``{"quality_check": {...}}`` 与直连模式的
    ``{"violations": [...]}``（quality 字典本身就是 result）。

    **空表也算未解决的违规** —— 0 行时字段级校验是**空跑**，必须阻断交付。
    不能只看违规列表：标志在（跨进程序列化 / 旧版结果）丢失时，
    「一行都没校验到」会被读成「全部通过」。
    """
    payload = result or {}
    quality = payload.get("quality_check")
    if not isinstance(quality, dict):
        quality = payload
    if _is_empty_table(quality):
        return True
    violations = quality.get("violations") or []
    return any(
        isinstance(item, dict) and item.get("severity") == "error" for item in violations
    )


def _is_empty_table(quality: dict[str, Any]) -> bool:
    """本轮是不是「空表（0 行）」。

    **两个来源都要认**：``quality["empty_table"]`` 标志，或违规列表里的
    ``empty_table`` 规则。只看标志的话，标志丢了（跨进程序列化、旧版结果、
    手工构造的结果）就会把「一行都没校验到」渲染成「—」（不适用）——
    正是这个 bug 要消灭的那件事。
    """
    if quality.get("empty_table"):
        return True
    return any(
        isinstance(item, dict) and item.get("rule") == "empty_table"
        for item in quality.get("violations") or []
    )


def _render_report(*, result: dict[str, Any] | None) -> str:
    """渲染试算 + 校验结果。

    形状固定（见 :mod:`dataops_quality_report_format`）—— 十个小节永远按同一
    顺序出现，内容为空时写明「（无内容）」。用户的心智模型是「这是一张固定
    形状的表，缺的那一格是空的」，而不是「每次都要重新扫一遍结构才知道这次
    查了什么」。

    摘要区在最前面 —— 终端渲染层把工具结果截断到 500 字符
    （``DEFAULT_RICH_RESULT_TRUNCATION``），固定形状的报告很长，用户不该翻到
    最后才看到结论。摘要区里带上**关键事实**（哪几项没跑完、哪几项被跳过）
    以及**流程型校验的抽样数据**（结论后面紧跟数据行）。
    """
    if not result:
        return f"{_REPORT_PREFIX}未产出校验结论。"

    if result.get("skipped"):
        reason = result.get("message") or result.get("reason") or result.get("summary") or "unknown"
        return f"{_REPORT_PREFIX}已跳过（{reason}）。"

    if not result.get("passed"):
        lines = [f"{_REPORT_PREFIX}试算失败，SQL 未能成功执行。"]
        summary = result.get("error") or result.get("summary")
        if summary:
            lines.append(f"- 原因：{summary}")
        analysis = result.get("log_analysis")
        if isinstance(analysis, dict):
            if analysis.get("error_type"):
                lines.append(f"- 错误类型：{analysis['error_type']}")
            for suggestion in (analysis.get("suggestions") or [])[:3]:
                lines.append(f"- 建议：{suggestion}")
        lines.append("请按上述诊断修改 `insert_*.sql` 后重新执行数据校验。")
        return "\n".join(lines)

    quality = result.get("quality_check")
    if quality is None:
        # 试算跑通了，但没做校验（没有可校验数据 / 资源不可用）。
        return f"{_REPORT_PREFIX}试算通过；本次未执行数据正确性校验（无可校验数据或资源不可用）。"

    if not quality.get("sampled", True):
        # 校验没跑起来 —— 不能报成「全部通过」，也不能报成「0 处告警」。
        reason = quality.get("error") or "未知原因"
        return f"{_REPORT_PREFIX}试算通过，但数据正确性校验未能执行（{reason}），本次未产出质量结论。"

    lines = _render_summary(quality)
    body = render_fixed_report(
        quality,
        scope=_trial_scope(quality, result),
        mode="trial",
        result=result,
    )
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


def _render_summary(quality: dict[str, Any]) -> list[str]:
    """渲染**摘要区** —— 结论 + 关键事实 + 抽样数据。

    摘要区要带数据：终端渲染层把工具结果截断到 500 字符，固定形状的报告
    一进去就被吃光 —— 用户只看到「抽样 20 行；发现 0 处违规」这种没有内容
    支撑的结论句，一行业务数据都看不到。摘要区里塞进**紧凑的抽样行**，用户
    不用翻文件就能判断「这轮到底验了什么」。
    """
    warnings = quality.get("warnings") or []
    failed_checks = quality.get("failed_checks") or []
    completed_checks = quality.get("completed_checks") or []
    skipped_checks = quality.get("skipped_checks") or []
    checks = "、".join(quality.get("checks") or []) or "—"
    sample_size = quality.get("sample_size")

    lines: list[str] = []
    if quality.get("ok"):
        lines.append(
            f"{_REPORT_PREFIX}试算通过，数据正确性校验全部通过。"
            f"（校验项：{checks}；采样 {sample_size} 行）"
        )
    elif failed_checks:
        done = "、".join(completed_checks) or "无"
        # 「没跑完」的名字必须出现在摘要句里 —— 报告会被前端截断，只把它
        # 放在末尾的小节里等于没说。
        missing = "、".join(
            str(item.get("name") or "?") for item in failed_checks if isinstance(item, dict)
        )
        lines.append(
            f"{_REPORT_PREFIX}试算通过，数据正确性校验**未完整完成**"
            f"（已完成：{done}；**未跑完：{missing}**；采样 {sample_size} 行）。"
        )
        lines.append(
            f"⚠️ **未跑完的校验项没有产出结论 —— 不等于校验通过。**"
            f"（已跑完的部分{'发现 ' + str(len(warnings)) + ' 处告警' if warnings else '未发现告警'}）"
        )
    else:
        if _is_empty_table(quality):
            # 空表 → 不能说「发现 0 处告警」—— 那读起来像「没问题」。
            # 真实情况是「一行数据都没校验到」。
            lines.append(
                f"{_REPORT_PREFIX}试算通过，但目标表 **0 行** —— "
                f"**本次校验是空跑，没有任何数据被校验过**"
                f"（校验项：{checks}；采样 {sample_size} 行）。"
            )
        else:
            lines.append(
                f"{_REPORT_PREFIX}试算通过，但数据正确性校验发现 {len(warnings)} 处告警"
                f"（校验项：{checks}；采样 {sample_size} 行）。"
            )

    # 被跳过的校验项必须在摘要句里点名 —— 它们和「跑完了但没告警」完全是
    # 两回事：前者根本没跑，后者跑过且没问题。
    #
    # 不点名的后果：报告写「校验全部通过」，末尾小节里却有一行
    # 「pk_duplicate_rate：未提供主键列配置」—— 用户读摘要会以为所有规则
    # 都跑过了。而主键重复率恰恰是正确性校验里最硬的一条，它没跑 =
    # 这一轮的核心结论缺失。
    if skipped_checks:
        skipped_names = "、".join(
            str(item.get("name") or "?") for item in skipped_checks if isinstance(item, dict)
        )
        lines.append(
            f"⚠️ **{len(skipped_checks)} 项校验因信息不足被跳过"
            f"（{skipped_names}）—— 这些项**没有产出任何结论**，"
            f"「全部通过」不包含它们。**"
        )

    # 抽样数据紧跟摘要 —— 结论后面必须有数据行，否则用户看到的只是一句
    # 没有内容支撑的结论。
    sample_block = _render_summary_samples(quality)
    if sample_block:
        lines.append("")
        lines.extend(sample_block)
    return lines


def _render_summary_samples(quality: dict[str, Any]) -> list[str]:
    """摘要区里的**流程型校验明细**（结论 + 抽样数据）。

    完整明细（:func:`render_sample_rows`）动辄上千字，而摘要区要能塞进
    500 字符的截断窗口。这里每项只展示前几行、每格截断，保证「结论后面
    立刻有数据」。

    必须带「流程型校验明细」这个标题：它是**结论型**信息 —— 用户要一眼
    看出「常识检测 / 回源比对这两项到底跑没跑」。标题在，结论和抽样行在，
    截断窗口里就完整了。
    """
    flow_notes = quality.get("flow_notes") or {}
    if not isinstance(flow_notes, dict) or not flow_notes:
        return []

    lines: list[str] = ["流程型校验明细："]
    for name, notes in flow_notes.items():
        if not isinstance(notes, dict):
            continue
        label = _FLOW_CHECK_LABELS.get(str(name), str(name))
        lines.append(f"- {label}：{format_flow_note(notes)}")

    # 每条流程型校验的抽样行 —— 结论后面紧跟数据。
    for name, notes in flow_notes.items():
        if not isinstance(notes, dict):
            continue
        rows = notes.get("sample_data")
        if not isinstance(rows, list) or not rows:
            continue
        label = _FLOW_CHECK_LABELS.get(str(name), str(name))
        lines.append(f"{label}抽样明细（共 {len(rows)} 行）：")
        for offset, item in enumerate(rows[:_SUMMARY_SAMPLE_ROWS], start=1):
            cells = _summary_cells_of(item)
            lines.append(f"- 第 {_summary_row_index(item, offset)} 行：{cells}")
        remaining = len(rows) - _SUMMARY_SAMPLE_ROWS
        if remaining > 0:
            lines.append(f"- （另有 {remaining} 行未展示）")

    # 统一口径的样本 —— 覆盖全部字段（含主键）。
    sample_rows = quality.get("sample_rows") or []
    if isinstance(sample_rows, list) and sample_rows:
        lines.append(f"数据示例（共 {len(sample_rows)} 行）：")
        for offset, item in enumerate(sample_rows[:_SUMMARY_SAMPLE_ROWS], start=1):
            cells = _summary_cells_of(item)
            lines.append(f"- 第 {_summary_row_index(item, offset)} 行：{cells}")
        remaining = len(sample_rows) - _SUMMARY_SAMPLE_ROWS
        if remaining > 0:
            lines.append(f"- （另有 {remaining} 行未展示）")
    return lines


def _summary_cells_of(item: Any) -> str:
    """取一行的单元格（兼容 ``cells`` 包装与原始行两种形状）。"""
    if not isinstance(item, dict):
        return str(item)
    cells = item.get("cells")
    if not isinstance(cells, dict):
        cells = {k: v for k, v in item.items() if k != "row_index"}
    return _summary_cells(cells)


def _summary_row_index(item: Any, fallback: int) -> Any:
    """取一行的行号（没有就用它在列表里的位置）。

    流程型校验记进 notes 的是原始行（没有 ``row_index``）—— 用列表下标兜底，
    否则报告里会出现「第 ? 行」这种看不懂的标记。
    """
    if isinstance(item, dict) and item.get("row_index") is not None:
        return item["row_index"]
    return fallback


def _summary_cells(cells: dict[str, Any]) -> str:
    """把一行样本压成 ``k=v，k=v`` 形式（值超长则截断）。"""
    parts: list[str] = []
    for key, value in cells.items():
        text = "NULL" if value is None else str(value)
        if len(text) > _SUMMARY_CELL_CHARS:
            text = text[: _SUMMARY_CELL_CHARS - 3] + "..."
        parts.append(f"{key}={text}")
    return "，".join(parts)


def _trial_scope(quality: dict[str, Any], result: dict[str, Any]) -> str:
    """试算链路的「校验对象」描述。

    **两个都要给**：真实目标表（用户交付的那张）与临时表（实际被查的那张）。
    只给临时表，用户看到 ``adhoctemp.tmp_xxx`` 会不知道查的是哪张表。
    """
    real_table = str(result.get("target_table") or "").strip()
    temp_table = str(quality.get("temp_table") or "").strip()
    if real_table and temp_table:
        return f"目标表 `{real_table}`（试算临时表 `{temp_table}`）"
    if real_table:
        return f"目标表 `{real_table}`"
    if temp_table:
        return f"试算临时表 `{temp_table}`"
    return "试算临时表"


# ============================================================================


# ============================================================================

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
