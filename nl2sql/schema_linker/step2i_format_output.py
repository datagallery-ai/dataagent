#!/usr/bin/env python3
"""
Step2i: Schema Formatting and Output Generation
三阶段处理：JSON文件生成 → Schema过滤 → DDL生成

三个阶段：
    1. 阶段1：为每个DataItem生成JSON文件到dump_json目录
    2. 阶段2：更新DataItem字段（使用7个召回路径）
    3. 阶段3：生成DDL语句并输出到单个JSON文件
"""

import argparse
import logging
import sys
import time
import pickle
import json
from pathlib import Path
from typing import Dict, Any, List, Set, Optional, Callable
from collections import defaultdict
from tqdm import tqdm

from .. import config
from .data_types import DataItem
from ..client.local_artifact_client import LocalArtifactClient
from .utils import get_log_file_path
from ..common.atomic_io import atomic_write_json, atomic_write_pickle

_COLUMNS_SAMPLE_VALUES_CACHE = None

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(get_log_file_path('step2i_format_output'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def ensure_dir(dir_path: str) -> None:
    """确保目录存在"""
    Path(dir_path).mkdir(parents=True, exist_ok=True)


def merge_schema_linking_results(results_list: List[Dict[str, List[str]]]) -> Dict[str, List[str]]:
    """
    合并多个召回路径的结果
    
    Args:
        results_list: 多个tables_and_columns格式的结果
        
    Returns:
        合并后的tables_and_columns字典
    """
    merged = defaultdict(set)
    
    for result in results_list:
        if isinstance(result, dict):
            for table_name, columns in result.items():
                if isinstance(columns, list):
                    merged[table_name].update(columns)
    
    # 转换为普通字典
    return {table: list(columns) for table, columns in merged.items()}


def filter_used_database_schema(
    original_schema: Dict[str, Any], 
    used_tables_and_columns: Dict[str, List[str]], 
    force_include_pks_and_fks: bool = True
) -> Dict[str, Any]:
    """
    过滤数据库schema，只保留使用的表和列
    
    Args:
        original_schema: 原始数据库schema
        used_tables_and_columns: 使用的表和列
        force_include_pks_and_fks: 是否强制包含主键和外键
        
    Returns:
        过滤后的schema
    """
    if not isinstance(original_schema, dict) or "tables" not in original_schema:
        return original_schema
        
    filtered_schema = {
        "db_id": original_schema.get("db_id", ""),
        "tables": {}
    }
    
    for table_name, table_info in original_schema["tables"].items():
        if table_name in used_tables_and_columns:
            used_columns = set(used_tables_and_columns[table_name])
            
            # 如果需要，添加主键和外键列
            if force_include_pks_and_fks and isinstance(table_info, dict) and "columns" in table_info:
                for col_name, col_info in table_info["columns"].items():
                    if isinstance(col_info, dict):
                        if col_info.get("primary_key") or col_info.get("foreign_keys"):
                            used_columns.add(col_name)
            
            # 构建过滤后的表信息
            filtered_table = {
                "table_name": table_info.get("table_name", table_name),
                "columns": {}
            }
            
            if isinstance(table_info, dict) and "columns" in table_info:
                for col_name, col_info in table_info["columns"].items():
                    if col_name in used_columns:
                        filtered_table["columns"][col_name] = col_info
            
            filtered_schema["tables"][table_name] = filtered_table
    
    return filtered_schema


def generate_json_dump(item: DataItem, dump_dir: Path) -> None:
    """
    阶段1：为DataItem生成JSON dump文件
    增强candidate_columns_detail的recall_path和values信息
    """
    qid = item.question_id
    db_id = item.database_id
    
    # 构建候选列列表
    candidate_columns = item.candidate_columns or []
    
    # 增强candidate_columns_detail
    enhanced_detail = enhance_candidate_columns_detail(item)
    
    # 获取召回路径详情（如果存在）
    recall_path_detail = getattr(item, 'recall_path_detail', None) or {}
    
    # 获取JOIN关系（如果存在）
    join_relations = getattr(item, 'join_relations', None) or []
    
    # 构建JSON数据，完全匹配参考格式
    json_data = {
        "question_id": qid,
        "db_id": db_id,
        "question": item.question,
        "evidence": item.evidence or "",
        "candidate_columns": candidate_columns,
        "candidate_columns_detail": enhanced_detail,
        "recall_path_detail": recall_path_detail,
        "join_relations": join_relations
    }
    
    # 保存JSON文件
    json_filename = f"qid_{qid}__{db_id}.json"
    json_path = dump_dir / json_filename
    
    atomic_write_json(json_path, json_data)
    
    logger.debug(f"Generated JSON dump: {json_filename}")


def update_database_schema_after_linking(item: DataItem, exclude_recall_fields: Optional[Set[str]] = None) -> None:
    """
    阶段2：更新database_schema_after_schema_linking和final_linked_tables_and_columns字段
    使用7个召回路径的结果
    """
    # 收集所有7个召回路径的结果
    recall_results = []
    
    # 7个召回路径字段
    recall_fields = [
        "column_match_tables_and_columns",
        "llm_match_tables_and_columns", 
        "value_match_lsh_tables_and_columns",
        "value_match_desc_tables_and_columns",
        "value_retrieval_tables_and_columns",
        "sql_reversed_tables_and_columns",
        "join_closure_tables_and_columns"
    ]
    
    exclude = set(exclude_recall_fields or [])

    for field_name in recall_fields:
        if field_name in exclude:
            continue
        field_value = getattr(item, field_name, None)
        if isinstance(field_value, dict):
            recall_results.append(field_value)
    
    # 合并所有召回结果 - 这就是final_linked_tables_and_columns
    merged_results = merge_schema_linking_results(recall_results)
    
    # 更新final_linked_tables_and_columns字段
    item.final_linked_tables_and_columns = merged_results
    
    # 过滤原始schema，只保留使用的表和列
    # 使用database_schema_after_value_retrieval作为源，而不是database_schema
    original_schema = item.database_schema_after_value_retrieval or {}
    filtered_schema = filter_used_database_schema(
        original_schema, 
        merged_results, 
        force_include_pks_and_fks=True
    )
    
    # 更新database_schema_after_schema_linking字段
    item.database_schema_after_schema_linking = filtered_schema
    
    logger.debug(f"Updated schema for qid={item.question_id}: merged {len(merged_results)} tables, filtered {len(filtered_schema.get('tables', {}))} tables")


def _map_recall_abbr_to_field(abbr: str) -> Optional[str]:
    if not isinstance(abbr, str):
        return None
    a = abbr.strip().lower()
    if not a:
        return None
    if a == "column_match":
        return "column_match_tables_and_columns"
    if a == "llm_match":
        return "llm_match_tables_and_columns"
    if a == "value_match_lsh":
        return "value_match_lsh_tables_and_columns"
    if a == "value_match_desc":
        return "value_match_desc_tables_and_columns"
    if a == "value_retrieval":
        return "value_retrieval_tables_and_columns"
    if a == "sql_reversed":
        return "sql_reversed_tables_and_columns"
    if a == "join_closure":
        return "join_closure_tables_and_columns"
    return None


def _compute_filtered_schema_for_item(
    item: DataItem,
    include_recall_abbrs: Optional[List[str]],
) -> Dict[str, Any]:
    if not item:
        return {}

    if include_recall_abbrs is None:
        return getattr(item, "database_schema_after_schema_linking", None) or {}

    include_fields: List[str] = []
    for abbr in include_recall_abbrs or []:
        field = _map_recall_abbr_to_field(abbr)
        if field and field not in include_fields:
            include_fields.append(field)

    recall_results: List[Dict[str, List[str]]] = []
    for field_name in include_fields:
        field_value = getattr(item, field_name, None)
        if isinstance(field_value, dict):
            recall_results.append(field_value)

    merged_results = merge_schema_linking_results(recall_results)

    original_schema = getattr(item, "database_schema_after_value_retrieval", None) or {}
    filtered_schema = filter_used_database_schema(
        original_schema,
        merged_results,
        force_include_pks_and_fks=True,
    )
    return filtered_schema


def extract_short_description(description: str) -> str:
    """
    从description字段提取短描述
    """
    if not description:
        return ""
    # 优先取 Column Description 部分
    parts = description.split("|")
    for part in parts:
        part = part.strip()
        if part.startswith("Column Description:"):
            return part[len("Column Description:"):].strip()
    # 次选 Expanded Column Name
    for part in parts:
        part = part.strip()
        if part.startswith("Expanded Column Name:"):
            return part[len("Expanded Column Name:"):].strip()
    # 最后取第一个非空部分
    for part in parts:
        part = part.strip()
        if part and not part.startswith("Value Statistics:") and not part.startswith("Value Examples:"):
            return part
    return ""


def enhance_candidate_columns_detail(item: DataItem) -> Dict[str, Any]:
    """
    增强candidate_columns_detail，添加关键词信息到recall_path，
    从value_match字段填充values数组
    """
    base_detail = item.candidate_columns_detail or {}
    enhanced_detail = {}
    
    # 获取value match数据
    value_lsh_data = item.value_match_lsh_values or {}
    value_desc_data = item.value_match_desc_values or {}
    
    # 获取recall path detail数据（包含关键词信息）
    column_match_detail = item.column_match_recall_path_detail or []
    value_lsh_detail = item.value_match_lsh_recall_path_detail or []
    value_desc_detail = item.value_match_desc_recall_path_detail or []
    
    # 构建关键词映射
    keyword_map = build_keyword_mapping(column_match_detail, value_lsh_detail, value_desc_detail)
    logger.debug(f"keyword_map for debugging: {keyword_map}")
    
    for col_id, col_info in base_detail.items():
        enhanced_info = {
            "values": [],
            "recall_path": []
        }
        
        # 处理recall_path，添加关键词信息
        if isinstance(col_info, dict) and "recall_path" in col_info:
            for path_item in col_info["recall_path"]:
                enhanced_path = enhance_recall_path_item(path_item, col_id, keyword_map)
                enhanced_info["recall_path"].append(enhanced_path)
        
        # 填充values数组
        values_data = []
        
        # 统一的值添加函数，避免重复逻辑
        def add_value_with_dedup(value_item: dict, recall_prefix: str):
            if not isinstance(value_item, dict) or "value" not in value_item:
                return
                
            keyword = value_item.get("keyword", "")
            recall_path_str = f"{recall_prefix}: \"{keyword}\"" if keyword else recall_prefix
            
            # 检查重复值
            existing_value = None
            for existing in values_data:
                if existing["value"] == value_item["value"]:
                    existing_value = existing
                    break
            
            if existing_value:
                # 追加recall_path（避免重复的recall_path）
                if recall_path_str not in existing_value["recall_path"]:
                    existing_value["recall_path"].append(recall_path_str)
                # 更新描述（如果新的更详细）
                if value_item.get("enum_value_description") and not existing_value.get("enum_value_description"):
                    existing_value["enum_value_description"] = value_item["enum_value_description"]
            else:
                values_data.append({
                    "value": value_item["value"],
                    "recall_path": [recall_path_str],
                    "enum_value_description": value_item.get("enum_value_description", "")
                })
        
        # 从value_match_lsh_values添加数据（现在也检查重复）
        if col_id in value_lsh_data:
            for value_item in value_lsh_data[col_id]:
                add_value_with_dedup(value_item, "value_match_lsh")
        
        # 从value_match_desc_values添加数据
        if col_id in value_desc_data:
            for value_item in value_desc_data[col_id]:
                add_value_with_dedup(value_item, "value_match_desc")
        
        enhanced_info["values"] = values_data
        enhanced_detail[col_id] = enhanced_info
    
    return enhanced_detail


def build_keyword_mapping(column_detail: List[Dict], lsh_detail: List[Dict], desc_detail: List[Dict]) -> Dict[str, Dict[str, str]]:
    """
    构建列ID到关键词的映射，分别为不同的回路类型建立映射
    数据结构: {'keyword': 'xxx', 'columns': [{'column': 'col_id', 'score': 1.0}]}
    """
    keyword_map = {}
    
    # 处理column_match_recall_path_detail
    for item in column_detail:
        if isinstance(item, dict) and "keyword" in item and "columns" in item:
            keyword = item["keyword"]
            for col_info in item["columns"]:
                if isinstance(col_info, dict) and "column" in col_info:
                    col_id = col_info["column"]
                    if col_id not in keyword_map:
                        keyword_map[col_id] = {}
                    keyword_map[col_id]["column_match"] = keyword
    
    # 处理value_match_lsh_recall_path_detail
    for item in lsh_detail:
        if isinstance(item, dict) and "keyword" in item and "columns" in item:
            keyword = item["keyword"]
            for col_info in item["columns"]:
                if isinstance(col_info, dict) and "column" in col_info:
                    col_id = col_info["column"]
                    if col_id not in keyword_map:
                        keyword_map[col_id] = {}
                    keyword_map[col_id]["value_match_lsh"] = keyword
    
    # 处理value_match_desc_recall_path_detail
    for item in desc_detail:
        if isinstance(item, dict) and "keyword" in item and "columns" in item:
            keyword = item["keyword"]
            for col_info in item["columns"]:
                if isinstance(col_info, dict) and "column" in col_info:
                    col_id = col_info["column"]
                    if col_id not in keyword_map:
                        keyword_map[col_id] = {}
                    keyword_map[col_id]["value_match_desc"] = keyword
    
    return keyword_map


def enhance_recall_path_item(path_item: str, col_id: str, keyword_map: Dict) -> str:
    """
    增强单个recall_path项，添加关键词信息
    """
    # 如果已经包含关键词格式，直接返回
    if ":" in path_item and "\"" in path_item:
        return path_item
    
    # 有关键词的回路：column_match, value_match_lsh, value_match_desc
    recall_name = path_item.strip()
    col_keywords = keyword_map.get(col_id, {})
    
    # 根据回路名称查找对应的关键词
    if "column_match" in recall_name.lower():
        keyword = col_keywords.get("column_match")
        if keyword:
            return f'column_match: "{keyword}"'
    elif "value_match_lsh" in recall_name.lower():
        keyword = col_keywords.get("value_match_lsh") 
        if keyword:
            return f'value_match_lsh: "{keyword}"'
    elif "value_match_desc" in recall_name.lower():
        keyword = col_keywords.get("value_match_desc")
        if keyword:
            return f'value_match_desc: "{keyword}"'
    
    # 其他回路只写回路名称
    return path_item


def _normalize_example_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _format_value_literal(value: Any) -> str:
    """将值格式化为 DDL 可读的字面量。

    - 简单字符串用单引号包裹：'abc'
    - 复杂/不安全字符串用双引号包裹并按 JSON 字符串转义： "{\"a\":1}"
    """
    if value is None:
        return "''"

    if isinstance(value, (dict, list, tuple, set)):
        value_str = json.dumps(value, ensure_ascii=False)
    else:
        value_str = str(value)

    if not value_str:
        return "''"

    is_complex = (
        not isinstance(value, str)
        or len(value_str) > 80
        or "\n" in value_str
        or "\r" in value_str
        or "\t" in value_str
        or value_str.lstrip().startswith("{")
        or value_str.lstrip().startswith("[")
        or "'" in value_str
        or "\\" in value_str
        or "\"" in value_str
    )

    if is_complex:
        return json.dumps(value_str, ensure_ascii=False)

    return f"'{value_str}'"


def _format_quoted_values(values: List[Dict[str, Any]]) -> str:
    formatted_parts: List[str] = []
    for item in values:
        v = item.get("value")
        desc = item.get("enum_value_description", "")

        literal = _format_value_literal(v)
        if desc:
            formatted_parts.append(f"{literal}({desc})")
        else:
            formatted_parts.append(literal)

    return f"[{', '.join(formatted_parts)}]" if formatted_parts else ""


def _escape_comment_text_for_double_quotes(text: str) -> str:
    """转义注释文本，使其可安全置于双引号内。"""
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _load_columns_sample_values_cache() -> Dict[str, List[str]]:
    global _COLUMNS_SAMPLE_VALUES_CACHE
    if isinstance(_COLUMNS_SAMPLE_VALUES_CACHE, dict):
        return _COLUMNS_SAMPLE_VALUES_CACHE

    cache_path = Path(config.STEP1_CACHE_DIR) / "columns_sample_values_cache.json"
    if not cache_path.exists():
        _COLUMNS_SAMPLE_VALUES_CACHE = {}
        return _COLUMNS_SAMPLE_VALUES_CACHE

    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        _COLUMNS_SAMPLE_VALUES_CACHE = data if isinstance(data, dict) else {}
    except Exception:
        _COLUMNS_SAMPLE_VALUES_CACHE = {}
    return _COLUMNS_SAMPLE_VALUES_CACHE


def _load_enum_mapping_for_column(
    metavisor_client: LocalArtifactClient,
    db_name: str,
    table_name: str,
    column_name: str,
) -> Dict[str, str]:
    """加载指定列的枚举值 -> 描述映射。

    基于 Step1 的 value_desc_vectors 元数据（经 LocalArtifactClient），
    返回从归一化值文本到其枚举描述的映射。
    """
    if not db_name or not table_name or not column_name:
        return {}

    col_id = f"{db_name}.{table_name}.{column_name}"
    try:
        store = metavisor_client._load_value_desc_store(db_name)  # type: ignore[attr-defined]
    except Exception:
        return {}

    metadata = store.get("metadata") or []
    if not isinstance(metadata, list):
        return {}

    mapping: Dict[str, str] = {}
    for meta in metadata:
        if not isinstance(meta, dict):
            continue
        if str(meta.get("column_id") or "").strip() != col_id:
            continue
        value = meta.get("value")
        desc = str(meta.get("description") or "").strip()
        if not desc:
            continue
        norm = _normalize_example_value(value)
        if not norm or norm in mapping:
            continue
        mapping[norm] = desc

    return mapping


def build_column_value_examples_comment(
    *,
    col_info: Dict[str, Any],
    max_count: int,
    current_item: Optional[DataItem],
    db_name: str,
    table_name: str,
    column_name: str,
    metavisor_client: Optional[LocalArtifactClient] = None,
) -> str:
    col_id = f"{db_name}.{table_name}.{column_name}" if db_name else f"{table_name}.{column_name}"
    target_count = max(0, int(max_count or 0))
    if target_count <= 0:
        return ""

    collected: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    qid = getattr(current_item, "question_id", None) if current_item else None

    def add_value(value: Any, source: str, enum_desc: str = "", score: Any = None) -> None:
        if len(collected) >= target_count:
            return
        norm = _normalize_example_value(value)
        if not norm or norm in seen:
            return
        seen.add(norm)
        collected.append({
            "value": value,
            "enum_value_description": enum_desc or "",
            "source": source,
            "score": score,
        })

    desc_items = []
    if current_item and isinstance(getattr(current_item, "value_match_desc_values", None), dict):
        desc_items = (current_item.value_match_desc_values or {}).get(col_id, []) or []
    is_enum_column = bool(desc_items)
    if isinstance(desc_items, list) and desc_items:
        best_by_value: Dict[str, Dict[str, Any]] = {}
        for it in desc_items:
            if not isinstance(it, dict):
                continue
            v = it.get("value")
            norm = _normalize_example_value(v)
            if not norm:
                continue
            score = it.get("score", 0)
            prev = best_by_value.get(norm)
            if prev is None or (isinstance(score, (int, float)) and score > prev.get("score", 0)):
                best_by_value[norm] = it
            elif prev is not None and not prev.get("enum_value_description") and it.get("enum_value_description"):
                best_by_value[norm] = it
        sorted_desc = sorted(best_by_value.values(), key=lambda x: x.get("score", 0), reverse=True)
        for it in sorted_desc:
            add_value(
                it.get("value"),
                "value_match_desc_values",
                it.get("enum_value_description", "") or "",
                it.get("score"),
            )

    if len(collected) < target_count:
        rv_list: List[Dict[str, Any]] = []
        if current_item and isinstance(getattr(current_item, "retrieved_values", None), dict):
            rv_all = current_item.retrieved_values or {}
            rv_tbl = rv_all.get(table_name)
            if not isinstance(rv_tbl, dict):
                rv_tbl = rv_all.get(str(table_name).lower())
            if not isinstance(rv_tbl, dict):
                for tname, tcols in rv_all.items():
                    if isinstance(tname, str) and tname.lower() == str(table_name).lower() and isinstance(tcols, dict):
                        rv_tbl = tcols
                        break

            if isinstance(rv_tbl, dict):
                rv_list = rv_tbl.get(column_name)
                if not isinstance(rv_list, list):
                    rv_list = rv_tbl.get(str(column_name).lower())
                if not isinstance(rv_list, list):
                    for cname, cvals in rv_tbl.items():
                        if isinstance(cname, str) and cname.lower() == str(column_name).lower() and isinstance(cvals, list):
                            rv_list = cvals
                            break

        if isinstance(rv_list, list) and rv_list:
            best_by_norm: Dict[str, Dict[str, Any]] = {}
            for rv in rv_list:
                if not isinstance(rv, dict):
                    continue
                v = rv.get("value")
                norm = _normalize_example_value(v)
                if not norm:
                    continue
                d = rv.get("distance")
                if not isinstance(d, (int, float)):
                    continue
                prev = best_by_norm.get(norm)
                if prev is None or float(d) < float(prev.get("distance", float("inf"))):
                    best_by_norm[norm] = {"value": v, "distance": float(d)}

            for it in sorted(best_by_norm.values(), key=lambda x: x.get("distance", float("inf"))):
                add_value(it.get("value"), "retrieved_values", score=it.get("distance"))
                if len(collected) >= target_count:
                    break

    lsh_items = []
    if current_item and isinstance(getattr(current_item, "value_match_lsh_values", None), dict):
        lsh_items = (current_item.value_match_lsh_values or {}).get(col_id, []) or []
    if isinstance(lsh_items, list) and lsh_items and len(collected) < target_count:
        best_by_value = {}
        for it in lsh_items:
            if not isinstance(it, dict):
                continue
            v = it.get("value")
            norm = _normalize_example_value(v)
            if not norm:
                continue
            score = it.get("score", 0)
            prev = best_by_value.get(norm)
            if prev is None or (isinstance(score, (int, float)) and score > prev.get("score", 0)):
                best_by_value[norm] = it
        sorted_lsh = sorted(best_by_value.values(), key=lambda x: x.get("score", 0), reverse=True)
        for it in sorted_lsh:
            add_value(it.get("value"), "value_match_lsh_values", score=it.get("score"))
            if len(collected) >= target_count:
                break

    # 对枚举列的匹配值补全枚举描述（优先使用 Step1 枚举落盘结果）
    if collected and is_enum_column and metavisor_client is not None:
        enum_mapping = _load_enum_mapping_for_column(
            metavisor_client=metavisor_client,
            db_name=db_name,
            table_name=table_name,
            column_name=column_name,
        )
        if enum_mapping:
            for item in collected:
                if item.get("enum_value_description"):
                    continue
                norm = _normalize_example_value(item.get("value"))
                if not norm:
                    continue
                desc = enum_mapping.get(norm)
                if desc:
                    item["enum_value_description"] = desc

    matched_part = ""
    if collected:
        logger.debug(
            f"qid={qid} col={col_id}: final matched examples="
            f"{[(x.get('source'), x.get('value'), x.get('score')) for x in collected]}"
        )
        matched_part = f"Matched values: {_format_quoted_values(collected)}"

    examples_vals: List[Dict[str, Any]] = []
    if metavisor_client is not None and len(collected) < target_count:
        try:
            sample_map = metavisor_client.get_columns_sample_values([col_id], sample_count=target_count)
        except Exception as e:
            logger.debug(f"qid={qid} col={col_id}: get_columns_sample_values failed: {e}")
            sample_map = {}

        vals = sample_map.get(col_id, []) if isinstance(sample_map, dict) else []
        if isinstance(vals, list) and vals:
            for v in vals:
                if len(examples_vals) >= max(target_count - len(collected), 0):
                    break
                norm = _normalize_example_value(v)
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                examples_vals.append({
                    "value": v,
                    "enum_value_description": "",
                    "source": "random_examples",
                    "score": None,
                })
            if examples_vals:
                logger.debug(
                    f"qid={qid} col={col_id}: final random get examples="
                    f"{[(x.get('source'), x.get('value'), x.get('score')) for x in examples_vals]}"
                )

    if matched_part and examples_vals:
        return matched_part + ". " + f"Examples: {_format_quoted_values(examples_vals)}"
    if matched_part:
        return matched_part
    if examples_vals:
        return f"Examples: {_format_quoted_values(examples_vals)}"

    return ""


def schema_dict_to_ddl(schema_dict: Dict[str, Any], metavisor_client: LocalArtifactClient, current_item: DataItem = None) -> str:
    """
    将schema字典转换为DDL语句
    """
    if not isinstance(schema_dict, dict) or "tables" not in schema_dict:
        return ""
    
    # 从current_item获取database名
    db_name = ""
    if current_item:
        db_name = getattr(current_item, 'database_id', '')
    
    ddl_parts = []
    
    for table_name, table_info in schema_dict["tables"].items():
        if not isinstance(table_info, dict) or "columns" not in table_info:
            continue
        
        table_column_info: List[Dict[str, Any]] = []
        try:
            columns_info_result = metavisor_client.get_table_columns_info(db_name, [table_name])
            if isinstance(columns_info_result, dict):
                table_column_info = columns_info_result.get(table_name, []) or []
            elif isinstance(columns_info_result, list):
                table_column_info = columns_info_result
        except Exception as e:
            logger.debug(f"Failed to get table columns info for {db_name}.{table_name}: {e}")

        # 从table_column_info整理出{column_id: desc_short}的缓存字典
        desc_cache = {}
        if isinstance(table_column_info, list):
            for col_item in table_column_info:
                if isinstance(col_item, dict) and "column_name" in col_item:
                    col_name = col_item["column_name"]
                    column_id = f"{db_name}.{table_name}.{col_name}"
                    desc_cache[column_id] = col_item.get("desc_short", "")
        
        lines = [f"CREATE TABLE {table_name} ("]
        column_lines = []
        pk_columns = []
        fk_lines = []
        
        for col_name, col_info in table_info["columns"].items():
            if not isinstance(col_info, dict):
                continue
                
            col_type = col_info.get("column_type", "TEXT")
            
            # 拼接column_id，从缓存字典获取列短描述
            column_id = f"{db_name}.{table_name}.{col_name}"
            desc = desc_cache.get(column_id, "")
            
            # 如果缓存没有描述，从col_info获取
            if not desc:
                desc = extract_short_description(col_info.get("description", ""))
            
            examples_comment = build_column_value_examples_comment(
                col_info=col_info,
                max_count=config.OUTPUT_DDL_VALUE_EXAMPLE_MAX_COUNT,
                current_item=current_item,
                db_name=db_name,
                table_name=table_name,
                column_name=col_name,
                metavisor_client=metavisor_client,
            )
            
            comment_parts = []
            if desc:
                comment_parts.append(desc)
            if examples_comment:
                comment_parts.append(examples_comment)
            
            if comment_parts:
                comment = ". ".join(comment_parts)
                escaped_comment = _escape_comment_text_for_double_quotes(comment)
                column_lines.append(f"  `{col_name}` {col_type}, -- COMMENT \"{escaped_comment}\"")
            else:
                column_lines.append(f"  `{col_name}` {col_type}")
            
            # 收集主键
            if col_info.get("primary_key", False):
                pk_columns.append(col_name)
            
            # 收集外键
            for fk in col_info.get("foreign_keys", []):
                if isinstance(fk, (list, tuple)) and len(fk) == 2:
                    fk_lines.append(f"  FOREIGN KEY (`{col_name}`) REFERENCES {fk[0]}(`{fk[1]}`)") 
        
        lines.extend(column_lines)
        if pk_columns:
            lines.append(f"  PRIMARY KEY ({', '.join(f'`{pk}`' for pk in pk_columns)})")
        lines.extend(fk_lines)
        lines.append(");")
        ddl_parts.append("\n".join(lines))
    
    return "\n\n".join(ddl_parts) + "\n"


def generate_ddl_statements(
    dataset: List[DataItem],
    metavisor_client: LocalArtifactClient,
    schema_provider: Optional[Callable[[DataItem], Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    阶段3：为所有问题生成DDL语句
    
    Returns:
        包含所有DDL的列表
    """
    ddl_results = []
    
    for item in dataset:
        qid = item.question_id
        db_id = item.database_id
        
        try:
            # 使用过滤后的schema生成DDL
            if schema_provider is None:
                schema_dict = item.database_schema_after_schema_linking or {}
            else:
                schema_dict = schema_provider(item) or {}
            ddl_text = schema_dict_to_ddl(schema_dict, metavisor_client, item)
            
            ddl_results.append({
                "question_id": qid,
                "db_id": db_id,
                "db_desc": ddl_text
            })
            
            logger.info(f"Generated DDL for qid={qid}, db={db_id}")
            
        except Exception as e:
            logger.error(f"Failed to generate DDL for qid={qid}: {e}")
            ddl_results.append({
                "question_id": qid,
                "db_id": db_id,
                "db_desc": "",
                "error": str(e)
            })
    
    return ddl_results


def main() -> None:
    parser = argparse.ArgumentParser(description="Step2i: Schema Formatting and Output Generation")
    parser.add_argument("--input", type=str,
                        help="输入文件路径（默认使用config中的路径）")
    parser.add_argument("--output", type=str,
                        help="输出文件路径（默认使用config中的路径）")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制处理的问题数量（0=全部）")
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='启用详细日志')
    parser.add_argument('--force-rerun', '-f', action='store_true',
                       help='强制重新处理已完成的项目')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"\n")
    logger.info("=== Step2i: Schema Formatting and Output Generation ===")
    
    # 确定输入输出路径
    input_path = args.input or config.STEP2H_JOIN_CLOSURE_SAVE_PATH
    output_path = args.output or str(Path(config.STEP2I_OUTPUT_DIR) / "dataset.pkl")
    
    if not Path(input_path).exists():
        logger.error(f"Input file not found: {input_path}")
        logger.error("Please run step2h_join_closure_linker.py first")
        sys.exit(1)
        
    # 加载数据集
    logger.info(f"Loading dataset from: {input_path}")
    with open(input_path, 'rb') as f:
        dataset = pickle.load(f)
    logger.info(f"Dataset loaded successfully, total items: {len(dataset)}")
    
    # 限制处理数量
    if args.limit > 0:
        dataset = dataset[:args.limit]
        logger.info(f"Processing limited to {args.limit} items")
    
    # 初始化MetaVisor客户端（使用缓存）
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
    
    # 准备输出目录
    output_dir = Path(config.STEP2I_OUTPUT_DIR)
    output_ddl_dir = Path(config.STEP2I_OUTPUT_DDL_DIR)
    dump_json_dir = output_dir / "dump_json"
    ensure_dir(str(output_dir))
    ensure_dir(str(dump_json_dir))
    ensure_dir(str(output_ddl_dir))
    
    logger.info(f"Starting three-stage processing for {len(dataset)} items...")
    
    # 统计
    stage1_count = 0
    stage2_count = 0
    stage3_count = 0
    
    # 阶段1和2：为每个DataItem处理
    logger.info("=== Stage 1 & 2: JSON Dump and Schema Merge/Filter ===")
    for idx, item in enumerate(tqdm(dataset, desc="Processing items")):
        try:
            qid = getattr(item, 'question_id', idx)
            
            # 阶段1：生成JSON dump文件
            generate_json_dump(item, dump_json_dir)
            stage1_count += 1
            
            # 阶段2：更新database_schema_after_schema_linking
            update_database_schema_after_linking(item)
            stage2_count += 1
            
            logger.debug(f"Processed item qid={qid}, merged and updated item.database_schema_after_schema_linking")
            
        except Exception as e:
            logger.error(f"Failed to process item {idx}: {e}")

    # 保存更新后的数据集（Stage 2 完成后立即保存，确保保存结果不受 Stage 3 干扰）
    logger.info("Saving updated dataset (after Stage 2)...")
    ensure_dir(str(Path(output_path).parent))
    atomic_write_pickle(output_path, dataset)
    
    # 阶段3：生成DDL语句
    logger.info("=== Stage 3: DDL Generation ===")
    ddl_output_files: List[str] = []
    ddl_statement_count = 0
    try:
        ddl_outputs: List[tuple] = []

        # 基础：保留原始输出名以兼容
        ddl_results = generate_ddl_statements(dataset, metavisor_client)
        ddl_statement_count = len(ddl_results)
        ddl_outputs.append(("all_recallpath_ddl.json", ddl_results))

        # 由配置驱动的 include 组合
        combo_defs = [
            (
                "recall_first_schema.json",
                getattr(config, "OUTPUT_DDL_WITH_PATH_RECALL_FIRST", None),
            ),
            (
                "precision_first_schema.json",
                getattr(config, "OUTPUT_DDL_WITH_PATH_PRECISION_FIRST", None),
            ),
        ]
        for file_name, new_include_abbrs in combo_defs:
            include_abbrs = new_include_abbrs if isinstance(new_include_abbrs, list) and new_include_abbrs else []
            if not include_abbrs:
                continue

            def _schema_provider(it: DataItem, _abbrs=include_abbrs):
                return _compute_filtered_schema_for_item(it, _abbrs)

            ddl_outputs.append((file_name, generate_ddl_statements(dataset, metavisor_client, schema_provider=_schema_provider)))

        # 完整 schema 的 DDL
        ddl_outputs.append((
            "fullschema.json",
            generate_ddl_statements(
                dataset,
                metavisor_client,
                schema_provider=lambda it: getattr(it, "database_schema", None) or {},
            ),
        ))

        # 保存生成的DDL文件
        for fname, items in ddl_outputs:
            ddl_output_path = output_ddl_dir / fname
            atomic_write_json(ddl_output_path, items)
            logger.info(f"DDL statements saved to: {ddl_output_path}")
            ddl_output_files.append(str(ddl_output_path))
        
    except Exception as e:
        logger.error(f"Failed to generate DDL statements: {e}")
    
    # 输出统计
    logger.info("=== Step 2i Completed ===")
    logger.info(f"Stage 1 (JSON dumps): {stage1_count} files generated")
    logger.info(f"Stage 2 (Schema merge/filter): {stage2_count} items processed")
    logger.info(f"Stage 3 (DDL generation): {ddl_statement_count} DDL statements generated")
    logger.info(f"JSON dump directory: {dump_json_dir}")
    logger.info(f"DDL output dir: {output_ddl_dir}")
    if ddl_output_files:
        logger.info(f"DDL output files: {ddl_output_files}")
    logger.info(f"Updated dataset saved to: {output_path}")
    logger.info(f"\n")
    
    # 计算输出目录大小
    total_size = sum(f.stat().st_size for f in output_dir.rglob('*') if f.is_file())
    logger.info(f"Output directory size: {total_size / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
