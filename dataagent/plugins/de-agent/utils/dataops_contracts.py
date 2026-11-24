from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class CorrectnessViolation:
    """一条**结构化**的数据正确性违规。

    为什么要有这个结构：校验结论原本只有 ``warnings: list[str]`` —— 是给人看的
    字符串。自修正需要的是「哪条规则、哪个字段、证据是什么、建议怎么改」，
    光靠字符串喂给模型不可靠。

    ``warnings`` 保留（向后兼容现有前端渲染），但**由 violations 派生** ——
    保证「告警文案」与「结构化违规」永远一致（见 :meth:`to_warning_text`）。

    Attributes:
        rule: 规则名（``pk_duplicate_rate`` / ``negative_count`` /
            ``common_sense`` / ``source_reconciliation``）。
        field: 字段名；表级规则用主键组合描述（如 ``udid`` 或 ``udid+dt``）。
        severity: ``error`` / ``warning``。只有 ``error`` 触发自修正。
        message: 人类可读的结论。
        evidence: 证据（实测值 / 重复样例 / 两侧取值对比）。
        suggestion: 修正方向（喂给模型）。
    """

    rule: str
    field: str
    severity: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    #: 已定稿的告警文案（可选）。
    #:
    #: 历史 checker 的 warning 文案已经被前端按字符串消费（``_warned_field_names``
    #: 用 ``'字段名'`` 引号模式反查字段），改文案就会打断现有渲染。所以历史
    #: checker 的违规把原文案原样存进这里，``to_warning_text()`` 直接返回它 ——
    #: 既保证「告警文案由违规派生」，又不改动既有文案。
    warning_text: str = ""

    def to_warning_text(self) -> str:
        """派生出人类可读的告警文案。

        **文案必须能反推回字段名** —— 现有渲染层
        （:func:`_warned_field_names`）用 ``'字段名'`` 的引号模式从告警里
        反查命中告警的字段。表级规则的 ``field`` 是主键组合描述，同样带上引号。
        """
        if self.warning_text:
            return self.warning_text
        prefix = f"[{self.rule}]"
        if self.field:
            return f"{prefix} 字段 '{self.field}' {self.message}"
        return f"{prefix} {self.message}"

    @property
    def is_error(self) -> bool:
        """是否是 error 级（触发自修正的唯一条件）。"""
        return self.severity == SEVERITY_ERROR

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict（报告 / 自修正上下文用）。"""
        return {
            "rule": self.rule,
            "field": self.field,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence or {}),
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CorrectnessViolation:
        """从 dict 还原（``to_dict`` 的逆操作）。

        报告与自修正之间要跨进程/跨轮次传违规 —— 用 dict 序列化最省事，
        所以这里提供无损的还原。
        """
        return cls(
            rule=str(raw.get("rule") or ""),
            field=str(raw.get("field") or ""),
            severity=str(raw.get("severity") or SEVERITY_WARNING),
            message=str(raw.get("message") or ""),
            evidence=dict(raw.get("evidence") or {}),
            suggestion=str(raw.get("suggestion") or ""),
        )


def violations_from_dicts(raw: Any) -> list[CorrectnessViolation]:
    """把序列化的违规列表还原成 :class:`CorrectnessViolation` 列表。"""
    if not isinstance(raw, list):
        return []
    result: list[CorrectnessViolation] = []
    for item in raw:
        if isinstance(item, CorrectnessViolation):
            result.append(item)
        elif isinstance(item, dict):
            result.append(CorrectnessViolation.from_dict(item))
    return result
