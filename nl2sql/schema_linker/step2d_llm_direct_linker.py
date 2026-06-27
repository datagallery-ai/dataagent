#!/usr/bin/env python3
"""
Step2d: LLM Schema Selection
LLM 分析问题与 schema → 直接选择相关表列（自研 schema 选择器实现）

步骤：
    1. 从 step2c 输出数据集加载带schema的数据
    2. 初始化 LLM 客户端
    3. 对每个问题：
       a. 生成schema profile
       b. 调用 LLM.ask() 进行批量采样
       c. 提取 <selection> 块内的 JSON 响应
       d. 使用schema映射工具进行大小写映射
       e. 合并多次采样结果存储到 llm_match_tables_and_columns
    4. 保存处理结果到 STEP2D_LLM_DIRECT_SAVE_PATH，为后续 Step2e/2f 提供输入
"""

import sys
import pickle
import copy
import json
import time
import logging
import argparse
import re
import threading
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from .data_types import DataItem
from .utils import read_pickle, write_pickle, ensure_dir, get_log_file_path, parse_range_arg, parse_question_ids_arg, filter_dataset_by_qid_range, filter_dataset_by_question_ids, load_dataset_with_checkpoint_merge, get_error_questions_path, load_error_questions, save_error_questions, update_step2_state
from ..client.llm_client import LLMAdapter
from .prompt_factory import PromptFactory
from .schema_utils import (
    get_database_schema_profile,
    map_lower_table_name_to_original_table_name,
    map_lower_column_name_to_original_column_name,
    merge_schema_linking_results
)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2d_llm_direct_linker'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class LLMSchemaSelector:
    """
    基于 LLM 的 schema 选择器：让大模型阅读问题与 schema profile，
    输出与问题相关的表/列子集。

    模型按 <selection> 块返回一个 {表名: [列名]} 的 JSON 对象，
    本类负责解析该 JSON、做大小写回写映射，并对多次采样结果求并集。
    """

    def link(self, data_item, llm, sampling_budget=1):
        """
        执行 schema 选择，分析问题并返回相关的表列信息。

        流程：
        1. 生成 schema profile
        2. 拼装包含问题、证据和 schema 的 prompt
        3. 调用 LLM 多次采样
        4. 解析每次返回的 JSON 选择结果
        5. 对多次采样取并集合并

        Args:
            data_item: DataItem对象，包含问题、证据和schema信息
            llm: LLM客户端实例，支持ask方法进行批量采样
            sampling_budget: 采样次数，默认为1次

        Returns:
            Tuple: (linked_tables_and_columns, token_usage_stats)
                - linked_tables_and_columns: 字典格式 {table_name: [column_names]}
                - token_usage_stats: Token使用统计信息
        """
        if sampling_budget == 0:
            return {}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        # 优先使用value_retrieval后的schema
        schema = getattr(data_item, "database_schema_after_value_retrieval", None) or getattr(data_item, "database_schema", None) or {}
        if not isinstance(schema, dict) or "tables" not in schema:
            return {}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        # 生成schema profile
        schema_profile = get_database_schema_profile(schema)
        
        evidence = data_item.evidence
        if isinstance(evidence, list):
            # 如果evidence是list，转换为字符串
            evidence = '; '.join(str(item) for item in evidence if item)
        prompt = PromptFactory.format_schema_selection_prompt(schema_profile, data_item.question, evidence).strip()
        
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        selections = []
        
        # 进行批量采样
        while len(selections) < sampling_budget:
            try:
                responses, tu, *_ = llm.ask(
                    prompt,
                    num_samples=sampling_budget - len(selections),
                    stop=["</selection>"]
                )
                if responses is None:
                    responses = []
                if tu is None:
                    tu = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

                if not responses:
                    logger.warning("LLMSchemaSelector LLM call returned empty responses")
                    break

                for resp in responses:
                    try:
                        if not resp:
                            continue
                        parsed = self._extract_relations(str(resp).strip(), schema)
                        if parsed:
                            selections.append(parsed)
                    except Exception as e:
                        logger.warning(f"LLMSchemaSelector parse error: {e}")
                
                # 累计token使用统计
                token_usage["prompt_tokens"] += int(tu.get("prompt_tokens", 0) or 0)
                token_usage["completion_tokens"] += int(tu.get("completion_tokens", 0) or 0)
                token_usage["total_tokens"] += int(tu.get("total_tokens", 0) or 0)
                
            except Exception as e:
                logger.warning(f"LLMSchemaSelector LLM call failed: {e}")
                raise
        
        return merge_schema_linking_results(selections), token_usage
    
    def _extract_relations(self, response, schema):
        """
        从 LLM 响应中提取 <selection> 块内的 JSON，得到表/列选择。

        期望格式：
        <selection>
        {"table_name": ["col_a", "col_b"], "another_table": ["col_c"]}
        </selection>

        解析时对表名/列名做大小写回写映射，丢弃 schema 中不存在的项。

        Args:
            response: LLM返回的原始文本响应
            schema: 数据库schema字典，用于大小写映射

        Returns:
            Dict[str, List[str]] or None: 解析结果字典 {table_name: [column_names]}
                如果解析失败则返回None
        """
        # 截取 <selection> 与 </selection> 之间的内容；缺失结束标签时取开标签之后全部
        start = response.find("<selection>")
        if start == -1:
            return None
        body = response[start + len("<selection>"):]
        end = body.find("</selection>")
        if end != -1:
            body = body[:end]

        # 容错：剥离可能存在的 ```json 代码围栏，仅保留首个 JSON 对象
        brace_start = body.find("{")
        brace_end = body.rfind("}")
        if brace_start == -1 or brace_end == -1 or brace_end < brace_start:
            return None
        try:
            raw_selection = json.loads(body[brace_start:brace_end + 1])
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw_selection, dict):
            return None

        result: Dict[str, List[str]] = {}
        for tbl_name, columns in raw_selection.items():
            orig_tbl = map_lower_table_name_to_original_table_name(str(tbl_name), schema)
            if not orig_tbl:
                continue
            if not isinstance(columns, (list, tuple)):
                columns = [columns]
            mapped_cols: List[str] = []
            for col_name in columns:
                orig_col = map_lower_column_name_to_original_column_name(orig_tbl, str(col_name), schema)
                if orig_col:
                    mapped_cols.append(orig_col)
            result[orig_tbl] = mapped_cols
        
        return result if result else None


def _init_llm() -> LLMAdapter:
    """初始化LLM客户端，使用LLMAdapter"""
    if not config.LLM_API_KEY:
        raise ValueError("LLM_API_KEY is empty")
    return LLMAdapter(
        api_base=config.LLM_API_BASE,
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,
        max_retries=config.LLM_MAX_RETRIES,
        retry_delay=config.LLM_RETRY_DELAY,
        verify_ssl=config.LLM_VERIFY_SSL,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Step 2d: LLM Direct Schema Linking')
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
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable detailed logging')
    parser.add_argument('--resume', '-r', action='store_true',
                       help='Resume from previous checkpoint (load from output file if exists)')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"\n")
    logger.info("=== Step 2d: LLM Direct Schema Linking ===")
    logger.info(f"Config: LLM_MODEL={config.LLM_MODEL}")
    logger.info(f"Config: LLM_DIRECT_LINKING_BUDGET={config.LLM_DIRECT_LINKING_BUDGET}")
    
    # 确定输入/输出路径 - 以 Step2c 列匹配输出作为输入
    output_path = args.output or config.STEP2D_LLM_DIRECT_SAVE_PATH
    checkpoint_path = Path(output_path)

    input_path = args.input
    if not input_path:
        step4_path = config.STEP2C_COLUMN_MATCH_SAVE_PATH
        if Path(step4_path).exists():
            input_path = step4_path
            logger.info(f"Using Step2c column_match data as input: {input_path}")
        else:
            logger.error("No input data found, please run step2c_column_match_linker.py first")
            sys.exit(1)

    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")
    
    if not Path(input_path).exists():
        if args.resume and checkpoint_path.exists():
            logger.warning(f"Base input missing ({input_path}), fallback to checkpoint-only: {checkpoint_path}")
        else:
            logger.error(f"Input file does not exist: {input_path}")
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
    completed = sum(1 for item in dataset if hasattr(item, 'llm_match_tables_and_columns') and item.llm_match_tables_and_columns is not None)
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")
    
    # 初始化 LLM 客户端
    logger.info("Initializing LLM client...")
    try:
        llm_client = _init_llm()
        logger.info("LLM client initialized successfully")
    except Exception as e:
        logger.error(f"LLM client initialization failed: {e}")
        sys.exit(1)
    
    # 初始化 LLM schema 选择器
    schema_selector = LLMSchemaSelector()
    logger.info("LLMSchemaSelector initialized")

    # 筛选需处理的项
    error_questions_path = get_error_questions_path(5)
    error_qid_to_msg = load_error_questions(error_questions_path)
    question_ids_set = set(question_ids or [])

    dataset_lock = threading.RLock()

    def snapshot_dataset_for_save():
        with dataset_lock:
            return [it.model_copy(deep=True) if hasattr(it, "model_copy") else copy.deepcopy(it) for it in dataset]

    pending_items = []
    for item in dataset:
        qid = getattr(item, 'question_id', None)
        needs_processing = (not hasattr(item, 'llm_match_tables_and_columns') or item.llm_match_tables_and_columns is None)
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
            if hasattr(it, 'llm_match_tables_and_columns')
            and it.llm_match_tables_and_columns is not None
        )
        update_step2_state(
            step_id="2d",
            name="llm_direct",
            status="done",
            completed_questions=final_completed,
            total_questions=len(dataset),
        )
        logger.info(f"Dataset saved to: {output_path}")
        return
    
    logger.info(f"Starting to process {len(pending_items)} pending items...")
    
    # 处理统计
    total_linked = 0
    total_token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    sampling_budget = config.LLM_DIRECT_LINKING_BUDGET
    
    def process_single_item(item):
        """处理单个项的 LLM 直接链接"""
        start_time = time.time()
        
        try:
            # 初始化 token 用量统计
            total_llm_cost = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            qid = getattr(item, 'question_id', 0)
            
            # 获取 schema（优先使用 value_retrieval 后的 schema）
            schema = getattr(item, 'database_schema_after_value_retrieval', None)
            if not schema:
                logger.error(f"qid={qid}: item not have database_schema_after_value_retrieval")
                with dataset_lock:
                    item.llm_match_tables_and_columns = {}
                    item.llm_match_time = time.time() - start_time
                return False, 0, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "missing database_schema_after_value_retrieval"
            
            if not schema or 'tables' not in schema:
                logger.warning(f"qid={qid}: No valid schema data, skipping")
                with dataset_lock:
                    item.llm_match_tables_and_columns = {}
                    item.llm_match_time = time.time() - start_time
                return False, 0, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "schema missing tables"
            
            logger.info(f"qid={qid}: Starting schema selection...")
            
            # 调用 LLM schema 选择器
            linked_result, token_usage = schema_selector.link(
                data_item=item,
                llm=llm_client,
                sampling_budget=sampling_budget
            )
            
            # 保存结果到 DataItem
            with dataset_lock:
                item.llm_match_tables_and_columns = linked_result
            
            linked_count = 1 if linked_result else 0
            if linked_result:
                columns_count = sum(len(cols) for cols in linked_result.values())
                logger.info(f"qid={qid}: schema selection found {len(linked_result)} tables, {columns_count} columns")
            else:
                logger.info(f"qid={qid}: schema selection found no relevant tables/columns")
            
            # 更新 token 用量
            for key in total_llm_cost:
                total_llm_cost[key] += token_usage[key]
            processing_time = time.time() - start_time
            with dataset_lock:
                item.llm_match_llm_cost = total_llm_cost
                item.llm_match_time = processing_time
            logger.info(f"qid={qid}: Processing completed, took {processing_time:.2f} seconds")
            return True, linked_count, token_usage, ""
            
        except Exception as e:
            logger.error(f"qid={qid}: schema selection processing failed: {e}")
            with dataset_lock:
                item.llm_match_tables_and_columns = {}  # 置空结果
                item.llm_match_time = time.time() - start_time
            return False, 0, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, str(e)
    
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
        pbar = tqdm(total=len(pending_items), desc="LLM Direct Linking", initial=0)
        for future in as_completed(futures):
            item = futures[future]
            try:
                ok, linked_count, token_usage, err = future.result()
                qid = getattr(item, 'question_id', None)
                if ok:
                    total_linked += linked_count
                    total_token_usage["prompt_tokens"] += token_usage.get("prompt_tokens", 0)
                    total_token_usage["completion_tokens"] += token_usage.get("completion_tokens", 0)
                    total_token_usage["total_tokens"] += token_usage.get("total_tokens", 0)
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
                    if hasattr(it, 'llm_match_tables_and_columns')
                    and it.llm_match_tables_and_columns is not None
                )
                logger.info(f"Checkpoint saved: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2d",
                    name="llm_direct",
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
        logger.info("[VERIFY] Step2d: start rerun error questions")
        logger.info(f"[VERIFY] Step2d: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2d: rerun qids(all)={qids}")
        by_qid = {getattr(it, 'question_id', None): it for it in dataset}
        for qid in qids:
            item = by_qid.get(qid)
            if not item:
                continue
            ok, linked_count, token_usage, err = process_single_item(item)
            if ok:
                total_linked += linked_count
                total_token_usage["prompt_tokens"] += token_usage.get("prompt_tokens", 0)
                total_token_usage["completion_tokens"] += token_usage.get("completion_tokens", 0)
                total_token_usage["total_tokens"] += token_usage.get("total_tokens", 0)
                del error_qid_to_msg[qid]
            else:
                error_qid_to_msg[qid] = err or error_qid_to_msg.get(qid, "unknown error")

        if error_qid_to_msg:
            remaining_qids = sorted(list(error_qid_to_msg.keys()))
            logger.info(f"[VERIFY] Step2d: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2d: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2d: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 统计
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, 'llm_match_tables_and_columns')
        and it.llm_match_tables_and_columns is not None
    )
    final_linked = sum(
        1
        for it in dataset
        if hasattr(it, 'llm_match_tables_and_columns')
        and it.llm_match_tables_and_columns
    )

    update_step2_state(
        step_id="2d",
        name="llm_direct",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    logger.info(f"=== LLM Direct Schema Linking Summary ===")
    logger.info(f"Total processed items: {final_completed}/{len(dataset)}")
    logger.info(f"Successfully linked items: {final_linked}")
    logger.info(f"Token usage: prompt={total_token_usage['prompt_tokens']}, completion={total_token_usage['completion_tokens']}, total={total_token_usage['total_tokens']}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2d error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info(f"=== Step 2d Completed ===")
    logger.info(f"\n")

if __name__ == "__main__":
    main()
