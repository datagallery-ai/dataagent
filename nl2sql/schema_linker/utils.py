import json
import logging
import os
import pickle
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional, Set

from .. import config
from ..common.atomic_io import (
    atomic_write_json as _atomic_write_json,
    atomic_write_pickle as _atomic_write_pickle,
)
from .prompt_templates import EVIDENCE_PARSING_PROMPT
from .prompt_factory import PromptFactory


logger = logging.getLogger(__name__)

def ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def read_pickle(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, OSError, ValueError):
        # 损坏/半截 pickle（如旧版非原子写入中途被杀残留）→ 回退基底，触发该步全量重跑
        logger.warning("read_pickle: corrupted pickle at %s, falling back to default", path)
        return default


def write_pickle(path: str, obj: Any) -> None:
    # 原子写入：写 *.tmp → os.replace（含 Windows 文件锁退避重试），杜绝半截文件
    _atomic_write_pickle(path, obj)


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        # 损坏/半截 json → 回退基底
        logger.warning("read_json: corrupted json at %s, falling back to default", path)
        return default


def write_json(path: str, obj: Any, indent: int = 2) -> None:
    # 原子写入；indent 固定为 2（与既有所有调用一致）。保留 indent 形参以兼容调用方签名。
    _atomic_write_json(path, obj)


def _infer_step2_run_kind(args: Dict[str, Any] | None) -> str:
    """根据类 CLI 参数推断 Step2 状态历史的运行类型。

    内部小工具；当前保守地仅区分 baseline 与 resume 两种运行。
    """

    if not isinstance(args, dict):
        return "baseline"
    try:
        resume_flag = bool(args.get("resume"))
    except Exception:
        resume_flag = False
    if resume_flag:
        return "resume"
    return "baseline"


def update_step2_state(
    step_id: str,
    name: str,
    status: str,
    completed_questions: int | None = None,
    total_questions: int | None = None,
    *,
    event: str | None = None,
    args: Dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """更新 Step2 schema_linker 状态看板。

    状态文件路径（所有步骤共用一个文件）：
        log/schema_linker/state.json

    结构示例::

        {
          "steps": {
            "2b": {
              "step_id": "2b",
              "name": "keywords",
              "status": "running",
              "completed_questions": 5,
              "total_questions": 10,
              "start_time": "...",
              "last_update_time": "..."
            },
            ...
          },
          "last_update_time": "..."
        }

    函数为尽力而为；当状态文件缺失或损坏时不应抛异常，而是重建最小结构。
    """

    try:
        state_path = config.STEP2_STATE_PATH
    except AttributeError:
        # 旧配置未定义 STEP2_STATE_PATH；不做任何处理。
        return

    try:
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state: Dict[str, Any] = json.load(f) or {}
        else:
            state = {}
    except Exception:
        state = {}

    if not isinstance(state, dict):
        state = {}

    steps = state.get("steps")
    if not isinstance(steps, dict):
        steps = {}
        state["steps"] = steps

    import datetime as _dt

    now_dt = _dt.datetime.now()
    now = now_dt.isoformat(timespec="seconds")

    # 顶层元数据（版本号 + 时间戳）
    if "version" not in state:
        state["version"] = 1
    if not state.get("created_at"):
        state["created_at"] = now
    state["last_update_time"] = now

    # 规范化 status 用于历史记录，同时保留原始字符串
    normalized_status = status
    if status == "done":
        normalized_status = "success"

    # 调用方未显式提供 event 时，推导一个默认事件。
    # 既保证旧调用点可用，又支持更丰富的历史记录。
    if event is None:
        if normalized_status in {"success", "failed"}:
            event = "run_end"
        elif normalized_status == "running":
            event = "progress"
        else:
            event = "progress"

    entry = steps.get(step_id)
    if not isinstance(entry, dict):
        # 为该 Step2 子步骤新建条目。
        # start_time 表示本次运行的开始时间；首次观测时在此初始化，
        # 后续会在 runner 发出的显式 run_start 事件时更新。
        entry = {
            "step_id": step_id,
            "name": name,
            "status": status,
            "completed_questions": 0,
            "total_questions": 0,
            "start_time": now,
            "last_update_time": now,
            # 用于更丰富历史/调试的新增字段
            "last_run_index": 0,
            "last_error": None,
            "history": [],  # 仅追加的运行历史
        }
        steps[step_id] = entry

    # 更新基础字段
    entry["name"] = name
    if completed_questions is not None:
        try:
            entry["completed_questions"] = int(completed_questions)
        except Exception:
            pass
    if total_questions is not None:
        try:
            entry["total_questions"] = int(total_questions)
        except Exception:
            pass

    # 即使对很旧/不完整的条目，也确保 start_time 有值。
    if not entry.get("start_time"):
        entry["start_time"] = now

    entry["last_update_time"] = now

    # 历史记录管理（仅追加）
    history = entry.get("history")
    if not isinstance(history, list):
        history = []

    # 在出现 progress/end 时，确保存在一条当前运行记录
    def _ensure_current_run() -> Dict[str, Any]:
        nonlocal history
        if history:
            last = history[-1]
            if isinstance(last, dict):
                return last
        # 无历史运行记录：创建一条隐式 baseline 运行。
        run_index = len(history) + 1
        completed_now = 0
        try:
            completed_now = int(entry.get("completed_questions") or 0)
        except Exception:
            completed_now = 0
        current = {
            "run_index": run_index,
            "kind": _infer_step2_run_kind(args),
            "status": "running",
            "execution_start_time": now,
            "execution_end_time": None,
            "duration_seconds": None,
            "completed_before_run": completed_now,
            "completed_after_run": completed_now,
            "delta_completed": 0,
            "args": args or {},
            "error": None,
        }
        history.append(current)
        return current

    if event == "run_start":
        run_index = len(history) + 1
        completed_now = 0
        try:
            completed_now = int(entry.get("completed_questions") or 0)
        except Exception:
            completed_now = 0
        run_entry = {
            "run_index": run_index,
            "kind": _infer_step2_run_kind(args),
            "status": "running",
            "execution_start_time": now,
            "execution_end_time": None,
            "duration_seconds": None,
            "completed_before_run": completed_now,
            "completed_after_run": completed_now,
            "delta_completed": 0,
            "args": args or {},
            "error": None,
        }
        history.append(run_entry)
        # 每次新运行（baseline 或 resume）都在 step 条目上记录本次运行的开始时间，
        # 使 entry["start_time"] 反映当前执行窗口，而 first_seen_time 保留最早时间戳。
        entry["start_time"] = now
        entry["status"] = "running"
        entry["last_run_index"] = run_index
        entry["last_error"] = None
    else:
        run_entry = _ensure_current_run()

        # 更新当前运行的完成计数
        if completed_questions is not None:
            try:
                before = int(run_entry.get("completed_before_run") or 0)
            except Exception:
                before = 0
            try:
                after = int(completed_questions)
            except Exception:
                after = before
            run_entry["completed_after_run"] = after
            delta = after - before
            if delta < 0:
                delta = 0
            run_entry["delta_completed"] = delta

        if event == "run_end":
            # 标记本次运行已结束（success/failed）
            run_entry["status"] = normalized_status
            run_entry["execution_end_time"] = now
            start_str = run_entry.get("execution_start_time")
            if isinstance(start_str, str):
                try:
                    start_dt = _dt.datetime.fromisoformat(start_str)
                    run_entry["duration_seconds"] = (now_dt - start_dt).total_seconds()
                except Exception:
                    pass
            entry["status"] = normalized_status
            if error:
                run_entry["error"] = str(error)
                entry["last_error"] = str(error)
            else:
                entry["last_error"] = None
        else:
            # 仅进度更新——保持 status 为 running
            if not run_entry.get("status"):
                run_entry["status"] = "running"
            entry["status"] = "running"
            if error:
                run_entry["error"] = str(error)
                entry["last_error"] = str(error)

        try:
            entry["last_run_index"] = int(run_entry.get("run_index") or len(history))
        except Exception:
            entry["last_run_index"] = len(history)

    entry["history"] = history

    try:
        write_json(state_path, state, indent=2)
    except Exception:
        # 仅尽力而为；此处忽略所有错误以免影响主流程。
        return


def should_skip(force_rerun: bool, output_path: str) -> bool:
    return (not force_rerun) and os.path.exists(output_path)


def clamp_limit(data: Any, limit: Optional[int]) -> Any:
    if limit is None:
        return data
    if isinstance(data, list):
        return data[:limit]
    return data


def parse_range_arg(range_arg: Optional[str]) -> Optional[Tuple[int, int]]:
    if not range_arg:
        return None
    parts = [p.strip() for p in str(range_arg).split(",")]
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0])
        end = int(parts[1])
    except Exception:
        return None
    return start, end


def parse_question_ids_arg(question_ids_arg: Optional[str]) -> Optional[List[int]]:
    if not question_ids_arg:
        return None
    raw_parts = [p.strip() for p in str(question_ids_arg).split(",")]
    if not raw_parts or any(not p for p in raw_parts):
        return None
    out: List[int] = []
    seen = set()
    for p in raw_parts:
        try:
            qid = int(p)
        except ValueError:
            return None
        if qid not in seen:
            out.append(qid)
            seen.add(qid)
    return out


def filter_dataset_by_qid_range(dataset: Any, qid_range: Optional[Tuple[int, int]]) -> Any:
    if not qid_range:
        return dataset
    if not isinstance(dataset, list):
        return dataset
    start, end = qid_range
    out = []
    for item in dataset:
        qid = getattr(item, "question_id", None)
        if isinstance(qid, int) and start <= qid < end:
            out.append(item)
    return out


def filter_dataset_by_question_ids(dataset: Any, question_ids: Optional[List[int]]) -> Any:
    if not question_ids:
        return dataset
    if not isinstance(dataset, list):
        return dataset
    qid_set = set(question_ids)
    out = []
    for item in dataset:
        qid = getattr(item, "question_id", None)
        if isinstance(qid, int) and qid in qid_set:
            out.append(item)
    return out


def index_by_question_id(items: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        qid = it.get("question_id")
        if isinstance(qid, int):
            out[qid] = it
    return out


def merge_by_question_id(
    base_items: List[Dict[str, Any]],
    updated_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    base_idx = index_by_question_id(base_items or [])
    upd_idx = index_by_question_id(updated_items or [])
    all_qids = sorted(set(base_idx.keys()) | set(upd_idx.keys()))
    merged: List[Dict[str, Any]] = []
    for qid in all_qids:
        if qid in upd_idx:
            merged.append(upd_idx[qid])
        else:
            merged.append(base_idx[qid])
    return merged


def _get_question_id(item: Any) -> Optional[int]:
    if isinstance(item, dict):
        qid = item.get("question_id")
        return qid if isinstance(qid, int) else None
    qid = getattr(item, "question_id", None)
    return qid if isinstance(qid, int) else None


def _get_item_field_names(item: Any) -> List[str]:
    if item is None:
        return []
    if isinstance(item, dict):
        return list(item.keys())
    model_fields = getattr(item, "model_fields", None)
    if isinstance(model_fields, dict):
        return list(model_fields.keys())
    fields = getattr(item, "__fields__", None)
    if isinstance(fields, dict):
        return list(fields.keys())
    d = getattr(item, "__dict__", None)
    if isinstance(d, dict):
        return list(d.keys())
    return []


def _get_item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key, None)
    return getattr(item, key, None)


def _set_item_value(item: Any, key: str, value: Any) -> None:
    if isinstance(item, dict):
        item[key] = value
    else:
        setattr(item, key, value)


def _is_empty_merge_value(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) == 0
    return False


def _merge_item_fields(base_item: Any, updated_item: Any) -> Any:
    if base_item is None or updated_item is None:
        return base_item
    base_fields = _get_item_field_names(base_item)
    if not base_fields:
        return base_item
    for k in base_fields:
        v = _get_item_value(updated_item, k)
        if not _is_empty_merge_value(v):
            _set_item_value(base_item, k, v)
    return base_item


def merge_items_by_question_id(base_items: List[Any], updated_items: List[Any]) -> List[Any]:
    upd_idx: Dict[int, Any] = {}
    for it in updated_items or []:
        qid = _get_question_id(it)
        if qid is None:
            continue
        upd_idx[qid] = it

    merged: List[Any] = []
    base_qids: Set[int] = set()
    for it in base_items or []:
        qid = _get_question_id(it)
        if qid is None:
            merged.append(it)
            continue
        base_qids.add(qid)
        upd = upd_idx.get(qid)
        if upd is None:
            merged.append(it)
        else:
            merged.append(_merge_item_fields(it, upd))

    for qid, it in upd_idx.items():
        if qid not in base_qids:
            merged.append(it)
    return merged


def load_dataset_with_checkpoint_merge(input_path: str, checkpoint_path: str, resume: bool) -> List[Any]:
    input_items = read_pickle(input_path, default=[])
    if not resume:
        return list(input_items or [])
    if not os.path.exists(checkpoint_path):
        return list(input_items or [])
    checkpoint_items = read_pickle(checkpoint_path, default=[])
    if not checkpoint_items:
        return list(input_items or [])
    return merge_items_by_question_id(list(input_items or []), list(checkpoint_items or []))


def load_dataset_with_resume(
    input_path: str,
    output_path: str,
    force_rerun: bool,
) -> List[Dict[str, Any]]:
    input_items = read_pickle(input_path, default=[])
    if force_rerun or (not os.path.exists(output_path)):
        return list(input_items or [])
    output_items = read_pickle(output_path, default=[])
    if not output_items:
        return list(input_items or [])
    return merge_by_question_id(list(input_items or []), list(output_items or []))


def get_log_file_path(step_name: str) -> str:
    """获取日志文件路径，使用配置参数，默认为 nl2sql/log/schema_linker。"""
    log_dir = config.SCHEMA_LINKER_LOG_DIR
    ensure_dir(log_dir)
    return f"{log_dir}/{step_name}.log"


# Step 编号到标准 step2x tag 的映射（供错误文件命名复用）
_STEP_NUM_TO_STEP2_TAG = {
    0: "step2a",
    3: "step2b",
    4: "step2c",
    5: "step2d",
    6: "step2e",
    7: "step2f",
    8: "step2g",
    9: "step2h",
    10: "step2i",
}


def _normalize_step_tag(step: Any) -> str:
    """将 step 编号或字符串统一映射为用于文件命名的 tag。

    - 对于 Step2 pipeline：
        0  -> step2a
        3  -> step2b
        ...
        10 -> step2i
    - 若传入字符串：
        * "step2b" 形式 → 原样返回
        * "2b" 形式    → 规范化为 "step2b"
    - 其它未知编号则回退为 "step{num}"，保持兼容。
    """

    if isinstance(step, str):
        s = step.strip().lower()
        if s.startswith("step2"):
            return s
        # 将 "2b" 正规化为 "step2b"
        if len(s) == 2 and s[0] == "2" and s[1].isalpha():
            return f"step{s}"
        return s
    try:
        num = int(step)
    except Exception:
        return str(step)
    return _STEP_NUM_TO_STEP2_TAG.get(num, f"step{num}")


def get_error_questions_path(step_num: int) -> str:
    log_dir = config.SCHEMA_LINKER_LOG_DIR
    ensure_dir(str(log_dir))
    tag = _normalize_step_tag(step_num)
    return str(Path(log_dir) / f"{tag}_error_questions.json")


def get_error_databases_path(step_num: int) -> str:
    log_dir = config.SCHEMA_LINKER_LOG_DIR
    ensure_dir(str(log_dir))
    tag = _normalize_step_tag(step_num)
    return str(Path(log_dir) / f"{tag}_error_databases.json")


def load_error_questions(path: str) -> Dict[int, str]:
    data = read_json(path, default=[])
    out: Dict[int, str] = {}
    if not isinstance(data, list):
        return out
    for it in data:
        if not isinstance(it, dict):
            continue
        qid = it.get("question_id")
        err = it.get("error")
        if isinstance(qid, int) and isinstance(err, str):
            out[qid] = err
    return out


def save_error_questions(path: str, errors: Dict[int, str]) -> None:
    if not errors:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return

    items = []
    for qid in sorted(errors.keys()):
        err = errors.get(qid, "")
        if not isinstance(err, str):
            err = str(err)
        items.append({"question_id": int(qid), "error": err})
    write_json(path, items, indent=2)


def save_error_databases(path: str, errors: Dict[str, str]) -> None:
    if not errors:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return

    items = []
    for db_id in sorted(errors.keys()):
        err = errors.get(db_id, "")
        if not isinstance(err, str):
            err = str(err)
        items.append({"db_id": str(db_id), "error": err})
    write_json(path, items, indent=2)


# ==================== Evidence Parsing, Keyword Extract and Value Retrieval Functions ====================

def extract_evidence(evidence: str, llm, max_retry: int | None = None) -> tuple:
    """
    使用 LLM 从 evidence 中解析结构化数据库引用
    
    Args:
        evidence: 证据文本
        llm: LLMAdapter 实例
        max_retry: 最大重试次数
    
    Returns:
        (extracted_evidence_list, token_usage)
    """
    
    # 校验 evidence 是否有效（非空、非 None、非纯空白）
    if not evidence or not evidence.strip():
        logger.info("No evidence provided, skipping evidence parsing")
        return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    
    prompt = EVIDENCE_PARSING_PROMPT.format(evidence=evidence)
    retry = 0
    extracted_evidence = None
    total_token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if max_retry is None:
        max_retry = config.LLM_PARSE_FAIL_MAX_RETRIES

    last_exception: Exception | None = None

    while retry < max_retry:
        try:
            response, token_usage, *_ = llm.ask(prompt, num_samples=1)
            total_token_usage["prompt_tokens"] += token_usage["prompt_tokens"]
            total_token_usage["completion_tokens"] += token_usage["completion_tokens"]
            total_token_usage["total_tokens"] += token_usage["total_tokens"]

            content = response[0]
            logger.debug(f"Evidence parsing raw response: {content[:500]}")
            
            # 解析 JSON 响应
            evidence_result = json.loads(content)
            extracted_evidence = evidence_result.get("extracted_evidence", [])
            
            if isinstance(extracted_evidence, list):
                break
        except Exception as e:
            retry += 1
            last_exception = e
            logger.error(f"Error parsing evidence (retry {retry}): {e}")

    if extracted_evidence is None:
        if last_exception is None:
            raise ValueError("Evidence parsing failed: no valid result")
        raise ValueError(f"Evidence parsing failed after {max_retry} retries: {last_exception}")

    logger.info(f"Evidence parsed: {len(extracted_evidence)} items")
    return extracted_evidence, total_token_usage


def extract_keywords(question: str, evidence: str, llm, max_retry: int | None = None) -> tuple:
    """
    使用 LLM 从问题和提示中提取关键词
    
    Args:
        question: 问题文本
        evidence: 证据文本
        llm: LLMAdapter 实例
        max_retry: 最大重试次数
    
    Returns:
        (keywords_list, token_usage)
    """
    
    prompt = PromptFactory.format_keywords_extraction_prompt(question, evidence)
    retry = 0
    keywords_list = None
    total_token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if max_retry is None:
        max_retry = config.LLM_PARSE_FAIL_MAX_RETRIES

    last_exception: Exception | None = None

    while retry < max_retry:
        try:
            response, token_usage, *_ = llm.ask(prompt, num_samples=1, stop=["</result>"])
            total_token_usage["prompt_tokens"] += token_usage["prompt_tokens"]
            total_token_usage["completion_tokens"] += token_usage["completion_tokens"]
            total_token_usage["total_tokens"] += token_usage["total_tokens"]

            # 恢复 stop token: </result>
            content = response[0] + "</result>"
            raw_list = re.search(r"<result>(.*?)</result>", content, re.DOTALL).group(1)
            keywords_list = json.loads(raw_list)
            if isinstance(keywords_list, list):
                break
        except Exception as e:
            retry += 1
            last_exception = e
            logger.error(f"Error extracting keywords (retry {retry}): {e}")

    if keywords_list is None:
        if last_exception is None:
            raise ValueError("Keywords extraction failed: no valid result")
        raise ValueError(f"Keywords extraction failed after {max_retry} retries: {last_exception}")

    # 后处理：拆分子词
    processed_keywords = set()
    for keyword in keywords_list:
        keyword = keyword.strip()
        processed_keywords.add(keyword)
        processed_keywords.update(keyword.split(" "))
    keywords_list = list(processed_keywords)

    return keywords_list, total_token_usage


def retrieve_values_for_one_column(
    keywords: List[str],
    collection,
    table_name: str,
    column_name: str,
    n_results: int,
    lower_meta_data: bool,
    query_embeddings: Optional[List] = None,
) -> Dict[str, Any]:
    """
    对一个列进行向量检索，返回 top-k 值
    
    Args:
        keywords: 关键词列表
        collection: 向量数据库集合
        table_name: 表名
        column_name: 列名
        n_results: 返回结果数
        lower_meta_data: 是否转换为小写
        query_embeddings: 题级预编码向量（方案A）。命中时跳过 collection.query 内部
            对 keywords 的重复编码；为 None 时退回原行为（内部自行编码），结果完全等价。
        
    Returns:
        Dict: 检索结果
    """
    table_name_q = table_name.lower() if lower_meta_data else table_name
    column_name_q = column_name.lower() if lower_meta_data else column_name
    if not keywords:
        return {"table_name": table_name_q, "column_name": column_name_q, "values": []}
    query_results = collection.query(
        query_texts=keywords,
        query_embeddings=query_embeddings,
        where={"$and": [
            {"table_name": {"$eq": table_name_q}},
            {"column_name": {"$eq": column_name_q}},
        ]},
        n_results=n_results,
    )
    values = []
    for documents, distances in zip(query_results["documents"], query_results["distances"]):
        for doc, dist in zip(documents, distances):
            values.append((doc, dist))
    seen_values = set()
    top_k_values = []
    for value, distance in sorted(values, key=lambda x: x[1]):
        if value not in seen_values:
            seen_values.add(value)
            top_k_values.append({"value": value, "distance": distance})
            if len(top_k_values) >= n_results:
                break

    return {
        "table_name": table_name_q,
        "column_name": column_name_q,
        "values": top_k_values,
    }
