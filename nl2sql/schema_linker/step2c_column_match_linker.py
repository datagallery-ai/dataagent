#!/usr/bin/env python3
"""
Step2c: Two-Step Column Matching
两步列召回：Evidence 列验证 + 关键词语义匹配

两步召回流程：
    1. 从 step2b 数据集加载带 extracted_evidence 和关键词的数据
    2. 初始化 MetaVisor 客户端和数据库 schema
    3. 对每个问题：
       Step 1: Evidence 列验证
       a. 验证 extracted_evidence 中的列名是否在 schema 中存在
       b. 支持精确匹配和大小写不敏感回退匹配
       c. 记录验证通过的列到结果集
       
       Step 2: 关键词语义匹配
       d. 使用关键词调用 semantic_search_columns API
       e. 按语义分数阈值过滤结果
       f. 记录每个关键词匹配的列和分数
       g. 合并两步结果到 column_match_tables_and_columns 和 recall_path_detail
    4. 保存处理结果
"""

import sys
import json
import pickle
import time
import logging
import argparse
import threading
import copy
from pathlib import Path
from typing import Dict, Any, List, Set, Tuple, Optional
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from .data_types import DataItem
from .utils import read_pickle, write_pickle, ensure_dir, get_log_file_path, parse_range_arg, parse_question_ids_arg, filter_dataset_by_qid_range, filter_dataset_by_question_ids, load_dataset_with_checkpoint_merge, get_error_questions_path, load_error_questions, save_error_questions, update_step2_state
from ..client.local_artifact_client import LocalArtifactClient

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2c_column_match_linker'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def verify_evidence_columns(extracted_evidence: List[dict], database_name: str, full_schema: Dict[str, List[Dict]]) -> Set[str]:
    """
    验证 extracted_evidence 中的列是否存在于数据库 schema 中
    
    Args:
        extracted_evidence: [{"column": "...", "value": "...", "table": "..."}]
        database_name: 数据库名
        full_schema: 完整 schema 信息 {table_name: [{column_name, column_type, ...}]}
        
    Returns:
        验证存在的列ID集合 {db_name.table_name.column_name, ...}
    """
    if not extracted_evidence:
        return set()
    
    if full_schema is None:
        logger.warning("verify_evidence_columns: full_schema not provided, returning empty set")
        return set()
    
    verified_columns = set()
    
    try:
        # 构建列查找索引（原始大小写）：{(table, column): full_column_id}
        # 同时构建小写索引用于大小写不敏感回退匹配
        column_lookup = {}
        column_lookup_lower = {}  # {(table_lower, column_lower): (col_id, original_col_name)}
        
        for table_name, table_info in full_schema.items():
            # 实际schema结构: 
            # {
            #   'table_name': {
            #     'table_name': 'actual_table_name',
            #     'columns': {
            #       'column_name': {
            #         'column_name': 'actual_column_name',
            #         'column_type': 'TEXT',
            #         'primary_key': True/False,
            #         ...
            #       }
            #     }
            #   }
            # }
            if isinstance(table_info, dict) and 'columns' in table_info:
                columns_dict = table_info['columns']
                if isinstance(columns_dict, dict):
                    # columns是一个字典，key是列名，value是列信息字典
                    for col_name, col_info in columns_dict.items():
                        if isinstance(col_info, dict):
                            # 从列信息字典中提取实际列名
                            actual_col_name = col_info.get("column_name", col_name)
                        else:
                            # 如果列信息不是字典，使用key作为列名
                            actual_col_name = col_name
                        
                        if actual_col_name:
                            col_id = f"{database_name}.{table_name}.{actual_col_name}"
                            column_lookup[(table_name, actual_col_name)] = col_id
                            lower_key = (table_name.strip().lower(), actual_col_name.strip().lower())
                            if lower_key not in column_lookup_lower:
                                column_lookup_lower[lower_key] = (col_id, actual_col_name)
                elif isinstance(columns_dict, list):
                    # 回退支持：如果columns是列表
                    for col in columns_dict:
                        if isinstance(col, str):
                            col_name = col
                        elif isinstance(col, dict):
                            col_name = col.get("column_name", "")
                        else:
                            continue
                        
                        if col_name:
                            col_id = f"{database_name}.{table_name}.{col_name}"
                            column_lookup[(table_name, col_name)] = col_id
                            lower_key = (table_name.strip().lower(), col_name.strip().lower())
                            if lower_key not in column_lookup_lower:
                                column_lookup_lower[lower_key] = (col_id, col_name)
            else:
                # 回退到旧的结构支持
                cols = table_info
                if isinstance(cols, (list, dict)):
                    if isinstance(cols, list):
                        for col in cols:
                            if isinstance(col, str):
                                col_name = col
                            elif isinstance(col, dict):
                                col_name = col.get("column_name", "")
                            else:
                                continue
                            
                            if col_name:
                                col_id = f"{database_name}.{table_name}.{col_name}"
                                column_lookup[(table_name, col_name)] = col_id
                                lower_key = (table_name.strip().lower(), col_name.strip().lower())
                                if lower_key not in column_lookup_lower:
                                    column_lookup_lower[lower_key] = (col_id, col_name)
        
        total_cols = 0
        for table_info in full_schema.values():
            if isinstance(table_info, dict) and 'columns' in table_info:
                total_cols += len(table_info['columns'])
            else:
                total_cols += len(table_info) if table_info else 0
        
        logger.debug(f"verify_evidence_columns: column_lookup built with "
                    f"{len(column_lookup)} entries across {len(full_schema)} tables "
                    f"({total_cols} total columns)")
        
        # 验证每条 evidence 记录
        for ev in extracted_evidence:
            column_name = ev.get("column", "")
            table_name = ev.get("table", "")
            
            if not column_name:
                continue
            
            # 情况1：有明确的表名 — 先精确匹配，再大小写不敏感回退
            if table_name:
                key = (table_name, column_name)
                if key in column_lookup:
                    col_id = column_lookup[key]
                    verified_columns.add(col_id)
                    logger.info(f"Evidence column verified: {col_id} "
                              f"(table={table_name}, column={column_name})")
                else:
                    lower_key = (table_name.strip().lower(), column_name.strip().lower())
                    if lower_key in column_lookup_lower:
                        col_id, real_name = column_lookup_lower[lower_key]
                        verified_columns.add(col_id)
                        logger.info(f"Evidence column verified (case-insensitive): {col_id} "
                                  f"(table={table_name}, column={column_name} -> {real_name})")
                    else:
                        logger.warning(f"Evidence column not found: table={table_name}, column={column_name}")
            
            # 情况2：只有列名，搜索所有表 — 先精确匹配，再大小写不敏感回退
            else:
                found = False
                for (tbl, col), col_id in column_lookup.items():
                    if col == column_name:
                        verified_columns.add(col_id)
                        logger.info(f"Evidence column verified: {col_id} "
                                  f"(column={column_name}, inferred table={tbl})")
                        found = True
                
                if not found:
                    col_name_lower = column_name.strip().lower()
                    for (tbl_lower, col_lower), (col_id, real_name) in column_lookup_lower.items():
                        if col_lower == col_name_lower:
                            verified_columns.add(col_id)
                            logger.info(f"Evidence column verified (case-insensitive): {col_id} "
                                      f"(column={column_name} -> {real_name}, inferred table={tbl_lower})")
                            found = True
                
                if not found:
                    logger.warning(f"Evidence column not found: column={column_name} "
                                 f"(no table specified, schema has {len(column_lookup)} columns)")
    
    except Exception as e:
        logger.error(f"verify_evidence_columns failed: {e}")
    
    return verified_columns


def _convert_columns_to_tables_format(columns: Set[str]) -> Dict[str, List[str]]:
    """
    Convert column IDs to tables_and_columns format
    
    Args:
        columns: Set of column IDs like {"db.table.column1", "db.table.column2"}
        
    Returns:
        Dictionary format: {"table1": ["column1", "column2"], "table2": ["column3"]}
    """
    tables_and_columns = {}
    
    for col_id in columns:
        if '.' in col_id:
            # 拆分 "database.table.column"，提取表名与列名
            parts = col_id.split('.')
            if len(parts) >= 3:
                table_name = parts[1]  # 跳过库名，取表名
                column_name = '.'.join(parts[2:])  # 表名之后的部分即列名
                
                if table_name not in tables_and_columns:
                    tables_and_columns[table_name] = []
                if column_name not in tables_and_columns[table_name]:
                    tables_and_columns[table_name].append(column_name)
    
    return tables_and_columns


def _log_column_match_details(
    result: dict,
    keywords: List[str],
    keyword_results: Dict[str, List[Dict]],
    evidence_count: int,
    threshold: float
) -> None:
    """
    记录详细的列匹配日志（debug 级别），参照 schema_recall.py 格式
    
    Args:
        result: semantic_search_columns 返回结果
        keywords: 关键词列表
        keyword_results: 按关键词分组的匹配结果
        evidence_count: evidence 召回的列数
        threshold: 语义匹配阈值
    """
    logger.debug("=" * 60)
    logger.debug(f"Column Match Details (excluding {evidence_count} evidence columns, threshold={threshold}):")
    logger.debug("=" * 60)
    
    for keyword in keywords:
        if keyword in keyword_results and keyword_results[keyword]:
            logger.debug(f"Keyword: '{keyword}'")
            for match in keyword_results[keyword]:
                col_id = match.get("column", "")
                score = match.get("score", "N/A")
                logger.debug(f"  ├─ Column: {col_id}, Score: {score}")
        else:
            logger.debug(f"Keyword: '{keyword}'")
            logger.debug(f"  └─ (no matches above threshold)")
    
    # 统计信息
    total_matches = sum(len(matches) for matches in keyword_results.values())
    matched_keywords = len([k for k, v in keyword_results.items() if v])
    logger.debug("=" * 60)
    logger.debug(f"Summary: {matched_keywords}/{len(keywords)} keywords matched, {total_matches} total column matches")
    logger.debug("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description='Step 2c: Semantic Column Match Linking')
    parser.add_argument("--input", type=str,
                        help="Input file path (default: use path from config)")
    parser.add_argument("--output", type=str,
                        help="Output file path (default: use path from config)")
    limit_range_group = parser.add_mutually_exclusive_group()
    limit_range_group.add_argument("--limit", type=int, default=0,
                                  help="Limit number of questions to process (0=all)")
    limit_range_group.add_argument("--question-range", dest="question_range", type=str, default=None,
                                  help="Only process pending_items question_id in [start,end), format: start,end")
    limit_range_group.add_argument("--question-ids", dest="question_ids", type=str, default=None,
                                  help="Only process specified question_ids, format: 1,3,5")
    limit_range_group.add_argument("--range", dest="question_range", type=str, default=None,
                                  help=argparse.SUPPRESS)
    parser.add_argument("--threshold", type=float,
                        help="Semantic similarity threshold (default: use config.COLUMN_SEMANTIC_SCORE_THRESHOLD)")
    parser.add_argument("--top_k", type=int,
                        help="Number of columns to return (default: use config.COLUMN_SEMANTIC_MATCH_TOP_K)")
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable detailed logging')
    parser.add_argument('--resume', '-r', action='store_true',
                       help='Resume from previous checkpoint (load from output file if exists)')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"\n")
    logger.info("=== Step 2c: Semantic Column Match Linking ===")
    logger.info(f"Config: COLUMN_SEMANTIC_SCORE_THRESHOLD={config.COLUMN_SEMANTIC_SCORE_THRESHOLD}")
    logger.info(f"Config: COLUMN_SEMANTIC_MATCH_TOP_K={config.COLUMN_SEMANTIC_MATCH_TOP_K}")
    
    # 确定输入/输出路径
    input_path = args.input or config.STEP2B_KEYWORDS_SAVE_PATH
    output_path = args.output or config.STEP2C_COLUMN_MATCH_SAVE_PATH

    checkpoint_path = Path(output_path)
    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")
    
    # 确定阈值参数
    threshold = args.threshold if args.threshold is not None else config.COLUMN_SEMANTIC_SCORE_THRESHOLD
    top_k = args.top_k if args.top_k is not None else config.COLUMN_SEMANTIC_MATCH_TOP_K
    
    logger.info(f"Input file: {input_path}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Using threshold: {threshold}")
    
    if not Path(input_path).exists():
        if args.resume and checkpoint_path.exists():
            logger.warning(f"Base input missing ({input_path}), fallback to checkpoint-only: {checkpoint_path}")
        else:
            logger.error(f"Input file does not exist: {input_path}")
            logger.error("Please run step2b_keywords_and_retrieval.py first")
            sys.exit(1)

    # 加载数据集（合并断点到完整基础数据集）
    logger.info(f"Loading dataset: {input_path}")
    dataset = load_dataset_with_checkpoint_merge(input_path, str(checkpoint_path), args.resume)
    logger.info(f"Dataset loaded, {len(dataset)} items total")

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
        logger.info(f"Limited processing to {args.limit} items")
    
    # 初始化MetaVisor客户端
    logger.info("Initializing MetaVisor client...")
    try:
        metavisor = LocalArtifactClient(
            workspace_root=config.STEP1_PREPROCESS_WORKSPACE_DIR,
            cache_dir=config.STEP1_CACHE_DIR
        )
        logger.info("MetaVisor client initialized successfully")
    except Exception as e:
        logger.error(f"MetaVisor client initialization failed: {e}")
        sys.exit(1)

    # 检查已完成的项目
    completed = sum(1 for item in dataset if hasattr(item, 'column_match_tables_and_columns') and item.column_match_tables_and_columns is not None)
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")
    
    # 过滤需要处理的项目
    error_questions_path = get_error_questions_path(4)
    error_qid_to_msg = load_error_questions(error_questions_path)
    question_ids_set = set(question_ids or [])

    dataset_lock = threading.RLock()

    def snapshot_dataset_for_save():
        with dataset_lock:
            return [it.model_copy(deep=True) if hasattr(it, "model_copy") else copy.deepcopy(it) for it in dataset]

    pending_items = []
    for item in dataset:
        qid = getattr(item, 'question_id', None)
        needs_processing = (not hasattr(item, 'column_match_tables_and_columns') or item.column_match_tables_and_columns is None)
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
        logger.info("All items have been processed")
        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, snapshot_dataset_for_save())
        final_completed = sum(
            1
            for it in dataset
            if hasattr(it, "column_match_tables_and_columns")
            and it.column_match_tables_and_columns is not None
        )
        update_step2_state(
            step_id="2c",
            name="column_match",
            status="done",
            completed_questions=final_completed,
            total_questions=len(dataset),
        )
        logger.info(f"Dataset saved to: {output_path}")
        return
    
    logger.info(f"Starting to process {len(pending_items)} pending items...")
    
    # 处理统计
    total_matched = 0
    
    def process_single_item(item):
        """处理单个项的列匹配"""
        start_time = time.time()
        
        try:
            qid = getattr(item, 'question_id', 0)
            db_id = getattr(item, 'database_id', '')
            keywords = getattr(item, 'question_keywords', [])
            extracted_evidence = getattr(item, 'extracted_evidence', [])
            database_schema = getattr(item, 'database_schema', {})

            logger.info(f"qid={qid}: Starting two-step column recall...")
            
            # 获取完整 schema
            full_schema = database_schema.get('tables', {})
            if not full_schema:
                logger.warning(f"qid={qid}: No database_schema data, skipping")
                with dataset_lock:
                    item.column_match_tables_and_columns = {}
                    item.column_match_recall_path_detail = []
                    item.column_match_time = time.time() - start_time
                return False, 0, "missing database_schema tables"
            
            # === Step 1: Evidence Column Verification ===
            logger.info(f"qid={qid}: Step 2c-1 - Evidence column verification...")
            evidence_columns = set()
            if extracted_evidence:
                evidence_columns = verify_evidence_columns(
                    extracted_evidence=extracted_evidence,
                    database_name=db_id,
                    full_schema=full_schema
                )
                logger.info(f"qid={qid}: Evidence verification completed, found {len(evidence_columns)} columns")
                if evidence_columns:
                    logger.debug(f"qid={qid}: Evidence verified columns: {sorted(list(evidence_columns))}")
            else:
                logger.info(f"qid={qid}: No extracted_evidence data, skipping evidence verification")
            
            # === Step 2: Keyword Semantic Matching ===
            logger.info(f"qid={qid}: Step 2c-2 - Keyword semantic matching...")
            semantic_columns = set()
            recall_path_detail = []
            
            # 将 evidence 列加入 recall_path_detail
            if evidence_columns:
                evidence_recall_entry = {
                    "keyword": "evidence",
                    "columns": [{"column": col, "score": 1.0} for col in sorted(evidence_columns)]
                }
                recall_path_detail.append(evidence_recall_entry)
                logger.debug(f"qid={qid}: Added {len(evidence_columns)} evidence columns to recall_path_detail")
            
            if keywords:
                # 调用 MetaVisor 语义检索 API
                result = metavisor.semantic_search_columns(
                    keywords=keywords,
                    database_name=db_id,
                    top_k=top_k,
                    vector_boost=config.COLUMN_SEMANTIC_MATCH_VECTOR_BOOST,
                    text_boost=config.COLUMN_SEMANTIC_MATCH_TEXT_BOOST,
                    search_columns=True,
                    search_values=False
                )
                
                # 按关键词分组结果
                keyword_results = {}
                
                # API 响应格式: [{keyword: {column_name_search: [...]}}]
                for result_item in result:
                    if not isinstance(result_item, dict):
                        continue
                    for keyword, results in result_item.items():
                        if not isinstance(results, dict):
                            continue
                        entries = results.get('column_name_search', [])
                        
                        if keyword not in keyword_results:
                            keyword_results[keyword] = []
                        
                        for entry in entries:
                            if not isinstance(entry, dict):
                                continue
                            for col_id, col_data in entry.items():
                                score = None
                                if isinstance(col_data, dict):
                                    score = col_data.get("score", None)
                                
                                if score is not None:
                                    try:
                                        score = float(score)
                                        if score >= threshold:
                                            semantic_columns.add(col_id)
                                            keyword_results[keyword].append({
                                                "column": col_id,
                                                "score": score
                                            })
                                    except (ValueError, TypeError):
                                        continue
                
                # 为语义结果构建新的 recall_path_detail 格式
                for keyword in keywords:
                    if keyword in keyword_results:
                        recall_path_detail.append({
                            "keyword": keyword,
                            "columns": keyword_results[keyword]
                        })
                    else:
                        recall_path_detail.append({
                            "keyword": keyword,
                            "columns": []
                        })
                
                logger.info(f"qid={qid}: Keyword semantic matching completed, found {len(semantic_columns)} columns")
                
                # 详细日志
                _log_column_match_details(
                    result=result,
                    keywords=keywords,
                    keyword_results=keyword_results,
                    evidence_count=len(evidence_columns),
                    threshold=threshold
                )
            else:
                logger.info(f"qid={qid}: No question_keywords data, skipping semantic matching")

            # 合并两步召回结果
            final_columns = evidence_columns.union(semantic_columns)
            
            # 转换为 tables_and_columns 格式
            tables_and_columns = _convert_columns_to_tables_format(final_columns)
            
            # 保存结果到 DataItem
            with dataset_lock:
                item.column_match_tables_and_columns = tables_and_columns
                item.column_match_recall_path_detail = recall_path_detail
            
            matched_count = 1 if final_columns else 0
            if final_columns:
                logger.info(f"qid={qid}: Recall completed, found {len(tables_and_columns)} tables {len(final_columns)} columns, Evidence recall: {len(evidence_columns)} columns, Semantic recall: {len(semantic_columns)} columns")
            else:
                logger.info(f"qid={qid}: Two-step recall did not find any relevant columns")

            processing_time = time.time() - start_time
            with dataset_lock:
                item.column_match_time = processing_time
            logger.info(f"qid={qid}: Processing completed, took {processing_time:.2f} seconds")
            return True, matched_count, ""
            
        except Exception as e:
            logger.error(f"qid={qid}: Two-step column recall processing failed: {e}")
            with dataset_lock:
                item.column_match_tables_and_columns = {}  # 置空结果
                item.column_match_recall_path_detail = []
                item.column_match_time = time.time() - start_time
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
        pbar = tqdm(total=len(pending_items), desc="Two-Step Column Matching", initial=0)
        for future in as_completed(futures):
            item = futures[future]
            try:
                ok, matched_count, err = future.result()
                qid = getattr(item, 'question_id', None)
                if ok:
                    total_matched += matched_count
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
                    if hasattr(it, "column_match_tables_and_columns")
                    and it.column_match_tables_and_columns is not None
                )
                logger.info(f"Checkpoint saved: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2c",
                    name="column_match",
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
        logger.info("[VERIFY] Step2c: start rerun error questions")
        logger.info(f"[VERIFY] Step2c: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2c: rerun qids(all)={qids}")
        by_qid = {getattr(it, 'question_id', None): it for it in dataset}
        for qid in qids:
            item = by_qid.get(qid)
            if not item:
                continue
            ok, matched_count, err = process_single_item(item)
            if ok:
                total_matched += matched_count
                del error_qid_to_msg[qid]
            else:
                error_qid_to_msg[qid] = err or error_qid_to_msg.get(qid, "unknown error")

        if error_qid_to_msg:
            remaining_qids = sorted(list(error_qid_to_msg.keys()))
            logger.info(f"[VERIFY] Step2c: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2c: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2c: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 统计
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, "column_match_tables_and_columns")
        and it.column_match_tables_and_columns is not None
    )
    final_matched = sum(
        1
        for it in dataset
        if hasattr(it, "column_match_tables_and_columns")
        and it.column_match_tables_and_columns
    )

    update_step2_state(
        step_id="2c",
        name="column_match",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    logger.info(f"=== Semantic Column Match Linking Summary ===")
    logger.info(f"Total processed items: {final_completed}/{len(dataset)}")
    logger.info(f"Successfully matched items: {final_matched}")
    logger.info(f"Used threshold: {threshold}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2c error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info(f"=== Step 2c Completed ===")
    logger.info(f"\n")


if __name__ == "__main__":
    main()
