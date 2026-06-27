"""Step4 输出评估脚本 — 对比 gold SQL 计算 Execution Accuracy

使用方式:
  python -m nl2sql.runner.step4_evaluate --predict-file output/predict_dev.json --max-workers 8
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import config
from ..sql_evaluator.bird_evaluation import eval_ex


def load_gold_sqls() -> dict:
    """从 dev.json 加载 gold SQL，返回 {question_id: {"sql": str, "db_id": str}}"""
    with open(config.DEV_JSON, "r", encoding="utf-8") as f:
        dev_data = json.load(f)
    gold = {}
    for i, item in enumerate(dev_data):
        gold[i] = {
            "sql": item["SQL"],
            "db_id": item["db_id"],
        }
    return gold


def evaluate_one(args):
    """评估单题"""
    qid, pred_sql, gold_sql, db_path = args
    if not pred_sql or not pred_sql.strip():
        return qid, 0, "empty_prediction"

    try:
        result = eval_ex(pred_sql, gold_sql, db_path, timeout=config.EVAL_SQL_TIMEOUT)
        if result is None:
            return qid, 0, "gold_timeout"
        return qid, int(result), None
    except Exception as e:
        return qid, 0, str(e)


def main():
    parser = argparse.ArgumentParser(
        description="Step4 输出评估: 对比 gold SQL 计算 Execution Accuracy",
    )
    parser.add_argument(
        "--predict-file", type=str, required=True,
        help="Step4 输出的预测文件路径（如 output/predict_dev_verify_B5_v1rc3.json）",
    )
    parser.add_argument(
        "--max-workers", type=int, default=8,
        help="并行度（默认 8）",
    )
    args = parser.parse_args()

    # 加载预测文件
    predict_path = Path(config.NL2SQL_DIR) / args.predict_file
    if not predict_path.exists():
        predict_path = Path(args.predict_file)
    if not predict_path.exists():
        print(f"ERROR: Predict file not found: {args.predict_file}")
        sys.exit(1)

    with open(predict_path, "r", encoding="utf-8") as f:
        predictions = json.load(f)

    print(f"Loaded {len(predictions)} predictions from: {predict_path}")

    # 加载 gold SQL
    gold_sqls = load_gold_sqls()
    print(f"Loaded {len(gold_sqls)} gold SQLs from: {config.DEV_JSON}")

    # 构建评估任务
    work_items = []
    for qid_str, pred_sql in predictions.items():
        qid = int(qid_str)
        if qid not in gold_sqls:
            continue
        gold_info = gold_sqls[qid]
        db_path = str(Path(config.BIRD_DB_DIR) / gold_info["db_id"] / f"{gold_info['db_id']}.sqlite")
        work_items.append((qid, pred_sql, gold_info["sql"], db_path))

    print(f"Evaluating {len(work_items)} questions...")
    t0 = time.time()

    # 并行评估
    correct = 0
    errors = []
    gold_timeouts = 0

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(evaluate_one, item): item[0] for item in work_items}
        done = 0
        for future in as_completed(futures):
            qid, is_correct, error = future.result()
            correct += is_correct
            done += 1
            if error == "gold_timeout":
                gold_timeouts += 1
            elif error:
                errors.append((qid, error))
            if done % 200 == 0:
                elapsed = time.time() - t0
                pct = correct / done * 100
                print(f"  [{done}/{len(work_items)}] correct={correct} ({pct:.2f}%) {elapsed:.1f}s")

    elapsed = time.time() - t0
    total = len(work_items)
    pct = correct / total * 100 if total > 0 else 0

    print("\n" + "=" * 60)
    print("  Step4 评估结果")
    print("=" * 60)
    print(f"  Total questions  : {total}")
    print(f"  Correct          : {correct}")
    print(f"  Accuracy         : {pct:.2f}%")
    print(f"  Gold timeouts    : {gold_timeouts}")
    print(f"  Other errors     : {len(errors)}")
    print(f"  Elapsed          : {elapsed:.1f}s")
    print("=" * 60)

    if errors:
        print(f"\n  First 10 errors:")
        for qid, err in sorted(errors)[:10]:
            print(f"    q_{qid:04d}: {err[:100]}")


if __name__ == "__main__":
    main()
