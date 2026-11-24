"""From metavisor 6c0dcf4, nl2sql/validator/result_checker_v2.py; host I/O is asynchronous."""

import logging
from collections import Counter
from typing import Dict, List, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker, RepairError

logger = logging.getLogger(__name__)


class ResultChecker(BaseChecker):
    """结果校验器 - 执行SQL验证结果是否有效"""

    async def check_and_revise(
        self,
        sql: str,
        data_item,
        sampling_budget: int = 3,
        max_parse_retries: int = DEFAULT_REPAIR_ATTEMPTS,
        log_tag: str = "",
        cot_recorder=None,
        sql_temp_id=None,
    ) -> Tuple[str, Dict[str, int]]:
        """
        检查SQL执行结果并修复错误

        Raises:
            RepairError: 需要修复但解析失败达到最大重试次数时抛出
        """
        # 执行SQL验证
        execution_result = await data_item.execute(sql)

        if execution_result.result_type == "success":
            return sql, {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "content_tokens": 0,
            }

        # 需要修复
        prompt = data_item.execution_prompt(sql, execution_result)

        input_sql_for_cot = sql
        trigger_reason = f"execution_result_type={execution_result.result_type}"
        total_token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }
        all_sql_candidates = []
        parse_retries = 0
        # 候选 SQL -> 产出它的响应上下文（output_full, llm_reasoning, parsed_reasoning），
        # 用于循环结束后单次 CoT 记录（重试/多候选不再重复记录）
        candidate_ctx: Dict[str, Tuple[str, str, str]] = {}
        last_ctx: Tuple[str, str, str] = ("", "", "")

        while len(all_sql_candidates) < sampling_budget and parse_retries < max_parse_retries:
            (responses, token_usage, reasoning_contents) = await data_item.ask(
                prompt, count=sampling_budget - len(all_sql_candidates), stop=["</result>"]
            )

            parsed_any = False
            for resp_idx, response in enumerate(responses):
                if response.response_metadata.get("finish_reason") == "length":
                    logger.warning(
                        f"{log_tag}[ResultChecker] LLM response truncated (finish_reason=length)"
                    )
                response_content = response.content.strip()
                try:
                    parsed_sql_candidate = self._parse_llm_response(
                        response_content, question_id=data_item.question_id, log_tag=log_tag
                    )
                except Exception as e:
                    logger.error(f"{log_tag}Error parsing LLM response: {e}")
                    parsed_sql_candidate = None

                # 暂存响应上下文（CoT 在触发结束后仅记录一条）
                reasoning_text = (
                    reasoning_contents[resp_idx] if resp_idx < len(reasoning_contents) else ""
                )
                last_ctx = (
                    response_content,
                    reasoning_text,
                    self._parse_reasoning_tag(response_content),
                )
                if parsed_sql_candidate:
                    candidate_ctx[parsed_sql_candidate] = last_ctx
                    all_sql_candidates.append(parsed_sql_candidate)
                    parsed_any = True

            if not parsed_any:
                parse_retries += 1
                logger.warning(
                    f"{log_tag}[ResultChecker] Parse failed, retry {parse_retries}/{max_parse_retries}"
                )

            total_token_usage["input_tokens"] += token_usage.get("input_tokens", 0)
            total_token_usage["output_tokens"] += token_usage.get("output_tokens", 0)
            total_token_usage["reasoning_tokens"] += token_usage.get("reasoning_tokens", 0)
            total_token_usage["content_tokens"] += token_usage.get("content_tokens", 0)

        # 如果没有收集到任何候选且达到重试上限，抛出异常
        if len(all_sql_candidates) == 0 and parse_retries >= max_parse_retries:
            raise RepairError(
                f"[ResultChecker] LLM parse max retries ({max_parse_retries}) exceeded, no valid SQL generated"
            )

        if len(all_sql_candidates) < sampling_budget:
            logger.warning(
                f"{log_tag}[ResultChecker] Only got {len(all_sql_candidates)}/{sampling_budget} candidates after {parse_retries} retries"
            )

        selected_sql = await self._select_sql_candidate(all_sql_candidates, data_item)
        # CoT 记录：每次触发仅一条；内容为产出选中 SQL 的响应（无有效候选时取最后一次响应），token 为累计值
        if cot_recorder is not None and sql_temp_id is not None:
            try:
                out_full, llm_rs, parsed_rs = (
                    candidate_ctx.get(selected_sql, last_ctx) if selected_sql else last_ctx
                )
                cot_recorder.record_validation(
                    sql_temp_id=sql_temp_id,
                    checker_name="ResultChecker",
                    trigger_reason=trigger_reason,
                    input_sql=input_sql_for_cot,
                    rule_input_sql="",
                    rule_output_sql="",
                    llm_input_sql=input_sql_for_cot,
                    llm_output_sql=selected_sql,
                    input_prompt=prompt,
                    output_full=out_full,
                    llm_reasoning=llm_rs,
                    parsed_reasoning=parsed_rs,
                    output_sql=selected_sql,
                    token_usage=total_token_usage,
                )
            except Exception as e:
                logger.debug(
                    f"{log_tag}[CoT][ResultChecker] validation record failed (non-fatal): {e}"
                )
        if selected_sql:
            return selected_sql, total_token_usage
        return sql, total_token_usage

    async def _select_sql_candidate(self, all_sql_candidates: List[str], data_item) -> str:
        """选择最佳SQL候选"""
        valid_sql_candidates = []
        for sql_candidate in all_sql_candidates:
            execution_result = await data_item.execute(sql_candidate)
            if execution_result.result_type == "success":
                result_key = (
                    frozenset(execution_result.result_rows)
                    if execution_result.result_rows
                    else frozenset()
                )
                valid_sql_candidates.append((sql_candidate, result_key))

        if len(valid_sql_candidates) == 0:
            return None

        counter = Counter(result for _, result in valid_sql_candidates)
        return max(valid_sql_candidates, key=lambda x: counter[x[1]])[0]
