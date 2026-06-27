from __future__ import annotations

from typing import Any, Dict, List

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_json, read_json
from ..models.step1_models import StepResult
from ..sources.bird_metadata import (
    build_column_description_payload,
    iter_table_names_from_dev_table,
    load_dev_tables,
    load_foreign_keys,
    load_sample_values,
    load_sqlite_tables,
    load_table_description_map,
    load_table_info,
    select_databases,
    sqlite_path,
)
from .base import StepBuilder


class Step1aSchemaCacheBuilder(StepBuilder):
    step_name = "step1a_build_schema_cache"

    def _run_impl(self) -> StepResult:
        dev_tables = load_dev_tables(self.context.bird_tables_json)
        db_entries = select_databases(dev_tables, db_filter=self.context.db_filter, limit=self.context.limit)

        rest_cache_dir = self.context.artifacts.rest_cache_dir
        table_list_path = rest_cache_dir / "table_list_cache.json"
        columns_info_path = rest_cache_dir / "table_columns_info_cache.json"
        sample_values_path = rest_cache_dir / "columns_sample_values_cache.json"

        if self.context.force_rerun:
            table_list_cache: Dict[str, List[Dict[str, Any]]] = {}
            columns_info_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
            sample_values_cache: Dict[str, List[str]] = {}
        else:
            table_list_cache = read_json(table_list_path, default={})
            columns_info_cache = read_json(columns_info_path, default={})
            sample_values_cache = read_json(sample_values_path, default={})

        processed_count = 0
        skipped_count = 0

        for entry in db_entries:
            db_id = str(entry.get("db_id") or "").strip()
            if not db_id:
                continue
            if not self.context.force_rerun and db_id in self.progress.completed_keys():
                skipped_count += 1
                continue

            db_path = sqlite_path(self.context.bird_db_dir, db_id)
            if not db_path.exists():
                raise FileNotFoundError(f"sqlite database not found: {db_path}")

            dev_table_names = list(iter_table_names_from_dev_table(entry))
            actual_tables = load_sqlite_tables(db_path)
            actual_table_set = set(actual_tables)
            filtered_table_names = [name for name in (dev_table_names or actual_tables) if name in actual_table_set]

            table_list_cache[db_id] = [
                {
                    "table_name": table_name,
                    "table_description": "",
                }
                for table_name in filtered_table_names
            ]

            db_columns_info: Dict[str, List[Dict[str, Any]]] = {}
            for table_name in filtered_table_names:
                desc_map = load_table_description_map(self.context.bird_db_dir, db_id, table_name)
                foreign_key_columns = {
                    str(item.get("source_column") or "").strip()
                    for item in load_foreign_keys(db_path, table_name)
                    if str(item.get("source_column") or "").strip()
                }
                columns_meta: List[Dict[str, Any]] = []
                for column in load_table_info(db_path, table_name):
                    column_name = str(column.get("name") or "").strip()
                    column_id = f"{db_id}.{table_name}.{column_name}"
                    desc_row = desc_map.get(column_name) or desc_map.get(column_name.lower()) or {}
                    desc_payload = build_column_description_payload(column_name, desc_row)
                    column_record = {
                        "column_id": column_id,
                        "column_name": column_name,
                        "column_type": str(column.get("type") or ""),
                        "desc_short": desc_payload["desc_short"],
                        "column_description_short": desc_payload["column_description_short"],
                        "desc_simple": desc_payload["desc_simple"],
                        "desc": desc_payload["desc"],
                        "value_description": desc_payload["value_description"],
                        "is_primary_key": bool(column.get("pk")),
                        "is_foreign_key": column_name in foreign_key_columns,
                    }
                    sample_values_cache[column_id] = load_sample_values(
                        db_path,
                        table_name,
                        column_name,
                        sample_count=int(config.STEP1A_SAMPLE_VALUE_COUNT),
                    )
                    columns_meta.append(column_record)
                db_columns_info[table_name] = columns_meta
            columns_info_cache[db_id] = db_columns_info

            # 先写产物，再标记 state（保证写入顺序一致）
            atomic_write_json(table_list_path, table_list_cache)
            atomic_write_json(columns_info_path, columns_info_cache)
            atomic_write_json(sample_values_path, sample_values_cache)

            processed_count += 1
            self.progress.add_completed_key(db_id)

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=[str(table_list_path), str(columns_info_path), str(sample_values_path)] if processed_count > 0 else [],
        )
