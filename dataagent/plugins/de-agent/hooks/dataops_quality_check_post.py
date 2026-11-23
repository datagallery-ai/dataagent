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
"""Run the legacy non-blocking quality trial when the root Agent finishes a turn."""

import logging
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, HumanMessage
from utils.dataops_mcp import dataops_settings
from utils.dataops_quality_check import render_metrics_table, render_table_metrics
from utils.dataops_trial_compute_tool import trial_compute_once

logger = logging.getLogger(__name__)

_REPORT_PREFIX = "[DataOps 数据质量检查] "


async def dataops_quality_check_post(state, runtime, *, params):
    context = runtime.context
    config = context.get_plugin_config("de-agent").get("dataops", {})
    if config.get("enabled", True) is False or config.get("auto_trial_compute", True) is False:
        return None
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
        return None
    outputs = context.session.outputs
    dml_path = _find_dml(outputs)
    if dml_path is None:
        return None
    user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    query = str(getattr(user, "content", ""))
    reports = []
    path = dml_path
    await _emit(f"正在对 {path.name} 执行数据试算与质量校验，请稍候。")
    try:
        with dataops_settings(context):
            result, reused = await trial_compute_once(
                _guess_target_table(path),
                dml_path=str(path),
                query=query,
                state=state,
                context=context,
            )
        if not reused:
            reports.append(_render_report(dml_path=path, result=result))
    except Exception as exc:
        logger.exception("[dataops_quality_check_post] trial failed: %s", path)
        reports.append(f"{_REPORT_PREFIX}试算未完成：{type(exc).__name__}: {exc}")
    report = "\n\n".join(text for text in reports if text)
    if not report:
        return None
    # Preserve the delivered message. Non-streaming clients read messages[-1],
    # so the new report also includes delivery text; stream only the report.
    delivery = messages[-1].text
    content = f"## 最终交付\n\n{delivery}\n\n{report}" if delivery else report
    message = AIMessage(content=content, name="DataOps", id=str(uuid4()))
    await _emit(report, message.id)
    return {"messages": [message]}


async def _emit(content: str, message_id: str | None = None) -> None:
    """Emit the native AG-UI message event, without adding progress to state."""
    try:
        await adispatch_custom_event(
            "manually_emit_message",
            {"message_id": message_id or str(uuid4()), "message": content},
        )
    except Exception as exc:
        logger.debug("[dataops_quality_check_post] stream unavailable: %s", exc)


def _render_report(*, dml_path: Path, result: dict[str, Any] | None) -> str:
    """Render the trial compute result as a user-facing text block.

    The quality metrics are rendered as a **Markdown table** so the frontend
    can display them as a table (not as a wall of text). The table is built by
    the shared :func:`render_metrics_table` helper, so the post-hook and the
    standalone ``dataops_validate_data`` tool produce **identical** tables.

    Returns an empty string when there is nothing worth reporting, so the hook
    stays silent instead of adding noise to every turn.
    """
    if not result:
        return ""

    if result.get("skipped"):
        # 优雅结束：优先用工具给的人话，退回到 reason 兜底。
        reason = result.get("message") or result.get("reason") or result.get("summary") or "unknown"
        return f"{_REPORT_PREFIX}已跳过（{reason}）。"

    if not result.get("passed"):
        lines = [f"{_REPORT_PREFIX}试算失败，SQL 未能成功执行。"]
        # ``error`` 是收敛过的失败原因；``summary`` 是状态行（含同样的原因）。
        summary = result.get("error") or result.get("summary")
        if summary:
            lines.append(f"- 原因：{summary}")
        analysis = result.get("log_analysis")
        if isinstance(analysis, dict):
            if analysis.get("error_type"):
                lines.append(f"- 错误类型：{analysis['error_type']}")
            suggestions = analysis.get("suggestions") or []
            for suggestion in suggestions[:3]:
                lines.append(f"- 建议：{suggestion}")
        lines.append("请按上述诊断修改 `insert_*.sql` 后重新交付。")
        return "\n".join(lines)

    quality = result.get("quality_check")
    if quality is None:
        # Trial ran fine; check was skipped (non-INSERT, no dataops resource, ...).
        return f"{_REPORT_PREFIX}试算通过；本次未执行数据正确性校验（无可校验数据或资源不可用）。"

    if not quality.get("sampled", True):
        # 校验没跑起来（提交失败/超时）—— 不能报成「全部通过」，
        # 也不能报成「0 处告警」，那两种说法都会让人以为数据已经查过。
        reason = quality.get("error") or "未知原因"
        return (
            f"{_REPORT_PREFIX}试算通过，但数据正确性校验未能执行（{reason}），本次未产出质量结论。"
        )

    # --- 有指标：渲染 Markdown 表格（前端直接渲染成表格）----------------
    details = quality.get("details") or {}
    warnings = quality.get("warnings") or []
    failed_checks = quality.get("failed_checks") or []
    completed_checks = quality.get("completed_checks") or []
    checks = "、".join(quality.get("checks") or []) or "—"
    sample_size = quality.get("sample_size")

    lines: list[str] = []
    if quality.get("ok"):
        lines.append(
            f"{_REPORT_PREFIX}试算通过，数据正确性校验全部通过。"
            f"（校验项：{checks}；采样 {sample_size} 行）"
        )
    elif failed_checks:
        # 部分完成：跑出来的指标照常展示，失败的校验项单独说明。
        # 早先这里因为「一条挂了 = 整个校验没跑成」而只报一句「未能执行」，
        # 已经算出来的指标全丢了 —— 用户看不到任何结果。
        done = "、".join(completed_checks) or "无"
        lines.append(
            f"{_REPORT_PREFIX}试算通过，数据正确性校验**部分完成**"
            f"（已完成的校验项：{done}；采样 {sample_size} 行）。"
        )
        if warnings:
            lines.append(f"已完成部分发现 {len(warnings)} 处告警。")
    else:
        lines.append(
            f"{_REPORT_PREFIX}试算通过，但数据正确性校验发现 {len(warnings)} 处告警"
            f"（校验项：{checks}；采样 {sample_size} 行）。"
        )

    if details:
        lines.append("")
        lines.append(render_metrics_table(details, warnings, quality.get("failed_metrics") or {}))

    table_metrics_md = render_table_metrics(
        quality.get("table_metrics") or {}, bool(quality.get("row_count_truncated"))
    )
    if table_metrics_md:
        lines.extend(["", table_metrics_md])

    if failed_checks:
        lines.append("")
        lines.append("未完成的校验项：")
        for item in failed_checks:
            lines.append(f"- {item.get('name')}：{item.get('reason') or item.get('status')}")
        lines.append("以上校验项未产出结果（超时或执行失败），其余结果如上表所示。")

    if warnings:
        lines.append("")
        lines.append("告警明细：")
        lines.extend(f"- {warning}" for warning in warnings[:20])
        lines.append(
            "请检查对应字段的口径（是否漏 JOIN、INNER JOIN 丢数据、分组粒度错误等）后再交付。"
        )
    return "\n".join(lines)


def _find_dml(workspace: Path) -> Path | None:
    """Locate the deliverable INSERT, preferring the most recently modified file.

    ``organize_workspace`` mirrors deliverables into ``sql/``；但它挂在 agent 级
    post-hook（图的收尾），而本 hook 挂在**节点级**（planner post）—— 节点级
    先于 agent 级执行，所以此刻 ``sql/`` 里可能是**上一轮**留下的旧副本。

    因此不能只按目录优先级（sql/ 优先）取第一个 —— 那样可能挑到陈旧的副本。
    改为在所有候选里取**最新修改**的那个：本轮 agent 写出的 insert_*.sql
    一定比上一轮拷贝进 sql/ 的更新。
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

    # 最新修改优先；mtime 相同则退回 sql/ 优先（保持原有偏好）。
    return max(candidates, key=lambda p: (_mtime(p), p.parent.name == "sql"))


def _guess_target_table(dml_path: Path) -> str:
    """Best-effort target table for reporting: the DDL's ``CREATE TABLE db.tbl``.

    Purely cosmetic — the scratch rewrite is derived from the DML itself.
    """
    try:
        text = dml_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\w.-]+(?:\.[`\w.-]+)+)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return ""
    # Strip the backticks of each dotted segment: `biads`.`x` -> biads.x
    return ".".join(part.strip("`") for part in m.group(1).split("."))


# ---------------------------------------------------------------------------
# Run guard
# ---------------------------------------------------------------------------
