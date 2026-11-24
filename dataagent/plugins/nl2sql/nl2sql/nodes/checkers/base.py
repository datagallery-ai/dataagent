"""From metavisor 6c0dcf4, nl2sql/validator/base.py; host I/O is asynchronous."""

import logging
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_REPAIR_ATTEMPTS = 10


class RepairError(RuntimeError):
    """Model repair or XML parse exhausted its retry budget."""


class BaseChecker(ABC):
    """SQL校验器基类"""

    @abstractmethod
    async def check_and_revise(
        self,
        sql: str,
        data_item,
        sampling_budget: int = 1,
        max_parse_retries: int = DEFAULT_REPAIR_ATTEMPTS,
        log_tag: str = "",
    ) -> Tuple[str, Dict[str, int]]:
        """
        检查并修订SQL

        Args:
            sql: 待检查的SQL
            data_item: DataAgent 提供的异步执行、修复提示与模型接口
            sampling_budget: 采样次数（保留用于兼容）
            max_parse_retries: LLM响应解析失败时的最大重试次数
            log_tag: 回路/SQL 级日志标识，如 "[dc/sql_1] "

        Returns:
            (revised_sql, token_usage) 元组
        """
        pass

    @staticmethod
    def _parse_llm_response(
        response: str, question_id: int = None, log_tag: str = ""
    ) -> Optional[str]:
        """解析LLM响应，严格提取<result>标签中的SQL"""
        # restore the stop token: </result>
        response += "</result>"

        # fix some common format errors
        if "</reasoning>\n[result>" in response:
            response = response.replace("</reasoning>\n[result>", "</reasoning>\n<result>")

        try:
            answer_match = re.search(r"<result>(.*?)</result>", response, re.DOTALL)
            if not answer_match:
                # [FALLBACK DISABLED] 禁用 fallback 解析，严格要求 <result> 标签
                # # 兼容：Claude可能直接返回SSQL，尝试提取```sql```代码块或直接使用
                # original_response = response.replace("</result>", "").strip()
                #
                # # 尝试提取```sql```代码块
                # sql_block_match = re.search(r"```sql\s*(.*?)\s*```", original_response, re.DOTALL)
                # if sql_block_match:
                #     logger.warning(f"{log_tag}Fallback: extracted SQL from ```sql``` block (no <result> tag)")
                #     return sql_block_match.group(1).strip()
                #
                # # 尝试提取```代码块
                # code_block_match = re.search(r"```\s*(.*?)\s*```", original_response, re.DOTALL)
                # if code_block_match:
                #     content = code_block_match.group(1).strip()
                #     if content.upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
                #         logger.warning(f"{log_tag}Fallback: extracted SQL from ``` block (no <result> tag)")
                #         return content
                #
                # # 最后尝试：如果响应本身就是SQL语句
                # if original_response.upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
                #     logger.warning(f"{log_tag}Fallback: response appears to be raw SQL (no <result> tag)")
                #     return original_response

                logger.warning(f"{log_tag}No <result> tag found in LLM response")
                logger.debug(f"{log_tag}Response content: {response[:500]}")
                return None

            answer_content = answer_match.group(1).strip()
            # strip ```sql```
            if answer_content.startswith("```sql") and answer_content.endswith("```"):
                answer_content = answer_content[len("```sql") : -len("```")].strip()
            elif answer_content.startswith("```") and answer_content.endswith("```"):
                answer_content = answer_content[3:-3].strip()
            return answer_content
        except Exception as e:
            logger.error(f"{log_tag}Error parsing LLM response: {e}")
            logger.debug(f"{log_tag}Response content: {response[:500]}")
            return None

    @staticmethod
    def _parse_reasoning_tag(response: str) -> str:
        """从 LLM 响应中提取 <reasoning>...</reasoning> 标签的内容

        与 `_parse_llm_response` 解耦：仅用于 CoT 输出，不影响主流程的 SQL 解析。
        若未匹配返回空字符串。
        """
        if not response:
            return ""
        try:
            match = re.search(r"<reasoning>(.*?)</reasoning>", response, re.DOTALL)
            return match.group(1).strip() if match else ""
        except Exception:
            return ""
