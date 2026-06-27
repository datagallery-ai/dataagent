"""
Schema utilities 
Provides complete database schema loading and processing functionality
"""
import logging
from charset_normalizer import from_bytes
import pandas as pd
from pathlib import Path
from functools import lru_cache
from typing import List, Dict, Any, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ==================== SQL Execution ====================
# 统一复用 sql_evaluator 的执行内核，避免在本模块维护重复实现
from ..sql_evaluator.execute_sql import run_query, ExecStatus



# ==================== Schema Loading Functions ====================

def load_table_names(db_path) -> List[str]:
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence';"
    result = run_query(str(db_path), sql)
    if result.status != ExecStatus.OK:
        raise Exception(f"Failed to load table names from {db_path}: {result.message}")
    return [row[0] for row in result.rows]


def load_column_names_and_types(db_path, table_name: str) -> List[Tuple[str, str]]:
    sql = f"PRAGMA table_info(`{table_name}`);"
    result = run_query(str(db_path), sql)
    if result.status != ExecStatus.OK:
        raise Exception(f"Failed to load column names and types from {db_path}: {result.message}")
    return [(row[1], row[2]) for row in result.rows]


def load_primary_keys(db_path, table_name: str) -> List[str]:
    sql = f"PRAGMA table_info(`{table_name}`);"
    result = run_query(str(db_path), sql)
    if result.status != ExecStatus.OK:
        raise Exception(f"Failed to load primary keys from {db_path}: {result.message}")
    return [row[1] for row in result.rows if row[5] != 0]


def load_foreign_keys(db_path, table_name: str) -> List[Tuple[str, str, str, str]]:
    sql = f"PRAGMA foreign_key_list(`{table_name}`);"
    result = run_query(str(db_path), sql)
    if result.status != ExecStatus.OK and result.status != ExecStatus.NO_ROWS:
        raise Exception(f"Failed to load foreign keys from {db_path}: {result.message}")
    foreign_keys_list = result.rows
    deduplicated_foreign_keys = set([(fk[3], fk[2], fk[4]) for fk in foreign_keys_list])
    fixed_foreign_keys = []
    for foreign_key in deduplicated_foreign_keys:
        source_table_name = table_name.strip()
        source_column_name = foreign_key[0].strip()
        target_table_name = foreign_key[1].strip()
        target_column_name = None
        if foreign_key[2] is not None:
            target_column_name = foreign_key[2].strip()
        else:
            target_table_primary_keys = load_primary_keys(db_path, target_table_name)
            if len(target_table_primary_keys) > 1:
                for pk in target_table_primary_keys:
                    if pk.lower() == source_column_name.lower():
                        target_column_name = pk
                        break
            elif len(target_table_primary_keys) == 1:
                target_column_name = target_table_primary_keys[0]
            else:
                raise ValueError(f"Target column is None and cannot be fixed: {target_table_name}")
        foreign_key_tuple = (source_table_name, source_column_name, target_table_name, target_column_name)

        # 特殊处理 bird train 数据库
        special_cases = {
            ("works_cycles", "SalesOrderHeader", "ShipMethodID", "Address", "AddressID"): ("SalesOrderHeader", "ShipMethodID", "ShipMethod", "ShipMethodID"),
            ("mondial_geo", "city", "Province", "province", None): ("city", "Province", "province", "Name"),
            ("mondial_geo", "geo_desert", "Province", "province", None): ("geo_desert", "Province", "province", "Name"),
            ("mondial_geo", "geo_estuary", "Province", "province", None): ("geo_estuary", "Province", "province", "Name"),
            ("mondial_geo", "geo_island", "Province", "province", None): ("geo_island", "Province", "province", "Name"),
            ("mondial_geo", "geo_lake", "Province", "province", None): ("geo_lake", "Province", "province", "Name"),
            ("mondial_geo", "geo_mountain", "Province", "province", None): ("geo_mountain", "Province", "province", "Name"),
            ("mondial_geo", "geo_river", "Province", "province", None): ("geo_river", "Province", "province", "Name"),
            ("mondial_geo", "geo_sea", "Province", "province", None): ("geo_sea", "Province", "province", "Name"),
            ("mondial_geo", "geo_source", "Province", "province", None): ("geo_source", "Province", "province", "Name"),
            ("mondial_geo", "located", "Province", "province", None): ("located", "Province", "province", "Name"),
            ("mondial_geo", "located", "City", "city", None): ("located", "City", "city", "Name"),
            ("mondial_geo", "locatedOn", "Province", "province", None): ("locatedOn", "Province", "province", "Name"),
            ("mondial_geo", "locatedOn", "City", "city", None): ("locatedOn", "City", "city", "Name"),
            ("mondial_geo", "organization", "Province", "province", None): ("organization", "Province", "province", "Name"),
            ("mondial_geo", "organization", "City", "city", None): ("organization", "City", "city", "Name"),
        }
        db_path_obj = Path(db_path) if isinstance(db_path, str) else db_path
        current_db_id = db_path_obj.stem
        if (current_db_id, *foreign_key_tuple) in special_cases:
            foreign_key_tuple = special_cases[(current_db_id, *foreign_key_tuple)]
        assert None not in foreign_key_tuple, f"Foreign key tuple contains None: {foreign_key_tuple}"
        fixed_foreign_keys.append(foreign_key_tuple)
    return fixed_foreign_keys


def load_value_examples(db_path: str, table_name: str, column_name: str, max_num_examples: int = 3, max_example_length: int = 100) -> List[str]:
    result = run_query(db_path, f"SELECT DISTINCT `{column_name}` FROM `{table_name}` WHERE `{column_name}` IS NOT NULL AND `{column_name}` != '' AND length(cast(`{column_name}` as text)) <= {max_example_length} LIMIT {max_num_examples};")
    if result.status != ExecStatus.OK and result.status != ExecStatus.NO_ROWS:
        raise ValueError(f"Failed to load value_examples from {db_path}: {result.message}")
    return [str(row[0]) for row in result.rows]


def load_value_statistics(db_path: str, table_name: str, column_name: str) -> Dict[str, Any]:
    sql = f"""
        SELECT COUNT(`{column_name}`) AS total_count, COUNT(DISTINCT `{column_name}`) AS distinct_count, SUM(CASE WHEN `{column_name}` IS NULL THEN 1 ELSE 0 END) AS null_count
        FROM (SELECT `{column_name}` FROM `{table_name}` LIMIT 100000) AS limited_dataset;
    """
    result = run_query(db_path, sql)
    if result.status != ExecStatus.OK:
        raise ValueError(f"Failed to load value_statistics from {db_path}: {result.message}")
    return {
        "total_count": result.rows[0][0],
        "distinct_count": result.rows[0][1],
        "null_count": result.rows[0][2],
    }


def _normalize_description_string(description: str) -> str:
    description = description.replace("\r", " ").replace("\n", " ").replace("commonsense evidence:", "").strip()
    while "  " in description:
        description = description.replace("  ", " ")
    return description.strip()


def load_database_description(db_id: str, database_dir: Path, use_database_description: bool = True) -> Dict[str, Dict[str, Dict[str, Any]]]:
    if not use_database_description:
        return {}
    db_description_dir = database_dir / "database_description"
    if not db_description_dir.exists():
        logger.warning(f"Database description for database {db_id} does not exist, skipping...")
        return {}
    database_description = {}
    for csv_file in db_description_dir.glob("*.csv"):
        table_name_lower = csv_file.stem.lower().strip()
        detection = from_bytes(csv_file.read_bytes()).best()
        encoding_type = detection.encoding if detection else "utf-8"
        table_description = {}
        table_description_df = pd.read_csv(csv_file, encoding=encoding_type, index_col=False)
        for _, row in table_description_df.iterrows():
            if pd.isna(row["original_column_name"]):
                continue
            original_column_name_lower = row["original_column_name"].strip().lower()
            expanded_column_name = row["column_name"].strip() if pd.notna(row["column_name"]) else ""
            column_description = _normalize_description_string(row["column_description"]) if pd.notna(row["column_description"]) else ""
            data_format = row["data_format"].strip() if pd.notna(row["data_format"]) else ""
            value_description = _normalize_description_string(row["value_description"]) if pd.notna(row["value_description"]) else ""
            if value_description.lower().startswith("not useful"):
                value_description = value_description[len("not useful"):].strip()
            table_description[original_column_name_lower] = {
                "original_column_name_lower": original_column_name_lower,
                "expanded_column_name": expanded_column_name,
                "column_description": column_description,
                "data_format": data_format,
                "value_description": value_description,
            }
        database_description[table_name_lower] = table_description
    return database_description


@lru_cache(maxsize=1000)
def load_database_schema_dict(db_path: Union[str, Path], use_database_description: bool = True) -> Dict[str, Any]:
    db_path = Path(db_path) if isinstance(db_path, str) else db_path
    db_id = db_path.stem
    database_description = load_database_description(db_id, db_path.parent, use_database_description)
    database_schema_dict = {"db_id": db_id, "db_path": str(db_path), "tables": {}}
    table_names = load_table_names(db_path)
    for table_name in table_names:
        table_schema_dict = {"table_name": table_name, "columns": {}}
        primary_keys = load_primary_keys(db_path, table_name)
        foreign_keys = load_foreign_keys(db_path, table_name)
        column_names_and_types = load_column_names_and_types(db_path, table_name)
        for column_name, column_type in column_names_and_types:
            column_schema_dict = {"column_name": column_name, "column_type": column_type}
            column_schema_dict["primary_key"] = column_name.lower() in [pk.lower() for pk in primary_keys]
            column_schema_dict["foreign_keys"] = []
            for src_tbl, src_col, tgt_tbl, tgt_col in foreign_keys:
                assert src_tbl == table_name
                if src_col.lower() == column_name.lower():
                    column_schema_dict["foreign_keys"].append((tgt_tbl, tgt_col))
            # 描述
            descriptions = []
            tbl_desc = database_description.get(table_name.lower(), {})
            col_desc = tbl_desc.get(column_name.lower(), {})
            if col_desc.get("expanded_column_name", ""):
                descriptions.append(f"Expanded Column Name: {col_desc['expanded_column_name']}")
            if col_desc.get("column_description", ""):
                descriptions.append(f"Column Description: {col_desc['column_description']}")
            if col_desc.get("value_description", ""):
                descriptions.append(f"Value Description: {col_desc['value_description']}")
            column_schema_dict["description"] = " | ".join(descriptions) if descriptions else ""
            # 值示例
            if use_database_description and column_type.upper() != "BLOB":
                column_schema_dict["value_examples"] = load_value_examples(str(db_path), table_name, column_name)
            else:
                column_schema_dict["value_examples"] = []
            if use_database_description:
                column_schema_dict["value_statistics"] = load_value_statistics(str(db_path), table_name, column_name)
            else:
                column_schema_dict["value_statistics"] = None
            table_schema_dict["columns"][column_name] = column_schema_dict
        database_schema_dict["tables"][table_name] = table_schema_dict

    # 修复悬空外键
    for table_name, table_schema_dict in database_schema_dict["tables"].items():
        for column_name, column_schema_dict in table_schema_dict["columns"].items():
            for tgt_tbl, tgt_col in list(column_schema_dict["foreign_keys"]):
                if tgt_tbl not in database_schema_dict["tables"] or tgt_col not in database_schema_dict["tables"][tgt_tbl]["columns"]:
                    column_schema_dict["foreign_keys"].remove((tgt_tbl, tgt_col))
    return database_schema_dict


# ==================== Schema 工具函数 ====================

def get_database_schema_profile(database_schema_dict: Dict[str, Any]) -> str:
    profile = ""
    db_id = database_schema_dict["db_id"]
    profile += f"Database ID: `{db_id}`\n"
    profile += "Schema:\n"
    for table_name, table_schema_dict in database_schema_dict["tables"].items():
        profile += f"- Table: `{table_name}`\n"
        profile += "[\n"
        column_profiles = []
        columns = list(table_schema_dict["columns"].items())
        pk_columns = [(cn, cs) for cn, cs in columns if cs["primary_key"]]
        non_pk_columns = [(cn, cs) for cn, cs in columns if not cs["primary_key"]]
        ordered_columns = pk_columns + non_pk_columns
        for column_name, csd in ordered_columns:
            cp = f"`{column_name}`: {csd['column_type']}"
            if csd["primary_key"]:
                cp += " | Primary Key"
            if csd["description"]:
                cp += f" | {csd['description']}"
            if csd["value_statistics"]:
                cp += f" | Value Statistics: {csd['value_statistics']['null_count']} NULL values, {csd['value_statistics']['distinct_count']} distinct values, {csd['value_statistics']['total_count']} total values"
            if csd["value_examples"]:
                cp += f" | Value Examples: {csd['value_examples']}"
            column_profiles.append(f"({cp})")
        profile += ",\n".join(column_profiles) + "\n"
        profile += "]\n"
    # 外键
    all_foreign_keys = []
    for table_name, table_schema_dict in database_schema_dict["tables"].items():
        for column_name, csd in table_schema_dict["columns"].items():
            for tgt_tbl, tgt_col in csd["foreign_keys"]:
                if tgt_tbl in database_schema_dict["tables"] and tgt_col in database_schema_dict["tables"][tgt_tbl]["columns"]:
                    all_foreign_keys.append(f"`{table_name}`.`{column_name}` = `{tgt_tbl}`.`{tgt_col}`")
    if all_foreign_keys:
        profile += "Foreign Keys:\n"
        profile += "\n".join(all_foreign_keys) + "\n"
    return profile


def map_lower_table_name_to_original_table_name(table_name: str, database_schema_dict: Dict[str, Any]) -> Optional[str]:
    for tsd in database_schema_dict["tables"].values():
        if tsd["table_name"].lower() == table_name.lower():
            return tsd["table_name"]
    return None


def map_lower_column_name_to_original_column_name(table_name: str, column_name: str, database_schema_dict: Dict[str, Any]) -> Optional[str]:
    for tsd in database_schema_dict["tables"].values():
        if tsd["table_name"].lower() == table_name.lower():
            for csd in tsd["columns"].values():
                if csd["column_name"].lower() == column_name.lower():
                    return csd["column_name"]
    return None


def filter_used_database_schema(database_schema_dict: Dict[str, Any], linked_tables_and_columns: Dict[str, List[str]], force_include_pks_and_fks: bool = True):
    filtered = {"db_id": database_schema_dict["db_id"], "db_path": database_schema_dict["db_path"], "tables": {}}
    for table_name in linked_tables_and_columns.keys():
        table_dict = database_schema_dict["tables"][table_name]
        filtered_table = {"table_name": table_dict["table_name"], "columns": {}}
        for column_name in linked_tables_and_columns[table_name]:
            filtered_table["columns"][column_name] = table_dict["columns"][column_name].copy()
        if len(filtered_table["columns"]) > 0:
            filtered["tables"][table_name] = filtered_table
    if force_include_pks_and_fks:
        for table_name, table_dict in database_schema_dict["tables"].items():
            for column_name, csd in table_dict["columns"].items():
                if csd["primary_key"] and table_name in filtered["tables"]:
                    filtered["tables"][table_name]["columns"][column_name] = csd.copy()
                if csd["foreign_keys"]:
                    for tgt_tbl, tgt_col in csd["foreign_keys"]:
                        if (table_name in filtered["tables"] and tgt_tbl in filtered["tables"]
                                and tgt_col in database_schema_dict["tables"][tgt_tbl]["columns"]):
                            filtered["tables"][table_name]["columns"][column_name] = csd.copy()
                            filtered["tables"][tgt_tbl]["columns"][tgt_col] = database_schema_dict["tables"][tgt_tbl]["columns"][tgt_col].copy()
    return filtered


def merge_schema_linking_results(results: List[Dict[str, List[str]]]) -> Dict[str, List[str]]:
    """
    合并多个schema链接结果，去重并保持列的顺序
    
    Args:
        results: 多个schema链接结果的列表，每个结果是{table_name: [column_names]}的字典
        
    Returns:
        Dict[str, List[str]]: 合并后的schema链接结果，相同表的列会去重合并
        
    Example:
        >>> result1 = {"users": ["id", "name"], "orders": ["order_id"]}
        >>> result2 = {"users": ["id", "email"], "products": ["product_id"]}
        >>> merge_schema_linking_results([result1, result2])
        {"users": ["id", "name", "email"], "orders": ["order_id"], "products": ["product_id"]}
    """
    merged_result = {}
    
    # 遍历每个链接结果
    for result in results:
        if not isinstance(result, dict):
            continue
            
        for table_name, columns in result.items():
            if not isinstance(columns, list):
                continue
                
            # 如果表名还没有在合并结果中，创建一个集合来去重
            if table_name not in merged_result:
                merged_result[table_name] = set()
            
            # 添加列名到集合中（自动去重）
            merged_result[table_name].update(columns)
    
    # 将集合转换回列表，保持一定的顺序
    return {table_name: list(columns) for table_name, columns in merged_result.items()}