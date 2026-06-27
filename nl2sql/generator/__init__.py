"""
nl2sql.generator — SQL 生成模块

提供 3 路并行生成（DC / Skeleton / ICL）+ 动态策略编排。
"""

from .base import BaseSQLGenerator
from .dc_generator import DivideConquerGenerator
from .skeleton_generator import StepwiseGenerator
from .icl_generator import ExemplarGenerator
from .sql_generator import generate_and_validate, RouteBudgetUnmetError

__all__ = [
    "BaseSQLGenerator",
    "DivideConquerGenerator",
    "StepwiseGenerator",
    "ExemplarGenerator",
    "generate_and_validate",
    "RouteBudgetUnmetError",
]
