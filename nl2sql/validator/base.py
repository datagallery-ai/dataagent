"""SQL 校验器基类 — 定义校验器接口与通用解析/选择方法
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Optional
from collections import Counter
import json
import re
import logging

from . import LLMParseMaxRetriesExceeded
from .. import config
from ..sql_evaluator import run_query
from ..common.log_utils import qp

logger = logging.getLogger(__name__)


class BaseChecker(ABC):
    """SQL 校验器基类"""

    @abstractmethod
    def check_and_revise(self, sql: str, data_item, llm, sampling_budget: int = 1,
                         max_parse_retries: int = None, log_tag: str = "") -> Tuple[str, Dict[str, int]]:
        """
        检查并修订 SQL

        Args:
            sql: 待检查的 SQL
            data_item: SimpleDataItem 数据项
            llm: LLMAdapter 实例
            sampling_budget: 采样次数（保留用于兼容）
            max_parse_retries: LLM 响应解析失败时的最大重试次数
            log_tag: 回路/SQL 级日志标识，如 "[dc/sql_1] "

        Returns:
            (revised_sql, token_usage) 元组
        """
        pass

    @classmethod
    def _extract_revised_sql(cls, response: str, question_id: int = None, log_tag: str = "") -> Optional[str]:
        """从 LLM 的 JSON 响应中提取修订后的 SQL（读取 sql 字段）"""
        payload = cls._load_json_payload(response)
        if payload is None:
            logger.warning(f"{qp(question_id)}{log_tag}No parseable JSON object found in LLM response")
            logger.debug(f"{qp(question_id)}{log_tag}Response content: {response[:500]}")
            return None
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return None
        sql = sql.strip()
        # 容错：去除模型可能附带的代码围栏
        if sql.startswith("```sql") and sql.endswith("```"):
            sql = sql[len("```sql"):-len("```")].strip()
        elif sql.startswith("```") and sql.endswith("```"):
            sql = sql[3:-3].strip()
        return sql

    @staticmethod
    def _load_json_payload(response: str) -> Optional[dict]:
        """从原始响应文本中提取并解析 JSON 对象

        先尝试直接 json.loads；失败时回退到识别 ```json``` 围栏、再截取首个 `{`
        到末个 `}` 的子串后解析，以兼容模型在 JSON 前后附带说明文字的情形。
        无法解析时返回 None。
        """
        if not response:
            return None
        text = response.strip()
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
    def _extract_reasoning(cls, response: str) -> str:
        """从 LLM 的 JSON 响应中读取 reasoning 字段（仅用于 CoT 记录）"""
        if not response:
            return ""
        payload = cls._load_json_payload(response)
        if not payload:
            return ""
        reasoning = payload.get("reasoning", "")
        return reasoning.strip() if isinstance(reasoning, str) else ""

    @staticmethod
    def _vote_best_candidate(candidates: List[str], data_item, accepted_statuses) -> Optional[str]:
        """对一组候选 SQL 执行后按结果做多数投票，选出出现频次最高的有效候选

        Args:
            candidates: 候选 SQL 列表
            data_item: 提供 database_path 的数据项
            accepted_statuses: 视为有效的 ExecStatus 集合

        Returns:
            频次最高的有效候选 SQL；无有效候选时返回 None
        """
        valid: List[Tuple[str, frozenset]] = []
        for candidate in candidates:
            outcome = run_query(data_item.database_path, candidate)
            if outcome.status in accepted_statuses:
                key = frozenset(outcome.rows) if outcome.rows else frozenset()
                valid.append((candidate, key))
        if not valid:
            return None
        counter = Counter(key for _, key in valid)
        return max(valid, key=lambda pair: counter[pair[1]])[0]
