"""单题 SQL 生成编排器 — 3 路并行 + 动态策略 + 数量保障

核心逻辑：
  Phase 1: DC/Skeleton/ICL 三路并行，每路生成 INITIAL_BUDGET(3) 条可执行 SQL
  Single 判定: 所有 revised SQL 执行结果全部一致 → 直接返回
  Phase 2: 若非 single，继续三路并行生成剩余 SQL，总计 ≤15 条

LLM 致命异常不在此层捕获，向上传播给 runner 层处理（跳题 + errors.json）。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Any

from .. import config
from ..common.data_types import SimpleDataItem, TaggedSQL, SingleQuestionResult
from ..common.log_utils import qp
from ..sql_evaluator import run_query, ExecStatus
from ..validator import validate

from .dc_generator import DivideConquerGenerator
from .skeleton_generator import StepwiseGenerator
from .icl_generator import ExemplarGenerator

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 异常定义
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RouteBudgetUnmetError(Exception):
    """单路 SQL 生成在 MAX_ROUTE_RETRY_ATTEMPTS 轮后仍未达到 budget 时抛出。

    用于"数量保障 + 整题失败拦截"机制：
      - Phase 1：每路 budget = GENERATOR_INITIAL_BUDGET（默认 3）
      - Phase 2：每路 budget = GENERATOR_xx_BUDGET - GENERATOR_INITIAL_BUDGET（默认 2）
    任一路在任一阶段达不到 budget 即抛出本异常，由 runner 捕获写 errors.json，
    避免 selector 收到不完整候选导致 strategy=empty 的静默失败。

    Attributes:
        route_name: 路线名 ("dc" | "skeleton" | "icl")
        phase:      阶段标识 ("phase1" | "phase2")
        actual:     该路实际产出的可执行 SQL 数
        budget:     该路在该阶段需达成的目标数
        attempts:   实际已尝试轮数（== MAX_ROUTE_RETRY_ATTEMPTS）
    """

    def __init__(self, route_name: str, phase: str, actual: int, budget: int, attempts: int):
        self.route_name = route_name
        self.phase = phase
        self.actual = actual
        self.budget = budget
        self.attempts = attempts
        super().__init__(
            f"Route '{route_name}' at {phase} only produced {actual}/{budget} "
            f"executable SQLs after {attempts} retry attempts (budget unmet)"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _accumulate(target: Dict[str, int], source: Dict[str, int]) -> None:
    """累加 token 统计"""
    target["input_tokens"] += source.get("input_tokens", 0)
    target["output_tokens"] += source.get("output_tokens", 0)
    target["reasoning_tokens"] += source.get("reasoning_tokens", 0)
    target["content_tokens"] += source.get("content_tokens", 0)


def _zero_token() -> Dict[str, int]:
    """返回零 token 字典"""
    return {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "content_tokens": 0}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单路执行（含数量保障）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_single_route(
    generator,
    budget: int,
    route_name: str,
    data_item: SimpleDataItem,
    llm,
    max_route_retries: int = 3,
    combo_tag: str = "",
    cot_recorder=None,
    temp_id_prefix: str = "",
    phase: str = "phase1",
) -> Tuple[List[TaggedSQL], List[TaggedSQL], Dict[str, int], Dict[str, int]]:
    """执行单个生成回路（含数量保障重试）

    Args:
        generator: BaseSQLGenerator 实例
        budget: 本轮需要收集的可执行 SQL 数量
        route_name: 回路名称 ("dc" | "skeleton" | "icl")
        data_item: SimpleDataItem
        llm: LLMAdapter 实例
        max_route_retries: 每路最大重试轮数
        combo_tag: Sweeper combo 标识前缀，如 "[combo:C] "（Step3b 为空）
        cot_recorder: 可选 CoTRecorder 实例（None 时不记录 CoT）
        temp_id_prefix: sql_temp_id 命名空间前缀（如 "p1"/"p2"）
        phase: 阶段标识（"phase1" | "phase2"），仅用于 RouteBudgetUnmetError 标记

    Returns:
        (originals, revised, gen_token, val_token) 四元组
        - originals: 原始生成的 SQL（TaggedSQL 列表）
        - revised: 校验修正后的 SQL（TaggedSQL 列表，仅含可执行 SQL）
        - gen_token: 生成阶段 token 消耗
        - val_token: 校验阶段 token 消耗
    """
    all_originals: List[TaggedSQL] = []
    all_revised: List[TaggedSQL] = []
    gen_token = _zero_token()
    val_token = _zero_token()
    sql_counter = 0  # 回路内全局 SQL 计数器

    for attempt in range(max_route_retries):
        remaining = budget - len(all_revised)
        if remaining <= 0:
            break

        # 生成 SQL（含 sql_temp_id，用于 CoT 关联）
        # temp_id_prefix 用于跨 phase / route_attempt 区分，避免 sql_temp_id 重复
        # 命名约定：{phase}[_a{attempt}]，例如 "p1" / "p1_a1" / "p2" / "p2_a1"
        attempt_prefix = (
            f"{temp_id_prefix}_a{attempt}" if (temp_id_prefix and attempt > 0)
            else (temp_id_prefix if temp_id_prefix else "")
        )
        sql_candidates, g_tok, sql_temp_ids = generator.generate(
            data_item, llm, remaining, cot_recorder=cot_recorder,
            temp_id_prefix=attempt_prefix,
        )
        _accumulate(gen_token, g_tok)

        # 兜底：sql_temp_ids 应与 sql_candidates 等长
        if len(sql_temp_ids) != len(sql_candidates):
            sql_temp_ids = [f"{attempt_prefix}_{route_name}_attempt{attempt}_{i}" for i in range(len(sql_candidates))]

        # 逐条校验 + 执行可行性检查
        for sql, sql_temp_id in zip(sql_candidates, sql_temp_ids):
            sql_counter += 1
            sql_tag = f"{combo_tag}[{route_name}/sql_{sql_counter}] "

            sql_preview = ' '.join(sql.split())[:80]
            logger.debug(f"{qp(data_item)}{sql_tag}Validating: {sql_preview}...")
            rev_sql, v_tok = validate(
                sql, data_item, llm, log_tag=sql_tag,
                cot_recorder=cot_recorder, sql_temp_id=sql_temp_id,
            )
            _accumulate(val_token, v_tok)

            # 执行检查：只保留可执行的 SQL
            exec_result = run_query(data_item.database_path, rev_sql)
            if exec_result.status in (ExecStatus.OK, ExecStatus.NO_ROWS, ExecStatus.ALL_NULL):
                all_originals.append(TaggedSQL(sql=sql, source=route_name))
                all_revised.append(TaggedSQL(sql=rev_sql, source=route_name))
                logger.debug(f"{qp(data_item)}{sql_tag}Executable, accepted")

                # CoT：仅在该 SQL 通过执行检查后 commit（未通过的 calls 在 finalize 时会丢弃）
                if cot_recorder is not None:
                    try:
                        cot_recorder.commit_sql(
                            sql_temp_id=sql_temp_id,
                            final_sql=rev_sql,
                            source=route_name,
                        )
                    except Exception as e:
                        logger.debug(f"{qp(data_item)}{sql_tag}[CoT] commit_sql failed (non-fatal): {e}")
            else:
                logger.info(f"{qp(data_item)}{sql_tag}Not executable ({exec_result.status.value}), discarded")
                logger.debug(f"{qp(data_item)}{sql_tag}Revised SQL: {' '.join(rev_sql.split())[:120]}")

        if len(all_revised) >= budget:
            break

        logger.warning(f"{qp(data_item)}{combo_tag}[{route_name}] attempt {attempt + 1}: "
                       f"got {len(all_revised)}/{budget} executable SQLs, retrying...")

    # 数量保障：用尽 max_route_retries 仍未达 budget → 抛异常触发整题失败
    if len(all_revised) < budget:
        logger.error(
            f"{qp(data_item)}{combo_tag}[{route_name}] {phase} BUDGET UNMET: "
            f"only {len(all_revised)}/{budget} executable SQLs after "
            f"{max_route_retries} attempts → marking question as failed"
        )
        raise RouteBudgetUnmetError(
            route_name=route_name,
            phase=phase,
            actual=len(all_revised),
            budget=budget,
            attempts=max_route_retries,
        )

    return all_originals, all_revised, gen_token, val_token


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Single 判定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_single(revised_sqls: List[TaggedSQL], db_path: str) -> bool:
    """执行所有 revised SQL，检查结果是否全部一致

    判定条件：
    - 所有 SQL 都执行成功（status == ExecStatus.OK）
    - 所有 SQL 的结果行集完全一致
    - 至少有 1 条 SQL 且结果非空
    """
    if not revised_sqls:
        return False

    results = set()
    for tagged in revised_sqls:
        exec_result = run_query(db_path, tagged.sql)
        if exec_result.status == ExecStatus.OK and exec_result.rows:
            results.add(frozenset(tuple(r) for r in exec_result.rows))
        else:
            return False  # 有执行失败/空结果，不算 single

    return len(results) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 并行执行辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_routes_parallel(
    routes: List[Tuple[Any, int, str]],
    data_item: SimpleDataItem,
    llm,
    max_route_retries: int,
    max_workers: int,
    combo_tag: str = "",
    cot_recorder=None,
    temp_id_prefix: str = "",
    phase: str = "phase1",
) -> Tuple[List[TaggedSQL], List[TaggedSQL], Dict[str, Dict[str, int]], Dict[str, int]]:
    """并行执行多个生成回路

    Args:
        routes: [(generator, budget, route_name), ...]
        data_item, llm, max_route_retries, max_workers: 配置参数
        combo_tag: Sweeper combo 标识前缀（透传给 _run_single_route）
        cot_recorder: 可选 CoTRecorder 实例（透传给 _run_single_route）
        temp_id_prefix: sql_temp_id 命名空间前缀（如 "p1"/"p2"），用于跨 phase 区分
        phase: 阶段标识（"phase1" | "phase2"），透传给 _run_single_route 用于异常标记

    Returns:
        (all_originals, all_revised, gen_tokens_by_route, total_val_token)
    """
    all_originals: List[TaggedSQL] = []
    all_revised: List[TaggedSQL] = []
    gen_tokens: Dict[str, Dict[str, int]] = {}
    total_val_token = _zero_token()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_route = {}
        for generator, budget, route_name in routes:
            if budget <= 0:
                gen_tokens[route_name] = _zero_token()
                continue
            future = executor.submit(
                _run_single_route, generator, budget, route_name,
                data_item, llm, max_route_retries, combo_tag, cot_recorder,
                temp_id_prefix, phase,
            )
            future_to_route[future] = route_name

        for future in as_completed(future_to_route):
            route_name = future_to_route[future]
            originals, revised, g_tok, v_tok = future.result()
            all_originals.extend(originals)
            all_revised.extend(revised)
            gen_tokens[route_name] = g_tok
            _accumulate(total_val_token, v_tok)

    return all_originals, all_revised, gen_tokens, total_val_token


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 核心编排接口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_and_validate(
    data_item: SimpleDataItem,
    llm,
    db_id: str,
    dc_generator: DivideConquerGenerator = None,
    skeleton_generator: StepwiseGenerator = None,
    icl_generator: ExemplarGenerator = None,
    combo_tag: str = "",
    cot_recorder=None,
) -> SingleQuestionResult:
    """单题 SQL 生成 + 校验编排器

    3 路并行生成 → single 判定 → 可选 Phase 2 扩展。
    LLM 致命异常（LLMMaxRetriesExceeded / LLMParseMaxRetriesExceeded）
    不在此层捕获，向上传播给 runner 层统一处理。

    Args:
        data_item: SimpleDataItem 数据项
        llm: LLMAdapter 实例
        db_id: 数据库 ID
        dc_generator: DC 生成器实例（可选，默认自动创建）
        skeleton_generator: Skeleton 生成器实例（可选，默认自动创建）
        icl_generator: ICL 生成器实例（可选，默认自动创建）
        combo_tag: Sweeper combo 标识前缀，如 "[combo:C] "（Step3b 为空）
        cot_recorder: 可选 CoTRecorder 实例（None 时不记录 CoT，主流程行为完全等同改动前）

    Returns:
        SingleQuestionResult 包含 sql_candidates、sql_candidates_after_revision、token_usage

    Raises:
        LLMMaxRetriesExceeded: LLM 调用达到最大重试次数
        LLMParseMaxRetriesExceeded: LLM 响应解析达到最大重试次数
        RouteBudgetUnmetError: 某路在用尽 MAX_ROUTE_RETRY_ATTEMPTS 后仍未达 budget；
            该异常向上传播至 runner，触发整题失败入 errors.json
    """
    # 初始化生成器
    if dc_generator is None:
        dc_generator = DivideConquerGenerator()
    if skeleton_generator is None:
        skeleton_generator = StepwiseGenerator()
    if icl_generator is None:
        icl_generator = ExemplarGenerator()

    initial_budget = config.GENERATOR_INITIAL_BUDGET
    dc_total = config.GENERATOR_DC_BUDGET
    skeleton_total = config.GENERATOR_SKELETON_BUDGET
    icl_total = config.GENERATOR_ICL_BUDGET
    max_route_retries = config.MAX_ROUTE_RETRY_ATTEMPTS
    max_workers = config.GENERATOR_MAX_WORKERS

    question_id = data_item.question_id or 0

    # ── Phase 1: 每路生成 initial_budget 条 ──
    logger.info(f"{qp(data_item)}{combo_tag}Phase 1: generating {initial_budget} SQLs per route (3 routes)")

    phase1_routes = [
        (dc_generator, min(initial_budget, dc_total), "dc"),
        (skeleton_generator, min(initial_budget, skeleton_total), "skeleton"),
        (icl_generator, min(initial_budget, icl_total), "icl"),
    ]

    all_originals, all_revised, gen_tokens, total_val_token = _run_routes_parallel(
        phase1_routes, data_item, llm, max_route_retries, max_workers, combo_tag,
        cot_recorder=cot_recorder,
        temp_id_prefix="p1",
        phase="phase1",
    )

    logger.info(f"{qp(data_item)}{combo_tag}Phase 1 done: {len(all_revised)} executable SQLs "
                f"(dc={sum(1 for t in all_revised if t.source == 'dc')}, "
                f"skeleton={sum(1 for t in all_revised if t.source == 'skeleton')}, "
                f"icl={sum(1 for t in all_revised if t.source == 'icl')})")

    # ── Single 判定 ──
    is_single = _check_single(all_revised, data_item.database_path)
    if is_single:
        logger.info(f"{qp(data_item)}{combo_tag}Single detected! All {len(all_revised)} SQLs produce identical results.")
        return SingleQuestionResult(
            question_id=question_id,
            db_id=db_id,
            sql_candidates=all_originals,
            sql_candidates_after_revision=all_revised,
            token_usage={
                "generation": gen_tokens,
                "validation": total_val_token,
            },
        )

    # ── Phase 2: 每路继续生成剩余 budget ──
    dc_remaining = dc_total - min(initial_budget, dc_total)
    skeleton_remaining = skeleton_total - min(initial_budget, skeleton_total)
    icl_remaining = icl_total - min(initial_budget, icl_total)

    logger.info(f"{qp(data_item)}{combo_tag}Phase 2: generating remaining SQLs "
                f"(dc={dc_remaining}, skeleton={skeleton_remaining}, icl={icl_remaining})")

    phase2_routes = [
        (dc_generator, dc_remaining, "dc"),
        (skeleton_generator, skeleton_remaining, "skeleton"),
        (icl_generator, icl_remaining, "icl"),
    ]

    p2_originals, p2_revised, p2_gen_tokens, p2_val_token = _run_routes_parallel(
        phase2_routes, data_item, llm, max_route_retries, max_workers, combo_tag,
        cot_recorder=cot_recorder,
        temp_id_prefix="p2",
        phase="phase2",
    )

    # 合并 Phase 1 + Phase 2 的结果
    all_originals.extend(p2_originals)
    all_revised.extend(p2_revised)

    # 合并 token 统计（同一 route 累加）
    for route_name, tok in p2_gen_tokens.items():
        if route_name in gen_tokens:
            _accumulate(gen_tokens[route_name], tok)
        else:
            gen_tokens[route_name] = tok
    _accumulate(total_val_token, p2_val_token)

    logger.info(f"{qp(data_item)}{combo_tag}Phase 2 done: total {len(all_revised)} executable SQLs")

    return SingleQuestionResult(
        question_id=question_id,
        db_id=db_id,
        sql_candidates=all_originals,
        sql_candidates_after_revision=all_revised,
        token_usage={
            "generation": gen_tokens,
            "validation": total_val_token,
        },
    )
