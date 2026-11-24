"""From metavisor 6c0dcf4, nl2sql/validator/join_checker_v2.py; host I/O is asynchronous."""

import logging
import re
from typing import Dict, Optional, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker, RepairError

logger = logging.getLogger(__name__)

COT_INSTRUCTION = "Analyze step by step as follows:"

JOIN_SIDE_EFFECT = (
    "If rewriting: ONLY simplify the JOIN ON condition. "
    "Do NOT modify SELECT/DISTINCT/WHERE/GROUP BY/ORDER BY. "
    "Do not rewrite an OR chain into an IN list (or vice versa) — that is "
    "a no-op change, not a simplification."
)


def build_join_suggestion() -> str:
    """JoinChecker 评估式 suggestion：引导 LLM 从 schema 列注释判断列语义关系

    泛化设计（2026-07-25 去 dev 过拟合）：判别依据为列注释的文本模式
    （注释一致 → 同实体备选；注释编号递增/互异 → 不同实体），不再绑定
    具体数据库列名（旧版示例 AdmFName1/2/3、home_player_1..11、
    atom_id/atom_id2 恰好是 dev 触发集全部三种模式，属 prompt 级过拟合）；
    明确该模式本身是合法 SQL（多值关联），仅同实体备选场景需改写；
    不确定时默认保留（压 q_0269 型边界波动）；禁止 OR↔IN 等价转写
    （堵 q_1128 型无效修改）。
    """
    return (
        "### Case: Multi-column OR/IN in JOIN ON\n\n"
        "The SQL uses `JOIN ... ON Ta.col = Tb.col1 OR Ta.col = Tb.col2 [OR ...]` "
        "or `JOIN ... ON Ta.col IN (Tb.col1, Tb.col2, ...)`. This is valid "
        "SQL for multi-valued lookups; it is wrong only when the columns are "
        "fallback variants of one attribute.\n\n"
        f"{COT_INSTRUCTION}\n"
        "Step 1 - Column relationship: In the database schema, compare the "
        "comments of col1, col2, ... .\n"
        "  - Same entity: the comments are identical or near-identical — all "
        "columns are described as the same attribute (e.g. several columns "
        "all described as a customer's contact phone number)\n"
        "  - Different entities: the comments are numbered, incrementing, or "
        "otherwise distinct (e.g. 'departure airport code' vs 'arrival "
        "airport code', or one column for each endpoint of a relationship)\n\n"
        "Step 2 - Question intent: Does the question ask about a specific "
        "entity that maps to one particular column, or about all entities "
        "across all columns?\n\n"
        "Rewrite to single `=` when: the columns are alternative names for "
        "the same entity — keep only the first column.\n"
        "Keep OR/IN when: the columns represent different entities/positions, "
        "or whenever you are not confident that they are the same entity.\n\n"
        "Example - Rewrite: the question asks for the contact phone numbers "
        "of customers; three columns are all commented as 'customer contact "
        "phone number' → same entity → "
        "`ON S.phone_1 = T.phone OR S.phone_2 = T.phone OR S.phone_3 = T.phone` "
        "→ `ON S.phone_1 = T.phone`\n"
        "Example - Keep: the question counts flights involving a certain "
        "airport; two columns are commented 'departure airport code' and "
        "'arrival airport code' → different endpoints of a flight → keep "
        "`ON F.origin = A.code OR F.destination = A.code`\n\n"
        f"{JOIN_SIDE_EFFECT}"
    )


class JoinChecker(BaseChecker):
    """JOIN校验器 - 检测并修复JOIN语法错误"""

    async def check_and_revise(
        self,
        sql: str,
        data_item,
        sampling_budget: int = 1,
        max_parse_retries: int = DEFAULT_REPAIR_ATTEMPTS,
        log_tag: str = "",
        cot_recorder=None,
        sql_temp_id: str = None,
    ) -> Tuple[str, Dict[str, int]]:
        """
        检查JOIN语法并修复错误

        Raises:
            RepairError: 需要修复但解析失败达到最大重试次数时抛出
        """
        join_suggestion = self._check_join(sql)

        if join_suggestion:
            logger.info(f"{log_tag}[JoinChecker] Found join errors, requesting LLM revision")
            logger.debug(f"{log_tag}[JoinChecker] Input SQL: {sql}")

            prompt = data_item.common_prompt(sql, join_suggestion)
            input_sql_for_cot = sql
            trigger_reason = join_suggestion

            parsed_sql_candidate = None
            total_token_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "content_tokens": 0,
            }
            retries = 0

            while not parsed_sql_candidate and retries < max_parse_retries:
                (responses, token_usage, reasoning_contents) = await data_item.ask(
                    prompt, count=1, stop=["</result>"]
                )
                response_content = responses[0].content.strip()
                if responses[0].response_metadata.get("finish_reason") == "length":
                    logger.warning(
                        f"{log_tag}[JoinChecker] LLM response truncated (finish_reason=length)"
                    )

                total_token_usage["input_tokens"] += token_usage.get("input_tokens", 0)
                total_token_usage["output_tokens"] += token_usage.get("output_tokens", 0)
                total_token_usage["reasoning_tokens"] += token_usage.get("reasoning_tokens", 0)
                total_token_usage["content_tokens"] += token_usage.get("content_tokens", 0)

                try:
                    parsed_sql_candidate = self._parse_llm_response(
                        response_content, question_id=data_item.question_id, log_tag=log_tag
                    )
                except Exception as e:
                    logger.error(f"{log_tag}Error parsing LLM response: {e}")
                    parsed_sql_candidate = None

                if parsed_sql_candidate:
                    logger.info(f"{log_tag}[JoinChecker] LLM revision successful")
                    logger.debug(f"{log_tag}[JoinChecker] Revised SQL: {parsed_sql_candidate}")
                    # CoT 记录：每次触发仅一条（解析重试不再重复记录），内容为本次成功响应，token 为累计值
                    if cot_recorder is not None and sql_temp_id is not None:
                        try:
                            llm_reasoning = reasoning_contents[0] if reasoning_contents else ""
                            parsed_reasoning = self._parse_reasoning_tag(response_content)
                            cot_recorder.record_validation(
                                sql_temp_id=sql_temp_id,
                                checker_name="JoinChecker",
                                trigger_reason=trigger_reason,
                                input_sql=input_sql_for_cot,
                                rule_input_sql="",
                                rule_output_sql="",
                                llm_input_sql=input_sql_for_cot,
                                llm_output_sql=parsed_sql_candidate,
                                input_prompt=prompt,
                                output_full=response_content,
                                llm_reasoning=llm_reasoning,
                                parsed_reasoning=parsed_reasoning,
                                output_sql=parsed_sql_candidate,
                                token_usage=total_token_usage,
                            )
                        except Exception as e:
                            logger.debug(
                                f"{log_tag}[CoT][JoinChecker] validation record failed (non-fatal): {e}"
                            )
                    return parsed_sql_candidate, total_token_usage

                retries += 1
                logger.warning(
                    f"{log_tag}[JoinChecker] Parse failed, retry {retries}/{max_parse_retries}"
                )

            # 达到重试上限且需要修复，抛出异常
            raise RepairError(
                f"[JoinChecker] LLM parse max retries ({max_parse_retries}) exceeded, failed to revise SQL"
            )

        return sql, {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }

    def _check_join(self, sql: str) -> Optional[str]:
        """检测JOIN语法错误

        正则维持基线 8605423 原版不变（2026-07-25 回滚确认）：2026-07-25
        曾试用增强版（identifier 去 "."、裸列支持、IN 分支 [^)]*、search
        替代 findall），经实证评估旧版无明确检测漏洞或误报（golden 1534
        条 0 匹配、53 例生产触发集 100% 触发、跨语句拼接/CASE WHEN/
        WHERE-OR 对抗样本零误报），按“非必要不修改”原则回滚维持原版。
        """
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        join_pattern = re.compile(
            rf"JOIN\s+{identifier}(\s+AS\s+{identifier}){{0,1}}\s+ON(\s+{identifier}\.{identifier}\s*(=\s*{identifier}\.{identifier}(?:\s+OR\s+{identifier}\.{identifier}\s*=\s*{identifier}\.{identifier})+|IN\s+\(.*?\)))",
            re.IGNORECASE | re.DOTALL,
        )
        if join_pattern.findall(sql):
            return build_join_suggestion()
        return None
