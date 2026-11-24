"""From metavisor 6c0dcf4, nl2sql/validator/max_min_checker_v2.py; host I/O is asynchronous."""

import logging
import re
from typing import Dict, List, Optional, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker, RepairError

logger = logging.getLogger(__name__)


class MaxMinChecker(BaseChecker):
    """MAX/MIN校验器 - 检测并修复MAX/MIN使用错误（旧版基线 + P4前置过滤 + ④-11 lookbehind 修正）

    核心设计：
    - 简洁 suggestion：仅提供改写方向指令，不含结构化规则/执行结果引导
    - 不注入执行结果：LLM 仅根据改写方向做出决策，无需执行结果辅助
    - P4 前置过滤：仅对理论最简模式（nesting<=2）执行 SQL 并判断行数，
      结果为 1 行时跳过改写，保留原 SQL
    """

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
        检查MAX/MIN使用并修复错误

        Raises:
            RepairError: 需要修复但解析失败达到最大重试次数时抛出
        """
        max_min_suggestion = self._check_max_min(sql)

        if max_min_suggestion:
            logger.info(f"{log_tag}[MaxMinChecker] Found max-min errors, requesting LLM revision")
            logger.debug(f"{log_tag}[MaxMinChecker] Input SQL: {sql}")

            # ── P4: 单行结果跳过改写（仅限理论最简模式 nesting<=2） ──
            # 理论依据：checker 触发的最简结构 = 外层SELECT + 子查询SELECT = 2个SELECT
            # nesting=2 时 rc=1 → 极值筛选已正确（两步推导无干扰因素），改写无意义
            # nesting>=3 时存在额外结构因素（嵌套计算、多子查询、多层嵌套），
            #   rc=1 不能反推极值筛选正确性，P4 假设不成立，应交给 LLM 判断
            #
            # 数据验证（BIRD dev, GLM-5.2 no-think）：
            #   nesting=2, rc=1: 349 条，旧 checker 0 Fixed / 21 Broke → P4 保留
            #   nesting>=3, rc=1: 30 条，旧 checker 5 Fixed / 2 Broke → P4 豁免
            #
            # 实现：仅对 nesting<=2 执行 SQL（P4 判断需要），
            #       nesting>=3 不执行 SQL 直接进入 LLM 改写流程
            nesting_depth = sql.upper().count("SELECT")
            if nesting_depth <= 2:
                execution_result = await data_item.execute(sql)
                if (
                    execution_result.result_rows is not None
                    and len(execution_result.result_rows) == 1
                ):
                    logger.info(
                        f"{log_tag}[MaxMinChecker] Single row result, "
                        f"skip rewrite (P4, nesting={nesting_depth})"
                    )
                    return sql, {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "content_tokens": 0,
                    }

            prompt = data_item.common_prompt(sql, max_min_suggestion)
            input_sql_for_cot = sql
            trigger_reason = max_min_suggestion

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
                        f"{log_tag}[MaxMinChecker] LLM response truncated (finish_reason=length)"
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
                    logger.info(f"{log_tag}[MaxMinChecker] LLM revision successful")
                    logger.debug(f"{log_tag}[MaxMinChecker] Revised SQL: {parsed_sql_candidate}")
                    # CoT 记录：每次触发仅一条（解析重试不再重复记录），内容为本次成功响应，token 为累计值
                    if cot_recorder is not None and sql_temp_id is not None:
                        try:
                            llm_reasoning = reasoning_contents[0] if reasoning_contents else ""
                            parsed_reasoning = self._parse_reasoning_tag(response_content)
                            cot_recorder.record_validation(
                                sql_temp_id=sql_temp_id,
                                checker_name="MaxMinChecker",
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
                                f"{log_tag}[CoT][MaxMinChecker] validation record failed (non-fatal): {e}"
                            )
                    return parsed_sql_candidate, total_token_usage

                retries += 1
                logger.warning(
                    f"{log_tag}[MaxMinChecker] Parse failed, retry {retries}/{max_parse_retries}"
                )

            # 达到重试上限且需要修复，抛出异常
            raise RepairError(
                f"[MaxMinChecker] LLM parse max retries ({max_parse_retries}) exceeded, failed to revise SQL"
            )

        return sql, {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }

    def _check_max_min(self, sql: str) -> Optional[str]:
        """检测MAX/MIN使用错误"""
        # P1: 排除含 COUNT/SUM/AVG/TOTAL 的查询
        # 聚合函数返回单行，ORDER BY+LIMIT 对其无意义但会移除 WHERE 过滤（q_0786: COUNT 63→623）
        #
        # 案例 q_0786 (superhero):
        #   原始SQL: SELECT COUNT(hero_id) FROM hero_attribute JOIN attribute ...
        #            WHERE attribute_name='Strength'
        #              AND attribute_value = (SELECT MAX(attribute_value) FROM hero_attribute)
        #   Golden:  同上，COUNT = 63（仅 maxValue=100 的英雄）
        #   旧版A规则触发 → LLM 移除 WHERE 子查询, 改为:
        #            SELECT COUNT(hero_id) ... WHERE attribute_name='Strength'
        #            ORDER BY attribute_value DESC LIMIT 1
        #   结果: COUNT = 623（全部 Strength 英雄） ❌
        #   新版: 检测到 COUNT( → 直接跳过，SQL 原样保留 ✅
        if re.search(r"\b(COUNT|SUM|AVG|TOTAL)\s*\(", sql, re.IGNORECASE):
            return None

        # P3: SELECT 子句中含 MIN/MAX 时跳过 Rule A
        # 当 SELECT 中已有 MIN/MAX 聚合时，WHERE 中的 =(SELECT MIN/MAX) 用于过滤行范围，
        # 将 WHERE 替换为 ORDER BY+LIMIT 会移除过滤条件，改变 SELECT 中 MIN/MAX 的聚合范围
        #
        # 案例 q_0670 (codebase_community):
        #   原始SQL: SELECT MIN(T1.CreaionDate) FROM posts AS T1 JOIN users AS T2
        #            ON T1.OwnerUserId = T2.Id
        #            WHERE T2.Age = (SELECT MIN(Age) FROM users)
        #   语义: 在"最年轻用户(Age=MIN(Age))"的帖子中找最早发帖日期(MIN(CreaionDate))
        #         两层 MIN 作用不同：WHERE 的 MIN 过滤行范围，SELECT 的 MIN 聚合结果
        #   旧版 Rule A 触发 → LLM 移除 WHERE 子查询 + SELECT MIN, 改为:
        #            SELECT T1.CreaionDate ... ORDER BY T2.Age ASC LIMIT 1
        #   结果: 返回全局最早的帖子而非最年轻用户的最早帖子 ❌
        #   新版: 检测到 SELECT 中有 MIN( → 跳过 Rule A, SQL 原样保留 ✅
        #
        # 消融验证（142259 日志，移除 P3）: 36 条此特征 SQL 被触发，
        # 0 fixed / 4 broke（q_0670×4，改前执行=golden）→ 净收益 +4、零损失
        select_part = re.split(r"\bFROM\b", sql, 1, re.IGNORECASE)[0]
        if re.search(r"\b(MIN|MAX)\s*\(", select_part, re.IGNORECASE):
            return None

        # ④-11 就地修复：`=` 前补 negative lookbehind (?<![!<>=])，排除
        # `<=`/`!=`/`>=`/`==` 误吃（q_0846：`q1 <= (SELECT MAX(q1))` 被误判
        # 重写，LIMIT 5→1 越界）；单独 `<`/`>` 无 `=` 锚点，天然不误匹配。
        # round2 十批次 + old 4 run 回归扫描零差异，对既有触发行为无影响
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        max_min_pattern = re.compile(
            rf"(?<![!<>=])=\s*\(\s*SELECT\s*(MAX|MIN)\s*\(\s*({identifier})\s*\)\s*FROM\s*({identifier})",
            re.IGNORECASE | re.DOTALL,
        )
        fun_amb = max_min_pattern.findall(sql)
        order_amb = set(re.findall(r"= (\(SELECT .* LIMIT \d\))", sql, re.IGNORECASE | re.DOTALL))

        suggestions: List[str] = []

        for fun in fun_amb:
            fuc, col, table = fun
            # P2: 修复大小写敏感 bug
            # regex 使用 re.IGNORECASE 匹配 Max/Min/MAX/MIN，但捕获组保留原始大小写
            # fuc == "MAX" 对 "Max" 返回 False → 错误使用 ASC（应为 DESC）
            #
            # 案例 q_0990 (formula_1):
            #   原始SQL: ... WHERE time = (SELECT Max(time) FROM ...)
            #   旧版: fuc="Max", fuc=="MAX" 为 False → order="ASC"
            #         suggestion 提示 ASC（错误方向，应为 DESC）❌
            #   修正: fuc.upper()=="MAX" 为 True → order="DESC" ✅
            order = "DESC" if fuc.upper() == "MAX" else "ASC"
            suggestion = f"Please use ORDER BY {table}.{col} {order} LIMIT 1 instead of nested SQL"
            suggestions.append(suggestion)

        for fun in order_amb:
            suggestions.append(f"{fun}: Please use JOIN instead of nested SQL")

        if len(suggestions) > 0:
            return "\n".join(
                [f"{idx + 1}. {suggestion}" for idx, suggestion in enumerate(suggestions)]
            )
        return None
