"""执行结果校验器 — 基于真实执行反馈复核并修订 SQL
"""
from .base import BaseChecker
from .prompts import PromptFactory
from ..sql_evaluator import run_query, ExecStatus
from . import LLMParseMaxRetriesExceeded
from .. import config
from ..common.log_utils import qp
from typing import Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)

_ACCEPTED_STATUSES = (ExecStatus.OK, ExecStatus.NO_ROWS, ExecStatus.ALL_NULL)


class ExecutionOutcomeChecker(BaseChecker):
    """执行结果校验器 — 执行 SQL 并在结果异常时驱动 LLM 修复"""

    def check_and_revise(self, sql: str, data_item, llm, sampling_budget: int = 1,
                         max_parse_retries: int = None, log_tag: str = "",
                         cot_recorder=None, sql_temp_id: str = None) -> Tuple[str, Dict[str, int]]:
        """
        执行 SQL，结果非 success 时驱动 LLM 修复

        Raises:
            LLMParseMaxRetriesExceeded: 需要修复但解析失败达到最大重试次数时抛出
        """
        if max_parse_retries is None:
            max_parse_retries = config.LLM_PARSE_FAIL_MAX_RETRIES

        outcome = run_query(data_item.database_path, sql)
        empty_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
        if outcome.status in _ACCEPTED_STATUSES:
            return sql, empty_usage

        # 需要修复
        database_schema_profile = PromptFactory.get_enhanced_database_schema_profile(data_item)
        sql_guidance = PromptFactory.get_sql_guidance(data_item)
        prompt = PromptFactory.build_execution_review_prompt(
            database_schema_profile,
            data_item.question,
            data_item.evidence,
            sql,
            outcome.preview,
            sql_guidance,
        )
        trigger_reason = f"execution_status={outcome.status.value}"
        input_sql_for_cot = sql

        total_token_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
        candidates: List[str] = []
        parse_retries = 0

        while len(candidates) < sampling_budget and parse_retries < max_parse_retries:
            responses, token_usage, reasoning_contents = llm.ask(
                [{"role": "user", "content": prompt}],
                n=sampling_budget - len(candidates),
                temperature=config.LLM_TEMPERATURE_VALIDATION,
            )

            parsed_any = False
            for idx, response in enumerate(responses):
                if getattr(response, 'finish_reason', None) == "length":
                    logger.warning(f"{qp(data_item)}{log_tag}[ExecutionOutcomeChecker] LLM response truncated (finish_reason=length, max_tokens={config.LLM_MAX_TOKENS})")
                response_content = response.content.strip()
                try:
                    parsed_sql = self._extract_revised_sql(response_content, question_id=data_item.question_id, log_tag=log_tag)
                except Exception as e:
                    logger.error(f"{qp(data_item)}{log_tag}Error parsing LLM response: {e}")
                    parsed_sql = None

                # CoT 记录
                if cot_recorder is not None and sql_temp_id is not None:
                    try:
                        reasoning_text = reasoning_contents[idx] if idx < len(reasoning_contents) else ""
                        cot_recorder.record_validation(
                            sql_temp_id=sql_temp_id,
                            checker_name="ExecutionOutcomeChecker",
                            trigger_reason=trigger_reason,
                            input_sql=input_sql_for_cot,
                            input_prompt=prompt,
                            output_full=response_content,
                            llm_reasoning=reasoning_text,
                            parsed_reasoning=self._extract_reasoning(response_content),
                            parsed_result_sql=parsed_sql,
                            token_usage=token_usage,
                        )
                    except Exception as e:
                        logger.debug(f"{qp(data_item)}{log_tag}[CoT][ExecutionOutcomeChecker] validation record failed (non-fatal): {e}")

                if parsed_sql:
                    candidates.append(parsed_sql)
                    parsed_any = True

            if not parsed_any:
                parse_retries += 1
                logger.warning(f"{qp(data_item)}{log_tag}[ExecutionOutcomeChecker] Parse failed, retry {parse_retries}/{max_parse_retries}")

            total_token_usage["input_tokens"] += token_usage.get("input_tokens", 0)
            total_token_usage["output_tokens"] += token_usage.get("output_tokens", 0)
            total_token_usage["reasoning_tokens"] += token_usage.get("reasoning_tokens", 0)
            total_token_usage["content_tokens"] += token_usage.get("content_tokens", 0)

        # 没有收集到任何候选且达到重试上限，抛出异常
        if len(candidates) == 0 and parse_retries >= max_parse_retries:
            raise LLMParseMaxRetriesExceeded(
                f"[ExecutionOutcomeChecker] LLM parse max retries ({max_parse_retries}) exceeded, no valid SQL generated"
            )

        if len(candidates) < sampling_budget:
            logger.warning(f"{qp(data_item)}{log_tag}[ExecutionOutcomeChecker] Only got {len(candidates)}/{sampling_budget} candidates after {parse_retries} retries")

        selected_sql = self._vote_best_candidate(candidates, data_item, _ACCEPTED_STATUSES)
        if selected_sql:
            return selected_sql, total_token_usage
        return sql, total_token_usage
