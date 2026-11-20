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
"""数据正确性校验失败后的**自修正闭环**（需求 1.5）。

闭环（复用 ``SQLCheckerPipeline`` 的成熟模式：定位 → 定向修正 → 程序复检
→ 未通过不发布）::

    校验产出 error 级违规
        │
        ▼
    构造修正上下文（原始 INSERT SQL + 违规的 rule/field/evidence/suggestion）
        │
        ▼
    模型产出**增量补丁**（old_text → new_text），在原 SQL 基础上修改
        │
        ▼
    备份原文件 → 写回修正后的 SQL
        │
        ▼
    重新试算 + 重新校验（走同一套校验项）
        │
        ├─ 违规清空 → 收敛，报告「自修正成功（第 N 轮）」
        ├─ 仍有违规且轮次 < 上限 → 继续下一轮（带上本轮违规）
        └─ 轮次用尽 → needs_manual_review=True，报告写明「已尝试 N 轮仍未通过」

**为什么是增量补丁而不是整条重写**（用户明确要求：「自修正时还是在原基础上修改
不要完全重写」）：整条重写会让模型丢掉原 SQL 里那些**本来就对**的部分 ——
过滤条件、JOIN 关系、时间窗口。数据特征任务的 SQL 通常几十上百行，整条重写
等于让模型凭几条违规信息重建整条 SQL，改坏的地方往往比修好的地方多。
增量补丁只动「与违规直接相关的那几行」，其余部分逐字保留。

**补丁的锚点必须唯一**：``old_text`` 在原 SQL 里出现**恰好一次**才算数。
出现多次就无法确定改的是哪一处 —— 这时整条补丁作废，本轮失败。

**必须写回 workspace**：只修内存里的 SQL 没有意义，用户交付的是文件。

**必须先备份**：修正会覆盖用户的 ``insert_*.sql``。备份为
``insert_<table>.sql.bak.<round>``，保留原始交付物可追溯。

**收敛失败不许静默**：报告必须显式写「需人工复核」，并列出未收敛的违规全文。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dataagent.actions.tools.local_tool.dataops_correctness_flow import (
    invoke_llm_json,
    json_dumps,
)
from dataagent.actions.tools.local_tool.dataops_quality_check import CorrectnessViolation
from dataagent.utils.log import logger

# ============================================================================
# Prompt
# ============================================================================

_SYSTEM_PROMPT = """你是数据特征 SQL 修复助手。你会拿到一条 INSERT SQL 和若干条**数据正确性违规**。

修复方式：**在原 SQL 基础上做增量修改**，不要整条重写。

规则：
1. 只输出 JSON，不要输出解释文字。
2. 每次调用只针对**一条**违规（你会收到一条违规的详情），输出 1~3 条增量补丁。
3. 每条补丁的格式：{"old_text": "原 SQL 里要替换的片段", "new_text": "替换后的片段"}。
4. **old_text 必须逐字来自原 SQL，且在原 SQL 里只出现一次** —— 出现多次的话
   系统无法确定改的是哪一处，整条补丁会被拒绝。挑一段足够长、能唯一定位的文本
   （例如包含完整的表达式或 WHERE 条件），不要只写一个短词。
5. **只改与这条违规直接相关的地方**，其余部分逐字保留。
6. 修正的方向以违规里的 suggestion 为准，但最终要以「SQL 能正确表达业务口径」
   为目标 —— 别为了消掉告警而把 SQL 改成另一种同样错误的口径。
7. **绝对禁止「掩耳式修正」** —— 以下写法一律不接受：
   - 用 `LEAST(...)` / `GREATEST(...)` / `CASE WHEN ... THEN <常量>` 把越界值
     夹逼到合规区间（例如 `LEAST(ratio * 1.2, 100.0)`）；
   - 用 `COALESCE(x, 0)` 把空值兜成 0 来假装有数据；
   - 新增恒空的 `UNION` 分支（如 `HAVING COUNT(*) = 0`）或 `WHERE 1 = 0`；
   - 留下定义了却没被引用的 CTE。
   这些只改了**数值形状**，没有修**口径**，还会引入新的错误（真实值被截断、
   死代码让 SQL 变复杂）。**要修的是分子分母本身**，不是把结果夹进范围。
8. 修正不了就给空的 edits 数组，并在 reason 里说明原因 —— 不要瞎改。
9. **如果你判定这条违规本身是误报**（实际值与期望值一致、口径宽松、采样
   偏差、字段语义另有解释等），就给空的 edits，并在 reason 里**明确写出**
   「该违规不成立」及理由。这类反馈会被单独标记为 `disputed`，走**复核
   校验规则**的流程，而不是「SQL 改不动」。**不要**用含糊的措辞（例如只写
   「无需修正」而不说为什么），系统会把它当成「改不动」。
10. 规则为 `empty_table`（目标表 0 行）时，**先判断根因再决定动不动 SQL**：
   - 上游源表在该分区确实没有数据 → 改 SQL 没用，给空的 edits，说明
     「上游源表在该分区无数据，不是 SQL 的问题」；
   - JOIN 的关联键写错 / 漏了必要的 LEFT JOIN → 改 JOIN 条件；
   - 分区过滤条件把数据全过滤掉了 → 改分区条件。
   **绝不能**为了让结果不为空而删掉 JOIN、去掉分区过滤、或把 INNER JOIN
   改成更宽松的写法 —— 那是把「没数据」伪装成「有数据」，比空表更糟。
11. 如果根因是**源表缺少必要字段**（例如要「投放转化目标次数」但源表没有），
    不要用 `COUNT(*)` 之类的近似量硬凑 —— 那会制造量纲不一致的错误比率。
    此时应说明「源表缺少 X 字段，无法计算 Y，需要确认口径」，并给出空的 edits。

输出格式：
{"edits": [{"old_text": "...", "new_text": "..."}], "reason": "改了什么、为什么"}
"""

_USER_TEMPLATE = """当前 INSERT SQL：
```sql
{sql}
```

本轮要修复的违规：
{violation}

请输出针对这条违规的增量补丁。
"""


def build_correction_prompt(
    sql: str,
    violation: CorrectnessViolation,
) -> str:
    """构造修正提示词（单条违规 → 单次调用）。"""
    evidence = json_dumps(violation.evidence) if violation.evidence else "（无）"
    return _USER_TEMPLATE.format(
        sql=sql,
        violation=(
            f"- 规则：{violation.rule}\n"
            f"- 字段：{violation.field or '（表级）'}\n"
            f"- 级别：{violation.severity}\n"
            f"- 结论：{violation.message}\n"
            f"- 证据：{evidence}\n"
            f"- 修正方向：{violation.suggestion or '（无）'}"
        ),
    )


# ============================================================================
# 补丁应用
# ============================================================================


class PatchError(ValueError):
    """补丁无法安全应用（锚点不唯一 / 越界 / 重叠）。"""


def apply_edits(sql: str, edits: list[dict[str, Any]]) -> str:
    """把增量补丁应用到原 SQL 上。

    规则（与 ``sql_rule_detectors.RuleDetector.apply`` 一致）：

    - ``old_text`` 必须在原 SQL 里**恰好出现一次**；
    - ``new_text`` 不能与 ``old_text`` 相同（那不叫修正）；
    - 多个补丁的锚点**不能重叠**。

    Raises:
        PatchError: 补丁不合法时抛出（调用方记为本轮失败）。
    """
    if not edits:
        raise PatchError("没有可应用的补丁")
    source = str(sql or "")
    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise PatchError("补丁必须是对象")
        old_text = edit.get("old_text")
        new_text = edit.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise PatchError("补丁缺少 old_text")
        if not isinstance(new_text, str):
            raise PatchError("补丁缺少 new_text")
        if old_text == new_text:
            raise PatchError("补丁的 old_text 与 new_text 相同")
        start = source.find(old_text)
        if start < 0:
            raise PatchError(f"old_text 在原 SQL 中不存在：{old_text[:60]!r}")
        if source.find(old_text, start + 1) >= 0:
            raise PatchError(f"old_text 在原 SQL 中不唯一：{old_text[:60]!r}")
        spans.append((start, start + len(old_text), new_text))

    updated = source
    boundary = len(source)
    for start, end, replacement in sorted(spans, reverse=True):
        if end > boundary:
            raise PatchError("补丁锚点重叠")
        updated = updated[:start] + replacement + updated[end:]
        boundary = start
    return updated


def summarize_diff(original: str, updated: str, limit: int = 20) -> str:
    """产出「修正前后 SQL 的差异摘要」—— 让人工复核者一眼看出改了什么。"""
    import difflib

    diff = list(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
            n=1,
        )
    )
    if not diff:
        return "（无变化）"
    body = diff[:limit]
    text = "\n".join(body)
    if len(diff) > limit:
        text += f"\n…（共 {len(diff)} 行差异，已截断）"
    return text


# ============================================================================
# 自修正闭环
# ============================================================================


@dataclass
class SelfCorrectResult:
    """自修正闭环的结果。"""

    #: 是否收敛（修正后不再有 error 级违规）。
    converged: bool = False
    #: 用了几轮。
    rounds: int = 0
    #: 是否需要人工复核（收敛失败时为真）。
    needs_manual_review: bool = False
    #: 最终仍未收敛的违规。
    remaining_violations: list[dict[str, Any]] = field(default_factory=list)
    #: 每轮的摘要（轮次、修正原因、失败原因）。
    round_summaries: list[dict[str, Any]] = field(default_factory=list)
    #: 备份文件路径。
    backups: list[str] = field(default_factory=list)
    #: 修正后的 SQL（收敛时是最终版；失败时是最后一版尝试）。
    final_sql: str = ""
    #: 差异摘要（人工复核用）。
    diff_summary: str = ""
    #: 失败原因（没跑起来的整体原因）。
    reason: str = ""
    #: 模型判定该违规是**误报**（与「模型改不动」区分开：前者复核校验规则，
    #: 后者改 SQL）。
    disputed: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.converged:
            status = "converged"
        elif self.disputed:
            status = "disputed"
        elif self.needs_manual_review and not self.rounds:
            status = "not_needed"
        else:
            status = "not_converged"
        return {
            "status": status,
            "converged": self.converged,
            "rounds": self.rounds,
            "needs_manual_review": self.needs_manual_review,
            "remaining_violations": list(self.remaining_violations),
            "round_summaries": list(self.round_summaries),
            "backups": list(self.backups),
            "diff_summary": self.diff_summary,
            "reason": self.reason,
            "disputed": self.disputed,
        }


async def run_self_correct(
    *,
    dml_path: str | Path,
    original_sql: str,
    violations: list[CorrectnessViolation],
    recheck: Any,
    llm: Any,
    max_rounds: int = 3,
    enabled: bool = True,
    llm_kwargs: dict[str, Any] | None = None,
) -> SelfCorrectResult:
    """跑自修正闭环。

    Args:
        dml_path: 交付的 INSERT 文件路径（会被写回）。
        original_sql: 当前 INSERT SQL（第一轮的底稿）。
        violations: 本轮校验产出的**全部**违规（只修 error 级）。
        recheck: 复检回调 ``async (sql) -> 复检结论``。**必须重走整个流程**
            （重新试算 → 重新跑全部确定性 checker → 重新跑流程型校验），
            返回值可以是：
            - ``list[CorrectnessViolation]`` —— 只有违规列表（旧接口，兼容）；
            - ``dict`` —— 完整的 quality_check 结论（``violations`` /
              ``failed_checks`` / ``partial`` / ``ok`` …）。
            传 dict 时本函数会额外判断**这次复检本身是否完整**。
        llm: 模型实例（``None`` → 不修正，只报违规）。
        max_rounds: 轮次上限（与 DataOps 提交次数上限**互相独立**）。
        enabled: 自修正开关（关闭 → 只输出违规，不调模型）。
        llm_kwargs: 模型调用参数。

    Returns:
        :class:`SelfCorrectResult`。
    """
    result = SelfCorrectResult()
    error_violations = [v for v in violations if v.is_error]
    if not error_violations:
        # 只有 warning 级 → 不触发自修正（需求：仅 error 级触发）。
        result.converged = True
        result.reason = "没有 error 级违规"
        return result
    if not enabled:
        result.needs_manual_review = True
        result.remaining_violations = [v.to_dict() for v in error_violations]
        result.reason = "自修正已关闭"
        return result
    if llm is None:
        result.needs_manual_review = True
        result.remaining_violations = [v.to_dict() for v in error_violations]
        result.reason = "模型不可用"
        return result
    if max_rounds <= 0:
        result.needs_manual_review = True
        result.remaining_violations = [v.to_dict() for v in error_violations]
        result.reason = "自修正轮次上限为 0"
        return result

    path = Path(dml_path)
    current_sql = original_sql
    pending = error_violations
    original_snapshot = original_sql

    for round_index in range(1, max_rounds + 1):
        result.rounds = round_index
        summary: dict[str, Any] = {"round": round_index}

        # --- 逐条违规修正（一次违规一次调用，保持职责单一）--------------
        edits: list[dict[str, Any]] = []
        reasons: list[str] = []
        failed = False
        # 模型对每条违规的判定：判定为误报 / 判定成立但改不动。
        disputed_rules: list[str] = []
        for violation in pending:
            payload = await invoke_llm_json(
                llm,
                system=_SYSTEM_PROMPT,
                user=build_correction_prompt(current_sql, violation),
                llm_kwargs=llm_kwargs,
            )
            if payload is None:
                failed = True
                reasons.append(f"[{violation.rule}] 模型返回无法解析")
                continue
            round_edits = payload.get("edits")
            if not isinstance(round_edits, list) or not round_edits:
                reason = str(payload.get("reason") or "模型未给出补丁")
                reasons.append(f"[{violation.rule}] {reason}")
                if _is_dispute(reason):
                    disputed_rules.append(violation.rule)
                continue
            try:
                current_sql = apply_edits(current_sql, round_edits)
            except PatchError as exc:
                failed = True
                reasons.append(f"[{violation.rule}] 补丁无法应用：{exc}")
                continue
            edits.extend(round_edits)
            reasons.append(f"[{violation.rule}] {payload.get('reason') or '已修正'}")

        summary["edits"] = len(edits)
        summary["reasons"] = reasons
        if disputed_rules:
            summary["disputed"] = disputed_rules
        if failed or not edits:
            summary["status"] = "failed"
            summary["reason"] = "; ".join(reasons) or "本轮没有产生任何补丁"
            result.round_summaries.append(summary)
            result.remaining_violations = [v.to_dict() for v in pending]
            result.needs_manual_review = True
            result.reason = summary["reason"]
            result.final_sql = current_sql
            # 全部违规都被判为误报 → 这是「误报」，不是「改不动」。
            if disputed_rules and len(disputed_rules) == len(pending):
                result.disputed = True
                result.reason = (
                    "模型判定下列违规不成立（判定为误报）："
                    + "、".join(disputed_rules)
                    + "；"
                    + summary["reason"]
                )
            return result

        # --- 备份 + 写回 -------------------------------------------------
        backup = _backup_file(path, round_index)
        if backup:
            result.backups.append(str(backup))
        try:
            path.write_text(current_sql, encoding="utf-8")
        except OSError as exc:
            summary["status"] = "failed"
            summary["reason"] = f"写回失败：{exc}"
            result.round_summaries.append(summary)
            result.remaining_violations = [v.to_dict() for v in pending]
            result.needs_manual_review = True
            result.reason = summary["reason"]
            result.final_sql = current_sql
            return result

        # --- 复检（**重走整个流程**：试算 → 全部 checker → 流程型校验）----
        try:
            recheck_outcome = await recheck(current_sql)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[self_correct] recheck raised: {exc!r}")
            summary["status"] = "failed"
            summary["reason"] = f"复检失败：{exc!r}"
            result.round_summaries.append(summary)
            result.remaining_violations = [v.to_dict() for v in pending]
            result.needs_manual_review = True
            result.reason = summary["reason"]
            result.final_sql = current_sql
            result.diff_summary = summarize_diff(original_snapshot, current_sql)
            return result

        new_violations, recheck_incomplete = _extract_recheck(recheck_outcome)
        summary["status"] = "ok"
        summary["remaining"] = len([v for v in new_violations if v.is_error])
        if recheck_incomplete:
            # ⚠️ **这次复检本身没跑完** —— 「没查到违规」不等于「校验通过」。
            #
            # 早先这里只看违规列表：复检时某个 checker 超时 / 常识判定降级，
            # 违规列表恰好是空的，就被判成「收敛」—— 正是「校验没做成却判通过」
            # 的翻版。这次复检不完整 → 本轮**不算修好**，继续下一轮。
            summary["status"] = "incomplete"
            summary["incomplete_reason"] = recheck_incomplete
            summary["reason"] = f"复检未完成（{recheck_incomplete}）—— 本轮不判定为收敛"
        result.round_summaries.append(summary)

        remaining_errors = [v for v in new_violations if v.is_error]
        if not remaining_errors and not recheck_incomplete:
            result.converged = True
            result.remaining_violations = []
            result.final_sql = current_sql
            result.diff_summary = summarize_diff(original_snapshot, current_sql)
            return result

        pending = remaining_errors or pending
        result.remaining_violations = [v.to_dict() for v in pending]
        result.final_sql = current_sql
        result.diff_summary = summarize_diff(original_snapshot, current_sql)

    # --- 轮次用尽 --------------------------------------------------------
    result.needs_manual_review = True
    result.reason = f"已尝试 {result.rounds} 轮仍未通过"
    return result


def _extract_recheck(
    outcome: Any,
) -> tuple[list[CorrectnessViolation], str]:
    """把复检回调的返回值解析成 ``(违规列表, 未完成原因)``。

    复检回调有两种返回形状：

    - ``list[CorrectnessViolation]`` —— 旧接口，只有违规列表；
    - ``dict`` —— 完整的 quality_check 结论。**这时要额外判断这次复检本身
      是否跑完** —— ``failed_checks`` 非空或 ``partial=True`` 都说明有校验项
      没跑成，此时「没有违规」只是「没查出来」，不是「查过且通过」。

    Returns:
        ``(违规列表, 未完成原因)``；未完成原因为空串表示复检完整。
    """
    from dataagent.actions.tools.local_tool.dataops_quality_check import (
        violations_from_dicts,
    )

    if isinstance(outcome, dict):
        violations = violations_from_dicts(outcome.get("violations"))
        failed = outcome.get("failed_checks") or []
        partial = bool(outcome.get("partial"))
        # **空字典 = 复检什么都没产出** —— 不是「复检完整且无违规」。
        # 早先这里返回 ``([], "")``，调用方据此判成**收敛**，报告写
        # 「✅ 自修正已收敛，当前 SQL 已通过复检」—— 但复检根本没跑成
        # （试算失败 / 校验不可用）。必须当成「未完成」。
        if not outcome:
            return violations, "复检未产出任何结果"
        if failed:
            names = "、".join(
                str(item.get("name") or "?") for item in failed if isinstance(item, dict)
            )
            return violations, f"校验项未跑完：{names or '未知'}"
        if partial:
            return violations, "校验未完整完成"
        if not outcome.get("sampled", True):
            # sampled=False = 校验没跑起来（提交失败 / 超时 / 没数据）。
            return violations, outcome.get("error") or "校验未执行"
        return violations, ""

    if isinstance(outcome, list):
        return [v for v in outcome if isinstance(v, CorrectnessViolation)], ""
    return [], "复检返回了无法识别的结果"


def _backup_file(path: Path, round_index: int) -> Path | None:
    """备份原交付文件（``<name>.sql.bak.<round>``）。

    备份失败**不阻断**修正 —— 只是少一条留痕，日志里记一笔。
    """
    try:
        backup = path.with_name(f"{path.name}.bak.{round_index}")
        shutil.copy2(path, backup)
        return backup
    except OSError as exc:
        logger.warning(f"[self_correct] backup of {path} failed: {exc!r}")
        return None


#: 模型回复里表示「这条违规不成立」的措辞 —— 与「我改不动」区分开。
_DISPUTE_MARKERS: tuple[str, ...] = (
    "无违规",
    "不存在违规",
    "不是违规",
    "并非违规",
    "不构成违规",
    "违规不成立",
    "误报",
    "假阳性",
    "符合预期",
    "实际值与期望值一致",
    "与期望值一致",
    "无需修正",
    "不需要修正",
    "不必修改",
)


def _is_dispute(reason: str) -> bool:
    """判断模型的 ``reason`` 是否在说「这条违规不成立（误报）」。

    - 误报  → 模型认为 SQL 没问题，是**校验规则**判错了；
    - 改不动 → 模型认为 SQL 有问题，但自己给不出安全补丁。
    """
    if not reason:
        return False
    text = reason.strip()
    return any(marker in text for marker in _DISPUTE_MARKERS)


__all__ = [
    "PatchError",
    "SelfCorrectResult",
    "apply_edits",
    "build_correction_prompt",
    "run_self_correct",
    "summarize_diff",
]
