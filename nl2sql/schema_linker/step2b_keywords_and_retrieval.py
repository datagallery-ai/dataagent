"""
Step2b: Keywords & Value Retrieval
LLM Evidence提取 + 关键词提取 → 向量检索 → 更新 schema value_examples

步骤：
    1. 从 step2a 数据集加载带有 database_schema 的数据
    2. 初始化 LLM 客户端和向量检索系统
    3. 对每个问题：
       a. 使用 LLM 从 question + evidence 中提取结构化 evidence（表名、列名、值）
       b. 使用 LLM 从 question 中提取关键词列表
       c. 使用关键词从向量数据库检索相关列值
       d. 将检索到的值更新到数据库 schema 的 value_examples 字段
       e. 保存 extracted_evidence、question_keywords、retrieved_values 和增强后的 database_schema_after_value_retrieval
    4. 保存处理结果到 STEP2B_KEYWORDS_SAVE_PATH，为后续列匹配做准备
"""
import sys
import pickle
import copy
import time
import logging
import argparse
import threading
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from .. import config
from .data_types import DataItem
from ..client.llm_client import LLMAdapter
from .utils import extract_evidence, extract_keywords, retrieve_values_for_one_column, ensure_dir, write_pickle, read_pickle, get_log_file_path, parse_range_arg, parse_question_ids_arg, filter_dataset_by_qid_range, filter_dataset_by_question_ids, load_dataset_with_checkpoint_merge, get_error_questions_path, load_error_questions, save_error_questions, update_step2_state
from .schema_utils import map_lower_table_name_to_original_table_name, map_lower_column_name_to_original_column_name

# 日志配置
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2b_keywords_and_retrieval'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 导入向量库工具  
try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    logger.error(f"Missing required packages: {e}")
    logger.error("Please install: pip install sentence-transformers numpy")
    sys.exit(1)


class _SentenceTransformerEmbedder:
    """轻量 embedding 封装：__call__(texts) -> 向量数组，替代 chromadb 的 EF 接口"""

    def __init__(self, model_name_or_path: str, device: str = "cpu"):
        self._model = SentenceTransformer(model_name_or_path, device=device, trust_remote_code=True)

    def __call__(self, texts):
        return self._model.encode(list(texts), normalize_embeddings=False)


def get_embedding_function(model_name_or_path: str, device: str = "cpu"):
    """获取 SentenceTransformer embedding 函数（CPU 模式）"""
    logger.info(f"Using SentenceTransformer embedding: {model_name_or_path}, device={device}")
    return _SentenceTransformerEmbedder(model_name_or_path, device=device)


class NumpyCollection:
    """ChromaDB collection 兼容接口（numpy 实现）"""

    def __init__(self, vector_db_path: str, embedding_function=None, lower_meta_data: bool = True):
        self._ef = embedding_function
        self._lower_meta_data = lower_meta_data
        self._path = vector_db_path

        vectors_path = Path(vector_db_path) / "vectors.npy"
        meta_path = Path(vector_db_path) / "metadata.pkl"

        self._vectors = np.load(str(vectors_path))     # (N, dim)
        with open(meta_path, "rb") as f:
            data = pickle.load(f)
        self._metadata = data["metadata"]
        self._values = data["values"]

        # 预计算 L2 归一化向量，加速 cosine similarity
        norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)
        self._normalized = self._vectors / np.maximum(norms, 1e-8)

        # 预构建 (table_name, column_name) -> 行索引列表 分组索引，
        # 将 query 的 where 过滤从 O(全库值数) 线性扫描降为 O(1) 查表。
        # 仅在加载时遍历一次全库；collection 被 collection_cache 缓存，每库只付一次。
        self._col_index = defaultdict(list)
        for i, m in enumerate(self._metadata):
            key = (m.get("table_name"), m.get("column_name"))
            self._col_index[key].append(i)

    def query(self, query_texts=None, query_embeddings=None, n_results=10, where=None):
        """chromadb 兼容的查询"""
        # 解析 where 过滤条件（支持 chromadb $and 格式）
        table_filter = None
        column_filter = None
        if where:
            conditions = where.get("$and", [])
            for cond in conditions:
                if "table_name" in cond:
                    table_filter = cond["table_name"].get("$eq")
                elif "column_name" in cond:
                    column_filter = cond["column_name"].get("$eq")

        # 按 table/column 过滤索引
        if table_filter is not None and column_filter is not None:
            # 双条件（step2b 真实调用路径）：O(1) 分组索引查表，等价于原线性过滤
            indices = self._col_index.get((table_filter, column_filter), [])
        elif table_filter is not None or column_filter is not None:
            # 单条件（罕见）：保留原线性逻辑作 fallback，行为完全等价
            indices = [
                i for i, m in enumerate(self._metadata)
                if (table_filter is None or m.get("table_name") == table_filter)
                and (column_filter is None or m.get("column_name") == column_filter)
            ]
        else:
            indices = list(range(len(self._metadata)))

        # 无 query 或无匹配
        n_queries = len(query_texts or query_embeddings or [])
        if not indices or n_queries == 0:
            return {
                "ids": [[]] * max(n_queries, 1),
                "documents": [[]] * max(n_queries, 1),
                "distances": [[]] * max(n_queries, 1),
            }

        filtered_normalized = self._normalized[np.array(indices)]  # (M, dim)

        # 计算 query embedding
        if query_embeddings is None:
            query_embeddings = self._ef(query_texts)

        result_ids, result_docs, result_distances = [], [], []
        for qe in query_embeddings:
            qe_arr = np.array(qe, dtype=np.float32)
            qe_norm = np.linalg.norm(qe_arr)
            qe_normalized = qe_arr / max(float(qe_norm), 1e-8)

            # 余弦距离 = 1 - 余弦相似度
            sims = filtered_normalized @ qe_normalized
            dists = 1.0 - sims

            top_k = min(n_results, len(indices))
            top_local = np.argsort(dists)[:top_k]

            result_ids.append([str(indices[i]) for i in top_local])
            result_docs.append([self._values[indices[i]] for i in top_local])
            result_distances.append([float(dists[i]) for i in top_local])

        return {"ids": result_ids, "documents": result_docs, "distances": result_distances}


def load_numpy_collection(vector_db_path: str, embedding_function=None, lower_meta_data: bool = True):
    """加载已有的 numpy 向量库，返回 NumpyCollection"""
    vectors_path = Path(vector_db_path) / "vectors.npy"
    meta_path = Path(vector_db_path) / "metadata.pkl"
    if not vectors_path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"Numpy VDB not found or incomplete: {vector_db_path}")
    return NumpyCollection(vector_db_path, embedding_function=embedding_function, lower_meta_data=lower_meta_data)


def _init_llm() -> LLMAdapter:
    """初始化 LLM 客户端"""
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
    parser = argparse.ArgumentParser(description='Step 2b: Keywords extraction and value retrieval')
    limit_range_group = parser.add_mutually_exclusive_group()
    limit_range_group.add_argument('--limit', type=int, default=0, 
                                  help='Only process first N questions (0=all)')
    limit_range_group.add_argument('--question-range', dest='question_range', type=str, default=None,
                                  help='Only process pending_items question_id in [start,end), format: start,end')
    limit_range_group.add_argument('--question-ids', dest='question_ids', type=str, default=None,
                                  help='Only process specified question_ids, format: 1,3,5')
    limit_range_group.add_argument('--range', dest='question_range', type=str, default=None,
                                  help=argparse.SUPPRESS)
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose logging')
    parser.add_argument('--resume', '-r', action='store_true',
                       help='Resume from previous checkpoint (load from output file if exists)')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info(f"\n")
    logger.info("=== Step 2b: Keywords & Value Retrieval ===")
    logger.info(f"Config: LLM_MODEL={config.LLM_MODEL}")
    logger.info(f"Config: EMBEDDING_MODEL={config.EMBEDDING_MODEL}")
    logger.info(f"Config: VALUE_RETRIEVAL_N_RESULTS={config.VALUE_RETRIEVAL_N_RESULTS}")
    
    # 检查输入/输出文件
    dataset_path = config.STEP2A_DATASET_SAVE_PATH
    output_path = config.STEP2B_KEYWORDS_SAVE_PATH
    checkpoint_path = Path(output_path)

    if not Path(dataset_path).exists():
        logger.error(f"Dataset file not found: {dataset_path}")
        logger.error("Please run step2a_load_dataset.py first (Step2a: dataset preprocessing)")
        sys.exit(1)

    if args.resume and checkpoint_path.exists():
        logger.info(f"Resuming from checkpoint (merge): {checkpoint_path}")

    # 加载数据集（合并断点到完整基础数据集）
    logger.info(f"Loading dataset from {dataset_path}")
    start_time = time.time()
    dataset = load_dataset_with_checkpoint_merge(dataset_path, str(checkpoint_path), args.resume)
    logger.info(f"Dataset loaded in {time.time() - start_time:.2f}s")

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
        logger.info(f"Limited to first {args.limit} questions")
    
    # 统计已完成项
    completed = sum(1 for item in dataset if hasattr(item, 'database_schema_after_value_retrieval') and item.database_schema_after_value_retrieval is not None)
    remaining = len(dataset) - completed
    logger.info(f"Found {completed} completed items, {remaining} pending items")

    # 创建 LLM 客户端（必需）
    logger.info("Initializing LLM client...")
    try:
        llm = _init_llm()
        logger.info("LLM client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize LLM client: {e}")
        sys.exit(1)

    # 创建 embedding 函数
    logger.info("Initializing embedding function...")
    try:
        ef = get_embedding_function(config.EMBEDDING_MODEL, device=config.EMBEDDING_DEVICE)
        logger.info("Embedding function initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize embedding function: {e}")
        sys.exit(1)
    
    # 预加载所有库的值向量集合（由 Step1g 预先构建）
    unique_db_ids = list(set(item.database_id for item in dataset))
    logger.info(
        f"Loading value vector collections for {len(unique_db_ids)} databases from "
        f"STEP1_VALUE_VECTOR_STORE_DIR={config.STEP1_VALUE_VECTOR_STORE_DIR} ..."
    )
    
    collection_cache = {}
    failed_collections = []
    
    for db_id in unique_db_ids:
        if not db_id:
            continue
        vdb_path = str(Path(config.STEP1_VALUE_VECTOR_STORE_DIR) / db_id)
        if not Path(vdb_path).exists():
            logger.error(f"Vector database not found for {db_id}: {vdb_path}")
            logger.error("Please run step1g first")
            failed_collections.append(db_id)
            continue
            
        try:
            logger.debug(f"Loading collection for {db_id}")
            collection_cache[db_id] = load_numpy_collection(vdb_path, embedding_function=ef, lower_meta_data=config.LOWER_META_DATA)
            logger.debug(f"Collection loaded for {db_id}")
        except Exception as e:
            logger.error(f"Failed to load collection for {db_id}: {e}")
            failed_collections.append(db_id)
    
    if failed_collections:
        logger.error(f"Failed to load collections for: {failed_collections}")
        # 过滤掉集合加载失败的项
        dataset = [item for item in dataset if item.database_id not in failed_collections]
        logger.info(f"Filtered dataset to {len(dataset)} items")
    
    logger.info(f"Successfully loaded {len(collection_cache)} vector collections")

    # 筛选需处理的项
    error_questions_path = get_error_questions_path(3)
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
            not hasattr(item, 'extracted_evidence') or item.extracted_evidence is None or
            not hasattr(item, 'question_keywords') or item.question_keywords is None or
            not hasattr(item, 'retrieved_values') or item.retrieved_values is None or
            not hasattr(item, 'database_schema_after_value_retrieval') or item.database_schema_after_value_retrieval is None
        )
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
        # 仍保存数据集
        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, snapshot_dataset_for_save())
        final_completed = sum(
            1
            for it in dataset
            if hasattr(it, "keyword_and_retrieval_time") and it.keyword_and_retrieval_time is not None
        )
        update_step2_state(
            step_id="2b",
            name="keywords",
            status="done",
            completed_questions=final_completed,
            total_questions=len(dataset),
        )
        logger.info(f"Dataset saved to: {output_path}")
        return
    
    logger.info(f"Processing {len(pending_items)} pending items...")
    
    # 并行处理每个问题
    total_evidence_items = 0
    total_keywords = 0
    total_retrieved_values = 0
    
    def process_single_item(item):
        """处理单个项的关键词提取与值检索"""
        start = time.time()
        
        # 处理 DataItem 对象
        qid = item.question_id
        question = item.question
        evidence = item.evidence or ''
        database_id = item.database_id
        database_schema = item.database_schema
        
        logger.info(f"Processing qid={qid}: {question[:50]}...")
        
        try:
            # 初始化 token 用量统计
            total_llm_cost = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            
            # 1. 提取 evidence（仅当存在 evidence 时）
            logger.info(f"qid={qid}: Extracting evidence...")
            # 如果该步骤已经执行过，直接使用已有的结果
            if not item.extracted_evidence:
                extracted_evidence, evidence_token_usage = extract_evidence(evidence, llm)
            else:
                extracted_evidence = item.extracted_evidence
                evidence_token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            
            # 更新 token 用量
            for key in total_llm_cost:
                total_llm_cost[key] += evidence_token_usage[key]
            
            # 更新 DataItem
            item.extracted_evidence = extracted_evidence
            evidence_items_count = len(extracted_evidence)
            logger.info(f"qid={qid}: Parsed {evidence_items_count} evidences: {extracted_evidence}")
            
            # 2. 提取关键词
            logger.info(f"qid={qid}: Extracting keywords...")
            # 如果该步骤已经执行过，直接使用已有的结果
            if not item.question_keywords:
                keywords, keywords_token_usage = extract_keywords(question, evidence, llm)
            else:
                keywords = item.question_keywords
                keywords_token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            
            # 更新 token 用量
            for key in total_llm_cost:
                total_llm_cost[key] += keywords_token_usage[key]
            
            # 更新 DataItem
            item.question_keywords = keywords
            item.keyword_llm_cost = total_llm_cost
            
            keywords_count = len(keywords)
            logger.info(f"qid={qid}: Extracted {keywords_count} keywords: {keywords}")
            
            # 3. 为每个 TEXT 列检索值
            logger.info(f"qid={qid}: Retrieve values for TEXT columns...")
            if database_id not in collection_cache:
                logger.error(f"qid={qid}: No collection found for database {database_id}")
                return evidence_items_count, keywords_count, 0
                
            collection = collection_cache[database_id]
            retrieved = defaultdict(dict)
            text_columns_count = 0
            retrieved_values_count = 0

            # 题级编码一次 keywords，供该题所有 TEXT 列检索复用，
            # 避免在每列 query 内重复编码（编码次数从 TEXT列数 → 1）。
            # keywords 为空时置 None，retrieve_values_for_one_column 会提前返回、不会用到它。
            kw_emb = ef(keywords) if keywords else None

            for tbl_name, tbl_dict in database_schema.get("tables", {}).items():
                for col_name, col_dict in tbl_dict.get("columns", {}).items():
                    ct = col_dict.get("column_type", "").upper()
                    if ct == "TEXT" or ct.startswith("VARCHAR") or ct.startswith("CHAR"):
                        text_columns_count += 1
                        try:
                            result = retrieve_values_for_one_column(
                                keywords, collection, tbl_name, col_name,
                                config.VALUE_RETRIEVAL_N_RESULTS, config.LOWER_META_DATA,
                                query_embeddings=kw_emb,
                            )
                            orig_tbl = map_lower_table_name_to_original_table_name(result["table_name"], database_schema)
                            orig_col = map_lower_column_name_to_original_column_name(result["table_name"], result["column_name"], database_schema)
                            if orig_tbl and orig_col:
                                retrieved[orig_tbl][orig_col] = result["values"]
                                retrieved_values_count += len(result["values"])
                        except Exception as e:
                            logger.warning(f"qid={qid}: Failed to retrieve values for {tbl_name}.{col_name}: {e}")
            
            # 更新 DataItem
            item.retrieved_values = dict(retrieved)
            
            logger.info(f"qid={qid}: Retrieved values for {len(retrieved)} tables, {text_columns_count} text columns")
            
            # 3. 用检索到的值更新 database_schema
            schema_after = copy.deepcopy(database_schema)
            for tbl_name, col_dict in retrieved.items():
                for col_name, values in col_dict.items():
                    if tbl_name in schema_after.get("tables", {}) and col_name in schema_after["tables"][tbl_name].get("columns", {}):
                        orig_vals = schema_after["tables"][tbl_name]["columns"][col_name].get("value_examples", [])
                        new_vals = [v["value"] for v in values] + orig_vals
                        new_vals = new_vals[:config.VALUE_RETRIEVAL_N_RESULTS]
                        schema_after["tables"][tbl_name]["columns"][col_name]["value_examples"] = new_vals
            
            # 更新 DataItem
            with dataset_lock:
                item.database_schema_after_value_retrieval = schema_after
                item.keyword_and_retrieval_time = time.time() - start
            
            logger.info(f"qid={qid}: Completed in {time.time() - start:.2f}s")
            return True, evidence_items_count, keywords_count, retrieved_values_count, ""
            
        except Exception as e:
            logger.error(f"qid={qid}: Processing failed: {e}")
            return False, 0, 0, 0, str(e)
    
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
        pbar = tqdm(total=len(pending_items), desc="Keywords & Value Retrieval", initial=0)
        for future in as_completed(futures):
            item = futures[future]
            try:
                ok, evidence_count, keywords_count, retrieved_count, err = future.result()
                qid = getattr(item, 'question_id', None)
                if ok:
                    total_evidence_items += evidence_count
                    total_keywords += keywords_count
                    total_retrieved_values += retrieved_count
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
                    if hasattr(it, "keyword_and_retrieval_time") and it.keyword_and_retrieval_time is not None
                )
                logger.info(f"Checkpoint saved: {completed_now}/{len(dataset)} completed")
                save_error_questions(error_questions_path, error_qid_to_msg)
                update_step2_state(
                    step_id="2b",
                    name="keywords",
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

    # 对失败问题重试一次（qid 在错误日志中）
    if error_qid_to_msg:
        qids = sorted(list(error_qid_to_msg.keys()))
        logger.info(f"[VERIFY] Step2b: start rerun error questions")
        logger.info(f"[VERIFY] Step2b: rerun qids count={len(qids)}")
        logger.info(f"[VERIFY] Step2b: rerun qids(all)={qids}")
        by_qid = {getattr(it, 'question_id', None): it for it in dataset}
        for qid in qids:
            item = by_qid.get(qid)
            if not item:
                continue
            ok, evidence_count, keywords_count, retrieved_count, err = process_single_item(item)
            if ok:
                total_evidence_items += evidence_count
                total_keywords += keywords_count
                total_retrieved_values += retrieved_count
                del error_qid_to_msg[qid]
            else:
                error_qid_to_msg[qid] = err or error_qid_to_msg.get(qid, "unknown error")

        if error_qid_to_msg:
            remaining_qids = sorted(list(error_qid_to_msg.keys()))
            logger.info(f"[VERIFY] Step2b: rerun finished with remaining errors count={len(remaining_qids)}")
            logger.info(f"[VERIFY] Step2b: remaining error qids(all)={remaining_qids}")
        else:
            logger.info("[VERIFY] Step2b: rerun finished, error questions cleared")

        ensure_dir(Path(output_path).parent)
        write_pickle(output_path, dataset)

    save_error_questions(error_questions_path, error_qid_to_msg)
    
    # 统计
    final_completed = sum(
        1
        for it in dataset
        if hasattr(it, "keyword_and_retrieval_time") and it.keyword_and_retrieval_time is not None
    )
    completed_times = [
        it.keyword_and_retrieval_time
        for it in dataset
        if hasattr(it, "keyword_and_retrieval_time") and it.keyword_and_retrieval_time is not None
    ]

    update_step2_state(
        step_id="2b",
        name="keywords",
        status="done",
        completed_questions=final_completed,
        total_questions=len(dataset),
        event="run_end",
    )
    
    avg_time = sum(completed_times) / max(len(completed_times), 1)
    
    logger.info(f"=== Keywords & Value Retrieval Summary ===")
    logger.info(f"Total items processed: {final_completed}/{len(dataset)}")
    logger.info(f"Average processing time: {avg_time:.2f}s per item")
    logger.info(f"Total evidence items parsed: {total_evidence_items}")
    logger.info(f"Total keywords extracted: {total_keywords}")
    logger.info(f"Total values retrieved: {total_retrieved_values}")
    logger.info(f"Output saved to: {output_path}")
    logger.info(f"Output file size: {Path(output_path).stat().st_size / 1024 / 1024:.2f} MB")
    if error_qid_to_msg:
        logger.warning(f"Step2b error questions remaining: {len(error_qid_to_msg)} (see {error_questions_path})")
    logger.info("=== Step 2b Complete ===")
    logger.info(f"\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
