"""语法合规校验器 — 把全部 SQL 语法规则交给 LLM 一次性自查并修订
"""
from .base import BaseChecker
from .prompts import PromptFactory
from . import LLMParseMaxRetriesExceeded
from .. import config
from ..common.log_utils import qp
from typing import Dict, Tuple
import logging

logger = logging.getLogger(__name__)


class SQLComplianceChecker(BaseChecker):
    """语法合规校验器"""

    def check_and_revise(self, sql: str, data_item, llm, sampling_budget: int = 1,
                         max_parse_retries: int = None, log_tag: str = "",
                         cot_recorder=None, sql_temp_id: str = None) -> Tuple[str, Dict[str, int]]:
        """
        Raises:
            LLMParseMaxRetriesExceeded: 解析失败达到最大重试次数时抛出
        """
        if max_parse_retries is None:
            max_parse_retries = config.LLM_PARSE_FAIL_MAX_RETRIES

        logger.debug(f"{qp(data_item)}{log_tag}[SQLComplianceChecker] Input SQL: {sql}")
        database_schema_profile = PromptFactory.get_enhanced_database_schema_profile(data_item)
        sql_guidance = PromptFactory.get_sql_guidance(data_item)
        prompt = PromptFactory.build_compliance_review_prompt(
            database_schema_profile,
            data_item.question,
            data_item.evidence,
            sql,
            sql_guidance,
        )
        input_sql_for_cot = sql
        trigger_reason = "compliance review (inlined rule checklist)"

        parsed_sql_candidate = None
        total_token_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
        retries = 0

        while not parsed_sql_candidate and retries < max_parse_retries:
            responses, token_usage, reasoning_contents = llm.ask(
                [{"role": "user", "content": prompt}],
                n=1,
                temperature=config.LLM_TEMPERATURE_VALIDATION,
            )
            response_content = responses[0].content.strip()
            if getattr(responses[0], 'finish_reason', None) == "length":
                logger.warning(f"{qp(data_item)}{log_tag}[SQLComplianceChecker] LLM response truncated (finish_reason=length, max_tokens={config.LLM_MAX_TOKENS})")

            total_token_usage["input_tokens"] += token_usage.get("input_tokens", 0)
            total_token_usage["output_tokens"] += token_usage.get("output_tokens", 0)
            total_token_usage["reasoning_tokens"] += token_usage.get("reasoning_tokens", 0)
            total_token_usage["content_tokens"] += token_usage.get("content_tokens", 0)

            try:
                parsed_sql_candidate = self._extract_revised_sql(response_content, question_id=data_item.question_id, log_tag=log_tag)
            except Exception as e:
                logger.error(f"{qp(data_item)}{log_tag}Error parsing LLM response: {e}")
                parsed_sql_candidate = None

            # CoT 记录
            if cot_recorder is not None and sql_temp_id is not None:
                try:
                    llm_reasoning = reasoning_contents[0] if reasoning_contents else ""
                    cot_recorder.record_validation(
                        sql_temp_id=sql_temp_id,
                        checker_name="SQLComplianceChecker",
                        trigger_reason=trigger_reason,
                        input_sql=input_sql_for_cot,
                        input_prompt=prompt,
                        output_full=response_content,
                        llm_reasoning=llm_reasoning,
                        parsed_reasoning=self._extract_reasoning(response_content),
                        parsed_result_sql=parsed_sql_candidate,
                        token_usage=token_usage,
                    )
                except Exception as e:
                    logger.debug(f"{qp(data_item)}{log_tag}[CoT][SQLComplianceChecker] validation record failed (non-fatal): {e}")

            if parsed_sql_candidate:
                if parsed_sql_candidate != sql:
                    logger.info(f"{qp(data_item)}{log_tag}[SQLComplianceChecker] SQL revised for compliance")
                    logger.debug(f"{qp(data_item)}{log_tag}[SQLComplianceChecker] Revised SQL: {parsed_sql_candidate}")
                return parsed_sql_candidate, total_token_usage

            retries += 1
            logger.warning(f"{qp(data_item)}{log_tag}[SQLComplianceChecker] Parse failed, retry {retries}/{max_parse_retries}")

        # 达到重试上限仍未解析出结果，抛出异常
        raise LLMParseMaxRetriesExceeded(
            f"[SQLComplianceChecker] LLM parse max retries ({max_parse_retries}) exceeded, failed to revise SQL"
        )
