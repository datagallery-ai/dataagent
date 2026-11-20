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
"""常识违规检测（需求 1.3「违犯 common sense 的错误」）。

**为什么需要模型**：常识违规必须**跨字段看同一行**才能判定 —— 年龄为 -5 是
常识错误，但单看「年龄是数值」没有任何问题；「VIP 与新用户同时为真」更是
只有把两个字段放在一起才看得出来。这类判定没法写成 SQL 谓词。

**模型只做判定，不做修复**：修复是自修正闭环里另一次独立的、带完整上下文
（原始 INSERT SQL）的调用。职责分离很重要 —— 判定与修复混在一次调用里，
模型会因为「要改」而放宽标准，误报率会显著上升。

**只接受带具体违反依据的输出**：模型无法判断的 MUST NOT 报告。否则「不确定」
会被当成「违规」，自修正就会去「修」本来正确的 SQL。

降级：模型不可用 / 返回不是合法 JSON → 本项记「未完成」，不抛异常，
不影响其它校验项的结果，也不让整轮校验失败。
"""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.local_tool.dataops_correctness_flow import (
    FlowChecker,
    FlowContext,
    extract_numeric_candidates,
    invoke_llm_json,
    json_dumps,
    matches_with_rounding,
    sample_rows,
)
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CorrectnessViolation,
)
from dataagent.utils.log import logger

# ============================================================================
# Prompt
# ============================================================================

_SYSTEM_PROMPT = """你是数据质量审查员，只做**判定**，不产出任何 SQL。

你的任务：在一批抽样数据行里，找出**违反常识**的取值。

「违反常识」的定义（必须能给出具体依据，不确定就不要报）：
- 取值本身在业务上不可能成立：年龄为负、时长为负、比率超出 [0,1]、
  未来时间戳、金额为负而字段语义是「消费金额」等。
- 同一行上多个字段的联合语义矛盾：互斥标记同时为真（如同时是 VIP 又是新用户）、
  「是否付费」为否但「付费次数」大于 0、状态字段与时间字段互相矛盾等。
- 字段语义与取值量纲明显不符（如「近 7 天活跃天数」取值为 30）。

**硬性要求**：
1. 只输出 JSON，不要输出任何解释性文字或 SQL。
2. 每条违规 MUST 给出：field（字段名）、row_index（行号，从 1 开始）、
   observed_value（实际取值）、reason（**具体的**违反依据）、
   severity（error / warning）、expected（你认为应该是什么）。
3. **无法判断的 MUST NOT 报告** —— 宁可漏报，也不要把「不确定」当「违规」。
   字段没有注释、语义不明确时，判定质量下降是允许的。
4. **不要输出修正后的 SQL** —— 你只负责指出问题，修正由另一步完成。
5. 每条违规的 reason MUST 说明「哪个字段的哪个取值违反了哪条常识」，
   空泛的描述（如「数据异常」）一律不接受。

**数值精度差异 MUST NOT 报告为违规**：
- 你算出来的期望值是**四舍五入后**的（如 0.3387），而实际值是原始精度
  （如 0.338683）—— 只要两者在**前 4 位小数内一致**（相对差 < 0.1%），
  这就是显示精度不同，**不是违规**，MUST NOT 报告。
- 只有相对差**明显超过 0.1%** 时才算口径不一致（如期望 0.3387、实际
  0.300000，或实际 0.40 vs 期望 0.3387）。
- 判定时**用相对差**判断，不要纠结最后一位小数。
"""

_USER_TEMPLATE = """下面是目标表 `{table}` 的一批抽样数据（分区条件：{partition}）。

字段语义：
{field_semantics}

抽样数据（{row_count} 行）：
{rows}

请输出 JSON，格式为：
{{"violations": [{{"field": "字段名", "row_index": 1, "observed_value": 取值, "reason": "具体违反依据", "severity": "error", "expected": "应该是什么"}}]}}

没有违反常识的行就输出：{{"violations": []}}
"""


def build_common_sense_prompt(ctx: FlowContext, rows: list[dict[str, Any]]) -> str:
    """构造常识判定的 user 提示词。"""
    semantics: list[str] = []
    for name in ctx.fields:
        column_type = ctx.column_types.get(name) or ctx.column_types.get(name.lower()) or ""
        comment = ctx.column_comments.get(name) or ctx.column_comments.get(name.lower()) or ""
        parts = [f"- {name}"]
        if column_type:
            parts.append(f"类型={column_type}")
        if comment:
            parts.append(f"含义={comment}")
        else:
            parts.append("含义=（无注释，只能依据字段名推断）")
        semantics.append(" ".join(parts))
    return _USER_TEMPLATE.format(
        table=ctx.target_table,
        partition=ctx.partition_filter or "（未指定）",
        field_semantics="\n".join(semantics) or "（无字段）",
        row_count=len(rows),
        rows=json_dumps(rows),
    )


# ============================================================================
# Checker
# ============================================================================

#: 单次抽样的行数上限 —— 防止模型输入爆炸（20 行是需求默认，够用）。
_MAX_SAMPLE_ROWS = 200


async def run_common_sense(ctx: FlowContext) -> list[CorrectnessViolation]:
    """常识违规检测的流程。

    Returns:
        结构化违规列表。**降级时返回空列表**，并把状态写进
        ``ctx.notes``（编排层据此记「未完成」）。
    """
    if not ctx.fields:
        ctx.notes["common_sense__status"] = "skipped"
        ctx.notes["common_sense__reason"] = "没有可校验的字段"
        return []
    if ctx.execute_sql is None:
        ctx.notes["common_sense__status"] = "unavailable"
        ctx.notes["common_sense__reason"] = "没有可用的查询执行器"
        return []
    if ctx.llm is None:
        ctx.notes["common_sense__status"] = "unavailable"
        ctx.notes["common_sense__reason"] = "模型不可用"
        return []

    limit = max(1, min(ctx.common_sense_sample_size, _MAX_SAMPLE_ROWS))
    rows, status = await sample_rows(ctx, list(ctx.fields), limit=limit)
    if status == "empty":
        ctx.notes["common_sense__status"] = "skipped"
        ctx.notes["common_sense__reason"] = "抽样结果为空"
        return []
    if status != "completed":
        ctx.notes["common_sense__status"] = "failed" if status == "failed" else "skipped"
        ctx.notes["common_sense__reason"] = f"抽样失败（{status}）"
        return []

    ctx.notes["common_sense__sample_rows"] = len(rows)
    # 把抽样到的实际数据记进 notes —— 报告里要能看到「校验的到底是什么样的
    # 数据」。常识判定是**跨字段**的，结论只有放回具体取值才看得懂；而「0 处
    # 违规」时报告里一条违规明细都没有，不摊出抽样数据，用户看到的只是一份
    # 「跑了 20 行、0 处违规」却**一行数据都没看见**的报告。
    #
    # 记原始行（不裁剪）—— notes 是**数据源**，裁剪是**渲染层**的活。
    ctx.notes["common_sense__sample_data"] = rows

    payload = await invoke_llm_json(
        ctx.llm,
        system=_SYSTEM_PROMPT,
        user=build_common_sense_prompt(ctx, rows),
        llm_kwargs=ctx.llm_kwargs,
    )
    if payload is None:
        # 模型调用失败 / 返回不是 JSON → 未完成，不猜。
        ctx.notes["common_sense__status"] = "failed"
        ctx.notes["common_sense__reason"] = "模型返回无法解析"
        return []

    violations = _parse_common_sense_violations(payload.get("violations"), len(rows))
    ctx.notes["common_sense__status"] = "completed"
    ctx.notes["common_sense__violation_count"] = len(violations)
    return violations


def _parse_common_sense_violations(
    raw: Any,
    row_count: int,
) -> list[CorrectnessViolation]:
    """把模型输出解析成结构化违规。

    **只接受带具体违反依据的项** —— ``reason`` 为空的直接丢弃（模型自己都
    说不出依据，那这条就不成立）。

    **数值精度差异不算违规** —— 见 :func:`_is_rounding_only`。
    """
    if not isinstance(raw, list):
        return []
    violations: list[CorrectnessViolation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not field or not reason:
            continue
        observed = item.get("observed_value")
        expected = str(item.get("expected") or "").strip()
        # **四舍五入差异不是违规** —— 直接丢弃。
        if _is_rounding_only(observed, expected, reason):
            continue
        row_index = item.get("row_index")
        try:
            row_number = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_number = None
        if row_number is not None and not (1 <= row_number <= row_count):
            # 行号越界 —— 模型可能把行号当成 0 基或整体偏移了。
            row_number = None
        severity = str(item.get("severity") or SEVERITY_WARNING).strip().lower()
        if severity not in (SEVERITY_ERROR, SEVERITY_WARNING):
            severity = SEVERITY_WARNING
        evidence: dict[str, Any] = {
            "field": field,
            "observed_value": observed,
            "reason": reason,
        }
        if row_number is not None:
            evidence["row_index"] = row_number
        if expected:
            evidence["expected"] = expected
        violations.append(
            CorrectnessViolation(
                rule="common_sense",
                field=field,
                severity=severity,
                message=f"违反常识：{reason}",
                evidence=evidence,
                suggestion=(
                    f"检查 `{field}` 的取值口径：{reason}"
                    + (f"；期望 {expected}" if expected else "")
                ),
            )
        )
    return violations


def _is_rounding_only(observed: Any, expected: str, reason: str) -> bool:
    """判断这条违规**只是数值精度差异**，不是真的口径错误。

    **为什么需要这道闸门**：模型在 ``expected`` 里给的是**手算四舍五入后**
    的值（``0.3387``），而 ``observed_value`` 是原始精度（``0.338683``）。
    两者差 ``1.7e-5`` —— 相对差 ``5e-5``，远超回源比对用的 ``1e-6`` 容差，
    但它**不是违规**，只是显示精度不同。

    误报的代价很大：这条会以 ``error`` 级**触发自修正**，让模型去「修」
    一份本来正确的 SQL —— 那正是「误报比漏报更糟」。

    判定分两步：

    1. **模型自认不违规** —— ``expected`` / ``reason`` 里出现「不视为违规」
       「基本一致」「精度差异」「四舍五入」等措辞 → 直接丢弃。
    2. **数值精度内一致** —— 模型给出的数字里，有**不同于实测值**的那个
       与实测值在四舍五入精度内一致 → 判定为精度差异，丢弃。

    第 2 步为什么要**先剔除等于实测值的数字**：模型的 ``reason`` 几乎一定
    会**重述实测值**（「计算值为 0.3387，但实际值为 0.30」）。如果不剔除，
    「实际值为 0.30」这个数字会和实测值精确相等 → 命中 → **真违规被误丢**。
    剔掉之后，剩下的才是模型主张的「应该是多少」——
    它与实测值精度内一致，才是精度差异。
    """
    if observed is None:
        return False
    # 实测值本身必须是单个数值 —— 多个数字（比值/区间）说明这不是精度问题。
    observed_numbers = extract_numeric_candidates(observed)
    if len(observed_numbers) != 1:
        return False
    observed_number = observed_numbers[0]

    # --- 第 1 步：模型自己说「不算违规」 ---------------------------------
    for text in (expected, reason):
        if any(token in text for token in _NOT_A_VIOLATION_TOKENS):
            return True

    # --- 第 2 步：数值精度内一致 ----------------------------------------
    candidates = extract_numeric_candidates(expected) + extract_numeric_candidates(reason)
    # 剔除与实测值**精确相等**的 —— 那是模型在重述实测值，不是主张的期望值。
    others = [value for value in candidates if abs(value - observed_number) > _EXACT_MATCH_EPSILON]
    return any(matches_with_rounding(candidate, observed_number) for candidate in others)


#: 模型**自认不违规**时的措辞标记。
#:
#: 模型经常在 ``expected`` 里写「0.338683 与公式计算结果 0.3387 基本一致，
#: 但精度差异可能为四舍五入，**不视为违规**」，同时又在 ``reason`` 里写
#: 「与计算值不一致，违反了字段定义的公式规则」—— **自相矛盾**。
#:
#: 这时 MUST 以「不视为违规」为准 —— 模型自己都说了不算违规，再判成
#: ``error`` 级触发自修正，就是让模型去「修」一份本来正确的 SQL。
_NOT_A_VIOLATION_TOKENS: tuple[str, ...] = (
    "不视为违规",
    "不算违规",
    "不是违规",
    "基本一致",
    "精度差异",
    "四舍五入",
    "舍入误差",
    "浮点误差",
)

#: 「精确相等」的判定阈值 —— 用于剔除模型**重述实测值**的数字。
#:
#: ``1e-12`` 足够小：只有真正相等的数字会被剔除，四舍五入差异（``1.7e-5``）
#: 不会被误剔。
_EXACT_MATCH_EPSILON = 1e-12


#: 流程型校验器（与 :data:`dataops_correctness_flow.FlowChecker` 并列）。
COMMON_SENSE_CHECKER = FlowChecker(    name="common_sense",
    run=run_common_sense,
    description="抽样行 + 字段语义，由模型判定跨字段的常识违规",
)


__all__ = [
    "COMMON_SENSE_CHECKER",
    "build_common_sense_prompt",
    "run_common_sense",
    "logger",
]
