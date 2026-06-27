"""SQL 生成器基类 — 定义生成器接口和通用方法

公共生成循环 _generate_with_retry() 提供 "LLM 调用 → 解析 → 重试" 模板方法，
子类只需实现 _build_prompt() 组装 Prompt 即可。
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Optional
import re
import json
import logging

from .. import config
from ..client import LLMParseMaxRetriesExceeded
from ..common.log_utils import qp

logger = logging.getLogger(__name__)


class BaseSQLGenerator(ABC):
    """SQL 生成器基类"""

    @abstractmethod
    def generate(self, data_item, llm, sampling_budget: int = 1,
                 max_parse_retries: int = None) -> Tuple[List[str], Dict[str, int]]:
        """
        生成 SQL

        Args:
            data_item: SimpleDataItem 数据项
            llm: LLMAdapter 实例
            sampling_budget: 采样次数（需要收集的有效 SQL 数量）
            max_parse_retries: LLM 响应解析失败时的最大重试次数

        Returns:
            (sql_candidates, token_usage) 元组
            token_usage: {"input_tokens": N, "output_tokens": N}
        """
        pass

    def _generate_with_retry(
        self,
        prompt: str,
        llm,
        sampling_budget: int,
        max_parse_retries: int,
        generator_name: str,
        question_id: int = None,
        cot_recorder=None,
        cot_stage: str = None,
        temp_id_prefix: str = "",
    ) -> Tuple[List[str], Dict[str, int], List[str]]:
        """公共生成循环 — LLM 调用 + JSON 解析 + 重试

        DC/Skeleton/ICL 三个子类的生成循环完全相同，仅 Prompt 不同。

        Args:
            prompt: 已格式化的 Prompt 字符串
            llm: LLMAdapter 实例
            sampling_budget: 需要收集的有效 SQL 数量
            max_parse_retries: 解析失败最大重试轮数
            generator_name: 日志前缀（如 "DivideConquerGenerator"）
            cot_recorder: 可选 CoTRecorder 实例（None 时不记录）
            cot_stage: CoT 阶段名（如 "dc"/"skeleton"/"icl"），未提供时回退到 generator_name
            temp_id_prefix: sql_temp_id 命名空间前缀（如 "p1"/"p2"），用于跨 phase 区分；
                            为空字符串时退化为旧行为（兼容历史调用）

        Returns:
            (sql_candidates, token_usage, sql_temp_ids) 三元组
            sql_temp_ids 与 sql_candidates 一一对应等长，用于后续 validate 阶段关联

        Raises:
            LLMParseMaxRetriesExceeded: 解析失败达到上限且无有效候选
        """
        if sampling_budget == 0:
            return [], {"input_tokens": 0, "output_tokens": 0}, []

        if max_parse_retries is None:
            max_parse_retries = config.LLM_PARSE_FAIL_MAX_RETRIES

        total_token_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
        all_sql_candidates: List[str] = []
        all_sql_temp_ids: List[str] = []
        parse_retries = 0
        retry_round = 0  # 用于构造 sql_temp_id 的轮次序号

        # CoT 阶段名（默认用 generator_name 小写）
        stage_for_cot = cot_stage or (generator_name or "").lower() or "generation"

        while len(all_sql_candidates) < sampling_budget and parse_retries < max_parse_retries:
            responses, token_usage, reasoning_contents = llm.ask(
                [{"role": "user", "content": prompt}],
                n=sampling_budget - len(all_sql_candidates),
                temperature=config.LLM_TEMPERATURE_GENERATION,
            )

            parsed_any = False
            for i, response in enumerate(responses):
                if getattr(response, 'finish_reason', None) == "length":
                    logger.warning(f"{qp(question_id)}[{generator_name}] LLM response truncated (finish_reason=length, max_tokens={config.LLM_MAX_TOKENS})")
                response_content = response.content.strip()
                parsed_sql_candidate = None
                parse_error = None
                try:
                    parsed_sql_candidate = self._parse_llm_response(response_content, question_id=question_id, generator_name=generator_name)
                    if parsed_sql_candidate:
                        sql_temp_id = (
                            f"{temp_id_prefix}_{stage_for_cot}_{retry_round}_{i}"
                            if temp_id_prefix
                            else f"{stage_for_cot}_{retry_round}_{i}"
                        )
                        all_sql_candidates.append(parsed_sql_candidate)
                        all_sql_temp_ids.append(sql_temp_id)
                        parsed_any = True
                except Exception as e:
                    parse_error = e
                    logger.error(f"{qp(question_id)}Error parsing LLM response: {e}")
                    logger.debug(f"{qp(question_id)}Response content: {response_content[:500]}")

                # CoT 记录：无论是否解析成功，本次 LLM 调用都记录一次
                if cot_recorder is not None:
                    try:
                        llm_reasoning = reasoning_contents[i] if i < len(reasoning_contents) else ""
                        # 优先用 _last_reasoning_content（OpenRouter 思考字段）；
                        # parsed_reasoning 从响应文本中提取 <reasoning> 标签
                        parsed_reasoning = self._parse_reasoning_tag(response_content)
                        # 仅成功解析时才有 sql_temp_id 关联；解析失败的调用归属一个本轮独立的临时 id
                        sql_temp_id_for_call = (
                            all_sql_temp_ids[-1]
                            if parsed_sql_candidate and not parse_error
                            else (
                                f"{temp_id_prefix}_{stage_for_cot}_{retry_round}_{i}__failed"
                                if temp_id_prefix
                                else f"{stage_for_cot}_{retry_round}_{i}__failed"
                            )
                        )
                        cot_recorder.record_generation(
                            sql_temp_id=sql_temp_id_for_call,
                            stage=stage_for_cot,
                            input_prompt=prompt,
                            output_full=response_content,
                            llm_reasoning=llm_reasoning,
                            parsed_reasoning=parsed_reasoning,
                            parsed_result_sql=parsed_sql_candidate,
                            token_usage=token_usage,
                        )
                    except Exception as e:
                        logger.debug(f"{qp(question_id)}[CoT][{stage_for_cot or generator_name}] generation record failed (non-fatal): {e}")

                if parse_error is not None:
                    continue

            # 如果本轮没有解析成功任何响应，计入重试次数
            if not parsed_any:
                parse_retries += 1
                logger.warning(f"{qp(question_id)}[{generator_name}] Parse failed, retry {parse_retries}/{max_parse_retries}")

            total_token_usage["input_tokens"] += token_usage.get("input_tokens", 0)
            total_token_usage["output_tokens"] += token_usage.get("output_tokens", 0)
            total_token_usage["reasoning_tokens"] += token_usage.get("reasoning_tokens", 0)
            total_token_usage["content_tokens"] += token_usage.get("content_tokens", 0)

            retry_round += 1

        # 如果没有收集到任何有效候选且达到重试上限，抛出异常
        if len(all_sql_candidates) == 0 and parse_retries >= max_parse_retries:
            raise LLMParseMaxRetriesExceeded(
                f"[{generator_name}] LLM parse max retries ({max_parse_retries}) exceeded, no valid SQL generated"
            )

        if len(all_sql_candidates) < sampling_budget:
            logger.warning(f"{qp(question_id)}[{generator_name}] Only got {len(all_sql_candidates)}/{sampling_budget} candidates after {parse_retries} retries")

        return all_sql_candidates, total_token_usage, all_sql_temp_ids

    def _parse_llm_response(self, response: str, question_id: int = None, generator_name: str = "") -> Optional[str]:
        """解析 LLM 响应，从 JSON 对象中提取 sql 字段

        模型按约定返回单个 JSON 对象 {"reasoning": ..., "sql": ...}。
        本方法定位响应中的 JSON 主体并 json.loads，取出 sql 字段；
        与 XML 标签解析彻底脱钩。
        """
        payload = self._load_json_payload(response)
        if payload is None:
            logger.warning(f"{qp(question_id)}[{generator_name}] No parseable JSON object found in LLM response")
            logger.debug(f"{qp(question_id)}Response content: {response[:500]}")
            return None

        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            logger.warning(f"{qp(question_id)}[{generator_name}] JSON payload missing non-empty 'sql' field")
            return None

        sql = sql.strip()
        # 容错：去除模型可能附带的 ```sql``` 包裹
        if sql.startswith("```sql") and sql.endswith("```"):
            sql = sql[len("```sql"):-len("```")].strip()
        elif sql.startswith("```") and sql.endswith("```"):
            sql = sql[3:-3].strip()
        return sql

    @staticmethod
    def _load_json_payload(response: str) -> Optional[dict]:
        """从原始响应文本中提取并解析 JSON 对象

        先尝试直接 json.loads；失败时回退到截取首个 `{` 到末个 `}` 的子串再解析，
        以兼容模型在 JSON 前后附带说明文字的情形。无法解析时返回 None。
        """
        if not response:
            return None
        text = response.strip()
        # 去除 ```json ... ``` 围栏
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        candidates = [text]
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            candidates.append(text[brace_start:brace_end + 1])
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except (ValueError, json.JSONDecodeError):
                continue
        return None

    @classmethod
    def _parse_reasoning_tag(cls, response: str) -> str:
        """从 LLM 响应的 JSON 对象中提取 reasoning 字段

        与 `_parse_llm_response` 解耦：仅用于 CoT 输出，不影响主流程的 SQL 解析。
        若无法解析或缺失字段返回空字符串。
        """
        if not response:
            return ""
        payload = cls._load_json_payload(response)
        if not payload:
            return ""
        reasoning = payload.get("reasoning", "")
        return reasoning.strip() if isinstance(reasoning, str) else ""

