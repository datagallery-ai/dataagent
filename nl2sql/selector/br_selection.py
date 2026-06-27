"""SQL 选择运行器 — 置信度感知的一次性 Top-K 裁决

功能说明：
1. 执行 SQL 候选并按结果聚类，计算一致性得分（consistency）
2. Top-1 一致性得分达阈值时直接选择（shortcut 策略）
3. 否则把完整 Top-K 候选一次性交给 LLM 裁决（full_review 策略）
"""
import json
import logging
import re
from typing import Dict, List, Any, Optional, Tuple
from collections import Counter

from .. import config
from ..client import LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded
from ..common.log_utils import qp
from ..sql_evaluator import run_query, ExecStatus
from tabulate import tabulate
from .prompts import format_topk_selection_prompt

logger = logging.getLogger(__name__)


class BRSelectionRunner:
    """置信度感知 SQL 选择器 — 一次性 Top-K 裁决"""

    def __init__(
        self,
        llm=None,
        filter_top_k_sql: int = None,
        evaluator_sampling_budget: int = None,
        shortcut_consistency_score_threshold: float = None,
        max_workers: int = None,
        max_parse_retries: int = None,
    ):
        """
        初始化 SQL 选择器

        Args:
            llm: LLMAdapter 实例
            filter_top_k_sql: Top-K候选数（默认从 config.SELECTOR_FILTER_TOP_K 读取）
            evaluator_sampling_budget: 采样次数（默认从 config.SELECTOR_EVALUATOR_BUDGET 读取）
            shortcut_consistency_score_threshold: 快捷路径阈值（默认从 config.SELECTOR_SHORTCUT_THRESHOLD 读取）
            max_workers: 保留参数（一次性裁决不再并行成对比较，默认从 config.SELECTOR_MAX_WORKERS 读取）
            max_parse_retries: LLM响应解析失败时的最大重试次数（默认从 config.LLM_PARSE_FAIL_MAX_RETRIES 读取）
        """
        self._llm = llm
        self._filter_top_k_sql = filter_top_k_sql if filter_top_k_sql is not None else config.SELECTOR_FILTER_TOP_K
        self._evaluator_sampling_budget = evaluator_sampling_budget if evaluator_sampling_budget is not None else config.SELECTOR_EVALUATOR_BUDGET
        self._shortcut_consistency_score_threshold = shortcut_consistency_score_threshold if shortcut_consistency_score_threshold is not None else config.SELECTOR_SHORTCUT_THRESHOLD
        self._max_workers = max_workers if max_workers is not None else config.SELECTOR_MAX_WORKERS
        self._max_parse_retries = max_parse_retries if max_parse_retries is not None else config.LLM_PARSE_FAIL_MAX_RETRIES

    # ------------------------------------------------------------------
    # JSON 解析
    # ------------------------------------------------------------------
    @staticmethod
    def _load_json_payload(response: str) -> Optional[dict]:
        """从原始响应文本中提取并解析 JSON 对象，无法解析时返回 None"""
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
    def _parse_selection_choice(cls, response: str, n_candidates: int, question_id: int = None) -> Optional[int]:
        """从 LLM 的 JSON 响应中解析所选候选序号，越界或缺失时返回 None"""
        payload = cls._load_json_payload(response)
        if payload is None:
            logger.warning(f"{qp(question_id)}No parseable JSON object found in selection response")
            return None
        choice = payload.get("choice")
        try:
            idx = int(choice)
        except (TypeError, ValueError):
            logger.warning(f"{qp(question_id)}Selection 'choice' is not an integer: {choice!r}")
            return None
        if idx < 0 or idx >= n_candidates:
            logger.warning(f"{qp(question_id)}Selection 'choice' out of range: {idx} (n={n_candidates})")
            return None
        return idx

    @classmethod
    def _extract_reasoning(cls, response: str) -> str:
        """从 LLM 的 JSON 响应中读取 reasoning 字段（仅用于 CoT 记录）"""
        payload = cls._load_json_payload(response)
        if not payload:
            return ""
        reasoning = payload.get("reasoning", "")
        return reasoning.strip() if isinstance(reasoning, str) else ""

    # ------------------------------------------------------------------
    # 执行结果预览：前 5 行 + 末行（中间用省略号连接）
    # ------------------------------------------------------------------
    @staticmethod
    def _truncate_cell(val: Any) -> Any:
        """超长字符串单元格截断，避免预览过宽"""
        if isinstance(val, str) and len(val) > 100:
            return f"'{val[:100]}...'"
        return val

    @classmethod
    def _format_result_preview(cls, execution_result) -> str:
        """构造执行结果预览：行数 ≤ 6 全显，否则取前 5 行 + 省略号 + 末行"""
        rows = execution_result.rows
        cols = execution_result.columns
        if not rows or not cols:
            return execution_result.preview or "(empty or non-tabular result)"
        total = len(rows)
        table_rows: List[List[Any]] = []
        if total <= 6:
            for row in rows:
                table_rows.append([cls._truncate_cell(v) for v in row])
        else:
            for row in rows[:5]:
                table_rows.append([cls._truncate_cell(v) for v in row])
            table_rows.append(["..."] * len(cols))
            table_rows.append([cls._truncate_cell(v) for v in rows[-1]])
        body = tabulate(tabular_data=table_rows, headers=cols, tablefmt="psql")
        return f"{body}\n(total {total} row(s))"

    def _get_top_k_sql_candidates(
        self,
        sql_candidates: List[str],
        database_path: str,
        question_id: int = None,
    ) -> List[Tuple[str, str, float, float]]:
        """
        获取Top-K SQL候选

        执行每个SQL，按结果聚类并计算一致性得分，返回按得分排序的去重候选列表。

        TopK选择逻辑：
        1. 按confidence score降序排序
        2. 默认取top-K（由filter_top_k_sql配置）
        3. 如果top-K之后还存在与top-1相同confidence score的候选，也一并纳入

        Returns:
            List of (sql, result_preview_str, consistency_score, execution_time)
        """
        valid_sql_candidates = []
        sql_map_to_result_preview = {}
        for sql_candidate in sql_candidates:
            execution_result = run_query(database_path, sql_candidate)
            if execution_result.rows is not None and len(execution_result.rows) > 0:
                result_key = frozenset(
                    tuple(row) if isinstance(row, (list, tuple)) else (row,)
                    for row in execution_result.rows
                )
                valid_sql_candidates.append((sql_candidate, result_key))
                sql_map_to_result_preview[sql_candidate] = self._format_result_preview(execution_result)

        if len(valid_sql_candidates) == 0:
            logger.warning(f"{qp(question_id)}No successful SQL candidates, backing to SQL candidates with not none result_rows")
            for sql_candidate in sql_candidates:
                execution_result = run_query(database_path, sql_candidate)
                if execution_result.rows is not None:
                    result_key = frozenset(
                        tuple(row) if isinstance(row, (list, tuple)) else (row,)
                        for row in execution_result.rows
                    )
                    valid_sql_candidates.append((sql_candidate, result_key))
                    sql_map_to_result_preview[sql_candidate] = self._format_result_preview(execution_result)

        if len(valid_sql_candidates) == 0:
            return []

        counter = Counter(execution_result for _, execution_result in valid_sql_candidates)

        deduplicated_valid_sql_candidates = []
        seen_result_set = set()
        for sql_candidate, execution_result in valid_sql_candidates:
            if execution_result not in seen_result_set:
                execution_time = 0.0
                deduplicated_valid_sql_candidates.append((
                    sql_candidate,
                    sql_map_to_result_preview[sql_candidate],
                    counter[execution_result] / len(valid_sql_candidates),
                    execution_time,
                ))
                seen_result_set.add(execution_result)
        valid_sql_candidates = deduplicated_valid_sql_candidates

        # 按(confidence_score, -execution_time)降序排序
        sorted_candidates = sorted(valid_sql_candidates, key=lambda x: (x[2], -x[3]), reverse=True)

        if len(sorted_candidates) == 0:
            return []

        # TopK选择逻辑：取top-K，但如果top-K之后还有与top-1相同confidence的候选，也纳入
        # 扩展上限：最多2倍topK
        top1_confidence = sorted_candidates[0][2]
        max_expansion = self._filter_top_k_sql * 2  # 最多扩展到2倍topK

        # 先取基础的top-K
        top_k_sql_candidates = sorted_candidates[:self._filter_top_k_sql]

        # 检查是否需要扩展：如果top-K后面还有与top-1相同confidence的候选
        for candidate in sorted_candidates[self._filter_top_k_sql:]:
            if len(top_k_sql_candidates) >= max_expansion:
                break  # 达到扩展上限
            if abs(candidate[2] - top1_confidence) < 1e-9:  # 浮点数相等判断
                top_k_sql_candidates.append(candidate)
            else:
                # 由于已排序，后面的confidence只会更小，无需继续检查
                break

        # 日志：如果发生了扩展
        if len(top_k_sql_candidates) > self._filter_top_k_sql:
            logger.info(f"{qp(question_id)}[topK expansion] Extended from {self._filter_top_k_sql} to {len(top_k_sql_candidates)} candidates (top1_conf={top1_confidence:.3f}, max={max_expansion})")

        return top_k_sql_candidates

    def _build_candidates_block(self, top_k_sql_candidates: List[Tuple[str, str, float, float]]) -> str:
        """拼装注入 prompt 的候选清单（序号 + consistency + SQL + 执行结果预览）"""
        parts = []
        for idx, (sql, result_preview, conf, _) in enumerate(top_k_sql_candidates):
            parts.append(
                f"## Candidate {idx} (consistency={conf:.3f})\n"
                f"SQL:\n{sql}\n\n"
                f"Execution result preview:\n{result_preview}\n"
            )
        return "\n".join(parts)

    def _select_among_candidates(
        self,
        top_k_sql_candidates: List[Tuple[str, str, float, float]],
        data_item,
        cot_recorder=None,
    ) -> Tuple[int, Dict[str, int]]:
        """一次性把全部 Top-K 候选交给 LLM 裁决

        采样 evaluator_sampling_budget 次同一 prompt，对返回的 choice 做多数投票。

        Returns:
            (best_idx, token_usage) 元组

        Raises:
            LLMParseMaxRetriesExceeded: 解析失败达到最大重试次数时抛出
        """
        qid = getattr(data_item, 'question_id', None) if data_item else None
        log_prefix = f"{qp(qid)}[Selector full_review]"
        n_candidates = len(top_k_sql_candidates)

        candidates_block = self._build_candidates_block(top_k_sql_candidates)
        prompt = format_topk_selection_prompt(
            database_schema=data_item.database_schema_after_schema_linking,
            question=data_item.question,
            hint=data_item.evidence,
            candidates_block=candidates_block,
        )

        total_token_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
        votes: List[int] = []
        parse_retries = 0

        while len(votes) < self._evaluator_sampling_budget and parse_retries < self._max_parse_retries:
            try:
                responses, token_usage, reasoning_contents = self._llm.ask(
                    [{"role": "user", "content": prompt}],
                    n=self._evaluator_sampling_budget - len(votes),
                    temperature=config.LLM_TEMPERATURE_SELECTION,
                )
                total_token_usage["input_tokens"] += token_usage.get("input_tokens", 0)
                total_token_usage["output_tokens"] += token_usage.get("output_tokens", 0)
                total_token_usage["reasoning_tokens"] += token_usage.get("reasoning_tokens", 0)
                total_token_usage["content_tokens"] += token_usage.get("content_tokens", 0)

                parsed_any = False
                for resp_idx, response in enumerate(responses):
                    if getattr(response, 'finish_reason', None) == "length":
                        logger.warning(f"{log_prefix} LLM response truncated (finish_reason=length, max_tokens={config.LLM_MAX_TOKENS})")
                    choice = self._parse_selection_choice(response.content, n_candidates, question_id=qid)

                    # CoT 记录
                    if cot_recorder is not None:
                        try:
                            reasoning_text = reasoning_contents[resp_idx] if resp_idx < len(reasoning_contents) else ""
                            cot_recorder.record_selection(
                                input_prompt=prompt,
                                output_full=response.content or "",
                                llm_reasoning=reasoning_text,
                                parsed_reasoning=self._extract_reasoning(response.content or ""),
                                token_usage=token_usage,
                            )
                        except Exception as e:
                            logger.debug(f"{log_prefix}[CoT] selection record failed (non-fatal): {e}")

                    if choice is not None:
                        votes.append(choice)
                        parsed_any = True

                if not parsed_any:
                    parse_retries += 1
                    logger.warning(f"{log_prefix} Parse failed, retry {parse_retries}/{self._max_parse_retries}")
            except (LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded):
                raise
            except Exception as e:
                logger.error(f"{log_prefix} Error in LLM selection: {e}")
                parse_retries += 1

        # 达到重试上限且没有有效裁决，抛出异常
        if not votes:
            raise LLMParseMaxRetriesExceeded(
                f"{log_prefix} LLM parse max retries ({self._max_parse_retries}) exceeded, failed to get a valid choice"
            )

        # 多数投票：票数最高的候选序号（Counter.most_common 对同票取首个出现者）
        best_idx = Counter(votes).most_common(1)[0][0]
        logger.info(f"{log_prefix} votes={votes} -> candidate {best_idx}")
        return best_idx, total_token_usage

    def select_best_sql(
        self,
        sql_candidates: List[str],
        db_path: str,
        data_item=None,
        cot_recorder=None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        选择最佳SQL

        Args:
            sql_candidates: SQL候选列表
            db_path: 数据库路径
            data_item: 数据项（LLM裁决时需要）

        Returns:
            (selected_sql, selection_info) 元组
            selection_info 包含 strategy, confidence, token_usage 等字段
        """
        zero_token = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
        qid = getattr(data_item, 'question_id', None) if data_item else None

        if not sql_candidates:
            return "", {"strategy": "empty", "confidence": 0.0, "token_usage": zero_token}

        top_k_sql_candidates = self._get_top_k_sql_candidates(sql_candidates, db_path, question_id=qid)

        if len(top_k_sql_candidates) == 0:
            logger.warning(f"{qp(qid)}No valid SQL candidates, backing to top-1 SQL")
            return sql_candidates[0], {
                "strategy": "fallback",
                "confidence": 0.0,
                "top_k_candidates": [],
                "token_usage": zero_token,
            }

        if len(top_k_sql_candidates) == 1:
            logger.info(f"{qp(qid)}Only one valid SQL candidate, directly select it")
            return top_k_sql_candidates[0][0], {
                "strategy": "single",
                "confidence": top_k_sql_candidates[0][2],
                "top_k_candidates": self._format_top_k_info(top_k_sql_candidates),
                "token_usage": zero_token,
            }

        # shortcut 策略：Top-1一致性得分足够高时直接选择
        if top_k_sql_candidates[0][2] >= self._shortcut_consistency_score_threshold:
            logger.info(f"{qp(qid)}Top-1 SQL candidate has a large consistency score: {top_k_sql_candidates[0][2]}, directly select it")
            return top_k_sql_candidates[0][0], {
                "strategy": "shortcut",
                "confidence": top_k_sql_candidates[0][2],
                "top_k_candidates": self._format_top_k_info(top_k_sql_candidates),
                "token_usage": zero_token,
            }

        # 无LLM时降级为直接选Top-1
        if self._llm is None or data_item is None:
            logger.info(f"{qp(qid)}No LLM available, fallback to top-1 (confidence: {top_k_sql_candidates[0][2]:.2f})")
            return top_k_sql_candidates[0][0], {
                "strategy": "fallback_no_llm",
                "confidence": top_k_sql_candidates[0][2],
                "top_k_candidates": self._format_top_k_info(top_k_sql_candidates),
                "token_usage": zero_token,
            }

        # full_review: 一次性把完整 Top-K 候选交给 LLM 裁决
        logger.info(f"{qp(qid)}[full_review] One-shot Top-K selection over {len(top_k_sql_candidates)} candidates")
        for i, (sql, _, conf, _) in enumerate(top_k_sql_candidates):
            logger.info(f"{qp(qid)}  Candidate {i}: conf={conf:.2f}")
            logger.debug(f"{qp(qid)}  Candidate {i} SQL: {sql}")

        best_idx, total_token_usage = self._select_among_candidates(
            top_k_sql_candidates, data_item, cot_recorder=cot_recorder,
        )

        logger.info(f"{qp(qid)}[full_review] Selected candidate {best_idx} (conf={top_k_sql_candidates[best_idx][2]:.3f})")

        return top_k_sql_candidates[best_idx][0], {
            "strategy": "full_review",
            "confidence": top_k_sql_candidates[best_idx][2],
            "top_k_candidates": self._format_top_k_info(top_k_sql_candidates),
            "token_usage": total_token_usage,
        }

    def _format_top_k_info(self, top_k_sql_candidates: List[Tuple[str, str, float, float]]) -> List[Dict[str, Any]]:
        """格式化Top-K候选信息用于输出"""
        return [
            {
                "sql": sql,
                "confidence": round(conf, 3),
            }
            for sql, _, conf, _ in top_k_sql_candidates
        ]
