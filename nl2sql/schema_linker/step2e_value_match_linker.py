"""
Step2e: Value Match Linking
列值 LSH 近似匹配 (C2) + 枚举值描述语义匹配 (C3)

处理流程：
    1. 从 step2d 输出数据集加载带关键词和schema的数据
    2. 初始化 MetaVisor 和 LSH 客户端
    3. 对每个问题执行两个并行的匹配回路：
       a. C2 回路: LSH 近似匹配 - 匹配拼写近似的列值
       b. C3 回路: 枚举描述匹配 - 匹配枚举值的描述文本语义
    4. 转换结果格式，更新 DataItem 相应字段：
       - value_match_lsh_tables_and_columns/values/recall_path_detail
       - value_match_desc_tables_and_columns/values/recall_path_detail
    5. 保存处理结果
"""

import sys
import pickle
import copy
import time
import logging
import argparse
import threading
from pathlib import Path
from typing import Dict, Any, List, Set, Tuple
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
        logging.FileHandler(get_log_file_path('step2e_value_match_linker'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def value_lsh_match(
    keywords: List[str],
    database_name: str,
    lsh_client,
    top_k: int = 3,
    score_threshold: float = 0.4,
) -> Tuple[Set[str], Dict[str, List[dict]], Dict[str, List[Tuple[str, str, float]]]]:
    """
    C2 回路: LSH 近似匹配
    
    Args:
        keywords: 关键词列表
        database_name: 数据库名
        lsh_client: LSH REST 客户端
        top_k: 每个关键词返回数量
        score_threshold: 分数阈值
        
    Returns:
        (columns: Set[str], mappings: Dict[str, List[dict]], kw_map: Dict[str, List[Tuple[str, str, float]]])
        mappings 结构: {column_id: [{"value": ..., "enum_value_description": ..., "score": ..., "keyword": ...}]}
        kw_map 结构:   {keyword: [(column_id, matched_value, score), ...]}
    """
    columns = set()
    mappings: Dict[str, List[dict]] = defaultdict(list)
    kw_map: Dict[str, List[Tuple[str, str, float]]] = {}
    match_count = 0

    for kw in keywords:
        kw_hits: List[Tuple[str, str, float]] = []
        try:
            matches = lsh_client.lsh_match(
                database=database_name,
                query=kw,
                top_k=top_k,
                threshold=score_threshold,
            )
        except Exception as e:
            raise RuntimeError(f"value_lsh_match failed for keyword {kw!r}: {e}") from e

        for match in matches:
            col_id = match.get("matched_column", "")
            if col_id:
                score = match.get("score", None)
                if score is not None:
                    try:
                        if float(score) < float(score_threshold):
                            continue
                    except Exception:
                        pass

                columns.add(col_id)
                match_count += 1
                matched_value = match.get("matched_value", "")
                mappings[col_id].append({
                    "value": matched_value,
                    "enum_value_description": "",
                    "score": score,
                    "keyword": kw,
                })
                kw_hits.append((col_id, matched_value, score))
        if kw_hits:
            logger.debug(
                f"  keyword='{kw}' → "
                + ", ".join(
                    f"{col_id}='{val}' (score={score})" for col_id, val, score in kw_hits
                )
            )
        else:
            logger.debug(f"  keyword='{kw}' → (no matches)")
        kw_map[kw] = kw_hits

    logger.info(f"value_lsh_match: {match_count} matches")
    return columns, mappings, kw_map


def value_desc_match(
    keywords: List[str],
    database_name: str,
    metavisor_client,
    top_k: int = 3,
    score_threshold: float = 1.8,
) -> Tuple[Set[str], Dict[str, List[dict]], Dict[str, List[Tuple[str, str, float]]], Dict[str, Dict[str, List[dict]]]]:
    """
    C3 回路: 枚举值描述语义匹配
    
    调用 search_column_value_descriptions 接口，通过枚举值的描述文本语义检索匹配列，
    适合查找描述语义与关键词吻合的枚举值（例如 "Ungraded" → EILCode.UG="Ungraded"）。
    
    Args:
        keywords: 关键词列表（一次性发送）
        database_name: 数据库名
        metavisor_client: MetaVisor REST 客户端
        top_k: 每个关键词返回的匹配列数量
        score_threshold: 分数阈值
        
    Returns:
        (columns: Set[str],
         mappings: Dict[str, List[{"value": ..., "enum_value_description": ..., "score": ..., "keyword": ...}]],
         kw_map: Dict[str, List[Tuple[col_id, matched_value, score]]],
         kw_col_value_map: Dict[str, Dict[str, List[dict]]])
    """
    columns: Set[str] = set()
    mappings: Dict[str, List[dict]] = defaultdict(list)
    # 预初始化所有关键词，确保无命中时也有空列表
    kw_map: Dict[str, List[Tuple[str, str, float]]] = {kw: [] for kw in keywords}
    # 细粒度映射：{keyword -> {column_id -> [ {"value", "enum_value_description"}, ... ]}}
    kw_col_value_map: Dict[str, Dict[str, List[dict]]] = {kw: {} for kw in keywords}
    match_count = 0

    try:
        raw_result = metavisor_client.search_column_value_descriptions(
            keywords=keywords,
            database_name=database_name,
            top_k=top_k,
        )
    except Exception as e:
        raise RuntimeError(f"value_desc_match failed for db={database_name!r}: {e}") from e

    for kw_dict in raw_result:
        if not isinstance(kw_dict, dict):
            continue
        for kw, data in kw_dict.items():
            if not isinstance(data, dict):
                continue
            col_matches = data.get("column_value_match", [])
            kw_hits: List[Tuple[str, str, float]] = []

            for match_entry in col_matches:
                if not isinstance(match_entry, dict):
                    continue
                for col_id, col_data in match_entry.items():
                    if not col_id:
                        continue
                    vals = col_data.get("values", []) if isinstance(col_data, dict) else []
                    top_val = ""
                    top_score = None
                    accepted_any = False
                    for v in vals:
                        val  = v.get("value", "")
                        desc = v.get("description", "")
                        score = v.get("score", None)

                        if score is not None:
                            try:
                                if float(score) < float(score_threshold):
                                    continue
                            except Exception:
                                pass

                        value_entry = {
                            "value": val,
                            "enum_value_description": desc,
                            "score": score,
                            "keyword": kw,
                        }
                        mappings[col_id].append(value_entry)
                        match_count += 1
                        accepted_any = True
                        # 细粒度记录：按 keyword -> column_id -> values
                        kw_col_value_map.setdefault(kw, {}).setdefault(col_id, []).append(value_entry)
                        if not top_val:
                            top_val = val   # 第一个即 score 最高
                            top_score = score
                    if accepted_any:
                        columns.add(col_id)
                        if top_val:
                            kw_hits.append((col_id, top_val, top_score))

            if kw_hits:
                logger.debug(
                    f"  keyword='{kw}' → "
                    + ", ".join(
                        f"{col_id}='{val}' (score={score})" for col_id, val, score in kw_hits
                    )
                )
            else:
                logger.debug(f"  keyword='{kw}' → (no matches)")
            kw_map[kw] = kw_hits

    logger.info(
        f"value_desc_match: {match_count} value matches across {len(columns)} columns"
    )
    return columns, mappings, kw_map, kw_col_value_map


def convert_to_dataitem_format(columns: Set[str], mappings: Dict[str, List[dict]], 
                              kw_map: Dict[str, List[Tuple[str, str, float]]], 
                              recall_type: str) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
    """
    将C2/C3回路结果转换为DataItem格式
    
    Args:
        columns: 匹配的列ID集合
        mappings: 列值映射 {column_id: [{"value", "enum_value_description", "score"}]}
        kw_map: 关键词映射 {keyword: [(column_id, matched_value, score)]}
        recall_type: 召回类型 "value_match_lsh" 或 "value_match_desc"
        
    Returns:
        (tables_and_columns, recall_path_detail)
    """
    # 转换为 {table_name: [column_names]} 格式
    tables_and_columns = {}
    
    # 从column_id中提取table.column信息
    for col_id in columns:
        if '.' in col_id:
            table_name, column_name = col_id.rsplit('.', 1)
            if table_name not in tables_and_columns:
                tables_and_columns[table_name] = []
            if column_name not in tables_and_columns[table_name]:
                tables_and_columns[table_name].append(column_name)
    
    # 构建召回路径详情，严格按照step2d column_match格式：
    # {"keyword": "keyword_name", "columns": [{"column": "col_id", "score": score}, ...]}
    recall_path_detail = []
    for keyword, hits in kw_map.items():
        keyword_columns = []
        for col_id, matched_value, score in hits:
            keyword_columns.append({
                "column": col_id,
                "score": score
            })
        
        recall_path_detail.append({
            "keyword": keyword,
            "columns": keyword_columns
        })
    
    return tables_and_columns, recall_path_detail


def main() -> None:
    parser = argparse.ArgumentParser(description='Step 2e: Value Match Schema Linking')
    parser.add_argument("--input", type=str,
                        help="Input file path (default: use config path)")
    parser.add_argument("--output", type=str,
                        help="Output file path (default: use config path)")
    limit_range_group = parser.add_mutually_exclusive_group()
    limit_range_group.add_argument("--limit", type=int, default=0,
                                  help="Limit number of questions to process (0=all)")
    limit_range_group.add_argument("--question-range", dest="question_range", type=str, default=None,
                                  help="Only process pending_items question_id in [start,end), format: start,end")
    limit_range_group.add_argument("--question-ids", dest="question_ids", type=str, default=None,
                                  help="Only process specified question_ids, format: 1,3,5")
    limit_range_group.add_argument("--range", dest="question_range", type=str, default=None,
                                  help=argparse.SUPPRESS)
    parser.add_argument("--lsh-threshold", type=float,
                        help="LSH similarity threshold (default: config.VALUE_LSH_SCORE_THRESHOLD)")
    parser.add_argument("--desc-threshold", type=float,
                        help="Description matching threshold (default: config.VALUE_DESC_SCORE_THRESHOLD)")
    parser.add_argument("--top-k", type=int,
                        help="Number of matches to return (default: config.VALUE_MATCH_TOP_K)")
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable detailed logging')
    parser.add_argument('--resume', '-r', action='store_true',
                       help='Resume from previous checkpoint (load from output file if exists)')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"\n")
    logger.info("=== Step 2e: Value Match Schema Linking ===")
    logger.info(f"Config: STEP1_PREPROCESS_WORKSPACE_DIR={config.STEP1_PREPROCESS_WORKSPACE_DIR}")
    logger.info(f"Config: STEP1_CACHE_DIR={config.STEP1_CACHE_DIR}")
    
    # 确定配置参数
    lsh_threshold = args.lsh_threshold if args.lsh_threshold is not None else config.VALUE_LSH_SCORE_THRESHOLD
    desc_threshold = args.desc_threshold if args.desc_threshold is not None else config.VALUE_DESC_SCORE_THRESHOLD
    top_k = getattr(args, 'top_k', None) or config.VALUE_MATCH_TOP_K
    
    logger.info(f"Config: VALUE_LSH_SCORE_THRESHOLD={lsh_threshold}")
    logger.info(f"Config: VALUE_DESC_SCORE_THRESHOLD={desc_threshold}")
    logger.info(f"Config: VALUE_MATCH_TOP_K={top_k}")
    
    # 确定输入/输出路径
    input_path = args.input or config.STEP2D_LLM_DIRECT_SAVE_PATH
    output_path = args.output or config.STEP2E_COLUMN_VALUE_SAVE_PATH

    checkpoint_path = Path(output_path)
    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")

    if not Path(input_path).exists():
        if args.resume and checkpoint_path.exists():
            logger.warning(f"Base input missing ({input_path}), fallback to checkpoint-only: {checkpoint_path}")
        else:
            logger.error(f"Input file does not exist: {input_path}")
            logger.error("Please run step2d_llm_direct_linker.py first")
            sys.exit(1)
        
    logger.info(f"Input file: {input_path}")
    logger.info(f"Output file: {output_path}")

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
    
    # 应用 limit 参数
    if args.limit > 0:
        dataset = dataset[:args.limit]
        logger.info(f"Limited processing to {args.limit} items")
    
    # 统计已完成项
    completed = sum(1 for item in dataset 
                   if (hasattr(item, 'value_match_lsh_tables_and_columns') and item.value_match_lsh_tables_and_columns is not None)
                   and (hasattr(item, 'value_match_desc_tables_and_columns') and item.value_match_desc_tables_and_columns is not None))
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")
    
    # 初始化客户端
    logger.info("Initializing MetaVisor and LSH clients...")
    try:
        metavisor = LocalArtifactClient(
            workspace_root=config.STEP1_PREPROCESS_WORKSPACE_DIR,
            cache_dir=config.STEP1_CACHE_DIR
        )
        logger.info("MetaVisor client initialized successfully")
        
        lsh_client = metavisor
        logger.info("LSH client initialized successfully")
        
    except Exception as e:
        logger.error(f"Client initialization failed: {e}")
        sys.exit(1)

    # 筛选需处理的项
    error_questions_path = get_error_questions_path(6)
    error_qid_to_msg = load_error_questions(error_questions_path)
    question_ids_set = set(question_ids or [])

    dataset_lock = threading.RLock()

    def snapshot_dataset_for_save():
        with dataset_lock:
            return [it.model_copy(deep=True) if hasattr(it, "model_copy") else copy.deepcopy(it) for it in dataset]

    pending_items = []
    for item in dataset:
        qid = getattr(item, 'question_id', None)
        needs_processing = (
                           not hasattr(item, 'value_match_lsh_tables_and_columns') or item.value_match_lsh_tables_and_columns is None or
                           not hasattr(item, 'value_match_desc_tables_and_columns') or item.value_match_desc_tables_and_columns is None)
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
            if hasattr(it, "value_match_lsh_tables_and_columns")
            and it.value_match_lsh_tables_and_columns is not None
        )
        update_step2_state(
            step_id="2e",
            name="column_value",
            status="done",
            completed_questions=final_completed,
            total_questions=len(dataset),
        )
        logger.info(f"Dataset saved to: {output_path}")
        return
    
    logger.info(f"Starting to process {len(pending_items)} pending items...")
    
    # 处理统计
    total_lsh_matched = 0
    total_desc_matched = 0
    
    def process_single_item(item):
        """处理单个项的值匹配链接"""
        start_time = time.time()
        
        try:
            qid = getattr(item, 'question_id', 0)
            db_id = getattr(item, 'database_id', '')
            
            # 获取关键词
            keywords = getattr(item, 'question_keywords', [])
            if not keywords:
                logger.error(f"qid={qid}: No keywords found, skipping")
                with dataset_lock:
                    item.value_match_lsh_tables_and_columns = {}
                    item.value_match_lsh_recall_path_detail = []
                    item.value_match_lsh_values = {}
                    item.value_match_desc_tables_and_columns = {}
                    item.value_match_desc_recall_path_detail = []
                    item.value_match_desc_values = {}
                    item.value_match_time = time.time() - start_time
                return False, 0, 0, "missing question_keywords"
            
            if not db_id:
                logger.error(f"qid={qid}: No database name found, skipping")
                with dataset_lock:
                    item.value_match_lsh_tables_and_columns = {}
                    item.value_match_lsh_recall_path_detail = []
                    item.value_match_lsh_values = {}
                    item.value_match_desc_tables_and_columns = {}
                    item.value_match_desc_recall_path_detail = []
                    item.value_match_desc_values = {}
                    item.value_match_time = time.time() - start_time
                return False, 0, 0, "missing database_id"
            
            logger.info(f"qid={qid}: Starting LSH and Enum description matching...")
            
            # C2 回路：LSH 匹配
            lsh_columns, lsh_mappings, lsh_kw_map = value_lsh_match(
                keywords=keywords,
                database_name=db_id,
                lsh_client=lsh_client,
                top_k=top_k,
                score_threshold=lsh_threshold
            )
            
            # C3 回路：描述匹配
            desc_columns, desc_mappings, desc_kw_map, desc_kw_col_value_map = value_desc_match(
                keywords=keywords,
                database_name=db_id,
                metavisor_client=metavisor,
                top_k=top_k,
                score_threshold=desc_threshold
            )
            
            # 转换为 DataItem 格式
            lsh_tables_and_columns, lsh_recall_detail = convert_to_dataitem_format(
                lsh_columns, lsh_mappings, lsh_kw_map, "value_match_lsh"
            )
            desc_tables_and_columns, desc_recall_detail = convert_to_dataitem_format(
                desc_columns, desc_mappings, desc_kw_map, "value_match_desc"
            )
            
            # 保存结果到 DataItem
            with dataset_lock:
                item.value_match_lsh_tables_and_columns = lsh_tables_and_columns
                item.value_match_lsh_recall_path_detail = lsh_recall_detail
                item.value_match_lsh_values = lsh_mappings
                item.value_match_desc_tables_and_columns = desc_tables_and_columns
                item.value_match_desc_recall_path_detail = desc_recall_detail
                item.value_match_desc_values = desc_mappings
            
            # 更新统计
            lsh_matched = 1 if lsh_tables_and_columns else 0
            desc_matched = 1 if desc_tables_and_columns else 0
            
            logger.info(f"qid={qid}: LSH matched {len(lsh_tables_and_columns)} tables {sum(len(cols) for cols in lsh_tables_and_columns.values())} columns, "
                       f"enum desc matched {len(desc_tables_and_columns)} tables {sum(len(cols) for cols in desc_tables_and_columns.values())} columns")
            
            processing_time = time.time() - start_time
            with dataset_lock:
                item.value_match_time = processing_time
            logger.info(f"qid={qid}: Processing completed, took {processing_time:.2f} seconds")
            return True, lsh_matched, desc_matched, ""
            
        except Exception as e:
            logger.error(f"qid={qid}: Value Match processing failed: {e}")
            with dataset_lock:
                item.value_match_lsh_tables_and_columns = {}  # 置空结果
                item.value_match_lsh_recall_path_detail = []
                item.value_match_lsh_values = {}
                item.value_match_desc_tables_and_columns = {}
                item.value_match_desc_recall_path_detail = []
                item.value_match_desc_values = {}
                item.value_match_time = time.time() - start_time
            return False, 0, 0, str(e)
    
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
        pbar = tqdm(total=len(pending_items), desc="Value Match Linking", initial=0)
        for future in as_completed(futures):
            item = futures[future]
            try:
                ok, lsh_matched, desc_matched, err = future.result()
                qid = getattr(item, 'question_id', None)
                if ok:
                    total_lsh_matched += lsh_matched
                    total_desc_matched += desc_matched
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
                    if hasattr(it, "value_match_lsh_tables_and_columns")
                    and it.value_match_lsh_tables_and_columns is not None
                )
                logger.info(f"Checkpoint save: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2e",
                    name="column_value",
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
        logger.info("[VERIFY] Step2e: start rerun error questions")
        logger.info(f"[VERIFY] Step2e: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2e: rerun qids(all)={qids}")
        by_qid = {getattr(it, 'question_id', None): it for it in dataset}
        for qid in qids:
            item = by_qid.get(qid)
            if not item:
                continue
            ok, lsh_matched, desc_matched, err = process_single_item(item)
            if ok:
                total_lsh_matched += lsh_matched
                total_desc_matched += desc_matched
                del error_qid_to_msg[qid]
            else:
                error_qid_to_msg[qid] = err or error_qid_to_msg.get(qid, "unknown error")

        if error_qid_to_msg:
            remaining_qids = sorted(list(error_qid_to_msg.keys()))
            logger.info(f"[VERIFY] Step2e: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2e: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2e: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 最终统计
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, "value_match_lsh_tables_and_columns")
        and it.value_match_lsh_tables_and_columns is not None
    )
    final_lsh_matched = sum(
        1
        for it in dataset
        if hasattr(it, "value_match_lsh_tables_and_columns")
        and it.value_match_lsh_tables_and_columns
    )
    final_desc_matched = sum(
        1
        for it in dataset
        if hasattr(it, "value_match_desc_tables_and_columns")
        and it.value_match_desc_tables_and_columns
    )

    update_step2_state(
        step_id="2e",
        name="column_value",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    logger.info(f"=== Value Match Schema Linking Summary ===")
    logger.info(f"Total processed items: {final_completed}/{len(dataset)}")
    logger.info(f"Successfully LSH matched items: {final_lsh_matched}")
    logger.info(f"Successfully desc matched items: {final_desc_matched}")
    logger.info(f"LSH threshold: {lsh_threshold}, desc threshold: {desc_threshold}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2e error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info("=== Step 2e Completed ===")
    logger.info(f"\n")


if __name__ == "__main__":
    main()
