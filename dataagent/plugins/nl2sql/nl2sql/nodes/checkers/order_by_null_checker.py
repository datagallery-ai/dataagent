"""From metavisor 6c0dcf4, nl2sql/validator/order_by_null_checker_v2.py; host I/O is asynchronous."""

import logging
import re
from typing import Dict, Optional, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker, RepairError

logger = logging.getLogger(__name__)

ORDER_BY_NULL_SUGGESTION_V2 = """Please add `IS NOT NULL` condition(s) in the WHERE clause for the sort key
column(s) of the target ORDER BY ... LIMIT segment(s) below.

Target segment(s):
{targets}

Analyze step by step as follows:
1. Locate each target segment in the SQL and identify its sort key column.
   If the sort key is an expression (function / arithmetic / CASE), add the
   condition for its underlying base column(s) — e.g. for `JULIANDAY(dob)`,
   add `dob IS NOT NULL`; the ORDER BY expression itself must stay unchanged.
2. In the WHERE clause at the SAME query level as that ORDER BY, add
   `<column> IS NOT NULL`: create the WHERE clause if none exists, append
   with `AND` otherwise, and skip any column already null-excluded there.
   If the ORDER BY is inside a subquery, modify THAT subquery's WHERE
   clause, not the outer one.
3. If an ORDER BY has multiple sort keys, handle only the first key.

Examples:
- Segment `ORDER BY col ASC LIMIT 1` in
  `SELECT a FROM t ORDER BY col ASC LIMIT 1`
  → `SELECT a FROM t WHERE col IS NOT NULL ORDER BY col ASC LIMIT 1`
- Segment `ORDER BY col ASC LIMIT 1` inside a subquery
  `... WHERE x = (SELECT f(col) FROM t2 ORDER BY col ASC LIMIT 1)`
  → add `WHERE col IS NOT NULL` inside that subquery, not in the outer
    query's WHERE clause."""


class OrderByNullChecker(BaseChecker):
    """ORDER BY NULL校验器 - 检测并修复ORDER BY列可能为NULL的情况"""

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
        """
        检查ORDER BY NULL并修复错误

        Raises:
            RepairError: 需要修复但解析失败达到最大重试次数时抛出
        """
        order_by_null_suggestion = self._check_order_by_null(sql)

        if order_by_null_suggestion:
            logger.info(
                f"{log_tag}[OrderByNullChecker] Found order-by-null errors, requesting LLM revision"
            )
            logger.debug(f"{log_tag}[OrderByNullChecker] Input SQL: {sql}")

            prompt = data_item.common_prompt(sql, order_by_null_suggestion)

            input_sql_for_cot = sql
            trigger_reason = order_by_null_suggestion
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
                        f"{log_tag}[OrderByNullChecker] LLM response truncated (finish_reason=length)"
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
                    logger.info(f"{log_tag}[OrderByNullChecker] LLM revision successful")
                    logger.debug(
                        f"{log_tag}[OrderByNullChecker] Revised SQL: {parsed_sql_candidate}"
                    )
                    # CoT 记录：每次触发仅一条（解析重试不再重复记录），内容为本次成功响应，token 为累计值
                    if cot_recorder is not None and sql_temp_id is not None:
                        try:
                            cot_recorder.record_validation(
                                sql_temp_id=sql_temp_id,
                                checker_name="OrderByNullChecker",
                                trigger_reason=trigger_reason,
                                input_sql=input_sql_for_cot,
                                rule_input_sql="",
                                rule_output_sql="",
                                llm_input_sql=input_sql_for_cot,
                                llm_output_sql=parsed_sql_candidate,
                                input_prompt=prompt,
                                output_full=response_content,
                                llm_reasoning=reasoning_contents[0] if reasoning_contents else "",
                                parsed_reasoning=self._parse_reasoning_tag(response_content),
                                output_sql=parsed_sql_candidate,
                                token_usage=total_token_usage,
                            )
                        except Exception as e:
                            logger.debug(
                                f"{log_tag}[CoT][OrderByNullChecker] validation record failed (non-fatal): {e}"
                            )
                    return parsed_sql_candidate, total_token_usage

                retries += 1
                logger.warning(
                    f"{log_tag}[OrderByNullChecker] Parse failed, retry {retries}/{max_parse_retries}"
                )

            # 达到重试上限且需要修复，抛出异常
            raise RepairError(
                f"[OrderByNullChecker] LLM parse max retries ({max_parse_retries}) exceeded, failed to revise SQL"
            )

        return sql, {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }

    @staticmethod
    def _has_toplevel_groupby(sql: str) -> bool:
        """检测 SQL 顶层查询是否含 GROUP BY（排除子查询中的 GROUP BY）

        通过去除括号内容后检测 GROUP BY，确保只匹配顶层查询的 GROUP BY。
        避免误杀子查询含 GROUP BY 但顶层查询不含的场景（如 q_1011）。
        """
        result = []
        depth = 0
        for char in sql:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0:
                result.append(char)
        return bool(re.search(r"GROUP BY", "".join(result), re.IGNORECASE))

    @staticmethod
    def _is_paren_balanced(segment: str) -> bool:
        """校验匹配段内括号配对（深度永不为负且终值为 0）

        拒绝跨子查询边界的缝合段（如 ORDER BY weight DESC LIMIT 1 ) LIMIT 1），
        保留段内完整括号结构（反引号列名含括号、CASE/函数表达式排序键）。
        """
        depth = 0
        for char in segment:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return False
        return depth == 0

    def _check_order_by_null(self, sql: str) -> Optional[str]:
        """检测ORDER BY NULL错误

        三层筛选：
        1. 顶层 GROUP BY 预排除：分组聚合结果不需要 WHERE IS NOT NULL
        2. ORDER BY...LIMIT 正则匹配（排除 DESC LIMIT）
        3. 聚合函数排除：SUM/COUNT/MIN/MAX/AVG 忽略 NULL
        """
        # 预排除 1：顶层 GROUP BY 场景跳过（排序的是分组聚合结果，
        # WHERE 中 IS NOT NULL 语义错误且可能生成无效 SQL）
        if self._has_toplevel_groupby(sql):
            return None

        # finditer 收集匹配段及其位置（触发判定与原 findall 口径完全一致）
        segments = []  # [(段原文, 是否子查询内)]
        for m in re.finditer(
            r"ORDER BY (?:(?!ORDER BY).)*?(?<!DESC )LIMIT +\d+;{0,1}", sql, re.IGNORECASE
        ):
            seg = m.group(0)
            # 括号配对校验：拒绝跨子查询边界的缝合段（如 ORDER BY weight DESC
            # LIMIT 1 ) LIMIT 1——段内出现未配对右括号），保留段内完整括号结构
            # （反引号列名含括号、表达式排序键）
            if not self._is_paren_balanced(seg):
                continue
            # 位置判定：段起始偏移处括号深度 >0 即在子查询内
            depth = 0
            for ch in sql[: m.start()]:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            segments.append((seg, depth > 0))
        if not segments:
            return None

        for seg, _ in segments:
            # 预排除 2：直接使用聚合函数时跳过（聚合函数本身忽略 NULL）
            if re.findall(r"SUM\(|COUNT\(|MIN\(|MAX\(|AVG\(", seg, re.IGNORECASE):
                return None

        # 段去重（仅 suggestion 生成层）：按（段原文, 位置）分组计数，
        # q_884 型同文本多段合并为一条并标注出现次数
        groups = []  # [[段原文, 是否子查询内, 计数]]
        for seg, in_subquery in segments:
            for g in groups:
                if g[0] == seg and g[1] == in_subquery:
                    g[2] += 1
                    break
            else:
                groups.append([seg, in_subquery, 1])

        target_lines = []
        for idx, (seg, in_subquery, count) in enumerate(groups, 1):
            position = "inside subquery" if in_subquery else "outer query"
            if count > 1:
                noun = "subqueries" if in_subquery else "locations"
                position = f"{position} (appears in {count} {noun}; apply to each)"
            target_lines.append(f"{idx}. `{seg}` — {position}")

        return ORDER_BY_NULL_SUGGESTION_V2.format(targets="\n".join(target_lines))
