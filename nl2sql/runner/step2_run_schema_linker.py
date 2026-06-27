"""
Schema Linking Pipeline 一键执行脚本（Runner 入口）

按顺序执行全部 9 个步骤（Step2a ~ Step2i）：
  Step2a:  数据集预处理 (加载 dev.json + SQLite schema)
  Step2b:  关键词抽取 (evidence parsing + keyword extraction)
  Step2c:  列名语义匹配 (column semantic matching)
  Step2d:  LLM 直接匹配 (LLM direct matching)
  Step2e:  列值匹配 (evidence value validation via LSH + enum desc)
  Step2f:  值检索匹配 (value retrieval via vector + LSH + enum-desc)
  Step2g:  SQL 逆向验证 (SQL generation validation)
  Step2h:  连接关系闭包 (join relation closure)
  Step2i:  格式化输出 (DDL format output)

使用方式（命令行参数使用 Step2a~Step2i 子步骤 ID）：
    python -m nl2sql.runner.step2_run_schema_linker                     # 执行全部步骤 (Step2a~Step2i)
    python -m nl2sql.runner.step2_run_schema_linker --only-step 2d      # 只执行 Step2d
    python -m nl2sql.runner.step2_run_schema_linker --step-range 2b-2f  # 执行 Step2b 到 Step2f（包含 2f）
    python -m nl2sql.runner.step2_run_schema_linker --resume            # Step2b-Step2h 断点续传
    python -m nl2sql.runner.step2_run_schema_linker --limit 10          # 只处理前10个问题（全链路）
    python -m nl2sql.runner.step2_run_schema_linker --question-ids 89   # 只处理指定 question_id 全链路（Step2a~2i）
    python -m nl2sql.runner.step2_run_schema_linker --verbose           # 启用详细日志
    python -m nl2sql.runner.step2_run_schema_linker --list-steps        # 列出所有步骤及状态

参数:
    --only-step 2x:       只执行单个 Step2 子步骤 (2a-2i)
    --step-range 2x-2y:   执行 Step2x 到 Step2y（包含 2y），格式支持 2a-2f 或 2a,2f
    --resume, -r: Step2b-Step2h 断点续传（从各步骤输出文件恢复）
    --limit N, -l: 只处理前N个问题（用于测试）
    --question-range start,end: 传递给 Step2b-Step2h pending_items 的 question_id 范围[start,end)
    --question-ids 1,3,5: 传递给 Step2b-Step2h，仅重跑指定 question_id
    --verbose, -v: 启用详细日志输出
    --list-steps: 列出所有步骤及其状态
"""
import sys
import time
import argparse
import subprocess
import logging
import re
import json
import pickle
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nl2sql.schema_linker.utils import get_log_file_path, parse_range_arg, parse_question_ids_arg, get_error_questions_path, update_step2_state
from nl2sql import config


SCHEMA_LINKER_DIR = Path(__file__).resolve().parents[1] / "schema_linker"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path("run_schema_linker"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _read_error_questions(step_tag: str) -> list:
    path = Path(get_error_questions_path(step_tag))
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _verify_step2a(
    limit: int | None,
    question_range: str | None = None,
    question_ids: str | None = None,
) -> bool:
    """验证 Step2a 输出条数是否与输入 dev.json 在相同步过滤条件下的条数一致。

    - 当未指定 question_range / question_ids 时：expected = len(dev.json) 或受 limit 约束
    - 当指定 question_range / question_ids 时：在 dev.json 上应用同样的过滤规则后再比较
    """

    dev_path = Path(config.DEV_JSON)
    out_path = Path(config.STEP2A_DATASET_SAVE_PATH)
    if not dev_path.exists() or not out_path.exists():
        return False

    with open(dev_path, "r", encoding="utf-8") as f:
        dev_data = json.load(f)
    if not isinstance(dev_data, list):
        return False

    # 在原始 dev.json 上应用与 Step2a 一致的过滤语义
    filtered = dev_data

    if question_range:
        qid_range = parse_range_arg(str(question_range))
        if not qid_range:
            return False
        start_qid, end_qid = qid_range

        def _in_range(item: dict) -> bool:
            qid = item.get("question_id")
            return isinstance(qid, int) and start_qid <= qid < end_qid

        filtered = [item for item in filtered if _in_range(item)]

    if question_ids:
        qids = parse_question_ids_arg(str(question_ids))
        if not qids:
            return False
        qid_set = set(int(q) for q in qids)

        def _in_ids(item: dict) -> bool:
            qid = item.get("question_id")
            return isinstance(qid, int) and qid in qid_set

        filtered = [item for item in filtered if _in_ids(item)]

    expected = len(filtered)
    if isinstance(limit, int) and limit and limit > 0:
        expected = min(expected, limit)

    with open(out_path, "rb") as f:
        dataset = pickle.load(f)
    actual = len(dataset) if isinstance(dataset, list) else 0

    logger.info(
        f"[VERIFY] Step2a: expected={expected}, actual={actual}, "
        f"limit={limit}, question_range={question_range}, question_ids={question_ids}"
    )
    return actual == expected


def _verify_step2i_output(output_path: str | None = None) -> bool:
    """验证 Step2i 最终输出数据集是否存在且非空。
    Step2i 对应的输出目录（config.STEP2I_OUTPUT_DIR）。
    """
    default_output = Path(config.STEP2I_OUTPUT_DIR) / "dataset.pkl"
    output_path = Path(output_path) if output_path else default_output
    return output_path.exists() and output_path.stat().st_size > 0


def _path_has_entries(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False
    except Exception:
        return False


def _collect_missing_step1_artifacts() -> list[str]:
    missing = []
    cache_paths = [
        Path(config.STEP1_CACHE_DIR) / "table_list_cache.json",
        Path(config.STEP1_CACHE_DIR) / "table_columns_info_cache.json",
        Path(config.STEP1_CACHE_DIR) / "columns_sample_values_cache.json",
    ]
    for cache_path in cache_paths:
        if not cache_path.exists():
            missing.append(str(cache_path))
            continue
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not payload:
                missing.append(str(cache_path))
        except Exception:
            missing.append(str(cache_path))

    dir_paths = [
        Path(config.STEP1_COLUMN_VECTOR_STORE_DIR),
        Path(config.STEP1_JOIN_RELATIONS_DIR),
        Path(config.STEP1_LSH_INDEX_DIR),
        Path(config.STEP1_VALUE_DESC_VECTOR_DIR),
        Path(config.STEP1_VALUE_VECTOR_STORE_DIR),
    ]
    for dir_path in dir_paths:
        if not _path_has_entries(dir_path):
            missing.append(str(dir_path))

    return missing


def _post_check_and_maybe_rerun(step_id: str, script_name: str, args: argparse.Namespace) -> None:
    """Step-level自动校验和必要时的单步重跑。

    step_id 使用 Step2a~Step2i 命名；底层脚本文件名仍为 step0/step3/.. ，二者通过 STEPS 映射。
    """

    if step_id == "2a":
        logger.info("[VERIFY] Step2a: start validation")
        ok = _verify_step2a(
            args.limit,
            getattr(args, "question_range", None),
            getattr(args, "question_ids", None),
        )
        if ok:
            logger.info("[VERIFY] Step2a: validation passed")
            return
        logger.error(
            "[VERIFY] Step2a failed: output dataset count != input dev json count (after filters/limit)"
        )
        logger.info("[VERIFY] Rerunning Step2a once...")
        args2 = argparse.Namespace(**vars(args))
        setattr(args2, "_skip_post_check", True)
        run_step(step_id, *STEP_DEFINITIONS[step_id], args2)
        logger.info("[VERIFY] Step2a: start validation after rerun")
        ok2 = _verify_step2a(
            args.limit,
            getattr(args2, "question_range", None),
            getattr(args2, "question_ids", None),
        )
        if ok2:
            logger.info("[VERIFY] Step2a passed after rerun")
            return
        msg = "[VERIFY] Step2a still failing after rerun"
        logger.error(msg)
        logger.info(
            "[VERIFY] Step2a conclusion: proceed to next step (errors should be recorded in step2a_error_questions.json)"
        )
        return

    if step_id in {"2b", "2c", "2d", "2e", "2f", "2g", "2h"}:
        logger.info(f"[VERIFY] Step{step_id}: start validation (error questions)")
        errors = _read_error_questions(step_id)
        if not errors:
            logger.info(f"[VERIFY] Step{step_id}: validation passed (no error questions)")
            return
        qids = []
        for it in errors:
            if isinstance(it, dict) and isinstance(it.get("question_id"), int):
                qids.append(int(it.get("question_id")))
        qids = sorted(set(qids))
        logger.info(f"[VERIFY] Step{step_id}: validation failed (error questions={len(errors)})")
        logger.info(f"[VERIFY] Step{step_id}: error qids count={len(qids)}")
        logger.info(f"[VERIFY] Step{step_id}: error qids(all)={qids}")
        logger.info(
            f"[VERIFY] Step{step_id}: conclusion: will NOT continue auto-rerun (each step already reruns once internally); proceed to next step"
        )
        return

    if step_id == "2i":
        logger.info("[VERIFY] Step2i: start validation")
        if _verify_step2i_output(getattr(args, "output", None)):
            logger.info("[VERIFY] Step2i: validation passed")
            return
        logger.error("[VERIFY] Step2i failed: final output dataset missing or empty")


def _parse_step_range_arg(step_range_arg: str):
    """解析类似 "2a-2i" 或 "2a,2f" 的 step 范围参数，返回 (start_id, end_id)。"""
    if not step_range_arg:
        return None
    raw = str(step_range_arg).strip().lower()
    sep = "-" if ("-" in raw and "," not in raw) else ","
    parts = [p.strip() for p in raw.split(sep)]
    if len(parts) != 2:
        return None
    start, end = parts[0], parts[1]
    return start, end


# 步骤定义：Step2a~Step2i 的 id、实际脚本文件名与描述
STEPS = [
    ("2a", "step2a_load_dataset.py", "Step2a: 数据集预处理"),
    ("2b", "step2b_keywords_and_retrieval.py", "Step2b: 关键词抽取与值检索"),
    ("2c", "step2c_column_match_linker.py", "Step2c: 列语义匹配召回"),
    ("2d", "step2d_llm_direct_linker.py", "Step2d: LLM直接召回"),
    ("2e", "step2e_value_match_linker.py", "Step2e: 值匹配召回 (LSH + 枚举值描述)"),
    ("2f", "step2f_value_retrieval_linker.py", "Step2f: 值检索阈值召回"),
    ("2g", "step2g_sql_reversed_linker.py", "Step2g: SQL反向召回"),
    ("2h", "step2h_join_closure_linker.py", "Step2h: Join关系召回"),
    ("2i", "step2i_format_output.py", "Step2i: 格式化输出"),
]
STEP_DEFINITIONS = {step_id: (script, desc) for step_id, script, desc in STEPS}
AVAILABLE_STEP_IDS = [step_id for step_id, _, _ in STEPS]


def run_step(step_id: str, script_name: str, description: str, args: argparse.Namespace):
    """执行单个 pipeline 步骤（含日志与错误处理）"""
    script_path = SCHEMA_LINKER_DIR / script_name

    # 检查脚本是否存在
    if not script_path.exists():
        logger.warning(f"[SKIP] {description} - 脚本不存在: {script_name}")
        return

    logger.info(f"{'='*60}")
    logger.info(f"  {description} ({step_id})")
    logger.info(f"  Running: python -m nl2sql.schema_linker.{script_name[:-3]}, more details see {script_name[:-3]}.log")
    logger.info(f"{'='*60}")

    # 在共享 Step2 state 中记录一次 run_start 事件（尽力而为，失败不影响主流程）
    try:
        state_args = {
            "step_id": step_id,
            "resume": bool(getattr(args, "resume", False)),
            "limit": getattr(args, "limit", None),
            "question_range": getattr(args, "question_range", None),
            "question_ids": getattr(args, "question_ids", None),
            "step_range": getattr(args, "step_range", None),
            "only_step": getattr(args, "only_step", None),
            "force_rerun_state": bool(getattr(args, "force_rerun_state", False)) if hasattr(args, "force_rerun_state") else False,
        }
        update_step2_state(
            step_id=step_id,
            name=description,
            status="running",
            event="run_start",
            args=state_args,
        )
    except Exception:
        # state 更新绝不可中断主流程
        pass

    # 构建命令行参数 — 使用 -m 模式运行，以支持相对导入
    module_name = f"nl2sql.schema_linker.{script_name[:-3]}"
    cmd = [sys.executable, "-m", module_name]

    # resume 仅对 Step2b~2h 生效
    if getattr(args, "resume", False) and step_id in {"2b", "2c", "2d", "2e", "2f", "2g", "2h"}:
        cmd.append("--resume")
    has_question_ids = bool(getattr(args, "question_ids", None))
    # limit 对所有步骤生效（包含 Step2a），便于快速调试
    if args.limit is not None and not (step_id in {"2b", "2c", "2d", "2e", "2f", "2g", "2h"} and has_question_ids):
        cmd.extend(["--limit", str(args.limit)])
    # question-range / question-ids 需要传给 Step2a~2h，使得“单题/子集”从头到尾保持一致
    if (not has_question_ids) and getattr(args, "question_range", None) and step_id in {"2a", "2b", "2c", "2d", "2e", "2f", "2g", "2h"}:
        cmd.extend(["--question-range", str(args.question_range)])
    if has_question_ids and step_id in {"2a", "2b", "2c", "2d", "2e", "2f", "2g", "2h"}:
        cmd.extend(["--question-ids", str(args.question_ids)])
    if args.verbose:
        cmd.append("--verbose")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parents[2]),  # metavisor/ 目录
            capture_output=args.verbose is False,  # 只在非详细模式下捕获输出
            text=True,
        )
        elapsed = time.time() - start

        if result.returncode != 0:
            logger.error(f"[ERROR] {description} failed (exit code {result.returncode}), elapsed {elapsed:.1f}s")
            if hasattr(result, "stderr") and result.stderr:
                logger.error(f"错误信息: {result.stderr}")
            sys.exit(result.returncode)
        logger.info(" ")
        logger.info(f"[OK] {description} completed in {elapsed:.1f}s")
        logger.info(" ")

        # 为不在内部维护 state 的步骤更新 Step2 state 面板
        try:
            if step_id == "2a":
                out_path = Path(config.STEP2A_DATASET_SAVE_PATH)
                if out_path.exists():
                    with out_path.open("rb") as f:
                        dataset = pickle.load(f)
                    total = len(dataset) if isinstance(dataset, list) else 0
                    update_step2_state(
                        step_id="2a",
                        name="dataset",
                        status="done",
                        completed_questions=total,
                        total_questions=total,
                    )
            elif step_id == "2i":
                out_path = Path(config.STEP2I_OUTPUT_DIR) / "dataset.pkl"
                if out_path.exists():
                    with out_path.open("rb") as f:
                        dataset = pickle.load(f)
                    total = len(dataset) if isinstance(dataset, list) else 0
                    update_step2_state(
                        step_id="2i",
                        name="output",
                        status="done",
                        completed_questions=total,
                        total_questions=total,
                    )
        except Exception:
            # 尽力而为；state 更新失败不应中断 runner
            pass

        if not getattr(args, "_skip_post_check", False):
            _post_check_and_maybe_rerun(step_id, script_name, args)
            logger.info(" ")

    except KeyboardInterrupt:
        logger.error("[INTERRUPTED] 用户中断执行")
        sys.exit(1)
    except Exception as e:
        logger.error(f"[ERROR] 执行脚本时发生异常: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Schema Linking Pipeline 一键执行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""使用示例:
  python -m nl2sql.runner.step2_run_schema_linker                     # 执行全部步骤 (Step2a~Step2i)
  python -m nl2sql.runner.step2_run_schema_linker --only-step 2d      # 只执行 Step2d
  python -m nl2sql.runner.step2_run_schema_linker --step-range 2b-2f  # 只执行 Step2b 到 Step2f（包含 2f）
  python -m nl2sql.runner.step2_run_schema_linker --resume            # Step3-Step9 断点续传
  python -m nl2sql.runner.step2_run_schema_linker --question-range 0,10  # 只处理 question_id in [0,10)（仅 step3-step9 生效）
  python -m nl2sql.runner.step2_run_schema_linker --question-ids 1,3,5   # 只处理指定 question_id（仅 step3-step9 生效）
  python -m nl2sql.runner.step2_run_schema_linker --limit 10          # 只处理前10个问题
  python -m nl2sql.runner.step2_run_schema_linker --verbose           # 启用详细日志
  python -m nl2sql.runner.step2_run_schema_linker --continue-on-error # 遇到错误继续执行下一步""",
    )

    step_group = parser.add_mutually_exclusive_group()
    step_group.add_argument("--only-step", type=str, default=None, help="只执行单个 Step2 子步骤（可用: 2a-2i）")
    step_group.add_argument("--step-range", type=str, default=None, help="只执行步骤范围（包含终点），格式 2a-2f 或 2a,2f")
    parser.add_argument("--resume", "-r", action="store_true", default=False, help="断点续传（仅 Step2b-Step2h 支持）")
    limit_range_group = parser.add_mutually_exclusive_group()
    limit_range_group.add_argument("--limit", "-l", type=int, default=None, help="只处理前N个问题（用于测试）")
    limit_range_group.add_argument(
        "--question-range",
        dest="question_range",
        type=str,
        default=None,
        help="只处理 pending_items 的 question_id 范围[start,end)，格式: start,end（仅 step3-step9 生效）",
    )
    limit_range_group.add_argument(
        "--question-ids",
        dest="question_ids",
        type=str,
        default=None,
        help="Comma-separated question_ids to process (e.g. '1,3,5') (仅 step3-step9 生效)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", default=False, help="启用详细日志输出")
    parser.add_argument("--list-steps", action="store_true", help="检查所有步骤脚本是否存在")
    parser.add_argument("--force-rerun-state", action="store_true", default=False, help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.list_steps:
        logger.info("Schema Linking Pipeline 步骤列表 (Step2a~Step2i):")
        for step_id, script, desc in STEPS:
            status = "✓" if SCHEMA_LINKER_DIR.joinpath(script).exists() else "✗"
            logger.info(f"  Step {step_id}: {desc:<40} [{script}] {status}")
        return

    # 可选：本次运行前重置共享的 Step2 state.json（历史 + 计数器）
    if getattr(args, "force_rerun_state", False):
        state_path = getattr(config, "STEP2_STATE_PATH", None)
        if state_path:
            try:
                if os.path.exists(state_path):
                    os.remove(state_path)
                    logger.info("[STATE] Reset Step2 state file: %s", state_path)
            except Exception as exc:
                logger.warning("[STATE] Failed to reset Step2 state file %s: %s", state_path, exc)

    if getattr(args, "question_range", None):
        qid_range = parse_range_arg(getattr(args, "question_range", None))
        if not qid_range:
            logger.error(f"错误: 无效 --question-range 参数 {args.question_range!r}, 期望格式 'start,end' (例如 0,10)")
            sys.exit(1)

    if getattr(args, "question_ids", None):
        qids = parse_question_ids_arg(getattr(args, "question_ids", None))
        if not qids:
            logger.error(f"错误: 无效 --question-ids 参数 {args.question_ids!r}, 期望格式 '1,3,5'")
            sys.exit(1)

    logger.info(" ")
    logger.info("Schema Linking Pipeline 开始执行 (Step2a~Step2i)...")
    logger.info(
        f"参数: resume={getattr(args, 'resume', False)}, limit={args.limit}, "
        f"question range={getattr(args, 'question_range', None)}, question_ids={getattr(args, 'question_ids', None)}, step_range={getattr(args, 'step_range', None)}, only_step={getattr(args, 'only_step', None)}, verbose={args.verbose}"
    )

    selected_steps: list[tuple[str, str, str]] = []
    if args.only_step is not None:
        key = str(args.only_step).lower()
        if key in STEP_DEFINITIONS:
            script, desc = STEP_DEFINITIONS[key]
            selected_steps = [(key, script, desc)]
        else:
            logger.error(f"错误: 无效步骤 {args.only_step!r}, 可用步骤为 {AVAILABLE_STEP_IDS}")
            sys.exit(1)
    elif getattr(args, "step_range", None):
        step_range = _parse_step_range_arg(getattr(args, "step_range", None))
        if not step_range:
            logger.error(f"错误: 无效 --step-range 参数 {args.step_range!r}, 期望格式 '2a-2f' 或 '2a,2f'")
            sys.exit(1)
        start_id, end_id = [p.lower() for p in step_range]
        order = {sid: idx for idx, (sid, _, _) in enumerate(STEPS)}
        if start_id not in order or end_id not in order:
            logger.error(f"错误: 无效步骤范围 {args.step_range!r}, 可用步骤为 {AVAILABLE_STEP_IDS}")
            sys.exit(1)
        if order[start_id] > order[end_id]:
            logger.error(f"错误: 无效步骤范围 {args.step_range!r}, start > end")
            sys.exit(1)
        selected_steps = [
            (step_id, script, desc)
            for step_id, script, desc in STEPS
            if order[start_id] <= order[step_id] <= order[end_id]
        ]
    else:
        # 默认执行 Step2a~Step2i 全链路
        selected_steps = list(STEPS)

    if any(step_id in {"2b", "2c", "2d", "2e", "2f", "2g", "2h", "2i"} for step_id, _, _ in selected_steps):
        missing_artifacts = _collect_missing_step1_artifacts()
        if missing_artifacts:
            logger.error("缺少 Step1 本地预处理产物，Schema Linker 无法继续执行。")
            for missing_path in missing_artifacts:
                logger.error(f"  missing: {missing_path}")
            logger.error("请先运行 python -m nl2sql.runner.step1_run_preprocess")
            sys.exit(1)

    total_start = time.time()
    executed_steps = 0
    for step_id, script, desc in selected_steps:
        run_step(step_id, script, desc, args)
        executed_steps += 1

    total_elapsed = time.time() - total_start
    logger.info(f"{'='*60}")
    logger.info(
        f"  执行完成! 共执行 {executed_steps} 个步骤，每步骤跑完 limit={args.limit}, question_range={getattr(args, 'question_range', None)}, question_ids={getattr(args, 'question_ids', None)}"
    )
    logger.info(f"  总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")
    logger.info(f"{'='*60}")
    logger.info("\n")


if __name__ == "__main__":
    main()
