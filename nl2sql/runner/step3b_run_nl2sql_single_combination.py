"""Step3b Runner — 单组合全流程（Generation → Selection）

单题处理流程（_process_single_question → generate_and_validate → select）:
  1. Generation — 三路并行（DC / Skeleton / ICL）× 两阶段:
     Phase 1: 每路生成 INITIAL_BUDGET(3) 条可执行 SQL
       每路内部循环（最多 MAX_ROUTE_RETRIES 轮）:
         a) 批量调用 LLM 生成 SQL 候选（generator.generate）
         b) 逐条校验修正（Validator 8 个 Checker 依次校验 + 修复）
         c) 执行检查（run_query）：仅保留可执行 SQL
         d) 若可执行 SQL 不足 budget → 继续下一轮生成补充
     Single 判定: 所有可执行 SQL 执行结果全部一致 → 跳过 Phase 2
     Phase 2（非 Single 时）: 三路并行生成剩余 budget，逻辑同上
  2. Selection — 基于置信度的最佳 SQL 选择（BRSelectionRunner）

异常处理:
  - LLMParseMaxRetriesExceeded（LLM 响应解析重试耗尽）→ 整题跳过，记入 errors.json
  - LLMMaxRetriesExceeded（LLM 网络调用重试耗尽）→ 整题跳过，记入 errors.json
  - 全局重试: 下一轮从 errors.json 中重试失败题目（最多 MAX_GLOBAL_RETRIES 轮）
  - 断点续跑: 跳过已存在 selector 输出的题目

使用方式:
  python -m nl2sql.runner.step3b_run_nl2sql_single_combination --run-id "RUN_ID" --schema-json "recall_first_schema.json" --llm-provider "deepseek_v4_flash" --fr-force-on --max-workers 5 --verbose 

并行度: 峰值 LLM 并发 = STEP3B_MAX_WORKERS × max(GEN, SEL)_MAX_WORKERS
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Iterable, Optional, Set

from .. import config
from ..common.atomic_io import atomic_write_json
from ..common.data_loader import load_dev_questions, load_ddl_schemas, build_data_item, create_llm
from ..common.data_types import SimpleDataItem
from ..common.log_utils import qp
from ..common.runner_utils import setup_logging, ErrorManager
from ..client import LLMAdapter, LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded
from ..generator import generate_and_validate, RouteBudgetUnmetError
from ..selector import select


logger = logging.getLogger("step3b")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单题处理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _process_single_question(
    question_id: int,
    data_item: SimpleDataItem,
    db_id: str,
    llm: LLMAdapter,
    run_id: str,
    fr_force_on: bool,
) -> None:
    """单题全流程：generation → selection → 持久化

    LLM 异常不在此函数内捕获，向上传播到并行调度层。

    Raises:
        LLMMaxRetriesExceeded: LLM 网络调用达到最大重试次数
        LLMParseMaxRetriesExceeded: LLM 响应解析达到最大重试次数
    """
    gen_dir = config.GENERATION_OUTPUT_DIR / run_id / "generation"
    sel_dir = config.GENERATION_OUTPUT_DIR / run_id / "selector"

    # ── CoT 记录器（仅当开关启用时创建；否则保持 None，所有 hook 路径短路）──
    cot_recorder = None
    if getattr(config, "COT_OUTPUT_ENABLED", False):
        try:
            from ..common.cot_recorder import CoTRecorder
            cot_recorder = CoTRecorder(question_id=question_id, db_id=db_id)
            logger.debug(f"{qp(question_id)}[CoT] recorder enabled (db_id={db_id})")
        except Exception as e:
            logger.debug(f"{qp(question_id)}[CoT] init recorder failed (non-fatal): {e}")
            cot_recorder = None
    else:
        logger.debug(f"{qp(question_id)}[CoT] disabled by config.COT_OUTPUT_ENABLED")

    # ── Step 1: Generation ──
    logger.info(f"{qp(question_id)}Starting generation...")
    gen_result = generate_and_validate(data_item, llm, db_id, cot_recorder=cot_recorder)

    # 持久化 generation 输出（原子写入，避免中途崩溃造成半截 JSON）
    gen_file = gen_dir / f"q_{question_id:04d}.json"
    atomic_write_json(gen_file, gen_result.to_dict())
    logger.info(f"{qp(question_id)}Generation done: {len(gen_result.sql_candidates_after_revision)} revised SQLs → {gen_file.name}")

    # ── Step 2: Selection ──
    db_path = os.path.join(config.BIRD_DB_DIR, db_id, f"{db_id}.sqlite")
    logger.info(f"{qp(question_id)}Starting selection (FR_force_on={fr_force_on})...")
    sel_result = select(
        data_item, gen_result, llm, db_path,
        FR_force_on=fr_force_on, cot_recorder=cot_recorder,
    )

    # 持久化 selector 输出（原子写入）
    sel_file = sel_dir / f"q_{question_id:04d}_selected.json"
    atomic_write_json(sel_file, sel_result.to_dict())
    logger.info(f"{qp(question_id)}Selection done: status={sel_result.status}, confidence={sel_result.confidence:.3f} → {sel_file.name}")

    # ── Step 3: CoT 落盘 (仅完全成功的题：有 after_revision 候选 且 selector 非 fallback) ──
    if cot_recorder is not None:
        try:
            success_status = {"single", "shortcut", "full_review"}
            if (
                gen_result.sql_candidates_after_revision
                and sel_result.status in success_status
            ):
                cot_dir = config.GENERATION_OUTPUT_DIR / run_id / "cot"
                cot_dir.mkdir(parents=True, exist_ok=True)
                cot_recorder.finalize_and_dump(cot_dir / f"q_{question_id:04d}_cot.json")
            else:
                logger.debug(
                    f"{qp(question_id)}[CoT] skipped dump: "
                    f"after_revision={len(gen_result.sql_candidates_after_revision)}, "
                    f"status={sel_result.status}"
                )
        except Exception as e:
            logger.warning(f"{qp(question_id)}[CoT] finalize failed (non-fatal): {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 核心编排
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _parse_qid_list(raw: Optional[str]) -> Set[int]:
    """解析 ``--rerun-qids`` 参数。

    支持逗号/空格/分号/换行分隔，如 ``"1,2,3"`` / ``"1 2 3"`` / ``"1; 2"``。
    空串或 None 返回空集。非法 token 报 ValueError。
    """
    if not raw:
        return set()
    out: Set[int] = set()
    for tok in raw.replace(",", " ").replace(";", " ").split():
        try:
            out.add(int(tok))
        except ValueError:
            raise ValueError(f"--rerun-qids 含非法题号: {tok!r}")
    return out


def run_all_questions(
    run_id: str,
    schema_json: str,
    llm_provider: str,
    fr_force_on: bool,
    max_workers: int,
    max_global_retries: int,
    dev_json: str = None,
    force_rerun: bool = False,
    rerun_qids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    """核心编排函数：并行处理所有题目 + 全局重试

    Args:
        dev_json: 自定义 dev.json 路径（默认使用 config.DEV_JSON）
        force_rerun: True 时忽略已完成题目，全量重跑（与 rerun_qids 互斥）
        rerun_qids: 非空时仅处理指定 qid 集合，无论是否已完成；优先级高于 force_rerun

    Returns:
        统计摘要 {"total": N, "completed": N, "errors": N, "retries_used": N}
    """
    t0 = time.time()
    rerun_qids_set: Set[int] = set(rerun_qids or [])

    # ── 1. 前置检查 ──
    dev_json_path = dev_json or config.DEV_JSON
    if not Path(dev_json_path).exists():
        logger.error(f"dev.json not found: {dev_json_path}")
        sys.exit(1)

    schema_path = Path(config.STEP2I_OUTPUT_DDL_DIR) / schema_json
    if not schema_path.exists():
        logger.error(f"DDL schema not found: {schema_path}")
        sys.exit(1)

    # ── 2. 数据加载 ──
    logger.info(f"Loading dev questions from: {dev_json_path}")
    dev_questions = load_dev_questions(dev_json_path)
    ddl_schemas = load_ddl_schemas(schema_json)
    all_qids = sorted(dev_questions.keys())
    logger.info(f"Loaded {len(all_qids)} questions, {len(ddl_schemas)} schemas")

    # ── 3. 创建 LLM ──
    llm = create_llm(llm_provider)

    # ── 4. 创建目录 ──
    gen_dir = config.GENERATION_OUTPUT_DIR / run_id / "generation"
    sel_dir = config.GENERATION_OUTPUT_DIR / run_id / "selector"
    gen_dir.mkdir(parents=True, exist_ok=True)
    sel_dir.mkdir(parents=True, exist_ok=True)

    # ── 5. 创建 ErrorManager ──
    error_file = config.STEP3B_LOG_DIR / run_id / "errors.json"
    error_manager = ErrorManager(error_file)

    total = len(all_qids)
    retries_used = 0

    # ── 6. 主循环（1 轮首次 + max_global_retries 轮重试）──
    for round_idx in range(max_global_retries + 1):
        if round_idx == 0:
            # 扫描已完成题目（selector 输出存在即视为完成）
            completed_qids = set()
            for sel_file in sel_dir.glob("q_*_selected.json"):
                try:
                    stem = sel_file.stem  # "q_0721_selected"
                    qid = int(stem.split("_")[1])
                    completed_qids.add(qid)
                except (IndexError, ValueError):
                    pass

            if rerun_qids_set:
                # 单题重跑：仅处理指定 qid （不受 completed_qids 影响）
                valid = rerun_qids_set & set(all_qids)
                invalid = sorted(rerun_qids_set - valid)
                if invalid:
                    logger.warning(f"[Round 0] --rerun-qids 中不在 dev.json 的题号已忽略: {invalid}")
                to_process = sorted(valid)
                # 单题重跑同时清理 errors.json 中该 qid 的历史错误，避免下轮重试双跳
                for qid in to_process:
                    error_manager.remove(qid)
                logger.info(
                    f"[Round 0] Rerun-qids mode: {len(to_process)} questions to rerun "
                    f"(ignoring {len(completed_qids & valid)} previously completed)"
                )
            elif force_rerun:
                # 全量重跑：忽略已完成状态
                to_process = list(all_qids)
                logger.info(f"[Round 0] Force-rerun mode: processing all {len(to_process)} questions (ignoring {len(completed_qids)} previously completed)")
            else:
                # 断点续跑：跳过已完成
                to_process = [qid for qid in all_qids if qid not in completed_qids]
                if completed_qids:
                    logger.info(f"[Round 0] Checkpoint resume: {len(completed_qids)} already done, {len(to_process)} to process")
                else:
                    logger.info(f"[Round 0] Processing all {len(to_process)} questions")
        else:
            # 重试轮：处理 errors.json 中的失败题目
            to_process = error_manager.get_failed_ids()
            if not to_process:
                logger.info(f"[Round {round_idx}] No errors to retry, done!")
                break
            retries_used = round_idx
            logger.info(f"[Round {round_idx}] Retrying {len(to_process)} failed questions")

        if not to_process:
            logger.info(f"[Round {round_idx}] Nothing to process")
            continue

        # ── 7. 并行处理 ──
        round_completed = 0
        round_errors = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for qid in to_process:
                # 跳过不在 ddl_schemas 中的题目
                if qid not in ddl_schemas:
                    logger.warning(f"{qp(qid)}Not in DDL schemas, skipping")
                    continue
                data_item = build_data_item(qid, dev_questions, ddl_schemas)
                db_id = dev_questions[qid]["db_id"]
                fut = pool.submit(
                    _process_single_question, qid, data_item, db_id,
                    llm, run_id, fr_force_on,
                )
                futures[fut] = qid

            for fut in as_completed(futures):
                qid = futures[fut]
                try:
                    fut.result()
                    error_manager.remove(qid)
                    round_completed += 1
                    logger.info(f"{qp(qid)}Completed ({round_completed}/{len(futures)})")
                except (LLMMaxRetriesExceeded, LLMParseMaxRetriesExceeded) as e:
                    error_manager.add(qid, f"{type(e).__name__}: {e}")
                    round_errors += 1
                    logger.warning(f"{qp(qid)}LLM error, skipped: {type(e).__name__}: {e}")
                except RouteBudgetUnmetError as e:
                    # 单路 SQL 数量未达 budget → 显式标记整题失败入 errors.json
                    error_manager.add(
                        qid,
                        f"RouteBudgetUnmetError[route={e.route_name},phase={e.phase},"
                        f"got={e.actual}/{e.budget},attempts={e.attempts}]"
                    )
                    round_errors += 1
                    logger.warning(
                        f"{qp(qid)}Route budget unmet, skipped: "
                        f"route={e.route_name} phase={e.phase} "
                        f"got={e.actual}/{e.budget} after {e.attempts} attempts"
                    )
                except Exception as e:
                    error_manager.add(qid, f"{type(e).__name__}: {e}")
                    round_errors += 1
                    logger.error(f"{qp(qid)}Unexpected error: {type(e).__name__}: {e}\n{traceback.format_exc()}")

        logger.info(f"[Round {round_idx}] Completed: {round_completed}, Errors: {round_errors}")

    # ── 8. 统计摘要 ──
    final_completed = len(list(sel_dir.glob("q_*_selected.json")))
    final_errors = error_manager.count()
    elapsed = time.time() - t0

    summary = {
        "total": total,
        "completed": final_completed,
        "errors": final_errors,
        "retries_used": retries_used,
        "elapsed_seconds": round(elapsed, 1),
    }

    logger.info("=" * 60)
    logger.info("  Step3b 执行完成")
    logger.info(f"  Run ID       : {run_id}")
    logger.info(f"  Total        : {total}")
    logger.info(f"  Completed    : {final_completed}")
    logger.info(f"  Errors       : {final_errors}")
    logger.info(f"  Retries used : {retries_used}")
    logger.info(f"  Elapsed      : {elapsed:.1f}s")
    logger.info("=" * 60)

    return summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置覆盖（命令行 > 环境变量 > 配置文件默认值）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _apply_config_overrides(args) -> None:
    """根据命令行参数覆盖 config 模块属性。

    必须在 parse_args() 之后、setup_logging / 任何数据加载之前调用。
    通过直接修改 config 模块属性，所有下游模块（data_loader、ExemplarGenerator 等）
    在运行时读取时自动使用新值，无需传参。

    优先级（高→低）:
        --dev-json-file / --bird-db-dir / --few-shot-file
        > --bird-data-dir 派生值
        > --dev-json（旧参数，兼容保留）
        > config.py 默认值

    Raises:
        FileNotFoundError: 指定的文件不存在
        ValueError: 指定的目录不存在或文件格式不符
    """
    overrides: dict = {}

    # ── 1. --bird-data-dir: 最先处理，为其他路径提供基础默认值 ──
    if args.bird_data_dir:
        d = Path(args.bird_data_dir)
        if not d.is_dir():
            raise ValueError(f"--bird-data-dir 目录不存在: {d}")
        config.BIRD_DATA_DIR = d
        overrides["BIRD_DATA_DIR"] = str(d)
        # 仅当更高优先级参数未显式指定时，从 bird_data_dir 派生
        if not args.dev_json_file and not args.dev_json:
            config.DEV_JSON = str(d / "dev.json")
            overrides["DEV_JSON"] = config.DEV_JSON
        if not args.bird_db_dir:
            # 自动检测数据库子目录（兼容 dev_databases / train_databases）
            db_dir = None
            for candidate in ("dev_databases", "train_databases"):
                if (d / candidate).is_dir():
                    db_dir = d / candidate
                    break
            if db_dir is None:
                raise ValueError(
                    f"--bird-data-dir 下未找到数据库子目录 "
                    f"(dev_databases 或 train_databases): {d}"
                )
            config.BIRD_DB_DIR = str(db_dir)
            overrides["BIRD_DB_DIR"] = config.BIRD_DB_DIR
        # BIRD_TABLES_JSON 同步更新（自动检测 dev_tables.json / train_tables.json）
        tables_json = None
        for candidate in (
            d / "dev_tables.json",
            d / "train_tables.json",
            d / "dev_databases" / "dev_tables.json",
            d / "train_databases" / "train_tables.json",
        ):
            if candidate.exists():
                tables_json = candidate
                break
        if tables_json:
            config.BIRD_TABLES_JSON = str(tables_json)
            overrides["BIRD_TABLES_JSON"] = config.BIRD_TABLES_JSON

    # ── 2. --bird-db-dir: 覆盖 bird_data_dir 派生的 BIRD_DB_DIR ──
    if args.bird_db_dir:
        d = Path(args.bird_db_dir)
        if not d.is_dir():
            raise ValueError(f"--bird-db-dir 目录不存在: {d}")
        config.BIRD_DB_DIR = str(d)
        overrides["BIRD_DB_DIR"] = str(d)

    # ── 3. --dev-json-file: 最高优先级的题目文件覆盖 ──
    if args.dev_json_file:
        p = Path(args.dev_json_file)
        if not p.exists():
            raise FileNotFoundError(f"--dev-json-file 文件不存在: {p}")
        if p.suffix.lower() != ".json":
            raise ValueError(f"--dev-json-file 必须是 .json 文件: {p}")
        config.DEV_JSON = str(p)
        overrides["DEV_JSON"] = str(p)
    elif args.dev_json:
        # 兼容旧参数 --dev-json，统一写入 config.DEV_JSON
        p = Path(args.dev_json)
        if not p.exists():
            raise FileNotFoundError(f"--dev-json 文件不存在: {p}")
        config.DEV_JSON = str(p)
        overrides.setdefault("DEV_JSON", str(p))

    # ── 4. --few-shot-file: ExemplarGenerator 在运行时读取 config.FEW_SHOT_PATH ──
    if args.few_shot_file:
        p = Path(args.few_shot_file)
        if not p.exists():
            raise FileNotFoundError(f"--few-shot-file 文件不存在: {p}")
        if p.suffix.lower() != ".json":
            raise ValueError(f"--few-shot-file 必须是 .json 文件: {p}")
        config.FEW_SHOT_PATH = str(p)
        overrides["FEW_SHOT_PATH"] = str(p)

    if overrides:
        print(f"[Config Override] {len(overrides)} 个配置项已通过命令行参数覆盖:", flush=True)
        for k, v in overrides.items():
            print(f"  {k} = {v}", flush=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(
        description="Step3b: 单组合全流程 Runner（Generation → Selection）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--run-id", type=str, default=config.STEP3B_RUN_ID,
        help=f"运行 ID（默认: {config.STEP3B_RUN_ID}）",
    )
    parser.add_argument(
        "--schema-json", type=str, default=config.STEP3B_SCHEMA_JSON,
        help=f"DDL schema 文件名（默认: {config.STEP3B_SCHEMA_JSON}）",
    )
    parser.add_argument(
        "--llm-provider", type=str, default=config.STEP3B_LLM_PROVIDER,
        help=f"LLM 供应商（默认: {config.STEP3B_LLM_PROVIDER or config.LLM_PROVIDER}）",
    )
    parser.add_argument(
        "--fr-force-on", action="store_true", default=config.STEP3B_FR_FORCE_ON,
        help="FR 强制模式（强制所有非 single 题执行 full_review）",
    )
    parser.add_argument(
        "--max-workers", type=int, default=config.STEP3B_MAX_WORKERS,
        help=f"题目级并行度（默认: {config.STEP3B_MAX_WORKERS}）",
    )
    parser.add_argument(
        "--max-retries", type=int, default=config.STEP3B_MAX_GLOBAL_RETRIES,
        help=f"全局重试轮数（默认: {config.STEP3B_MAX_GLOBAL_RETRIES}）",
    )
    parser.add_argument(
        "--force-rerun", action="store_true", default=False,
        help="全量重跑：忽略已存在的 selector 输出，对所有题重新跑 generation+selection。"
             "\n  与 --rerun-qids 互斥，后者优先级更高。",
    )
    parser.add_argument(
        "--rerun-qids", type=str, default=None,
        metavar="QIDS",
        help="单题重跑：仅重跑指定题号集合（逗号/空格/分号分隔），例如 "
             "--rerun-qids \"42,108,256\" 。启用后忽略 completed 状态及 --force-rerun。",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="启用 DEBUG 级别日志",
    )
    # ── 数据源路径覆盖（命令行 > 环境变量 > config.py 默认值）──
    parser.add_argument(
        "--dev-json", type=str, default=None,
        metavar="PATH",
        help="[旧参数，兼容保留] 题目文件路径，优先使用 --dev-json-file",
    )
    parser.add_argument(
        "--dev-json-file", type=str, default=None,
        metavar="PATH",
        help=f"题目文件路径，覆盖 config.DEV_JSON"
             f"（默认: {config.DEV_JSON}）"
             f"\n  场景1: dev_diff.json 差异题目重跑"
             f"\n  场景2: train 目录下的训练题目文件",
    )
    parser.add_argument(
        "--few-shot-file", type=str, default=None,
        metavar="PATH",
        help=f"Few-shot 示例文件路径，覆盖 config.FEW_SHOT_PATH"
             f"（默认: {config.FEW_SHOT_PATH}）"
             f"\n  如需使用 train cross-domain few-shot，指向 few_shot_train_crossdomain.json",
    )
    parser.add_argument(
        "--bird-db-dir", type=str, default=None,
        metavar="DIR",
        help=f"BIRD SQLite 数据库目录，覆盖 config.BIRD_DB_DIR"
             f"（默认: {config.BIRD_DB_DIR}）"
             f"\n  场景2: 指向 train 数据集的 train_databases 目录",
    )
    parser.add_argument(
        "--bird-data-dir", type=str, default=None,
        metavar="DIR",
        help=f"BIRD 数据集根目录，同时派生 DEV_JSON / BIRD_DB_DIR / BIRD_TABLES_JSON"
             f"（默认: {config.BIRD_DATA_DIR}）"
             f"\n  优先级低于各独立参数；场景2 可直接指向 train 目录",
    )
    args = parser.parse_args()

    # ── 解析 --rerun-qids（在 setup_logging 前完成，方便快速失败）──
    try:
        rerun_qids_set = _parse_qid_list(args.rerun_qids)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    if rerun_qids_set and args.force_rerun:
        print(
            "[WARN] --rerun-qids 与 --force-rerun 同时指定，将仅使用 --rerun-qids （单题优先）",
            file=sys.stderr, flush=True,
        )

    # ── 覆盖 config（必须在 setup_logging 和任何数据加载之前）──
    try:
        _apply_config_overrides(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] 参数验证失败: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    # 日志初始化（依赖 run_id，在 config 覆盖之后）
    log_file = setup_logging(config.STEP3B_LOG_DIR, args.run_id, "step3b", verbose=args.verbose)

    logger.info("=" * 60)
    logger.info("  Step3b: 单组合全流程 Runner")
    logger.info(f"  Time         : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  Run ID       : {args.run_id}")
    logger.info(f"  Dev JSON     : {config.DEV_JSON}")
    logger.info(f"  DB Dir       : {config.BIRD_DB_DIR}")
    logger.info(f"  Few-Shot     : {config.FEW_SHOT_PATH}")
    logger.info(f"  Schema       : {args.schema_json}")
    logger.info(f"  LLM Provider : {args.llm_provider or config.LLM_PROVIDER}")
    logger.info(f"  FR Force On  : {args.fr_force_on}")
    logger.info(f"  Max Workers  : {args.max_workers}")
    logger.info(f"  Max Retries  : {args.max_retries}")
    if rerun_qids_set:
        logger.info(f"  Rerun QIDs   : {sorted(rerun_qids_set)} ({len(rerun_qids_set)} questions)")
    elif args.force_rerun:
        logger.info(f"  Force Rerun  : ON (full re-run, ignoring completed)")
    else:
        logger.info(f"  Resume Mode  : ON (skip completed)")
    logger.info(f"  CoT Output   : {'ON' if getattr(config, 'COT_OUTPUT_ENABLED', False) else 'OFF'}")
    logger.info(f"  Log File     : {log_file}")
    logger.info("=" * 60)

    summary = run_all_questions(
        run_id=args.run_id,
        schema_json=args.schema_json,
        llm_provider=args.llm_provider,
        fr_force_on=args.fr_force_on,
        max_workers=args.max_workers,
        max_global_retries=args.max_retries,
        dev_json=None,  # 已通过 _apply_config_overrides 写入 config.DEV_JSON
        force_rerun=args.force_rerun,
        rerun_qids=rerun_qids_set,
    )

    # ── 日志聚合 ──
    from ..common.log_aggregator import aggregate_log
    aggregated_log = aggregate_log(log_file)
    logger.info(f"Aggregated log: {aggregated_log}")

    if summary["errors"] > 0:
        logger.warning(f"{summary['errors']} question(s) still have errors after all retries")
        sys.exit(1)


if __name__ == "__main__":
    main()
