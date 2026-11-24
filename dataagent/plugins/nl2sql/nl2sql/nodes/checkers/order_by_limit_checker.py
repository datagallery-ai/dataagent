"""From metavisor 6c0dcf4, nl2sql/validator/order_by_limit_checker_v2.py; host I/O is asynchronous."""

import logging
import re
from typing import Dict, Optional, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker, RepairError

logger = logging.getLogger(__name__)


class OrderByLimitChecker(BaseChecker):
    """ORDER BY LIMIT校验器 - 检测ORDER BY中错误使用MIN/MAX的情况"""

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
        检查ORDER BY LIMIT语法并修复错误

        Raises:
            RepairError: 需要修复但解析失败达到最大重试次数时抛出
        """
        order_by_limit_suggestion = self._check_order_by_limit(sql)

        if order_by_limit_suggestion:
            logger.info(
                f"{log_tag}[OrderByLimitChecker] Found order-by-limit errors, requesting LLM revision"
            )
            logger.debug(f"{log_tag}[OrderByLimitChecker] Input SQL: {sql}")

            prompt = data_item.common_prompt(sql, order_by_limit_suggestion)
            input_sql_for_cot = sql
            trigger_reason = order_by_limit_suggestion

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
                        f"{log_tag}[OrderByLimitChecker] LLM response truncated (finish_reason=length)"
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
                    logger.info(f"{log_tag}[OrderByLimitChecker] LLM revision successful")
                    logger.debug(
                        f"{log_tag}[OrderByLimitChecker] Revised SQL: {parsed_sql_candidate}"
                    )
                    # CoT 记录：每次触发仅一条（解析重试不再重复记录），内容为本次成功响应，token 为累计值
                    if cot_recorder is not None and sql_temp_id is not None:
                        try:
                            llm_reasoning = reasoning_contents[0] if reasoning_contents else ""
                            parsed_reasoning = self._parse_reasoning_tag(response_content)
                            cot_recorder.record_validation(
                                sql_temp_id=sql_temp_id,
                                checker_name="OrderByLimitChecker",
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
                                f"{log_tag}[CoT][OrderByLimitChecker] validation record failed (non-fatal): {e}"
                            )
                    return parsed_sql_candidate, total_token_usage

                retries += 1
                logger.warning(
                    f"{log_tag}[OrderByLimitChecker] Parse failed, retry {retries}/{max_parse_retries}"
                )

            # 达到重试上限且需要修复，抛出异常
            raise RepairError(
                f"[OrderByLimitChecker] LLM parse max retries ({max_parse_retries}) exceeded, failed to revise SQL"
            )

        return sql, {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }

    def _check_order_by_limit(self, sql: str) -> Optional[str]:
        """检测ORDER BY LIMIT语法错误"""
        # 标识符：点分隔限定名，每段可为反引号/方括号/双引号引用名或裸词
        # （如 T1.`Birthday`、Patient.[First Date]）
        identifier_seg = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|\w+)'
        identifier = rf"{identifier_seg}(?:\.{identifier_seg})*"
        # 聚合参数：标识符，或含至多一层嵌套括号的表达式
        # （如 CAST(a AS REAL)、b.spent * 1.0 / b.amount；二层及以上嵌套不接受）
        agg_arg = rf'(?:{identifier}|[^()"\']+(?:\([^()]*\)[^()]*)?)'
        # .*?\s*\bLIMIT\s+\d+：容忍 LIMIT 前换行/制表符/无空格及 LIMIT 后多空格
        # ORDER BY\s+：容忍 ORDER BY 与聚合函数之间的换行/多空格
        order_by_pattern = re.compile(
            rf"ORDER BY\s+((MIN|MAX)\(\s*({agg_arg})\s*\)).*?\s*\bLIMIT\s+\d+",
            re.IGNORECASE | re.DOTALL,
        )
        res = order_by_pattern.search(sql)
        if res:
            agg_expr = res.groups()[0]  # e.g. MAX(T1.points)
            func_name = res.groups()[1].upper()  # MAX or MIN
            col_name = res.groups()[2]  # e.g. T1.points
            sort_dir = "DESC" if func_name == "MAX" else "ASC"
            # 三分支评估式指令 + Step 2 横切方向检查 + 4 个泛化示例
            # （employees/dept/salary，不绑定特定数据集语义）：
            # (1) 单行极值意图 → 移除 GROUP BY 与聚合，保留原 LIMIT N；
            # (2) 分组聚合意图 → MIN/MAX 替换为 SUM/COUNT；
            # (3) 按分组 MAX/MIN 排序正是题意且方向已匹配 → 保持原样
            #     （假阳性出口，消除 broke 敞口）；
            # Step 2 横切方向检查 → ASC/DESC 与题意矛盾时仅翻转方向
            #     （针对“聚合选对、方向矛盾”形态，如 MIN(time) DESC 求 fastest）
            return (
                f"The SQL uses `ORDER BY {agg_expr} ... LIMIT N`. "
                f"Think step by step before revising:\n"
                f"Step 1: Determine what the question asks to rank:\n"
                f"(a) individual records by the raw value of `{col_name}`;\n"
                f"(b) groups ranked by a per-group aggregate such as a total or count;\n"
                f"(c) groups ranked by the per-group highest/lowest value of `{col_name}`.\n"
                f"Step 2: Check the sort direction against the question's intent: DESC returns\n"
                f"the largest value of the ranking key first, ASC the smallest. If they\n"
                f"contradict, flip only the direction and keep everything else unchanged.\n"
                f"Step 3: Revise according to the identified intent:\n"
                f"(1) If (a), remove any GROUP BY clause and the aggregate function, "
                f"using `ORDER BY {col_name} {sort_dir}` with the original `LIMIT N`.\n"
                f"(2) If (b), keep the GROUP BY clause and replace `{agg_expr}` with "
                f"the appropriate aggregate such as `SUM({col_name})` or `COUNT({col_name})`.\n"
                f"(3) If (c) and the direction already matches the intent (Step 2), "
                f"the SQL is already correct, output it unchanged.\n"
                f"Examples:\n"
                f'- "Which employee has the highest salary?" with '
                f"`SELECT name FROM employees GROUP BY name ORDER BY MAX(salary) DESC LIMIT 1`: "
                f"a single record by raw value (a) -> case (1): "
                f"`SELECT name FROM employees ORDER BY salary DESC LIMIT 1`.\n"
                f'- "Which department has the most employees?" with '
                f"`SELECT dept FROM employees GROUP BY dept ORDER BY MAX(emp_id) DESC LIMIT 1`: "
                f"a per-group count (b) -> case (2): "
                f"`SELECT dept FROM employees GROUP BY dept ORDER BY COUNT(emp_id) DESC LIMIT 1`.\n"
                f'- "Which department has the highest maximum salary?" with '
                f"`SELECT dept, MAX(salary) FROM employees GROUP BY dept ORDER BY MAX(salary) DESC LIMIT 1`: "
                f"the per-group highest value (c), direction matches -> case (3): keep unchanged.\n"
                f"- Same question and SQL as above but sorting ASC: ASC returns the lowest "
                f"maximum first, contradicting the intent (Step 2) -> flip only the direction: "
                f"`SELECT dept, MAX(salary) FROM employees GROUP BY dept "
                f"ORDER BY MAX(salary) DESC LIMIT 1`."
            )
        return None
