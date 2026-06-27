"""分治式生成器 — Divide-and-Conquer SQL 生成器

通过递归分解问题再自底向上组装的策略生成 SQL。
生成循环已提取到基类 BaseSQLGenerator._generate_with_retry()。
"""
from .base import BaseSQLGenerator
from .prompts import PromptFactory
from typing import Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)


class DivideConquerGenerator(BaseSQLGenerator):
    """分治式 SQL 生成器"""

    def generate(self, data_item, llm, sampling_budget: int = 1,
                 max_parse_retries: int = None,
                 cot_recorder=None,
                 temp_id_prefix: str = "") -> Tuple[List[str], Dict[str, int], List[str]]:
        """
        使用分治法生成 SQL

        Args:
            data_item: SimpleDataItem 数据项
            llm: LLMAdapter 实例
            sampling_budget: 采样次数（需要收集的有效 SQL 数量）
            max_parse_retries: LLM 响应解析失败时的最大重试次数
            cot_recorder: 可选 CoTRecorder 实例（None 时不记录 CoT）

        Returns:
            (sql_candidates, token_usage, sql_temp_ids) 三元组

        Raises:
            LLMParseMaxRetriesExceeded: 解析失败达到最大重试次数且无有效候选时抛出
        """
        database_schema_profile = PromptFactory.get_enhanced_database_schema_profile(data_item)
        sql_guidance = PromptFactory.get_sql_guidance(data_item)
        prompt = PromptFactory.build_divide_conquer_prompt(
            database_schema_profile,
            data_item.question,
            data_item.evidence,
            sql_guidance
        ).strip()

        return self._generate_with_retry(prompt, llm, sampling_budget, max_parse_retries, "DivideConquerGenerator",
                                         question_id=getattr(data_item, 'question_id', None),
                                         cot_recorder=cot_recorder, cot_stage="dc",
                                         temp_id_prefix=temp_id_prefix)
