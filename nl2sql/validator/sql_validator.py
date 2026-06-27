"""单条SQL校验编排器

对外接口：
  validate(sql, data_item, llm) -> (revised_sql, token_usage)

错误处理原则：
  - LLM 调用或解析达到最大重试次数（LLMMaxRetriesExceeded / LLMParseMaxRetriesExceeded）
    → 立即中断整条校验链，向上传播给 runner 记录 error.json 并跳到下一题
"""
import logging
from typing import Dict, List, Optional, Tuple

from . import LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded
from ..common.log_utils import qp

logger = logging.getLogger(__name__)


def _accumulate(total: Dict[str, int], delta: Dict[str, int]) -> None:
    """累加 token_usage"""
    for key in total:
        total[key] += delta.get(key, 0)


def validate(sql: str, data_item, llm, checkers=None, log_tag: str = "",
             cot_recorder=None, sql_temp_id: str = None) -> Tuple[str, Dict[str, int]]:
    """
    单条SQL校验编排器

    Args:
        sql: 待校验的SQL
        data_item: SimpleDataItem数据项
        llm: LLMAdapter实例（需提供 ask() 接口）
        checkers: 可选的checker列表，None时使用默认链
        log_tag: 回路/SQL 级日志标识，如 "[dc/sql_1] "
        cot_recorder: 可选 CoTRecorder 实例（None 时不记录 CoT）
        sql_temp_id: 该 SQL 在 CoT 中的临时 id（与 generation 阶段透传一致）

    Returns:
        (revised_sql, token_usage) 元组

    Raises:
        LLMMaxRetriesExceeded: LLM 网络调用达到最大重试次数，应中断当前题目
        LLMParseMaxRetriesExceeded: LLM 响应解析失败达到最大重试次数，应中断当前题目
    """
    if checkers is None:
        from . import get_default_checkers
        checkers = get_default_checkers()

    total_token_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}
    current_sql = sql

    # 校验链开始
    checker_names = [c.__class__.__name__ for c in checkers]
    logger.debug(f"{qp(data_item)}{log_tag}Validation: {len(checkers)} checkers [{' -> '.join(checker_names)}]")
    logger.debug(f"{qp(data_item)}{log_tag}Input SQL: {sql}")

    for checker in checkers:
        try:
            logger.debug(f"{qp(data_item)}{log_tag}[{checker.__class__.__name__}] checking...")
            revised_sql, token_usage = checker.check_and_revise(
                current_sql, data_item, llm, log_tag=log_tag,
                cot_recorder=cot_recorder, sql_temp_id=sql_temp_id,
            )
            _accumulate(total_token_usage, token_usage)
            if revised_sql != current_sql:
                logger.info(f"{qp(data_item)}{log_tag}[{checker.__class__.__name__}] SQL revised")
                logger.debug(f"{qp(data_item)}{log_tag}[{checker.__class__.__name__}] Revised: {revised_sql}")
                current_sql = revised_sql
            else:
                logger.debug(f"{qp(data_item)}{log_tag}[{checker.__class__.__name__}] passed")
        except (LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded):
            # LLM 致命错误 — 立即中断校验链，向上传播给 runner
            logger.error(f"{qp(data_item)}{log_tag}[{checker.__class__.__name__}] LLM fatal error, aborting validation")
            raise
        except Exception as e:
            # 非 LLM 致命错误 — 跳过当前 checker，继续执行下一个
            logger.warning(f"{qp(data_item)}{log_tag}[{checker.__class__.__name__}] Check failed: {e}, continuing with current SQL")
            continue

    # 校验链结束
    if current_sql != sql:
        logger.info(f"{qp(data_item)}{log_tag}Validation done: SQL was revised")
    else:
        logger.debug(f"{qp(data_item)}{log_tag}Validation done: all checkers passed")

    return current_sql, total_token_usage
