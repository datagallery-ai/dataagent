"""SQL校验模块

提供 Checker 链式校验机制，串行执行 2 个 checker，逐 checker 即时 LLM 修正：
  1. SQLComplianceChecker   — 规则合规复核
  2. ExecutionOutcomeChecker — 执行结果复核（执行 SQL，结果非 success 时驱动 LLM 修复）
"""

from ..client import LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded


from .base import BaseChecker
from .sql_compliance_checker import SQLComplianceChecker
from .execution_outcome_checker import ExecutionOutcomeChecker
from .sql_validator import validate


def get_default_checkers():
    """获取默认的校验器列表（按执行顺序排列）

    Checker 链顺序：先做规则合规复核，再做执行结果复核兜底：
    SQLComplianceChecker -> ExecutionOutcomeChecker
    """
    return [
        SQLComplianceChecker(),
        ExecutionOutcomeChecker(),
    ]


__all__ = [
    'BaseChecker',
    'SQLComplianceChecker',
    'ExecutionOutcomeChecker',
    'LLMMaxRetriesExceeded',
    'LLMParseMaxRetriesExceeded',
    'get_default_checkers',
    'validate',
]
