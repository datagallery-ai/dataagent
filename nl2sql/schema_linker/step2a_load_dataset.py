
"""
Step2a: 数据集预处理
加载 dev.json + SQLite 数据库 → DataItem 列表 → 保存 pkl
"""
import sys
import json
import pickle
import logging
import argparse
from pathlib import Path
from tqdm import tqdm

from .. import config
from .data_types import DataItem
from .schema_utils import load_database_schema_dict
from .utils import get_log_file_path, get_error_questions_path, save_error_questions
from ..common.atomic_io import atomic_write_pickle

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2a_load_dataset'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='Step 2a: Dataset preprocessing')
    parser.add_argument('--limit', type=int, default=0,
                        help='Only process first N questions (0=all)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    parser.add_argument('--db-id', type=str, default=None,
                        help='Only process questions for the specified db_id')
    parser.add_argument(
        '--question-range',
        type=str,
        default=None,
        help='Only process questions whose question_id is in [start,end), format: start,end',
    )
    parser.add_argument(
        '--question-ids',
        type=str,
        default=None,
        help="Comma-separated question_ids to process (e.g. '1,3,5')",
    )

    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info("=== Step2a: Dataset Preprocessing ===")
    logger.info(f"Config: DEV_JSON={config.DEV_JSON}")
    logger.info(f"Config: BIRD_DB_DIR={config.BIRD_DB_DIR}")
    logger.info(f"Config: USE_DATABASE_DESCRIPTION={config.USE_DATABASE_DESCRIPTION}")
    
    # 检查输入文件
    if not Path(config.DEV_JSON).exists():
        logger.error(f"Dev JSON file not found: {config.DEV_JSON}")
        sys.exit(1)
    
    if not Path(config.BIRD_DB_DIR).exists():
        logger.error(f"BIRD database directory not found: {config.BIRD_DB_DIR}")
        sys.exit(1)
    
    logger.info(f"Loading dev data from {config.DEV_JSON}")
    with open(config.DEV_JSON, "r", encoding="utf-8") as f:
        data_list = json.load(f)
    logger.info(f"Loaded {len(data_list)} questions")

    # 可选: 先按 db_id / question 条件进行过滤，用于单库 / 单题测试
    if args.db_id:
        before = len(data_list)
        data_list = [
            item for item in data_list
            if (item.get("db_id") or item.get("database_id")) == args.db_id
        ]
        logger.info(f"Filtered by db_id={args.db_id}: {before} -> {len(data_list)} questions")

    if args.question_range:
        try:
            raw = str(args.question_range).strip()
            start_str, end_str = [x.strip() for x in raw.split(",", 1)]
            start_qid = int(start_str)
            end_qid = int(end_str)
        except Exception:
            logger.error(f"Invalid --question-range value: {args.question_range!r}, expected 'start,end', e.g. 0,10")
            sys.exit(1)

        def _in_range(item):
            qid = item.get("question_id")
            return isinstance(qid, int) and start_qid <= qid < end_qid

        before = len(data_list)
        data_list = [item for item in data_list if _in_range(item)]
        logger.info(
            f"Filtered by question_id range [{start_qid},{end_qid}): {before} -> {len(data_list)} questions"
        )

    if args.question_ids:
        raw = str(args.question_ids).strip()
        id_list = []
        for part in raw.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                id_list.append(int(part))
            except Exception:
                logger.error(f"Invalid question_id in --question-ids: {part!r}, raw={raw!r}")
                sys.exit(1)
        qid_set = set(id_list)

        def _in_ids(item):
            qid = item.get("question_id")
            return isinstance(qid, int) and qid in qid_set

        before = len(data_list)
        data_list = [item for item in data_list if _in_ids(item)]
        logger.info(
            f"Filtered by question_ids={sorted(qid_set)}: {before} -> {len(data_list)} questions"
        )

    # 最后应用 limit 参数（如有），用于快速调试
    if args.limit > 0:
        before = len(data_list)
        data_list = data_list[: args.limit]
        logger.info(f"Limited to first {args.limit} questions: {before} -> {len(data_list)} questions")

    # Schema 缓存（同一 db_id 共享一份 schema）
    schema_cache = {}
    dataset = []
    db_stats = {}
    error_questions_path = get_error_questions_path(0)
    error_qid_to_msg = {}

    for item in tqdm(data_list, desc="Preprocessing"):
        question_id = item["question_id"]
        db_id = item["db_id"]
        db_path = str(Path(config.BIRD_DB_DIR) / db_id / f"{db_id}.sqlite")
        
        logger.info(f"Processing qid={question_id}: db_id={db_id}")
        
        # 检查数据库文件是否存在
        if not Path(db_path).exists():
            logger.error(f"Database file not found: {db_path}")
            error_qid_to_msg[int(question_id)] = f"Database file not found: {db_path}"
            continue

        if db_id not in schema_cache:
            logger.debug(f"Loading schema for database: {db_id}")
            try:
                schema_cache[db_id] = load_database_schema_dict(db_path, config.USE_DATABASE_DESCRIPTION)
                # 统计schema信息
                table_count = len(schema_cache[db_id]["tables"])
                column_count = sum(len(table["columns"]) for table in schema_cache[db_id]["tables"].values())
                db_stats[db_id] = {"tables": table_count, "columns": column_count}
                logger.debug(f"Schema loaded for {db_id}: {table_count} tables, {column_count} columns")
            except Exception as e:
                logger.error(f"Failed to load schema for {db_id}: {e}")
                error_qid_to_msg[int(question_id)] = f"Failed to load schema for {db_id}: {e}"
                continue

        try:
            data_item = DataItem(
                question_id=question_id,
                question=item["question"],
                evidence=item.get("evidence", ""),
                gold_sql=item["SQL"],
                difficulty=item.get("difficulty", ""),
                database_id=db_id,
                database_path=db_path,
                database_schema=schema_cache[db_id],
            )
            dataset.append(data_item)
            logger.info(f"Created DataItem for qid={question_id}")
        except Exception as e:
            logger.error(f"Failed to create DataItem for qid={question_id}: {e}")
            error_qid_to_msg[int(question_id)] = f"Failed to create DataItem for qid={question_id}: {e}"
            continue

    # 打印统计信息
    logger.info(f"=== Processing Summary ===")
    logger.info(f"Total questions processed: {len(dataset)}")
    logger.info(f"Unique databases: {len(schema_cache)}")
    
    for db_id, stats in db_stats.items():
        logger.info(f"  {db_id}: {stats['tables']} tables, {stats['columns']} columns")
    
    difficulty_stats = {}
    for item in dataset:
        diff = item.difficulty or "unknown"
        difficulty_stats[diff] = difficulty_stats.get(diff, 0) + 1
    
    logger.info(f"Difficulty distribution: {difficulty_stats}")
    
    # 保存
    save_path = Path(config.STEP2A_DATASET_SAVE_PATH)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Saving dataset to {save_path}")
    atomic_write_pickle(save_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    logger.info(f"Dataset saved successfully ({len(dataset)} items)")
    logger.info(f"Output file size: {save_path.stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2a error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info("=== Step 2a Complete ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
