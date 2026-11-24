"""From metavisor 6c0dcf4, nl2sql/validator/select_checker_v2.py; host I/O is asynchronous."""

import logging
import re
from typing import Dict, Optional, Tuple

from .base import DEFAULT_REPAIR_ATTEMPTS, BaseChecker, RepairError

logger = logging.getLogger(__name__)


class SelectChecker(BaseChecker):
    """SELECT校验器 - 检测并修复SELECT子句中的问题"""

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
        检查SELECT子句并修复错误

        Raises:
            RepairError: 需要修复但解析失败达到最大重试次数时抛出
        """
        # 保存原始输入 SQL，用于 CoT 记录
        # （input_sql 应始终代表 checker 收到的实际输入，而非 Part 1 替换后的 SQL）
        original_sql = sql
        concat_llm_suggestion = None
        # 先修复字符串连接问题（扩展模式：覆盖 SELECT 子句中所有 || 'xxx' || 拼接变体）
        select_match = re.match(
            r"^(SELECT\s+.*?)(\s+FROM)", sql, re.IGNORECASE | re.DOTALL | re.MULTILINE
        )
        if select_match:
            select_clause = select_match.group(1)
            # 排除 CASE WHEN 内部的 ||（检测时移除 CASE...END 块）
            select_clause_no_case = re.sub(
                r"\bCASE\b\s+.*?\bEND\b", "", select_clause, flags=re.IGNORECASE | re.DOTALL
            )
            # 检测 SELECT 子句中的 || 拼接模式（col || 'separator' || col）
            concat_pattern = r"\b\w+(?:\([^)]*\))?(?:\.[\w`]+)*\s*\|\|\s*'[^']*'\s*\|\|\s*\w+"
            if re.search(concat_pattern, select_clause_no_case, re.IGNORECASE):
                if self._select_has_nested_concat(select_clause):
                    # || 嵌套在函数调用内部（如 COALESCE），正则替换会破坏函数结构，改用 LLM 修复
                    concat_llm_suggestion = (
                        "The SELECT clause uses string concatenation (||) with nested function calls "
                        "(e.g., COALESCE, IFNULL) to merge multiple columns into a single output. "
                        "Please rewrite to select each column individually, separated by commas. "
                        "Remove all || operators and unwrap any COALESCE/IFNULL calls that were "
                        "only used for concatenation formatting."
                    )
                else:
                    # 替换所有 || 'xxx' || 为 ,（仅作用于 SELECT 子句范围）
                    select_end_pos = select_match.end(1)
                    select_text = sql[:select_end_pos]
                    rest_text = sql[select_end_pos:]
                    select_text = re.sub(r"\s*\|\|\s*'[^']*'\s*\|\|\s*", ", ", select_text)
                    sql = select_text + rest_text
        # Part 1 规则替换是否实际修改了 SQL（用于无 LLM 调用时的 CoT 记录）
        part1_rule_fixed = sql != original_sql

        select_suggestion = self._check_select(sql)

        # 合并 concat 函数嵌套和模糊列的 suggestion，统一编号为有序列表
        raw_suggestions = []
        if concat_llm_suggestion:
            raw_suggestions.append(concat_llm_suggestion)
        if select_suggestion:
            # select_suggestion 自带编号（"1. ...\n2. ..."），去除原编号后统一重新编号
            for line in select_suggestion.strip().split("\n"):
                line = re.sub(r"^\d+\.\s*", "", line.strip())
                if line:
                    raw_suggestions.append(line)
        combined_suggestion = None
        if raw_suggestions:
            combined_suggestion = "\n".join(
                f"{idx}. {s}" for idx, s in enumerate(raw_suggestions, 1)
            )

        if combined_suggestion:
            logger.info(f"{log_tag}[SelectChecker] Found select errors, requesting LLM revision")
            logger.debug(f"{log_tag}[SelectChecker] Input SQL: {sql}")

            prompt = data_item.common_prompt(sql, combined_suggestion)
            input_sql_for_cot = original_sql  # CoT 记录原始输入，而非 Part 1 替换后的 SQL
            trigger_reason = combined_suggestion

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
                        f"{log_tag}[SelectChecker] LLM response truncated (finish_reason=length)"
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
                    logger.info(f"{log_tag}[SelectChecker] LLM revision successful")
                    logger.debug(f"{log_tag}[SelectChecker] Revised SQL: {parsed_sql_candidate}")
                    # CoT 记录：每次触发仅一条（解析重试不再重复记录），内容为本次成功响应，token 为累计值
                    # 字段分层：input_sql = Part1 规则修复前的原始输入；rule_* 记录 Part1 规则修复；
                    # llm_input_sql = Part1 修复后的 SQL（LLM 实际输入）；output_sql = checker 最终输出
                    if cot_recorder is not None and sql_temp_id is not None:
                        try:
                            llm_reasoning = reasoning_contents[0] if reasoning_contents else ""
                            parsed_reasoning = self._parse_reasoning_tag(response_content)
                            cot_recorder.record_validation(
                                sql_temp_id=sql_temp_id,
                                checker_name="SelectChecker",
                                trigger_reason=trigger_reason,
                                input_sql=input_sql_for_cot,
                                rule_input_sql=(original_sql if part1_rule_fixed else ""),
                                rule_output_sql=(sql if part1_rule_fixed else ""),
                                llm_input_sql=sql,
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
                                f"{log_tag}[CoT][SelectChecker] validation record failed (non-fatal): {e}"
                            )
                    return parsed_sql_candidate, total_token_usage

                retries += 1
                logger.warning(
                    f"{log_tag}[SelectChecker] Parse failed, retry {retries}/{max_parse_retries}"
                )

            # 达到重试上限且需要修复，抛出异常
            raise RepairError(
                f"[SelectChecker] LLM parse max retries ({max_parse_retries}) exceeded, failed to revise SQL"
            )

        # 仅规则替换修复（无 LLM 调用）时，写入 CoT 记录（LLM 字段为空）
        if part1_rule_fixed and cot_recorder is not None and sql_temp_id is not None:
            try:
                cot_recorder.record_validation_rule(
                    sql_temp_id=sql_temp_id,
                    checker_name="SelectChecker",
                    trigger_reason="rule-based fix: replaced || 'separator' || concat with ', ' in SELECT clause",
                    input_sql=original_sql,
                    rule_input_sql=original_sql,
                    rule_output_sql=sql,
                    output_sql=sql,
                )
            except Exception as e:
                logger.debug(
                    f"{log_tag}[CoT][SelectChecker] rule validation record failed (non-fatal): {e}"
                )
        return sql, {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
        }

    def _check_select(self, sql: str) -> Optional[str]:
        """检测SELECT子句错误"""
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        # 切出 SELECT 子句（与 Part 1 同口径），在其内部捕获全部 别名.* 通配符；
        # 旧实现 findall(r"^SELECT.*? ({identifier}\.\*).*?FROM") 因整体匹配消耗到
        # FROM 且 ^SELECT 锚定行首，单行 SQL 只能捕获第一个通配符
        select_match = re.match(
            r"^(SELECT\s+.*?)(\s+FROM)", sql, re.IGNORECASE | re.DOTALL | re.MULTILINE
        )
        if not select_match:
            return None
        select_clause = select_match.group(1)
        # 剥离字符串字面量，避免字面量内容（如 'a.*'）误触发通配符检测
        select_clause = re.sub(r"'[^']*'", "''", select_clause)
        select_amb = []
        for w in re.findall(rf"{identifier}\.\*", select_clause, re.IGNORECASE):
            if w not in select_amb:
                select_amb.append(w)
        if select_amb:
            # 每个通配符一条问题陈述；全列展开禁令（含负面示例）与枚举场景引导
            # 作为公共条目仅追加一次。注意：check_and_revise 会按行拆分、剥离
            # 编号后统一重排，因此每条 suggestion 必须独占一行。
            lines = []
            for x in select_amb:
                lines.append(
                    f"The wildcard `{x}` returns every column of the table, "
                    "possibly more than the question asks for. Replace it with only "
                    "the specific column(s) requested by the question."
                )
            lines.append(
                "Do NOT expand the wildcard into all columns of the table "
                "— that returns the same result as the wildcard and is still "
                "wrong (e.g., rewriting `SELECT T1.* FROM employees AS T1 ...` "
                "as `SELECT T1.id, T1.name, T1.dept_id, T1.salary ...` is WRONG)."
            )
            lines.append(
                "When the question asks to enumerate entities without naming a "
                "specific attribute, prefer the entity's primary identifier "
                "column (e.g., 'name', 'title', 'id'); always determine the "
                "exact column(s) from the Question, Hint, and provided schema "
                "column infos."
            )
            suggestion = ""
            for idx, line in enumerate(lines, 1):
                suggestion += f"{idx}. {line}\n"
            return suggestion
        return None

    def _select_has_nested_concat(self, select_clause: str) -> bool:
        """检测 SELECT 子句中是否有 || 出现在函数调用的括号内部

        当 || 嵌套在 COALESCE/IFNULL 等函数内部时，正则替换 || 'xxx' || → ', '
        会破坏函数结构，需要改用 LLM 修复。
        """
        clause = re.sub(
            r"\bCASE\b\s+.*?\bEND\b", "", select_clause, flags=re.IGNORECASE | re.DOTALL
        )
        depth = 0
        i = 0
        while i < len(clause):
            ch = clause[i]
            if ch == "'":
                # 跳过单引号字符串（SQLite 中 '' 为转义）
                i += 1
                while i < len(clause):
                    if clause[i] == "'":
                        if i + 1 < len(clause) and clause[i + 1] == "'":
                            i += 2
                            continue
                        break
                    i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "|" and i + 1 < len(clause) and clause[i + 1] == "|":
                if depth > 0:
                    return True
                i += 1
            i += 1
        return False
