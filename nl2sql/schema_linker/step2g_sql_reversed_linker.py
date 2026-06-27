"""
Step2g: SQL-Backed Schema Selection
基于 LLM 反推式 schema 选择 - 让 LLM 在生成 SQL 的同时直接给出该 SQL 使用的表/列映射

功能说明：
    此步骤使用 LLM 反推式地验证 schema linking 结果。让 LLM 基于问题和 schema 起草 SQL，
    并在同一个结构化 JSON 响应中直接返回该 SQL 引用的表与列映射

处理流程：
    1. 从step2f输出数据集加载包含value_retrieval_tables_and_columns的数据
    2. 初始化 LLMAdapter 适配器
    3. 对每个问题执行 SQL 反推式 schema 选择：
       a. 使用schema_utils生成数据库schema profile
       b. 构建 prompt（包含few-shot示例，要求 LLM 同时输出 SQL 与表列映射）
       c. 调用LLM采样生成多个结构化响应
       d. 直接从每个 JSON 响应中读取表列映射并做大小写回写
       e. 合并多次采样结果
    4. 将结果保存到sql_reversed_tables_and_columns字段
    5. 保存处理完成的数据集
"""

import sys
import pickle
import copy
import time
import logging
import argparse
import re
import json
import threading
from pathlib import Path
from typing import Dict, Any, List, Tuple
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from .data_types import DataItem
from .utils import read_pickle, write_pickle, ensure_dir, get_log_file_path, parse_range_arg, parse_question_ids_arg, filter_dataset_by_qid_range, filter_dataset_by_question_ids, load_dataset_with_checkpoint_merge, get_error_questions_path, load_error_questions, save_error_questions, update_step2_state
from ..client.llm_client import LLMAdapter
from .schema_utils import get_database_schema_profile, map_lower_table_name_to_original_table_name, map_lower_column_name_to_original_column_name, merge_schema_linking_results
from .prompt_factory import PromptFactory

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2g_sql_reversed_linker'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class SQLBackedSelector:
    """SQL 反推式 schema 选择器

    功能：让 LLM 在生成 SQL 的同时，直接在结构化 JSON 中给出该 SQL 使用到的表/列映射，
          作为反向 schema 链接验证。不再由代码解析 SQL 文本来提取表列。
    特点：支持多次采样生成多个候选，提高召回覆盖率
    """
    
    def __init__(self, few_shot_examples_path: str):
        """
        初始化 SQLBackedSelector
        
        Args:
            few_shot_examples_path: few-shot示例文件路径
        """
        with open(few_shot_examples_path, "r", encoding="utf-8") as f:
            self._few_shot_examples = json.load(f)
    
    def link(self, data_item, llm, sampling_budget: int = 1) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
        """
        执行 SQL 反推式 schema 选择
        
        处理流程：
        1. 检查采样预算，如果为0直接返回空结果
        2. 使用database_schema_after_value_retrieval生成schema profile
        3. 获取few-shot示例并构建prompt（要求LLM同时输出SQL与表列映射）
        4. 循环采样直到达到预算或生成足够结果
        5. 从每个JSON响应中直接读取表列映射并做大小写回写
        6. 合并所有采样结果并返回
        
        Args:
            data_item: DataItem对象，需包含question、evidence、schema等信息
            llm: LLMAdapter实例，兼容ask方法
            sampling_budget: 采样次数预算
            
        Returns:
            Tuple: (merged_tables_and_columns, token_usage)
        """
        # 采样预算检查
        if sampling_budget == 0:
            return {}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        # 使用增强后的schema生成profile
        schema_profile = get_database_schema_profile(data_item.database_schema_after_value_retrieval)
        
        # 获取few-shot示例
        few_shot = self._few_shot_examples.get(str(data_item.question_id), [])
        
        # 构建 prompt（要求 LLM 在 JSON 中同时返回 reasoning / sql / sql_used_tables / sql_used_columns）
        prompt = PromptFactory.format_sql_backed_selection_prompt(
            few_shot, schema_profile, data_item.question, data_item.evidence
        ).strip()
        
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        selections = []
        
        # 循环采样直到达到预算
        while len(selections) < sampling_budget:
            try:
                # 调用LLM生成结构化响应
                responses, tu, *_ = llm.ask(
                    prompt,
                    num_samples=sampling_budget - len(selections),
                )
                
                # 处理每个响应
                for resp in responses:
                    try:
                        # 直接从 JSON 响应中读取 sql_used_tables / sql_used_columns 并回写
                        extracted = self._extract_selection(str(resp).strip(), data_item.database_schema_after_value_retrieval)
                        if extracted is not None:
                            selections.append(extracted)
                    except Exception as e:
                        logger.error(f"SQLBackedSelector parse error: {e}")
                
                # 累计token使用统计
                token_usage["prompt_tokens"] += tu["prompt_tokens"]
                token_usage["completion_tokens"] += tu["completion_tokens"]
                token_usage["total_tokens"] += tu["total_tokens"]
                
            except Exception as e:
                logger.warning(f"SQLBackedSelector LLM call failed: {e}")
                raise
        
        # 合并所有采样结果
        merged_result = merge_schema_linking_results(selections)
        return merged_result, token_usage
    
    def _extract_selection(self, response: str, schema: Dict) -> Dict[str, List[str]]:
        """从 LLM 的 JSON 响应中读取表/列清单并做大小写回写

        模型按约定返回单个 JSON 对象，含 reasoning / sql / sql_used_tables /
        sql_used_columns 四字段。本方法只消费 sql_used_tables 与 sql_used_columns
        （列为 "表名.列名" 形式），不再解析 SQL 文本提取表列；随后将每个表名/列名
        映射回 schema 中的原始大小写形式，并归并为下游所需的 {表: [列]} 结构。

        Args:
            response: LLM原始响应内容
            schema: 数据库schema字典

        Returns:
            映射回原始大小写的 {表: [列]} 字典，无法解析时返回 None
        """
        payload = self._load_json_payload(response)
        if payload is None:
            return None

        used_tables = payload.get("sql_used_tables")
        used_columns = payload.get("sql_used_columns")
        if not isinstance(used_tables, (list, tuple)) or not isinstance(used_columns, (list, tuple)):
            return None

        result: Dict[str, List[str]] = {}

        # 先按 sql_used_tables 初始化各表条目（即使该表暂无列也保留）
        for tbl_name in used_tables:
            orig_t = map_lower_table_name_to_original_table_name(str(tbl_name).lower(), schema)
            if orig_t and orig_t not in result:
                result[orig_t] = []

        # 再用 "表名.列名" 形式的 sql_used_columns 填充各表的列
        for qualified in used_columns:
            text = str(qualified)
            if "." not in text:
                continue
            tbl_part, col_part = text.split(".", 1)
            orig_t = map_lower_table_name_to_original_table_name(tbl_part.strip().lower(), schema)
            if not orig_t:
                continue
            orig_c = map_lower_column_name_to_original_column_name(orig_t, col_part.strip().lower(), schema)
            if not orig_c:
                continue
            cols = result.setdefault(orig_t, [])
            if orig_c not in cols:
                cols.append(orig_c)

        return result


    @staticmethod
    def _load_json_payload(response: str) -> Dict:
        """从原始响应文本中提取并解析 JSON 对象

        先尝试直接 json.loads；失败时回退到截取首个 `{` 到末个 `}` 的子串再解析，
        以兼容模型在 JSON 前后附带说明文字或 ```json``` 围栏的情形。无法解析返回 None。
        """
        if not response:
            return None
        text = response.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        candidates = [text]
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            candidates.append(text[brace_start:brace_end + 1])
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except (ValueError, json.JSONDecodeError):
                continue
        return None



def _init_llm() -> LLMAdapter:
    """
    初始化LLMAdapter适配器
    
    Returns:
        配置好的LLMAdapter实例
        
    Raises:
        ValueError: 当LLM_API_KEY为空时抛出异常
    """
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
    parser = argparse.ArgumentParser(description='Step 2g: SQL Reversed Schema Linking')
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
    logger.info("=== Step 2g: SQL Reversed Schema Linking ===")
    logger.info(f"LLM Model: {config.LLM_MODEL}")
    logger.info(f"Reversed Linking Budget: {config.REVERSED_LINKING_BUDGET}")
    
    # 确定输入输出路径 - 输入必须是 Step2f 的输出
    input_path = args.input or config.STEP2F_VALUE_RETRIEVAL_SAVE_PATH
    output_path = args.output or config.STEP2G_SQL_REVERSED_SAVE_PATH

    checkpoint_path = Path(output_path)
    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")
    
    if not Path(input_path).exists():
        if args.resume and checkpoint_path.exists():
            logger.warning(f"Base input missing ({input_path}), fallback to checkpoint-only: {checkpoint_path}")
        else:
            logger.error(f"Input file not found: {input_path}")
            logger.error("Please run step2f_value_retrieval_linker.py first")
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
    completed = sum(1 for item in dataset if hasattr(item, 'sql_reversed_tables_and_columns') and item.sql_reversed_tables_and_columns is not None)
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")
    
    # 初始化LLMAdapter
    logger.info("Initializing LLMAdapter...")
    try:
        llm_adapter = _init_llm()
        logger.info("LLMAdapter initialized successfully")
    except Exception as e:
        logger.error(f"LLMAdapter initialization failed: {e}")
        sys.exit(1)
    
    # 检查few-shot文件
    if not Path(config.FEW_SHOT_PATH).exists():
        logger.error(f"Few-shot file not found: {config.FEW_SHOT_PATH}")
        sys.exit(1)
    
    # 初始化SQLBackedSelector
    reversed_linker = SQLBackedSelector(config.FEW_SHOT_PATH)
    logger.info("SQLBackedSelector initialized successfully")

    # 过滤需要处理的项目
    error_questions_path = get_error_questions_path(8)
    error_qid_to_msg = load_error_questions(error_questions_path)
    question_ids_set = set(question_ids or [])

    dataset_lock = threading.RLock()

    def snapshot_dataset_for_save():
        with dataset_lock:
            return [it.model_copy(deep=True) if hasattr(it, "model_copy") else copy.deepcopy(it) for it in dataset]

    pending_items = []
    for item in dataset:
        qid = getattr(item, 'question_id', None)
        needs_processing = (not hasattr(item, 'sql_reversed_tables_and_columns') or item.sql_reversed_tables_and_columns is None)
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
            if hasattr(it, "sql_reversed_tables_and_columns")
            and it.sql_reversed_tables_and_columns is not None
        )
        update_step2_state(
            step_id="2g",
            name="sql_reversed",
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
    sampling_budget = config.REVERSED_LINKING_BUDGET
    
    def process_single_item(item):
        """处理单个项的 SQL 反推链接"""
        start_time = time.time()
        
        try:
            # 初始化 token 用量统计
            total_llm_cost = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            qid = getattr(item, 'question_id', 0)
            
            # 检查database_schema_after_value_retrieval数据
            if not hasattr(item, 'database_schema_after_value_retrieval') or not item.database_schema_after_value_retrieval:
                logger.warning(f"qid={qid}: No database_schema_after_value_retrieval data, skipping")
                item.sql_reversed_tables_and_columns = {}
                item.sql_reversed_time = time.time() - start_time
                return False, 0, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "missing database_schema_after_value_retrieval"
            
            logger.info(f"qid={qid}: Starting SQLBackedSelector processing...")
            
            # 调用SQLBackedSelector进行 SQL 反推式 schema 选择
            linked_result, token_usage = reversed_linker.link(
                data_item=item,
                llm=llm_adapter,
                sampling_budget=sampling_budget
            )
            
            # 保存结果到DataItem的指定字段
            with dataset_lock:
                item.sql_reversed_tables_and_columns = linked_result

            linked_count = 1 if linked_result else 0
            if linked_result:
                columns_count = sum(len(cols) for cols in linked_result.values())
                logger.debug(f"qid={qid}: SQLBackedSelector found {len(linked_result)} tables, {columns_count} columns")
            else:
                logger.debug(f"qid={qid}: SQLBackedSelector found no relevant tables/columns")

            # 更新 token 用量
            for key in total_llm_cost:
                total_llm_cost[key] += token_usage[key]
            processing_time = time.time() - start_time
            with dataset_lock:
                item.sql_reversed_llm_cost = total_llm_cost
                item.sql_reversed_time = processing_time
            logger.info(f"qid={qid}: Processing completed in {processing_time:.2f}s")
            return True, linked_count, token_usage, ""

        except Exception as e:
            logger.error(f"qid={qid}: SQLBackedSelector processing failed: {e}")
            with dataset_lock:
                item.sql_reversed_tables_and_columns = {}  # 设置空结果
                item.sql_reversed_time = time.time() - start_time
            return False, 0, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, str(e)

    # 并行处理
    n_parallel = config.SCHEMA_LINKING_N_PARALLEL
    logger.info(f"Processing {len(pending_items)} items with {n_parallel} parallel workers")

    completed = 0
    save_interval = config.SCHEMA_LINKING_SAVE_INTERVAL

    with ThreadPoolExecutor(max_workers=n_parallel) as executor:
        futures = {executor.submit(process_single_item, item): item for item in pending_items}
        pbar = tqdm(total=len(pending_items), desc="SQL Reversed Linking", initial=0)
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

            # 周期性保存（主线程）
            if completed % save_interval == 0:
                ensure_dir(Path(output_path).parent)
                write_pickle(output_path, snapshot_dataset_for_save())
                completed_now = sum(
                    1
                    for _item in dataset
                    if hasattr(_item, 'sql_reversed_tables_and_columns')
                    and _item.sql_reversed_tables_and_columns is not None
                )
                logger.info(f"Checkpoint saved: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2g",
                    name="sql_reversed",
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
        logger.info("[VERIFY] Step2g: start rerun error questions")
        logger.info(f"[VERIFY] Step2g: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2g: rerun qids(all)={qids}")
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
            logger.info(f"[VERIFY] Step2g: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2g: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2g: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 统计结果
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, 'sql_reversed_tables_and_columns')
        and it.sql_reversed_tables_and_columns is not None
    )
    final_linked = sum(
        1
        for it in dataset
        if hasattr(it, 'sql_reversed_tables_and_columns')
        and it.sql_reversed_tables_and_columns
    )

    update_step2_state(
        step_id="2g",
        name="sql_reversed",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    logger.info(f"=== SQL Reversed Schema Linking Summary ===")
    logger.info(f"Total processed items: {final_completed}/{len(dataset)}")
    logger.info(f"Successfully linked items: {final_linked}")
    logger.info(f"Token usage: prompt={total_token_usage['prompt_tokens']}, completion={total_token_usage['completion_tokens']}, total={total_token_usage['total_tokens']}")
    logger.info(f"Output file: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2g error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info("=== Step 2g Completed ===")
    logger.info(f"\n")

if __name__ == "__main__":
    main()
