"""批量评测编排器 — 读取 selector 输出，执行 BIRD 正确性验证

evaluate(run_id): 读取 {run_id}/selector/ 下的 q_XXXX_selected*.json，
对 top1_sql / full_review_sql / selector_output_sql 执行 eval_ex，
增量写入 {run_id}/evaluation/q_XXXX_eval.json

支持断点续跑：跳过已存在的 eval JSON。

命令行使用示例：
  # 评测指定 run_id（使用默认 dev.json）
  python -m nl2sql.sql_evaluator.sql_evaluator --run-id {RUN_ID}
"""
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Any, Optional

from .. import config
from .bird_evaluation import eval_ex

logger = logging.getLogger(__name__)

# 需要评测的 SQL 字段 → 对应的 *_correct 字段
_SQL_FIELDS = [
    ("top1_sql", "top1_correct"),
    ("full_review_sql", "full_review_correct"),
    ("selector_output_sql", "selector_output_correct"),
]


def _load_gold_sqls(dev_json: str = None) -> Dict[int, Dict[str, str]]:
    """从 dev.json 加载 golden SQL 映射 {question_id: {"SQL": ..., "db_id": ...}}

    Args:
        dev_json: 自定义 dev.json 路径，None 使用 config.DEV_JSON
    """
    path = dev_json or config.DEV_JSON
    with open(path, "r", encoding="utf-8") as f:
        dev_data = json.load(f)
    return {item["question_id"]: item for item in dev_data}


def _build_db_path(db_id: str) -> str:
    """构建数据库文件路径"""
    return str(Path(config.BIRD_DB_DIR) / db_id / f"{db_id}.sqlite")


def _eval_file_name(selector_filename: str) -> str:
    """selector 文件名 → eval 文件名: q_XXXX_selected*.json → q_XXXX_eval.json"""
    # 提取 q_XXXX 部分
    base = selector_filename
    if base.startswith("q_") and "_selected" in base:
        q_part = base.split("_selected")[0]  # "q_0721"
        return f"{q_part}_eval.json"
    # fallback: 直接替换后缀
    return base.replace(".json", "_eval.json")


def evaluate(
    run_id: str,
    timeout: int = None,
    force_rerun: bool = False,
    dev_json: str = None,
) -> Dict[str, Any]:
    """单组合评测：读取 selector 输出，执行正确性验证，写入 evaluation 目录

    Args:
        run_id: 运行 ID（对应 workspace/sql_generation/{run_id}/ 目录）
        timeout: SQL 执行超时（秒），None 使用 config.EVAL_SQL_TIMEOUT
        force_rerun: 是否强制重新评测（忽略已有的 eval 文件）
        dev_json: 自定义 dev.json 路径，None 使用 config.DEV_JSON

    Returns:
        统计摘要 {"total": N, "evaluated": N, "skipped": N, "results": {...}}
    """
    if timeout is None:
        timeout = config.EVAL_SQL_TIMEOUT

    # 1. 加载 golden SQL
    gold_sqls = _load_gold_sqls(dev_json)

    # 2. 定位 selector 输出目录
    selector_dir = config.GENERATION_OUTPUT_DIR / run_id / "selector"
    eval_dir = config.GENERATION_OUTPUT_DIR / run_id / "evaluation"

    if not selector_dir.exists():
        logger.error(f"Selector directory not found: {selector_dir}")
        return {"total": 0, "evaluated": 0, "skipped": 0, "results": {}}

    eval_dir.mkdir(parents=True, exist_ok=True)

    # 3. 扫描 selector 输出文件
    selector_files = sorted(selector_dir.glob("q_*_selected*.json"))
    logger.info(f"[evaluate] run_id={run_id}, found {len(selector_files)} selector files")

    total = len(selector_files)
    evaluated = 0
    skipped = 0
    gold_failed = 0
    results = {}  # question_id -> {field: correct}

    for sel_file in selector_files:
        eval_filename = _eval_file_name(sel_file.name)
        eval_path = eval_dir / eval_filename

        # 断点续跑：跳过已存在的 eval 文件
        if eval_path.exists() and not force_rerun:
            logger.info(f"  [skip] {eval_filename} already exists")
            skipped += 1
            continue

        # 读取 selector 输出
        with open(sel_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        question_id = data.get("question_id")
        db_id = data.get("db_id")

        if question_id is None or db_id is None:
            logger.warning(f"  [skip] {sel_file.name}: missing question_id or db_id")
            skipped += 1
            continue

        # 获取 golden SQL
        gold_item = gold_sqls.get(question_id)
        if gold_item is None:
            logger.warning(f"  [skip] q_{question_id:04d}: not found in dev.json")
            skipped += 1
            continue

        gold_sql = gold_item.get("SQL", "")
        db_path = _build_db_path(db_id)

        if not os.path.exists(db_path):
            logger.warning(f"  [skip] q_{question_id:04d}: database not found: {db_path}")
            skipped += 1
            continue

        # 对每个 SQL 字段执行 eval_ex
        q_results = {}
        has_gold_failure = False
        for sql_field, correct_field in _SQL_FIELDS:
            pred_sql = data.get(sql_field)
            if pred_sql is None:
                continue  # 缺失的 SQL 字段不生成 correct 字段
            result = eval_ex(pred_sql, gold_sql, db_path, timeout)
            if result is not None:
                data[correct_field] = bool(result)
                q_results[correct_field] = bool(result)
            else:
                has_gold_failure = True
                logger.warning(f"  [warn] q_{question_id:04d}.{sql_field}: gold SQL execution failed")

        # gold SQL 执行失败 → 不写入 eval 文件（保障断点续跑能重试）
        if has_gold_failure:
            logger.warning(f"  [gold_failed] q_{question_id:04d}: eval file NOT written, "
                           f"will be retried on next run")
            gold_failed += 1
            continue

        # 写入 eval 文件
        with open(eval_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        evaluated += 1
        results[question_id] = q_results
        logger.info(f"  [eval] q_{question_id:04d}: {q_results} → {eval_filename}")

    summary = {
        "total": total,
        "evaluated": evaluated,
        "skipped": skipped,
        "gold_failed": gold_failed,
        "results": results,
    }
    logger.info(f"[evaluate] Done: total={total}, evaluated={evaluated}, "
                f"skipped={skipped}, gold_failed={gold_failed}")
    return summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI 入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="SQL Evaluator: 对 selector 输出执行 BIRD 标准正确性验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--run-id", type=str, required=True,
        help="运行 ID（对应 workspace/sql_generation/{run_id}/ 目录）",
    )
    parser.add_argument(
        "--dev-json", type=str, default=None,
        help="自定义 dev.json 路径（默认使用 config.DEV_JSON）",
    )
    parser.add_argument(
        "--force-rerun", action="store_true", default=False,
        help="强制重新评测（忽略已有 eval 文件）",
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help=f"SQL 执行超时（秒，默认: {config.EVAL_SQL_TIMEOUT}）",
    )
    args = parser.parse_args()

    # 日志初始化
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("  SQL Evaluator")
    logger.info(f"  Run ID     : {args.run_id}")
    logger.info(f"  Dev JSON   : {args.dev_json or config.DEV_JSON}")
    logger.info(f"  Force Rerun: {args.force_rerun}")
    logger.info(f"  Timeout    : {args.timeout or config.EVAL_SQL_TIMEOUT}s")
    logger.info("=" * 60)

    summary = evaluate(
        run_id=args.run_id,
        timeout=args.timeout,
        force_rerun=args.force_rerun,
        dev_json=args.dev_json,
    )

    # 评测全部失败时返回非零退出码
    if summary["evaluated"] == 0 and summary["total"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
