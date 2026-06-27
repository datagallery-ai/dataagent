#!/usr/bin/env python3
"""
Step2f: Value Retrieval Distance Threshold Linking
基于距离阈值的值检索列链接 - 对step2e输出的retrieved_valu
基于已有的retrieved_values数据进行过滤处理。

处理流程：
    1. 从step2e输出数据集加载包含retrieved_values的数据
    2. 对每个问题的retrieved_values进行距离阈值过滤：
       a. 遍历retrieved_values中每个表和列
       b. 检查列中是否存在distance < threshold的值
       c. 符合条件的表列加入链接结果
    3. 将过滤结果保存到value_retrieval_tables_and_columns字段
    4. 保存处理完成的数据集
"""

import sys
import pickle
import copy
import time
import logging
import argparse
import threading
from pathlib import Path
from typing import Dict, Any, List, Tuple
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from .data_types import DataItem
from .utils import read_pickle, write_pickle, ensure_dir, get_log_file_path, parse_range_arg, parse_question_ids_arg, filter_dataset_by_qid_range, filter_dataset_by_question_ids, load_dataset_with_checkpoint_merge, get_error_questions_path, load_error_questions, save_error_questions, update_step2_state
from .schema_utils import map_lower_table_name_to_original_table_name, map_lower_column_name_to_original_column_name

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2f_value_retrieval_linker'), encoding='utf-8')
    ]
)

logger = logging.getLogger(__name__)

class ValueLinker:
    """
    值距离阈值过滤器
    
    功能：对retrieved_values按距离阈值过滤，筛选出高质量匹配列
    特点：不做新的API调用，无需LLM，仅基于已有数据进行过滤
    """
    
    def __init__(self, value_distance_threshold: float = 0.05):
        """
        初始化ValueLinker
        
        Args:
            value_distance_threshold: 值距离阈值，距离小于此值的匹配被认为是高质量的
        """
        self._threshold = value_distance_threshold
    
    def link(self, data_item, llm=None, sampling_budget=1) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
        """
        执行值距离阈值过滤
        
        处理流程：
        1. 检查data_item是否包含retrieved_values数据
        2. 遍历每个表和列的retrieved_values
        3. 对于每列，检查是否存在distance < threshold的值
        4. 如果存在，将该表列加入链接结果
        5. 使用schema_utils函数映射回原始大小写格式
        
        Args:
            data_item: DataItem对象，必须包含retrieved_values字段
            llm: 未使用（ValueLinker不需要LLM）
            sampling_budget: 未使用
            
        Returns:
            Tuple: (linked_tables_and_columns, empty_token_usage)
        """
        linked = defaultdict(list)
        
        # 检查retrieved_values是否存在且非空
        if not data_item.retrieved_values:
            return dict(linked), {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        # 遍历retrieved_values中的每个表和列
        for table_name, columns in data_item.retrieved_values.items():
            # 映射小写表名到原始表名
            orig_tbl = map_lower_table_name_to_original_table_name(table_name, data_item.database_schema)
            if not orig_tbl:
                continue
            
            for column_name, values in columns.items():
                # 检查该列是否有距离小于阈值的值匹配
                if any(v["distance"] < self._threshold for v in values):
                    # 映射小写列名到原始列名
                    orig_col = map_lower_column_name_to_original_column_name(orig_tbl, column_name, data_item.database_schema)
                    if orig_col:
                        linked[orig_tbl].append(orig_col)
        
        return dict(linked), {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

def main() -> None:
    parser = argparse.ArgumentParser(description='Step 2f: Value Retrieval Linking')
    parser.add_argument("--input", type=str,
                        help="输入文件路径（默认使用config中的路径）")
    parser.add_argument("--output", type=str,
                        help="输出文件路径（默认使用config中的路径）")
    limit_range_group = parser.add_mutually_exclusive_group()
    limit_range_group.add_argument("--limit", type=int, default=0,
                                  help="限制处理的问题数量（0=全部）")
    limit_range_group.add_argument("--question-range", dest="question_range", type=str, default=None,
                                  help="Only process pending_items question_id in [start,end), format: start,end")
    limit_range_group.add_argument("--question-ids", dest="question_ids", type=str, default=None,
                                  help="Only process specified question_ids, format: 1,3,5")
    limit_range_group.add_argument("--range", dest="question_range", type=str, default=None,
                                  help=argparse.SUPPRESS)
    parser.add_argument("--value-distance-threshold", type=float, 
                        help="Value distance threshold (default: config.VALUE_DISTANCE_THRESHOLD)")
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='启用详细日志')
    parser.add_argument('--resume', '-r', action='store_true',
                       help='Resume from previous checkpoint (load from output file if exists)')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"\n")
    logger.info("=== Step 2f: Value Retrieval Linking ===")
    
    # 确定输入输出路径 - 输入必须是 Step2e 的输出
    input_path = args.input or config.STEP2E_COLUMN_VALUE_SAVE_PATH
    output_path = args.output or config.STEP2F_VALUE_RETRIEVAL_SAVE_PATH

    checkpoint_path = Path(output_path)
    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")
    
    # 确定值距离阈值
    value_distance_threshold = config.VALUE_DISTANCE_THRESHOLD
    
    logger.info(f"Value distance threshold: {value_distance_threshold}")
    logger.info(f"Input file: {input_path}")
    logger.info(f"Output file: {output_path}")
    
    if not Path(input_path).exists():
        if args.resume and checkpoint_path.exists():
            logger.warning(f"Base input missing ({input_path}), fallback to checkpoint-only: {checkpoint_path}")
        else:
            logger.error(f"Input file not found: {input_path}")
            logger.error("Please run step2e_value_match_linker.py first")
            sys.exit(1)

    # 加载数据集（merge checkpoint into full base dataset）
    logger.info(f"Loading dataset from: {input_path}")
    dataset = load_dataset_with_checkpoint_merge(input_path, str(checkpoint_path), args.resume)
    logger.info(f"Dataset loaded successfully, total items: {len(dataset)}")

    qid_range = parse_range_arg(getattr(args, 'question_range', None))
    if getattr(args, 'question_range', None) and not qid_range:
        logger.error(f"Invalid --question-range value: {args.question_range!r}, expected format 'start,end' (e.g. 0,10)")
        sys.exit(1)

    question_ids = parse_question_ids_arg(getattr(args, 'question_ids', None))
    if getattr(args, 'question_ids', None) and not question_ids:
        logger.error(f"Invalid --question-ids value: {args.question_ids!r}, expected format '1,3,5'")
        sys.exit(1)
    
    # 应用limit参数
    if args.limit > 0:
        dataset = dataset[:args.limit]
        logger.info(f"Processing limited to {args.limit} items")
    
    # 检查已完成的项目
    completed = sum(1 for item in dataset if hasattr(item, 'value_retrieval_tables_and_columns') and item.value_retrieval_tables_and_columns is not None)
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")
    
    # 初始化ValueLinker
    value_linker = ValueLinker(value_distance_threshold=value_distance_threshold)
    logger.info(f"ValueLinker initialized with distance threshold: {value_distance_threshold}")
    
    # 过滤需要处理的项目
    error_questions_path = get_error_questions_path(7)
    error_qid_to_msg = load_error_questions(error_questions_path)
    question_ids_set = set(question_ids or [])

    dataset_lock = threading.RLock()

    def snapshot_dataset_for_save():
        with dataset_lock:
            return [it.model_copy(deep=True) if hasattr(it, "model_copy") else copy.deepcopy(it) for it in dataset]

    pending_items = []
    for item in dataset:
        qid = getattr(item, 'question_id', None)
        needs_processing = (not hasattr(item, 'value_retrieval_tables_and_columns') or item.value_retrieval_tables_and_columns is None)
        force_retry = isinstance(qid, int) and qid in error_qid_to_msg
        force_selected = isinstance(qid, int) and qid in question_ids_set
        if needs_processing or force_retry or force_selected:
            pending_items.append(item)

    if question_ids:
        pending_items = filter_dataset_by_question_ids(pending_items, question_ids)
        logger.info(f"Applied question_ids filter to pending_items: {question_ids}, remaining {len(pending_items)} pending items")
    elif qid_range:
        pending_items = filter_dataset_by_qid_range(pending_items, qid_range)
        logger.info(
            f"Applied question_id range filter to pending_items: "
            f"[{qid_range[0]}, {qid_range[1]}), remaining {len(pending_items)} pending items"
        )
    
    if not pending_items:
        logger.info("All items already processed")
        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, snapshot_dataset_for_save())
        final_completed = sum(
            1
            for it in dataset
            if hasattr(it, "value_retrieval_tables_and_columns")
            and it.value_retrieval_tables_and_columns is not None
        )
        update_step2_state(
            step_id="2f",
            name="value_retrieval",
            status="done",
            completed_questions=final_completed,
            total_questions=len(dataset),
        )
        logger.info(f"Dataset saved to: {output_path}")
        return
    
    logger.info(f"Starting to process {len(pending_items)} pending items...")
    
    # 处理统计
    total_linked = 0
    
    def process_single_item(item):
        """处理单个项的值检索链接"""
        start_time = time.time()
        
        try:
            qid = getattr(item, 'question_id', 0)
            
            # 检查retrieved_values数据 - ValueLinker的前提条件
            if not hasattr(item, 'retrieved_values') or not item.retrieved_values:
                logger.error(f"qid={qid}: No retrieved_values data, skipping")
                with dataset_lock:
                    item.value_retrieval_tables_and_columns = {}
                    item.value_retrieval_time = time.time() - start_time
                return False, 0, "missing retrieved_values"
            
            logger.info(f"qid={qid}: Starting ValueLinker distance threshold filtering...")
            
            # 调用ValueLinker进行距离阈值过滤
            linked_result, token_usage = value_linker.link(
                data_item=item,
                llm=None,
                sampling_budget=1
            )
            
            # 保存过滤结果到DataItem的指定字段
            with dataset_lock:
                item.value_retrieval_tables_and_columns = linked_result
            
            linked_count = 1 if linked_result else 0
            if linked_result:
                columns_count = sum(len(cols) for cols in linked_result.values())
                logger.info(f"qid={qid}: ValueLinker found {len(linked_result)} tables, {columns_count} columns")
            else:
                logger.info(f"qid={qid}: ValueLinker found no columns meeting threshold")
            
            processing_time = time.time() - start_time
            with dataset_lock:
                item.value_retrieval_time = processing_time
            logger.info(f"qid={qid}: Processing completed in {processing_time:.2f}s")
            return True, linked_count, ""
            
        except Exception as e:
            logger.error(f"qid={qid}: ValueLinker processing failed: {e}")
            with dataset_lock:
                item.value_retrieval_tables_and_columns = {}  # 设置空结果
                item.value_retrieval_time = time.time() - start_time
            return False, 0, str(e)
    
    # 并行处理
    n_parallel = config.SCHEMA_LINKING_N_PARALLEL
    logger.info(f"Processing {len(pending_items)} items with {n_parallel} parallel workers")
    
    completed = 0
    save_interval = config.SCHEMA_LINKING_SAVE_INTERVAL
    
    with ThreadPoolExecutor(max_workers=n_parallel) as executor:
        futures = {
            executor.submit(process_single_item, item): item
            for item in pending_items
        }
        pbar = tqdm(total=len(pending_items), desc="Value Retrieval Linking", initial=0)
        for future in as_completed(futures):
            item = futures[future]
            try:
                ok, linked_count, err = future.result()
                qid = getattr(item, 'question_id', None)
                if ok:
                    total_linked += linked_count
                    if isinstance(qid, int) and qid in error_qid_to_msg:
                        del error_qid_to_msg[qid]
                else:
                    if isinstance(qid, int):
                        error_qid_to_msg[qid] = err or "unknown error"
            except Exception as e:
                logger.error(f"qid={item.question_id}: unhandled error: {e}")
                qid = getattr(item, 'question_id', None)
                if isinstance(qid, int):
                    error_qid_to_msg[qid] = str(e)
            
            completed += 1
            pbar.update(1)
            
            # 周期性保存
            if completed % save_interval == 0:
                ensure_dir(Path(output_path).parent)
                write_pickle(output_path, snapshot_dataset_for_save())
                completed_now = sum(
                    1
                    for it in dataset
                    if hasattr(it, "value_retrieval_tables_and_columns")
                    and it.value_retrieval_tables_and_columns is not None
                )
                logger.info(f"Checkpoint saved: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2f",
                    name="value_retrieval",
                    status="running",
                    completed_questions=completed_now,
                    total_questions=len(dataset),
                )
        
        pbar.close()

    # 最终保存
    ensure_dir(Path(output_path).parent)
    write_pickle(output_path, snapshot_dataset_for_save())

    if error_qid_to_msg:
        save_error_questions(error_questions_path, error_qid_to_msg)

    # 对失败问题重试一次
    if error_qid_to_msg:
        qids = sorted(list(error_qid_to_msg.keys()))
        logger.info("[VERIFY] Step2f: start rerun error questions")
        logger.info(f"[VERIFY] Step2f: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2f: rerun qids(all)={qids}")
        by_qid = {getattr(it, 'question_id', None): it for it in dataset}
        for qid in qids:
            item = by_qid.get(qid)
            if not item:
                continue
            ok, linked_count, err = process_single_item(item)
            if ok:
                total_linked += linked_count
                del error_qid_to_msg[qid]
            else:
                error_qid_to_msg[qid] = err or error_qid_to_msg.get(qid, "unknown error")

        if error_qid_to_msg:
            remaining_qids = sorted(list(error_qid_to_msg.keys()))
            logger.info(f"[VERIFY] Step2f: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2f: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2f: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 统计结果
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, "value_retrieval_tables_and_columns")
        and it.value_retrieval_tables_and_columns is not None
    )
    final_linked = sum(
        1
        for it in dataset
        if hasattr(it, "value_retrieval_tables_and_columns")
        and it.value_retrieval_tables_and_columns
    )

    update_step2_state(
        step_id="2f",
        name="value_retrieval",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    logger.info(f"=== Value Retrieval Schema Linking Summary ===")
    logger.info(f"Total processed items: {final_completed}/{len(dataset)}")
    logger.info(f"Successfully linked items: {final_linked}")
    logger.info(f"Distance threshold used: {value_distance_threshold}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2f error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info("=== Step 2f Completed ===")
    logger.info(f"\n")


if __name__ == "__main__":
    main()
