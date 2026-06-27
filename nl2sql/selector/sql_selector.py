"""单题 SQL 选择编排器 — 调用 BRSelectionRunner 完成选择并组装 SelectionResult

核心逻辑：
  1. 从 generator 输出（SingleQuestionResult）提取 SQL 候选
  2. 创建 BRSelectionRunner 执行三阶段选择（single → shortcut → full_review）
  3. 根据 FR_force_on / confidence_only 参数决定运行模式
  4. 组装 SelectionResult 输出

三种运行模式（对应 Step3 runner 的不同调用场景）：

  1. 默认模式（FR_force_on=False, confidence_only=False）
     - 使用场景：Step3b 单组合常规执行
     - 行为：正常三阶段 single → shortcut → full_review
       * confidence=1.0（唯一结果簇）→ status="single"，无 LLM 调用
       * confidence ≥ SELECTOR_SHORTCUT_THRESHOLD → status="shortcut"，无 LLM 调用
       * confidence < 阈值 → status="full_review"，执行 LLM 成对投票
     - 输出字段：selector_output_sql（选定的 SQL）、status
     - full_review_always_enabled=False

  2. FR_force_on=True（实验模式）
     - 使用场景：Step3b 单组合实验，强制所有非 single 题执行 full_review
     - 行为：设置 shortcut_threshold=1.0 使 shortcut 永不命中
       * confidence=1.0 → status="single"（不受影响）
       * confidence < 1.0 → 强制进入 full_review，即使 confidence 高于正常阈值
     - 输出字段：full_review_sql（LLM 投票选出的 SQL）
     - full_review_always_enabled=True
     - 注意：仍计算 confidence 和 top1_sql，供多组合模式下 single/shortcut 判断

  3. confidence_only=True（Sweeper 阶段1 模式）
     - 使用场景：Step3 Sweeper 阶段1，逐组合收集 confidence 用于 single/shortcut 判断
     - 行为：不传 LLM 给底层，复用 BRSelectionRunner 的 fallback_no_llm 路径
       * confidence=1.0 → status="single"
       * confidence ≥ 阈值 → status="shortcut"
       * confidence < 阈值 → status="sweeper_stage1_no_FR"（跳过 full_review，直接返回 Top-1）
         底层返回 fallback_no_llm，编排层映射为 sweeper_stage1_no_FR 以区别于通用 fallback
     - 输出字段：selector_output_sql、status
     - full_review_always_enabled=False
     - 关键：selection.token_usage 始终为 0（无 LLM 调用）

  互斥约束：FR_force_on=True 与 confidence_only=True 不可同时为 True（ValueError）
"""
import logging
from typing import Dict, Any

from .. import config
from ..common.data_types import SimpleDataItem, SingleQuestionResult, SelectionResult
from ..common.log_utils import qp
from .br_selection import BRSelectionRunner

logger = logging.getLogger(__name__)


def select(
    data_item: SimpleDataItem,
    generator_result: SingleQuestionResult,
    llm,
    db_path: str,
    FR_force_on: bool = False,
    confidence_only: bool = False,
    combo_tag: str = "",
    cot_recorder=None,
) -> SelectionResult:
    """单题 SQL 选择编排器

    Args:
        data_item: SimpleDataItem 数据项（LLM 投票时需要 schema/question/evidence）
        generator_result: SingleQuestionResult（generator 输出，包含 sql_candidates_after_revision）
        llm: LLMAdapter 实例
        db_path: 数据库路径
        FR_force_on: 是否强制 Full Review（实验模式，Step3b）
        confidence_only: 是否只计算 confidence+top1_sql（Sweeper 阶段1，跳过 full_review）
        combo_tag: Sweeper combo 标识前缀，如 "[combo:C] "（Step3b 为空）

    Returns:
        SelectionResult 包含 confidence、status、selected_sql、token_usage

    Raises:
        ValueError: FR_force_on 和 confidence_only 互斥
        LLMMaxRetriesExceeded: LLM 调用达到最大重试次数
        LLMParseMaxRetriesExceeded: LLM 响应解析达到最大重试次数
    """
    if FR_force_on and confidence_only:
        raise ValueError("FR_force_on 和 confidence_only 互斥：不能同时强制 full_review 和跳过 full_review")

    question_id = generator_result.question_id
    db_id = generator_result.db_id

    # 提取 SQL 候选列表（纯 SQL 字符串，供 BRSelectionRunner 执行和聚类）
    sql_candidates = [t.sql for t in generator_result.sql_candidates_after_revision]

    logger.info(f"{qp(data_item)}{combo_tag}Selector: {len(sql_candidates)} candidates, "
                f"FR_force_on={FR_force_on}, confidence_only={confidence_only}")

    # 创建 BRSelectionRunner
    # FR_force_on=true 时，设置 shortcut_threshold=1.0 使 shortcut 永不命中，
    # 从而强制非 single 题进入 full_review
    shortcut_threshold = 1.0 if FR_force_on else None  # None 使用 config 默认值

    # confidence_only=true 时，不传 LLM 给底层，复用 BRSelectionRunner 已有的
    # fallback_no_llm 路径（L327-335）：跳过 full_review，直接返回 Top-1
    effective_llm = None if confidence_only else llm

    runner = BRSelectionRunner(
        llm=effective_llm,
        shortcut_consistency_score_threshold=shortcut_threshold,
    )

    # 执行选择
    selected_sql, selection_info = runner.select_best_sql(
        sql_candidates=sql_candidates,
        db_path=db_path,
        data_item=data_item if not confidence_only else None,
        cot_recorder=cot_recorder if not confidence_only else None,
    )

    strategy = selection_info.get("strategy", "unknown")
    confidence = selection_info.get("confidence", 0.0)
    selection_token_usage = selection_info.get("token_usage", {"input_tokens": 0, "output_tokens": 0})

    # confidence_only 模式下，底层 fallback_no_llm 映射为语义更明确的 sweeper_stage1_no_FR
    # 含义：Sweeper 阶段1 跳过了 full_review，后续由阶段3 对 fr_group 统一执行 FR
    if confidence_only and strategy == "fallback_no_llm":
        strategy = "sweeper_stage1_no_FR"

    logger.info(f"{qp(data_item)}{combo_tag}Selector result: strategy={strategy}, confidence={confidence:.3f}")
    logger.debug(f"{qp(data_item)}{combo_tag}Selected SQL: {selected_sql}")

    # 合并 token_usage：继承 generator 的 generation + validation，新增 selection
    merged_token_usage = dict(generator_result.token_usage)
    merged_token_usage["selection"] = selection_token_usage

    # 组装 SelectionResult
    # top1_sql: 始终填充（无论 strategy 为何）
    # 对于 single/shortcut/fallback 策略，top1_sql == selected_sql
    # 对于 full_review 策略，top1_sql 仍然是 confidence 最高的 SQL
    top1_candidates = selection_info.get("top_k_candidates", [])
    top1_sql = top1_candidates[0]["sql"] if top1_candidates else selected_sql

    result = SelectionResult(
        question_id=question_id,
        db_id=db_id,
        sql_candidates=generator_result.sql_candidates,
        sql_candidates_after_revision=generator_result.sql_candidates_after_revision,
        confidence=confidence,
        top1_sql=top1_sql,
        status=strategy,
        full_review_always_enabled=FR_force_on,
        token_usage=merged_token_usage,
    )

    # 根据模式填充对应的 SQL 字段
    if FR_force_on:
        # 实验模式：填充 full_review_sql（设计文档 L170-197）
        if strategy == "full_review":
            result.full_review_sql = selected_sql
        elif strategy == "single":
            # single 即使 FR_force_on=true 也直接返回，不需要 full_review_sql
            pass
        else:
            # shortcut/fallback 在 FR_force_on=true 下不应出现（threshold=1.0）
            # 但作为安全兜底，仍填充 full_review_sql
          # shortcut/fallback 在 FR_force_on=true 下不应出现
            # （shortcut_threshold=1.0 下，仅自然 single 跳过 full_review，
            #   其余全部走 full_review）
            # 作为安全兜底，仍填充 full_review_sql
            result.full_review_sql = selected_sql
    else:
        # 常规模式：填充 selector_output_sql（设计文档 L199-227）
        result.selector_output_sql = selected_sql

    return result
