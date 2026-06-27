"""CoT (Chain-of-Thought) 记录器

为单个题目记录 SQL 生成、校验、选择三个阶段的所有 LLM 调用轨迹，
仅在题目"完全成功"时（generation 有 after_revision 候选 且 selector 非 fallback）
落盘成 q_{question_id:04d}_cot.json。

设计原则：
- 仅作为输出层，不参与/不影响 NL2SQL 核心业务逻辑
- 调用方按需注入；记录失败不能反过来抛错破坏主流程
- sql_temp_id：跨生成 + 校验阶段唯一标识一条 SQL，commit 后映射为 sql_0/sql_1...
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CoTRecorder:
    """单题级 CoT 记录器（与单个 question_id 绑定，单线程使用）

    数据流：
      - record_generation / record_validation：写入 _pending_calls[sql_temp_id]
      - commit_sql：把 sql_temp_id 对应的 calls 转入 _committed_sqls（按 commit 顺序映射 sql_idx）
      - record_selection：写入 _selection_calls
      - finalize_and_dump：组装最终 JSON 并写盘
    """

    def __init__(self, question_id: int, db_id: str):
        self.question_id = question_id
        self.db_id = db_id
        # 与项目其他日志一致的题号前缀（与 common.log_utils.qp 输出对齐：含尾随空格）
        self._log_prefix = f"[q_{question_id:04d}] "

        # sql_temp_id -> [call_dict, ...]
        self._pending_calls: Dict[str, List[Dict[str, Any]]] = {}
        # 已确认进入 sql_after_revision 的 SQL（按 commit 顺序）
        # [(sql_temp_id, final_sql, source, calls), ...]
        self._committed_sqls: List[Dict[str, Any]] = []
        # selector 的 LLM 调用（topK > 2 时可能多次）
        self._selection_calls: List[Dict[str, Any]] = []
        # 全局 call_idx 计数器（跨所有阶段递增）
        self._global_call_idx: int = 0
        # 互斥锁：selector 内部 ThreadPoolExecutor 并行调用 record_selection
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _next_call_idx(self) -> int:
        idx = self._global_call_idx
        self._global_call_idx += 1
        return idx

    def _append_pending(self, sql_temp_id: str, call: Dict[str, Any]) -> None:
        if sql_temp_id not in self._pending_calls:
            self._pending_calls[sql_temp_id] = []
        self._pending_calls[sql_temp_id].append(call)

    # ------------------------------------------------------------------
    # 记录接口
    # ------------------------------------------------------------------
    def record_generation(
        self,
        sql_temp_id: str,
        stage: str,
        input_prompt: str,
        output_full: str,
        llm_reasoning: str,
        parsed_reasoning: str,
        parsed_result_sql: Optional[str],
        token_usage: Dict[str, int],
    ) -> None:
        """记录 generation 阶段一次 LLM 调用"""
        try:
            with self._lock:
                call = {
                    "call_idx": self._next_call_idx(),
                    "step_type": "generation",
                    "stage": stage,
                    "input_prompt": input_prompt or "",
                    "output_full": output_full or "",
                    "llm_reasoning": llm_reasoning or "",
                    "parsed_reasoning": parsed_reasoning or "",
                    "parsed_result_sql": parsed_result_sql,
                    "token_usage": dict(token_usage or {}),
                }
                self._append_pending(sql_temp_id, call)
        except Exception as e:
            logger.warning(f"{self._log_prefix}[CoTRecorder] record_generation failed: {e}")

    def record_validation(
        self,
        sql_temp_id: str,
        checker_name: str,
        trigger_reason: str,
        input_sql: str,
        input_prompt: str,
        output_full: str,
        llm_reasoning: str,
        parsed_reasoning: str,
        parsed_result_sql: Optional[str],
        token_usage: Dict[str, int],
    ) -> None:
        """记录 validation 阶段（某个 checker 触发）的一次 LLM 调用"""
        try:
            with self._lock:
                call = {
                    "call_idx": self._next_call_idx(),
                    "step_type": "validation",
                    "stage": checker_name,
                    "trigger_reason": trigger_reason or "",
                    "input_sql": input_sql or "",
                    "input_prompt": input_prompt or "",
                    "output_full": output_full or "",
                    "llm_reasoning": llm_reasoning or "",
                    "parsed_reasoning": parsed_reasoning or "",
                    "parsed_result_sql": parsed_result_sql,
                    "token_usage": dict(token_usage or {}),
                }
                self._append_pending(sql_temp_id, call)
        except Exception as e:
            logger.warning(f"{self._log_prefix}[CoTRecorder] record_validation failed: {e}")

    def record_selection(
        self,
        input_prompt: str,
        output_full: str,
        llm_reasoning: str,
        parsed_reasoning: str,
        token_usage: Dict[str, int],
    ) -> None:
        """记录 selection 阶段一次 LLM 调用（支持多次：topK > 2 投票时连续追加）"""
        try:
            with self._lock:
                call = {
                    "call_idx": self._next_call_idx(),
                    "step_type": "selection",
                    "input_prompt": input_prompt or "",
                    "output_full": output_full or "",
                    "llm_reasoning": llm_reasoning or "",
                    "parsed_reasoning": parsed_reasoning or "",
                    "token_usage": dict(token_usage or {}),
                }
                self._selection_calls.append(call)
        except Exception as e:
            logger.warning(f"{self._log_prefix}[CoTRecorder] record_selection failed: {e}")

    # ------------------------------------------------------------------
    # 提交：sql_temp_id -> sql_idx 映射
    # ------------------------------------------------------------------
    def commit_sql(self, sql_temp_id: str, final_sql: str, source: str) -> None:
        """将一个 sql_temp_id 的 pending calls 转为 committed（即将进入 sql_after_revision）

        - calls 顺序保留全局 call_idx 顺序
        - 即使没有 pending calls 也会创建一个空 calls 的 committed 项
          （理论上不会出现，但兜底以防丢失 SQL 入口）
        - 同一 sql_temp_id 多次 commit 不会重复（第二次起忽略）
        """
        try:
            # 防重复 commit
            with self._lock:
                for entry in self._committed_sqls:
                    if entry["sql_temp_id"] == sql_temp_id:
                        logger.debug(
                            f"{self._log_prefix}[CoTRecorder] sql_temp_id={sql_temp_id} already committed, skipped"
                        )
                        return

                calls = self._pending_calls.pop(sql_temp_id, [])
                self._committed_sqls.append({
                    "sql_temp_id": sql_temp_id,
                    "final_sql": final_sql or "",
                    "source": source or "",
                    "calls": calls,
                })
        except Exception as e:
            logger.warning(f"{self._log_prefix}[CoTRecorder] commit_sql failed: {e}")

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def build_payload(self) -> Dict[str, Any]:
        """构建最终 JSON 数据结构（不写盘，便于测试/调试）"""
        sql_traces: Dict[str, Any] = {}
        for sql_idx, entry in enumerate(self._committed_sqls):
            sql_traces[f"sql_{sql_idx}"] = {
                "final_sql": entry["final_sql"],
                "source": entry["source"],
                "calls": entry["calls"],
            }
        return {
            "question_id": self.question_id,
            "db_id": self.db_id,
            "sql_traces": sql_traces,
            "selection_calls": self._selection_calls,
        }

    def finalize_and_dump(self, output_path: Path) -> None:
        """把 committed SQL + selection_calls 序列化为 JSON 写到 output_path

        失败仅 log warning，不抛异常（避免影响主流程）。
        """
        try:
            payload = self.build_payload()
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(
                f"{self._log_prefix}[CoTRecorder] cot dumped: "
                f"{len(payload['sql_traces'])} sqls, "
                f"{len(payload['selection_calls'])} selection_calls -> {output_path}"
            )
        except Exception as e:
            logger.warning(f"{self._log_prefix}[CoTRecorder] finalize_and_dump failed: {e}")
