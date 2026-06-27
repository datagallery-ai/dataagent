"""示例驱动生成器 — 基于 few-shot 示例的 In-Context Learning SQL 生成器

从相似问题的示例中迁移 SQL 模式来生成目标 SQL。
生成循环已提取到基类 BaseSQLGenerator._generate_with_retry()。
"""
from .base import BaseSQLGenerator
from .prompts import PromptFactory
from .. import config
from ..common.log_utils import qp
from typing import Dict, List, Tuple, Optional
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ExemplarGenerator(BaseSQLGenerator):
    """示例驱动 SQL 生成器

    使用 few-shot 示例来指导 SQL 生成，通过从相似问题中学习 SQL 模式来生成新的 SQL。
    """

    _few_shot_examples: Dict[str, List[Dict[str, str]]] = None
    _few_shot_examples_path: Optional[str] = None

    def __init__(self, few_shot_examples_path: str = None):
        """
        初始化 ICL 生成器

        Args:
            few_shot_examples_path: few_shot_examples.json 文件路径
                                   如果为 None，使用 config.FEW_SHOT_PATH
        """
        super().__init__()

        if few_shot_examples_path is None:
            few_shot_examples_path = config.FEW_SHOT_PATH

        self._few_shot_examples_path = few_shot_examples_path
        self._load_few_shot_examples()

    def _load_few_shot_examples(self):
        """加载 few-shot 示例文件"""
        try:
            with open(self._few_shot_examples_path, "r", encoding="utf-8") as f:
                self._few_shot_examples = json.load(f)
            logger.debug(f"Loaded few-shot examples from {self._few_shot_examples_path}")
            logger.debug(f"Total question IDs: {len(self._few_shot_examples)}")
        except FileNotFoundError:
            logger.error(f"Few-shot examples file not found: {self._few_shot_examples_path}")
            self._few_shot_examples = {}
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse few-shot examples JSON: {e}")
            self._few_shot_examples = {}

    def generate(self, data_item, llm, sampling_budget: int = 1,
                 max_parse_retries: int = None,
                 cot_recorder=None,
                 temp_id_prefix: str = "") -> Tuple[List[str], Dict[str, int], List[str]]:
        """
        使用 ICL 方式生成 SQL

        Args:
            data_item: SimpleDataItem 数据项（需要包含 question_id 字段）
            llm: LLMAdapter 实例
            sampling_budget: 采样次数（需要收集的有效 SQL 数量）
            max_parse_retries: LLM 响应解析失败时的最大重试次数
            cot_recorder: 可选 CoTRecorder 实例（None 时不记录 CoT）

        Returns:
            (sql_candidates, token_usage, sql_temp_ids) 三元组

        Raises:
            LLMParseMaxRetriesExceeded: 解析失败达到最大重试次数且无有效候选时抛出
        """
        if sampling_budget == 0:
            return [], {"input_tokens": 0, "output_tokens": 0}, []

        # 获取 question_id 对应的 few-shot 示例
        question_id = str(getattr(data_item, 'question_id', ''))
        few_shot_examples = self._few_shot_examples.get(question_id, [])

        if not few_shot_examples:
            logger.warning(f"{qp(data_item)}No few-shot examples found for question_id={question_id}")
            return [], {"input_tokens": 0, "output_tokens": 0}, []

        database_schema_profile = PromptFactory.get_enhanced_database_schema_profile(data_item)
        sql_guidance = PromptFactory.get_sql_guidance(data_item)
        prompt = PromptFactory.build_exemplar_prompt(
            few_shot_examples,
            database_schema_profile,
            data_item.question,
            data_item.evidence,
            sql_guidance
        ).strip()

        return self._generate_with_retry(prompt, llm, sampling_budget, max_parse_retries, "ExemplarGenerator",
                                         question_id=getattr(data_item, 'question_id', None),
                                         cot_recorder=cot_recorder, cot_stage="icl",
                                         temp_id_prefix=temp_id_prefix)
