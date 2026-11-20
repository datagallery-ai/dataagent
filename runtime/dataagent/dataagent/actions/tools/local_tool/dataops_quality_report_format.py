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
"""数据质量校验报告的**固定形状**渲染器。

============================  ==================================================
小节                          内容
============================  ==================================================
① 校验概况                    这一轮查了什么、查没查完
② 校验项执行情况              **每一个**校验项一行状态
③ 字段级指标                  逐字段覆盖率 / 单一值比率 / 异常值占比
④ 表级指标                    数据行数 / 主键重复率
⑤ 数据示例                    **查出来的数据示例**（本轮抽到的原始行）
⑥ 正确性违规明细              结构化违规表 + 纵向明细
⑦ 告警明细                    可读的告警文案列表
⑧ 流程型校验明细              常识检测 / 回源比对（含抽样数据）
⑨ 自修正结果                  收敛 / 未收敛 / 误报 / 已关闭 / 执行失败
⑩ 交付结论                    可否交付 + 还需要人工做什么
============================  ==================================================

**为什么必须固定形状**：报告格式每次都不一样时，用户每次都要重新扫一遍
结构才知道「这次查了什么」—— 而且「这一小节没出现」到底是「没查」还是
「查完了没问题」根本分不清。固定形状把两种情况都变成明确的信息。

**为什么必须有「数据示例」小节**：校验结论（「非空率 0.9500」「抽样 20 行，
发现 0 处违规」）是没有内容支撑的 —— 用户看不到一行业务数据，无从判断
「这批数据到底长什么样」「指标是不是算在正确的数据上」。

**小节顺序固定，且「数据示例」紧跟指标表** —— 指标表说「这个字段的覆盖率
是多少」，数据示例说「这个字段的实际取值长什么样」，两者必须挨着，
否则用户要在报告里来回跳。
"""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.local_tool.dataops_quality_check import (
    format_flow_note,
    render_metrics_table,
    render_reconcile_evidence,
    render_table_metrics,
    render_violation_details,
    render_violations_table,
)

__all__ = [
    "FIXED_SECTIONS",
    "render_fixed_report",
    "render_sample_rows",
]


#: 报告的小节标题 —— **顺序固定**，永远按这个顺序出现。
FIXED_SECTIONS: tuple[str, ...] = (
    "校验概况",
    "校验项执行情况",
    "字段级指标",
    "表级指标",
    "数据示例",
    "正确性违规明细",
    "告警明细",
    "流程型校验明细",
    "自修正结果",
    "交付结论",
)

#: 校验项名 → 中文名（概况与状态表里用中文，不用内部英文 key）。
_CHECK_LABELS: dict[str, str] = {
    "null_rate": "空值覆盖率",
    "single_value_ratio": "单一值比率",
    "outlier_ratio": "异常值占比",
    "row_count": "数据行数",
    "pk_duplicate_rate": "主键重复率",
    "negative_count": "计数型字段负值占比",
    "common_sense": "常识违规检测",
    "source_reconciliation": "抽样回源比对",
}

#: 流程型校验的中文名。
_FLOW_CHECK_LABELS: dict[str, str] = {
    "common_sense": "常识违规检测",
    "source_reconciliation": "抽样回源比对",
}

#: 严重级别 → 中文标签。
_SEVERITY_LABELS: dict[str, str] = {
    "error": "❌ 错误",
    "warning": "⚠️ 告警",
}


def render_fixed_report(
    quality: dict[str, Any] | None,
    *,
    scope: str,
    mode: str = "trial",
    result: dict[str, Any] | None = None,
) -> str:
    """把一次质量校验渲染成**固定形状**的报告。

    Args:
        quality: ``quality_check`` 段（``run_quality_check`` 的返回值）。
        scope: 被校验对象的描述（试算链路是临时表，直连链路是真实表名）。
        mode: ``"trial"`` / ``"direct"`` —— 决定措辞。
        result: 外层结果字典（用于取 ``needs_manual_review``）。

    Returns:
        Markdown 报告文本。
    """
    quality = quality if isinstance(quality, dict) else {}
    result = result if isinstance(result, dict) else {}

    sections: list[str] = []
    for title in FIXED_SECTIONS:
        body = _SECTION_RENDERERS[title](quality, scope=scope, mode=mode, result=result)
        sections.append(f"### {title}\n{body}" if body else f"### {title}\n（无内容）")
    return "\n\n".join(sections)


# ============================================================================
# 各小节渲染器
# ============================================================================


def _render_overview(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """① 校验概况 —— 一眼看清「这次查了什么、查没查完」。"""
    sample_size = quality.get("sample_size")
    primary_key = quality.get("primary_key_columns") or []
    correctness_checks = [str(c) for c in (quality.get("correctness_checks") or [])]
    quality_checks = [
        str(c)
        for c in (quality.get("checks") or [])
        if str(c) not in correctness_checks
    ]
    direct = bool(quality.get("direct"))

    error_count, warning_count = _count_violations(quality)

    rows: list[tuple[str, str]] = [
        ("校验对象", scope or "（未指定）"),
        ("校验方式", "直连真实表" if direct or mode == "direct" else "试算临时表"),
        ("采样行数", str(sample_size) if sample_size is not None else "未采样（全表）"),
        (
            "主键列",
            " + ".join(str(c) for c in primary_key) if primary_key else "未提供（相关校验项已跳过）",
        ),
        ("质量校验项", "、".join(quality_checks) if quality_checks else "无"),
        ("正确性校验项", "、".join(correctness_checks) if correctness_checks else "无"),
        (
            "违规统计",
            f"{error_count} 处 error 级、{warning_count} 处 warning 级",
        ),
    ]
    partition = quality.get("partition_date")
    if partition:
        rows.append(("分区日期", str(partition)))

    lines = ["| 项 | 值 |", "|---|---|"]
    lines.extend(f"| {label} | {value} |" for label, value in rows)
    return "\n".join(lines)


def _render_check_status(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """② 校验项执行情况 —— **每一个**校验项一行状态。

    不允许用「小节消失」表达「没查」—— 完成 / 未完成 / 已跳过，各带原因，
    全都在这张表里。
    """
    completed = [str(c) for c in (quality.get("completed_checks") or [])]
    failed = [c for c in (quality.get("failed_checks") or []) if isinstance(c, dict)]
    skipped = [c for c in (quality.get("skipped_checks") or []) if isinstance(c, dict)]

    # 全部校验项（配置里启用的）—— 状态表要覆盖全集，不能只列跑过的。
    known = set(completed)
    known.update(str(item.get("name")) for item in failed)
    known.update(str(item.get("name")) for item in skipped)

    lines = ["| 校验项 | 状态 | 说明 |", "|---|---|---|"]
    seen: set[str] = set()
    for name in sorted(known):
        seen.add(name)
        label = _CHECK_LABELS.get(name, name)
        if name in completed:
            lines.append(f"| {label}（`{name}`） | ✅ 完成 | |")
            continue
        failure = next((item for item in failed if str(item.get("name")) == name), None)
        if failure is not None:
            lines.append(
                f"| {label}（`{name}`） | ❌ 未完成 | {failure.get('reason') or failure.get('status') or '未知原因'} |"
            )
            continue
        skip = next((item for item in skipped if str(item.get("name")) == name), None)
        if skip is not None:
            lines.append(
                f"| {label}（`{name}`） | ⚪ 已跳过 | {skip.get('reason') or '信息不足'} |"
            )
            continue
        lines.append(f"| {label}（`{name}`） | ❔ 未执行 | |")

    # 配置里启用、但这一轮压根没进任何名单的校验项 —— 也要有一行。
    for name in quality.get("correctness_checks") or []:
        name = str(name)
        if name in seen:
            continue
        label = _CHECK_LABELS.get(name, name)
        lines.append(f"| {label}（`{name}`） | ❔ 未执行 | |")

    return "\n".join(lines)


def _render_field_metrics(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """③ 字段级指标。"""
    details = quality.get("details") or {}
    if not details:
        return "（无字段级指标）"
    return render_metrics_table(
        details,
        quality.get("warnings") or [],
        quality.get("failed_metrics") or {},
        empty_table=bool(quality.get("empty_table")),
    )


def _render_table_metrics(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """④ 表级指标。"""
    table_metrics = quality.get("table_metrics") or {}
    if not table_metrics:
        return "（无表级指标）"
    return render_table_metrics(table_metrics, bool(quality.get("row_count_truncated")))


def render_sample_rows(sample_rows: Any, *, title: str = "数据示例") -> str:
    """⑤ **查出来的数据示例** —— 渲染成本轮实际抽到的原始行。

    指标表只给聚合值（「非空率 0.9500」），用户看不到任何一行业务数据。
    摊开实际查到的行，用户才能判断「指标算在正确的数据上吗」「这个字段的
    值看起来对吗」。

    渲染成 **Markdown 表格**（前端直接渲染成表格）—— 一行一个字段，比
    ``k=v，k=v`` 的行式更易读。

    **兼容两种输入形状**：

    - ``[{"row_index": 1, "cells": {...}}, ...]`` —— 统一口径抽样
      （:func:`record_sample_rows` 的产物）；
    - ``[{"列名": 取值}, ...]`` —— 流程型校验记进 notes 的原始行。

    Args:
        sample_rows: 上面两种形状之一。
        title: 小节标题。

    Returns:
        Markdown 文本；没有数据时返回空串。
    """
    if not isinstance(sample_rows, list) or not sample_rows:
        return ""

    normalized = [_normalize_sample_row(item, index) for index, item in enumerate(sample_rows, start=1)]
    normalized = [item for item in normalized if item is not None]
    if not normalized:
        return ""

    # 列名按**首次出现顺序**取全部行（不同行可能缺列）。
    columns: list[str] = []
    for item in normalized:
        for key in item["cells"]:
            if key not in columns:
                columns.append(key)
    if not columns:
        return ""

    lines = [f"{title}（共 {len(normalized)} 行）："]
    lines.append("| 行 | " + " | ".join(columns) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in columns) + " |")
    for item in normalized:
        cells = item["cells"]
        values = [_cell_text(cells.get(column)) for column in columns]
        lines.append(f"| {item['row_index']} | " + " | ".join(values) + " |")
    return "\n".join(lines)


def _normalize_sample_row(item: Any, index: int) -> dict[str, Any] | None:
    """把一行样本统一成 ``{"row_index": int, "cells": {...}}``。

    **两种输入形状都要认**（见 :func:`render_sample_rows` 的说明）。
    """
    if not isinstance(item, dict):
        return None
    cells = item.get("cells")
    if isinstance(cells, dict):
        return {
            "row_index": item.get("row_index", index),
            "cells": {str(key): value for key, value in cells.items()},
        }
    # 原始行形状 —— 整行就是 cells。
    if "cells" in item:
        return None
    return {
        "row_index": item.get("row_index", index),
        "cells": {str(key): value for key, value in item.items() if key != "row_index"},
    }


def _cell_text(value: Any) -> str:
    """把一个单元格渲染成 Markdown 表格里的短文本。

    **必须转义竖线** —— 值里出现 ``|`` 会把表格列冲散。
    """
    if value is None:
        return "—"
    text = str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    return text if text else "—"


def _render_sample_section(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """⑤ 数据示例小节。"""
    body = render_sample_rows(quality.get("sample_rows") or [])
    if body:
        return body
    # **没有数据也要说明为什么** —— 空着不写，用户会以为「这一小节忘了」。
    return "（本轮未抽到数据示例 —— 抽样未执行或未返回数据；不影响各项校验结论）"


def _render_violations(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """⑥ 正确性违规明细。"""
    violations = quality.get("violations") or []
    if not violations:
        return "（无违规）"
    parts = [render_violations_table(violations)]
    details = render_violation_details(violations)
    if details:
        parts.append(details)
    return "\n\n".join(parts)


def _render_warnings(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """⑦ 告警明细。"""
    warnings = [str(w) for w in (quality.get("warnings") or [])]
    if not warnings:
        return "（无告警）"
    lines = [f"- {warning}" for warning in warnings[:20]]
    if len(warnings) > 20:
        lines.append(f"- （另有 {len(warnings) - 20} 条告警未展示）")
    return "\n".join(lines)


def _render_flow_notes(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """⑧ 流程型校验明细（含抽样数据示例）。

    每一项流程型校验都要有自己的抽样数据 —— 常识检测抽 20 行、回源比对抽
    3 行，列也不一样。少一项，用户就少一份「到底看了什么」的证据。
    """
    flow_notes = quality.get("flow_notes") or {}
    if not isinstance(flow_notes, dict) or not flow_notes:
        return "（未执行流程型校验）"

    lines: list[str] = []
    for name, notes in flow_notes.items():
        label = _FLOW_CHECK_LABELS.get(str(name), str(name))
        if not isinstance(notes, dict):
            continue
        lines.append(f"- {label}：{format_flow_note(notes)}")

    # **每条流程型校验的抽样行** —— 结论后面紧跟数据。
    for name, notes in flow_notes.items():
        if not isinstance(notes, dict):
            continue
        body = render_sample_rows(
            notes.get("sample_data") or [],
            title=f"{_FLOW_CHECK_LABELS.get(str(name), str(name))}抽样数据",
        )
        if not body:
            continue
        lines.append("")
        lines.append(body)

    evidence = render_reconcile_evidence(flow_notes.get("source_reconciliation"))
    if evidence:
        lines.append("")
        lines.append(evidence)

    return "\n".join(lines) if lines else "（未执行流程型校验）"


def _render_self_correct(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """⑨ 自修正结果。

    直连模式永远不跑自修正 —— 真实表里的数据是历史产出，改它要重跑上游
    任务，不是校验器该干的事。这里明确写出来，不能让用户以为「没触发 =
    没有违规」。
    """
    if mode == "direct":
        return "- 未执行（直连模式只读校验，不修改真实数据与上游 SQL）"

    self_correct = quality.get("self_correct") or {}
    if not isinstance(self_correct, dict) or not self_correct:
        return "（未触发自修正）"

    status = str(self_correct.get("status") or "").strip()
    rounds = self_correct.get("rounds")
    reason = str(self_correct.get("reason") or "")

    if status == "not_needed":
        # 只说状态，不替用户下结论 —— 「没有 error 级违规」这个断言由
        # 编排层决定，渲染层复述它可能与「违规明细」自相矛盾。
        return "- 未触发（编排层判定无需自修正）"
    if status == "disabled":
        lines = ["- 未执行（自修正已关闭）"]
        lines.append("- ⚠️ 存在 error 级违规，需要人工修正后再交付。")
        lines.extend(_remaining_lines(self_correct))
        return "\n".join(lines)
    if status == "disputed":
        lines = [
            "- 🔍 模型判定该违规**不成立（误报）**，未对 SQL 做修改"
            f"（{reason or '模型认为实际值与期望值一致'}）"
        ]
        lines.append(
            "- ⚠️ 需要**复核校验规则本身**（口径是否过严 / 采样是否有偏差），"
            "而不是改 SQL。"
        )
        lines.extend(_remaining_lines(self_correct))
        return "\n".join(lines)
    if status == "unavailable":
        lines = [f"- 未执行：{reason or '没有交付文件可供写回'}"]
        lines.append("- ⚠️ 存在 error 级违规，需要人工修正后再交付。")
        lines.extend(_remaining_lines(self_correct))
        return "\n".join(lines)
    if status == "failed":
        return f"- 执行失败：{reason or '未知原因'}\n- ⚠️ 需要人工复核。"

    lines: list[str] = []
    if self_correct.get("converged"):
        lines.append(f"- ✅ 自修正成功（第 {rounds} 轮）")
        backups = self_correct.get("backups") or []
        if backups:
            lines.append(f"- 原始 SQL 已备份：{', '.join(str(b) for b in backups)}")
        diff = str(self_correct.get("diff_summary") or "").strip()
        if diff:
            lines.append("- 修正前后差异：")
            lines.extend(f"  {line}" for line in diff.splitlines()[:20])
        return "\n".join(lines)

    lines.append(
        f"- ⚠️ 自修正未收敛（已尝试 {rounds} 轮）：{reason or '仍有 error 级违规'}"
    )
    lines.append("- **需要人工复核**，不要直接按当前 SQL 交付。")
    lines.extend(_remaining_lines(self_correct))
    return "\n".join(lines)


def _remaining_lines(self_correct: dict[str, Any]) -> list[str]:
    """未解决的违规逐条列出。"""
    remaining = self_correct.get("remaining_violations") or []
    if not remaining:
        return []
    lines = [f"- 仍有 {len(remaining)} 处 error 级违规未解决："]
    for item in remaining[:20]:
        if isinstance(item, dict):
            lines.append(
                f"  - `{item.get('field') or '—'}`（{item.get('rule') or '—'}）：{item.get('message') or '（无说明）'}"
            )
        else:
            lines.append(f"  - {item}")
    return lines


def _render_conclusion(
    quality: dict[str, Any],
    *,
    scope: str,
    mode: str,
    result: dict[str, Any],
) -> str:
    """⑩ 交付结论 —— 三件事说清楚。

    ① 校验项**是否全部跑完**（部分完成 ≠ 通过）；② 是否还有**未解决的
    error 级违规**；③ 结论是「可交付」还是「需要人工复核后再交付」。

    直连模式的措辞不同 —— 那里没有「交付物」可交付，结论说的是「这张表
    现在能不能用」。但**小节标题固定**（形状不变），只有措辞变。
    """
    failed_checks = [c for c in (quality.get("failed_checks") or []) if isinstance(c, dict)]
    skipped_checks = [c for c in (quality.get("skipped_checks") or []) if isinstance(c, dict)]
    partial = bool(quality.get("partial")) or bool(failed_checks)
    violations = quality.get("violations") or []
    error_violations = [
        item for item in violations if isinstance(item, dict) and item.get("severity") == "error"
    ]
    error_count, warning_count = _count_violations(quality)
    empty_table = bool(quality.get("empty_table"))

    self_correct = quality.get("self_correct") or {}
    if not isinstance(self_correct, dict):
        self_correct = {}
    disputed = self_correct.get("status") == "disputed"
    rounds = self_correct.get("rounds")

    lines: list[str] = []
    if partial:
        names = "、".join(str(item.get("name") or "?") for item in failed_checks)
        lines.append(
            "- ⚠️ 本次校验**未完整完成**"
            + (f"（未跑完：{names}）" if names else "")
            + " —— **未跑完的校验项没有产出结论，不等于校验通过**"
        )
    else:
        lines.append("- ✅ 校验项全部跑完")

    if error_count:
        if disputed:
            lines.append(
                f"- 🔍 有 {error_count} 处 error 级违规，但自修正判定为**误报**"
                "（详见「正确性违规明细」与「自修正结果」小节）"
            )
        else:
            lines.append(
                f"- ❌ 仍有 {error_count} 处 **error 级违规**未解决"
                "（详见「正确性违规明细」小节）"
            )
    elif warning_count:
        lines.append(f"- ⚠️ 有 {warning_count} 处 warning 级告警，不阻断交付但建议确认")

    if skipped_checks:
        names = "、".join(str(item.get("name") or "?") for item in skipped_checks)
        lines.append(
            f"- ⚠️ 有 {len(skipped_checks)} 项校验**因信息不足被跳过**"
            f"（{names}）—— 它们**没有产出任何结论**，"
            "上述「全部跑完」不包含这些项"
        )

    if empty_table:
        lines.append(
            "- ❌ **目标表 0 行 —— 本次校验是空跑**：字段级指标全是 0/0 = NULL，"
            "**没有任何数据被校验过**"
        )

    # 自修正收敛/未收敛必须在结论里说清楚 —— 「有 error 级违规」和
    # 「自修正已经把它修好了」是两回事，用户读摘要就知道这一轮到底解没解决。
    if mode != "direct":
        if self_correct.get("converged"):
            lines.append(
                f"- ✅ 自修正已收敛（共 {self_correct.get('rounds')} 轮），"
                "当前 SQL 已通过复检"
            )
        elif self_correct.get("status") == "disputed":
            pass  # 误报已在上面单独说过
        elif rounds:
            lines.append(
                f"- ❌ 自修正未收敛（已尝试 {rounds} 轮）—— error 级违规需要人工修正"
            )

    needs_review = bool(result.get("needs_manual_review")) or bool(error_violations)
    if mode == "direct":
        # 直连模式没有「交付物」可交付 —— 结论说的是「这张表现在能不能用」。
        if needs_review or empty_table:
            lines.append(
                f"- **建议先排查 `{scope}` 的产出逻辑**："
                "直连模式只做只读体检，不会自动修改数据或上游 SQL"
            )
        elif partial:
            lines.append("- **数据可用，但质量结论不完整**：未跑完的校验项需另行确认")
        else:
            lines.append(f"- `{scope}` 校验已全部跑完且无 error 级违规，可按当前口径使用")
        return "\n".join(lines)

    if disputed:
        lines.append(
            "- **本表需人工确认校验口径**：自修正判定该 error 级违规为误报，"
            "请复核校验规则本身（而非修改 SQL）"
        )
    elif empty_table:
        lines.append(
            "- **本表不建议直接交付**：目标表 0 行，本次校验是空跑 —— "
            "先确认上游数据与 JOIN / 分区条件，确认有数据后重新执行数据校验"
        )
    elif needs_review:
        lines.append("- **本表不建议直接交付**：请先修正上述 error 级违规并重新交付校验")
    elif partial:
        lines.append("- **本表可交付，但质量结论不完整**：未跑完的校验项需另行确认")
    else:
        lines.append("- 本表校验已全部跑完且无 error 级违规，可按交付说明使用")
    return "\n".join(lines)


def _count_violations(quality: dict[str, Any]) -> tuple[int, int]:
    """数一数 error / warning 级违规各多少。"""
    error_count = 0
    warning_count = 0
    for item in quality.get("violations") or []:
        if not isinstance(item, dict):
            continue
        if item.get("severity") == "error":
            error_count += 1
        elif item.get("severity") == "warning":
            warning_count += 1
    return error_count, warning_count


#: 小节标题 → 渲染器。
_SECTION_RENDERERS = {
    "校验概况": _render_overview,
    "校验项执行情况": _render_check_status,
    "字段级指标": _render_field_metrics,
    "表级指标": _render_table_metrics,
    "数据示例": _render_sample_section,
    "正确性违规明细": _render_violations,
    "告警明细": _render_warnings,
    "流程型校验明细": _render_flow_notes,
    "自修正结果": _render_self_correct,
    "交付结论": _render_conclusion,
}
