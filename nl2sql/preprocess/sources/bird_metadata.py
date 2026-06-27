from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List


def load_dev_tables(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"invalid dev_tables format: {type(data)}")
    return data


def select_databases(
    dev_tables: List[Dict[str, Any]],
    db_filter: set[str] | None = None,
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in dev_tables:
        db_id = str(entry.get("db_id") or "").strip()
        if not db_id:
            continue
        if db_filter is not None and db_id not in db_filter:
            continue
        items.append(entry)
        if limit is not None and len(items) >= limit:
            break
    return items


def _normalize_description(text: str) -> str:
    value = str(text or "").replace("\r", " ").replace("\n", " ").replace("commonsense evidence:", " ").strip()
    while "  " in value:
        value = value.replace("  ", " ")
    return value.strip()


def load_table_description_map(db_root: Path, db_id: str, table_name: str) -> Dict[str, Dict[str, str]]:
    csv_path = db_root / db_id / "database_description" / f"{table_name}.csv"
    if not csv_path.exists():
        return {}
    mapping: Dict[str, Dict[str, str]] = {}
    encodings = ["utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1", "cp1252"]
    for encoding in encodings:
        try:
            with csv_path.open("r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f, skipinitialspace=True)
                for row in reader:
                    normalized = {(str(k or "").lstrip("\ufeff").strip()): str(v or "") for k, v in (row or {}).items()}
                    original_name = normalized.get("original_column_name", "").strip()
                    if not original_name:
                        continue
                    payload = {
                        "column_name": normalized.get("column_name", "").strip(),
                        "column_description": _normalize_description(normalized.get("column_description", "")),
                        "data_format": normalized.get("data_format", "").strip(),
                        "value_description": _normalize_description(normalized.get("value_description", "")),
                    }
                    mapping[original_name] = payload
                    mapping.setdefault(original_name.lower(), payload)
            return mapping
        except UnicodeDecodeError:
            mapping = {}
            continue
    with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        for row in reader:
            normalized = {(str(k or "").lstrip("\ufeff").strip()): str(v or "") for k, v in (row or {}).items()}
            original_name = normalized.get("original_column_name", "").strip()
            if not original_name:
                continue
            payload = {
                "column_name": normalized.get("column_name", "").strip(),
                "column_description": _normalize_description(normalized.get("column_description", "")),
                "data_format": normalized.get("data_format", "").strip(),
                "value_description": _normalize_description(normalized.get("value_description", "")),
            }
            mapping[original_name] = payload
            mapping.setdefault(original_name.lower(), payload)
    return mapping


def sqlite_path(db_root: Path, db_id: str) -> Path:
    return db_root / db_id / f"{db_id}.sqlite"


def load_sqlite_tables(db_path: Path) -> List[str]:
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence';"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [str(row[0]) for row in cursor.fetchall()]


def load_table_info(db_path: Path, table_name: str) -> List[Dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info(`{table_name}`);")
        rows = cursor.fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": row[3],
                "default": row[4],
                "pk": row[5],
            }
        )
    return result


def load_foreign_keys(db_path: Path, table_name: str) -> List[Dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA foreign_key_list(`{table_name}`);")
        rows = cursor.fetchall()
    results: List[Dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "source_table": table_name,
                "target_table": row[2],
                "source_column": row[3],
                "target_column": row[4],
            }
        )
    return results


def load_sample_values(db_path: Path, table_name: str, column_name: str, sample_count: int = 3) -> List[str]:
    sql = (
        f"SELECT DISTINCT `{column_name}` FROM `{table_name}` "
        f"WHERE `{column_name}` IS NOT NULL AND CAST(`{column_name}` AS TEXT) != '' "
        f"AND length(CAST(`{column_name}` AS TEXT)) <= 100 LIMIT {int(sample_count)};"
    )
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
    return [str(row[0]) for row in rows if row and row[0] is not None]


def build_column_description_payload(column_name: str, desc_row: Dict[str, str]) -> Dict[str, str]:
    expanded_name = str(desc_row.get("column_name") or "").strip()
    column_description = str(desc_row.get("column_description") or "").strip()
    value_description = str(desc_row.get("value_description") or "").strip()
    parts: List[str] = []
    if expanded_name:
        parts.append(f"Expanded Column Name: {expanded_name}")
    if column_description:
        parts.append(f"Column Description: {column_description}")
    if value_description:
        parts.append(f"Value Description: {value_description}")
    desc = " | ".join(parts)
    desc_simple = " | ".join([item for item in [expanded_name, column_description] if item])
    desc_short = expanded_name or column_description or column_name
    return {
        "desc_short": desc_short,
        "column_description_short": desc_short,
        "desc_simple": desc_simple,
        "desc": desc,
        "value_description": value_description,
    }


def iter_table_names_from_dev_table(entry: Dict[str, Any]) -> Iterable[str]:
    names = entry.get("table_names_original") or []
    for table_name in names:
        name = str(table_name or "").strip()
        if name:
            yield name
