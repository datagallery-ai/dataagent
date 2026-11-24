"""From metavisor 6c0dcf4, nl2sql/validator/time_checker_v2.py; host I/O is asynchronous."""

import logging
import re
from typing import Dict, Optional, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker

logger = logging.getLogger(__name__)


class TimeChecker(BaseChecker):
    """时间校验器 - 修复strftime函数中的时间格式错误（纯正则替换，无LLM调用）"""

    async def check_and_revise(
        self,
        sql: str,
        data_item,
        sampling_budget: int = 1,
        max_parse_retries: int = DEFAULT_REPAIR_ATTEMPTS,
        log_tag: str = "",
        cot_recorder=None,
        sql_temp_id=None,
    ) -> Tuple[str, Dict[str, int]]:
        """检查时间格式并修复错误

        Note:
            TimeChecker 是纯正则替换无 LLM 调用；触发时通过
            cot_recorder.record_validation_rule 写入 CoT 记录（LLM 相关字段为空），
            使 Checker Effect 统计覆盖无 LLM 调用的规则替换。
        """
        time_revised_sql = self._check_time(sql)

        if time_revised_sql:
            logger.info(f"{log_tag}[TimeChecker] Found time errors, applying regex fix")
            logger.debug(f"{log_tag}[TimeChecker] Input SQL: {sql}")
            logger.debug(f"{log_tag}[TimeChecker] Revised SQL: {time_revised_sql}")
            # CoT 记录（rule-based 触发，无 LLM 调用，LLM 字段为空）
            if cot_recorder is not None and sql_temp_id is not None:
                try:
                    cot_recorder.record_validation_rule(
                        sql_temp_id=sql_temp_id,
                        checker_name="TimeChecker",
                        trigger_reason="rule-based fix: quoted 4+ digit number in strftime comparison",
                        input_sql=sql,
                        rule_input_sql=sql,
                        rule_output_sql=time_revised_sql,
                        output_sql=time_revised_sql,
                    )
                except Exception as e:
                    logger.debug(
                        f"{log_tag}[CoT][TimeChecker] rule validation record failed (non-fatal): {e}"
                    )
            return time_revised_sql, {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "content_tokens": 0,
            }

        return sql, {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }

    def _check_time(self, sql: str) -> Optional[str]:
        """检测并修复时间格式错误"""
        # 修复strftime比较中数字未加引号的问题
        res = re.sub(r"(strftime *\([^\(]*?\) *[>=<]+ *)(\d{4,})", r"\1'\2'", sql)
        if res != sql:
            return res
        return None
