#!/usr/bin/env python3
"""
Step2h: JOIN Closure Schema Linking
JOIN关系获取和召回路径聚合

功能说明：
    此步骤包含两个处理阶段：
    1. 合并前序回路召回结果，生成candidate_columns和candidate_columns_detail
    2. 执行JOIN关系获取，完全参照schema_linker.py的"阶段3: JOIN 关系获取"逻辑

处理流程：
    1. 从step2g输出数据集加载数据
    2. 阶段1: 合并所有前序回路的召回结果到candidate_columns和candidate_columns_detail
    3. 阶段2: 基于候选列调用resolve_joins获取JOIN关系和JOIN键列
    4. 更新DataItem相关字段并保存数据集
"""

import argparse
import itertools
import copy
import json
import logging
import re
import sys
import time
import pickle
from pathlib import Path
from typing import Dict, Any, List, Set, Tuple
from collections import defaultdict
import threading
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
        logging.FileHandler(get_log_file_path('step2h_join_closure_linker'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def resolve_joins(
    candidate_columns: Set[str],
    database_name: str,
    metavisor_client: LocalArtifactClient,
    max_depth: int = 5,
    max_depth_limit: int = None,
) -> Tuple[List[Dict], Set[str]]:
    """
    阶段3：JOIN 关系获取 + Relational Closure
    完全参照join_resolver.py的实现

    从候选列中提取涉及的表，对所有表两两调用 table-relations-path 接口，
    汇总全链路 JOIN 路径，并返回关系列表和 JOIN 键列集合（Relational Closure）。

    Args:
        candidate_columns: 候选列 ID 集合，格式为 "database.table.column"
        database_name:     数据库名
        metavisor_client:  MetaVisor REST 客户端
        max_depth:         JOIN 路径最大跳数，默认 5
        max_depth_limit:   JOIN 路径最大跳数限制

    Returns:
        (join_relations, join_key_columns)
    """
    # 1. 提取 "db.table" 格式的表集合
    db_tables = _extract_db_tables(candidate_columns, database_name)

    # 2. 少于 2 张表时无需查询
    if len(db_tables) < 2:
        logger.info("Fewer than 2 tables, skipping JOIN resolution")
        return [], set()

    # 3. 两两查询 JOIN 路径，构建路径条目（笛卡尔积展开，过滤，去重）
    join_relations: List[Dict] = []
    seen: Set[tuple] = set()

    for db_table1, db_table2 in itertools.combinations(sorted(db_tables), 2):
        paths = metavisor_client.get_table_relations_path(db_table1, db_table2, max_depth)
        for entry in _build_path_entries(db_table1, db_table2, paths):
            key = (entry["source"], entry["target"], tuple(entry["path"]))
            if key not in seen:
                seen.add(key)
                join_relations.append(entry)

    join_columns: Set[str] = _extract_join_columns(join_relations)

    logger.info(
        f"Resolved {len(join_relations)} JOIN paths "
        f"across {len(db_tables)} tables, added {len(join_columns)} JOIN key columns"
    )
    return join_relations, join_columns

def _extract_db_tables(candidate_columns: Set[str], database_name: str) -> Set[str]:
    """
    从候选列 ID 集合中提取 "database.table" 格式的表名集合
    """
    db_tables: Set[str] = set()
    for col_id in candidate_columns:
        parts = col_id.split(".")
        if len(parts) == 3:
            db_tables.add(f"{parts[0]}.{parts[1]}")
        elif len(parts) == 2:
            # 格式 table.column（兼容处理）
            db_tables.add(f"{database_name}.{parts[0]}")
    return db_tables

def _build_path_entries(
    db_table1: str,
    db_table2: str,
    paths: List[List[Dict]],
) -> List[Dict]:
    """
    从 table-relations-path API 返回的路径集中构建路径条目
    """
    entries: List[Dict] = []
    for path_steps in paths:
        # 收集每个 step 的有效条件列表
        step_conditions: List[List[str]] = []
        for step in path_steps:
            raw_expr = step.get("expression", "[]")
            try:
                conditions = json.loads(raw_expr) if isinstance(raw_expr, str) else raw_expr
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Failed to parse expression: {raw_expr!r}")
                conditions = []
            valid = [c for c in conditions if isinstance(c, str) and c.strip()]
            if valid:
                step_conditions.append(valid)

        if not step_conditions:
            continue

        # 笛卡尔积：每一跳选一个条件，组合成完整路径
        for combo in itertools.product(*step_conditions):
            entries.append({
                "source": db_table1,
                "target": db_table2,
                "path":   list(combo),
            })
    return entries

def _extract_join_columns(join_relations: List[Dict]) -> Set[str]:
    """
    从 join_relations 路径条目中提取所有涉及的列 ID
    """
    columns: Set[str] = set()
    for jr in join_relations:
        for cond in jr.get("path", []):
            parts = re.split(r"\s*=\s*", cond, maxsplit=1)
            if len(parts) == 2:
                left, right = parts[0].strip(), parts[1].strip()
                if left:
                    columns.add(left)
                if right:
                    columns.add(right)
    return columns

def merge_recall_results(candidate_columns: dict, join_columns: set, recall_type: str):
    """
    合并召回结果
    
    将JOIN键列添加到候选列字典中，为已存在的列添加召回路径
    
    Args:
        candidate_columns: 候选列字典
        join_columns: JOIN列集合  
        recall_type: 召回类型
    """
    for column_id in join_columns:
        if column_id not in candidate_columns:
            # 创建新条目
            candidate_columns[column_id] = {
                "values": [],
                "recall_path": [recall_type]
            }
        else:
            # 为已存在的列添加召回路径（避免重复）
            if recall_type not in candidate_columns[column_id]["recall_path"]:
                candidate_columns[column_id]["recall_path"].append(recall_type)

def merge_all_recall_paths(data_item) -> Tuple[List[str], Dict[str, Any]]:
    """
    阶段1: 合并所有前序回路的召回结果
    
    生成candidate_columns列表和candidate_columns_detail字典
    
    Args:
        data_item: DataItem对象
        
    Returns:
        Tuple: (candidate_columns, candidate_columns_detail)
    """
    candidate_columns = []
    candidate_columns_detail = {}
    database_name = data_item.database_id
    question_id = data_item.question_id
    
    # 定义前序回路字段映射
    recall_fields = [
        ('column_match_tables_and_columns', 'column_match'),
        ('llm_match_tables_and_columns', 'llm_match'), 
        ('value_match_lsh_tables_and_columns', 'value_match_lsh'),
        ('value_match_desc_tables_and_columns', 'value_match_desc'),
        ('value_retrieval_tables_and_columns', 'value_retrieval'),
        ('sql_reversed_tables_and_columns', 'sql_reversed')
    ]
    
    # 遍历所有前序回路字段
    for field_name, recall_name in recall_fields:
        tables_and_columns = getattr(data_item, field_name, None) or {}
        
        # 将tables_and_columns格式转换为column_id格式
        for table_name, columns in tables_and_columns.items():
            for column_name in columns:
                # 构造database.table.column格式的列ID
                column_id = f"{database_name}.{table_name}.{column_name}"
                
                # 添加到candidate_columns列表（去重）
                if column_id not in candidate_columns:
                    candidate_columns.append(column_id)
                
                # 添加到candidate_columns_detail字典
                if column_id not in candidate_columns_detail:
                    candidate_columns_detail[column_id] = {
                        "values": [],  # 暂时设置为空列表
                        "recall_path": []
                    }
                
                # 添加召回路径信息（避免重复）
                if recall_name not in candidate_columns_detail[column_id]["recall_path"]:
                    candidate_columns_detail[column_id]["recall_path"].append(recall_name)
    
    logger.info(f"qid={question_id}: join_closure Stage1, Merged {len(candidate_columns)} candidate columns from {len(recall_fields)} recall paths")
    return candidate_columns, candidate_columns_detail

def convert_join_columns_to_tables_and_columns(join_columns: Set[str]) -> Dict[str, List[str]]:
    """
    将JOIN键列ID集合转换为tables_and_columns格式
    
    Args:
        join_columns: JOIN键列ID集合（database.table.column格式）
        
    Returns:
        tables_and_columns格式的字典
    """
    tables_and_columns = defaultdict(list)
    
    for column_id in join_columns:
        parts = column_id.split('.')
        if len(parts) >= 3:
            # 提取表名和列名（忽略数据库名）
            table_name = parts[1]
            column_name = parts[2]
            tables_and_columns[table_name].append(column_name)
    
    # 去重并转换为普通字典
    result = {}
    for table_name, columns in tables_and_columns.items():
        result[table_name] = list(set(columns))
    
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Step2h: JOIN Closure Schema Linking')
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
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='启用详细日志')
    parser.add_argument('--resume', '-r', action='store_true',
                       help='Resume from previous checkpoint (load from output file if exists)')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"\n")
    logger.info("=== Step2h: JOIN Closure Schema Linking ===")
    logger.info(f"JOIN Max Depth: {config.JOIN_MAX_DEPTH}")
    
    # 确定输入输出路径 - 输入必须是 Step2g 的输出
    input_path = args.input or config.STEP2G_SQL_REVERSED_SAVE_PATH
    output_path = args.output or config.STEP2H_JOIN_CLOSURE_SAVE_PATH

    checkpoint_path = Path(output_path)
    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")
    
    if not Path(input_path).exists():
        if args.resume and checkpoint_path.exists():
            logger.warning(f"Base input missing ({input_path}), fallback to checkpoint-only: {checkpoint_path}")
        else:
            logger.error(f"Input file not found: {input_path}")
            logger.error("Please run step2g_sql_reversed_linker.py first")
            sys.exit(1)
        
    logger.info(f"Input file: {input_path}")
    logger.info(f"Output file: {output_path}")

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
    completed = sum(1 for item in dataset 
                   if hasattr(item, 'candidate_columns') and item.candidate_columns is not None
                   and hasattr(item, 'candidate_columns_detail') and item.candidate_columns_detail is not None
                   and hasattr(item, 'join_closure_tables_and_columns') and item.join_closure_tables_and_columns is not None
                   and hasattr(item, 'join_relations') and item.join_relations is not None)
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")
    
    # 初始化MetaVisor客户端
    logger.info("Initializing MetaVisor client...")
    try:
        metavisor_client = LocalArtifactClient(
            workspace_root=config.STEP1_PREPROCESS_WORKSPACE_DIR,
            cache_dir=config.STEP1_CACHE_DIR
        )
        logger.info("MetaVisor client initialized successfully")
    except Exception as e:
        logger.error(f"MetaVisor client initialization failed: {e}")
        sys.exit(1)
    
    # 过滤需要处理的项目
    error_questions_path = get_error_questions_path(9)
    error_qid_to_msg = load_error_questions(error_questions_path)
    question_ids_set = set(question_ids or [])

    dataset_lock = threading.RLock()

    def snapshot_dataset_for_save():
        with dataset_lock:
            return [item.model_copy(deep=True) if hasattr(item, "model_copy") else copy.deepcopy(item) for item in dataset]

    pending_items = []
    for item in dataset:
        qid = getattr(item, 'question_id', None)
        needs_processing = (
                           not hasattr(item, 'candidate_columns') or item.candidate_columns is None or
                           not hasattr(item, 'candidate_columns_detail') or item.candidate_columns_detail is None or
                           not hasattr(item, 'join_closure_tables_and_columns') or item.join_closure_tables_and_columns is None or
                           not hasattr(item, 'join_relations') or item.join_relations is None)
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
            if hasattr(it, "candidate_columns") and it.candidate_columns is not None
        )
        update_step2_state(
            step_id="2h",
            name="join_closure",
            status="done",
            completed_questions=final_completed,
            total_questions=len(dataset),
        )
        logger.info(f"Dataset saved to: {output_path}")
        return
    
    logger.info(f"Starting to process {len(pending_items)} pending items...")
    
    # 处理统计
    total_with_joins = 0
    total_join_columns = 0
    
    # JOIN配置参数
    join_max_depth = config.JOIN_MAX_DEPTH
    
    def process_single_item(item) -> Tuple[int, int]:
        """处理单个项的 JOIN 闭包（两阶段）。返回 (with_joins, join_columns_count)。"""
        start_time = time.time()

        try:
            qid = getattr(item, 'question_id', 0)

            # 检查必要的数据
            if not hasattr(item, 'database_id') or not item.database_id:
                logger.warning(f"qid={qid}: No database_id data, skipping")
                with dataset_lock:
                    item.candidate_columns = []
                    item.candidate_columns_detail = {}
                    item.join_closure_tables_and_columns = {}
                    item.join_relations = []
                    item.join_closure_time = time.time() - start_time
                return False, 0, 0, "missing database_id"

            logger.info(f"qid={qid}: Starting join_closure two-stage processing...")

            # === 阶段1: 合并所有前序回路的召回结果 ===
            candidate_columns, candidate_columns_detail = merge_all_recall_paths(item)

            if not candidate_columns:
                logger.error(f"qid={qid}: No candidate columns found, skipping JOIN resolution")
                with dataset_lock:
                    item.candidate_columns = []
                    item.candidate_columns_detail = {}
                    item.join_closure_tables_and_columns = {}
                    item.join_relations = []
                    item.join_closure_time = time.time() - start_time
                return False, 0, 0, "no candidate columns"

            # === 阶段2: JOIN 关系获取 ===
            database_name = item.database_id
            candidate_columns_set = set(candidate_columns)  # 转换为集合格式

            logger.info(f"qid={qid}: Processing {len(candidate_columns_set)} candidate columns for database {database_name}")

            join_relations, join_columns = resolve_joins(
                candidate_columns_set,
                database_name,
                metavisor_client,
                max_depth=join_max_depth,
            )

            # 将JOIN键列合并到候选列详情中（为已存在的列添加join_closure召回路径）
            merge_recall_results(candidate_columns_detail, join_columns, "join_closure")

            # 将JOIN键列也合并到candidate_columns列表中（与阶段1相同的合并方式）
            for join_column_id in join_columns:
                if join_column_id not in candidate_columns:
                    candidate_columns.append(join_column_id)

            # 将JOIN键列转换为tables_and_columns格式
            join_closure_tables_and_columns = convert_join_columns_to_tables_and_columns(join_columns)

            # 更新DataItem的指定字段（加锁避免与保存线程并发冲突）
            with dataset_lock:
                item.candidate_columns = candidate_columns
                item.candidate_columns_detail = candidate_columns_detail
                item.join_closure_tables_and_columns = join_closure_tables_and_columns
                item.join_relations = join_relations

            join_columns_count = sum(len(cols) for cols in join_closure_tables_and_columns.values()) if join_closure_tables_and_columns else 0
            with_joins = 1 if join_closure_tables_and_columns else 0

            if with_joins:
                logger.info(
                    f"qid={qid}: Found {len(join_closure_tables_and_columns)} tables, {join_columns_count} JOIN columns, {len(join_relations)} relations"
                )
            else:
                logger.info(f"qid={qid}: No JOIN columns found")

            processing_time = time.time() - start_time
            with dataset_lock:
                item.join_closure_time = processing_time
            logger.info(f"qid={qid}: Two-stage join_closure recall completed in {processing_time:.2f}s")
            return True, with_joins, join_columns_count, ""

        except Exception as e:
            qid = getattr(item, 'question_id', 0)
            logger.error(f"qid={qid}: JOIN closure recall processing failed: {e}")
            with dataset_lock:
                item.candidate_columns = []
                item.candidate_columns_detail = {}
                item.join_closure_tables_and_columns = {}
                item.join_relations = []
                item.join_closure_time = time.time() - start_time
            return False, 0, 0, str(e)

    # 并行处理
    n_parallel = config.SCHEMA_LINKING_N_PARALLEL
    logger.info(f"Processing {len(pending_items)} items with {n_parallel} parallel workers")

    completed = 0
    save_interval = config.SCHEMA_LINKING_SAVE_INTERVAL

    with ThreadPoolExecutor(max_workers=n_parallel) as executor:
        futures = {executor.submit(process_single_item, item): item for item in pending_items}
        pbar = tqdm(total=len(pending_items), desc="JOIN Closure Processing", initial=0)
        for future in as_completed(futures):
            item = futures[future]
            try:
                ok, with_joins, join_cols, err = future.result()
                qid = getattr(item, 'question_id', None)
                if ok:
                    total_with_joins += with_joins
                    total_join_columns += join_cols
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

            # 周期性保存（主线程）
            if completed % save_interval == 0:
                ensure_dir(Path(output_path).parent)
                write_pickle(output_path, snapshot_dataset_for_save())
                completed_now = sum(
                    1
                    for _item in dataset
                    if hasattr(_item, 'candidate_columns') and _item.candidate_columns is not None
                )
                logger.info(f"Checkpoint saved: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2h",
                    name="join_closure",
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
        logger.info("[VERIFY] Step2h: start rerun error questions")
        logger.info(f"[VERIFY] Step2h: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2h: rerun qids(all)={qids}")
        by_qid = {getattr(it, 'question_id', None): it for it in dataset}
        for qid in qids:
            item = by_qid.get(qid)
            if not item:
                continue
            ok, with_joins, join_cols, err = process_single_item(item)
            if ok:
                total_with_joins += with_joins
                total_join_columns += join_cols
                del error_qid_to_msg[qid]
            else:
                error_qid_to_msg[qid] = err or error_qid_to_msg.get(qid, "unknown error")

        if error_qid_to_msg:
            remaining_qids = sorted(list(error_qid_to_msg.keys()))
            logger.info(f"[VERIFY] Step2h: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2h: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2h: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 统计结果
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, 'candidate_columns') and it.candidate_columns is not None
    )
    final_with_joins = sum(
        1
        for it in dataset
        if hasattr(it, 'join_closure_tables_and_columns') and it.join_closure_tables_and_columns
    )
    final_with_candidate_detail = sum(
        1
        for it in dataset
        if hasattr(it, 'candidate_columns_detail') and it.candidate_columns_detail
    )

    update_step2_state(
        step_id="2h",
        name="join_closure",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    logger.info(f"=== JOIN Closure Schema Linking Summary ===")
    logger.info(f"Total processed items: {final_completed}/{len(dataset)}")
    logger.info(f"Items with candidate columns detail: {final_with_candidate_detail}")
    logger.info(f"Items with JOIN columns: {final_with_joins}")
    logger.info(f"Total JOIN columns found: {total_join_columns}")
    logger.info(f"Average JOIN columns per item: {total_join_columns / max(len(pending_items), 1):.1f}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2h error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info("=== Step 2h Completed ===")
    logger.info(f"\n")


if __name__ == "__main__":
    main()
