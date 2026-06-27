from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, List

import numpy as np

from ...common.atomic_io import atomic_write_npy, atomic_write_pickle
from ..core.embedding import EmbeddingEncoder
from ..models.step1_models import StepResult
from ..sources.bird_metadata import load_dev_tables, load_sqlite_tables, select_databases, sqlite_path
from .base import StepBuilder

try:
    from ... import config
except ImportError:
    import config

logger = logging.getLogger(__name__)

_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NUMBER_PATTERN = re.compile(r"^[0-9]+\.?[0-9]*$")


class Step1gValueVectorDbBuilder(StepBuilder):
    step_name = "step1g_build_value_vector_db"

    def _run_impl(self) -> StepResult:
        dev_tables = load_dev_tables(self.context.bird_tables_json)
        db_entries = select_databases(dev_tables, db_filter=self.context.db_filter, limit=self.context.limit)
        encoder = EmbeddingEncoder(model_name=config.EMBEDDING_MODEL, device=config.EMBEDDING_DEVICE)
        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()
        logger.info("step1g: %d databases to process, %d already completed", len(db_entries), len(completed))

        for db_idx, entry in enumerate(db_entries, 1):
            db_id = str(entry.get("db_id") or "").strip()
            if not db_id:
                continue
            if not self.context.force_rerun and db_id in completed:
                skipped_count += 1
                logger.debug("step1g: skip db=%s (already completed)", db_id)
                continue
            db_path = sqlite_path(self.context.bird_db_dir, db_id)
            values: List[str] = []
            metadata: List[Dict[str, Any]] = []
            logger.info("step1g: [%d/%d] db=%s scanning text columns...", db_idx, len(db_entries), db_id)

            with sqlite3.connect(str(db_path)) as conn:
                cursor = conn.cursor()
                tables = load_sqlite_tables(db_path)
                for tbl_idx, table_name in enumerate(tables, 1):
                    cursor.execute(f"PRAGMA table_info(`{table_name}`);")
                    for row in cursor.fetchall():
                        column_name = str(row[1])
                        column_type = str(row[2] or "")
                        upper_type = column_type.upper()
                        if upper_type != "TEXT" and not upper_type.startswith("VARCHAR") and not upper_type.startswith("CHAR"):
                            continue
                        cursor.execute(
                            f"SELECT DISTINCT `{column_name}` FROM `{table_name}` WHERE `{column_name}` IS NOT NULL AND LENGTH(CAST(`{column_name}` AS TEXT)) <= {int(config.STEP1G_MAX_VALUE_LENGTH)} LIMIT {int(config.STEP1G_MAX_VALUES_PER_COLUMN)};"
                        )
                        sampled_values = [str(item[0]) for item in cursor.fetchall() if item and item[0] is not None]
                        if not sampled_values:
                            continue
                        if bool(config.STEP1G_SKIP_UUID_ONLY_COLUMNS) and all(_UUID_PATTERN.match(item) for item in sampled_values):
                            continue
                        if bool(config.STEP1G_SKIP_NUMBER_ONLY_COLUMNS) and all(_NUMBER_PATTERN.match(item) for item in sampled_values):
                            continue
                        for value in sampled_values:
                            values.append(value)
                            metadata.append(
                                {
                                    "db_id": db_id.lower() if config.STEP1G_LOWER_META_DATA else db_id,
                                    "table_name": table_name.lower() if config.STEP1G_LOWER_META_DATA else table_name,
                                    "column_name": column_name.lower() if config.STEP1G_LOWER_META_DATA else column_name,
                                }
                            )
                    logger.info("step1g:   db=%s [%d/%d] table=%s values_so_far=%d",
                                db_id, tbl_idx, len(tables), table_name, len(values))
            # 分批 encode，避免一次性分配巨大中间张量
            batch_size = int(config.STEP1G_ENCODE_BATCH_SIZE)
            logger.info("step1g: [%d/%d] db=%s encoding %d values (batch_size=%d)...",
                        db_idx, len(db_entries), db_id, len(values), batch_size)
            if values:
                all_embeddings: List[np.ndarray] = []
                for batch_start in range(0, len(values), batch_size):
                    batch = values[batch_start: batch_start + batch_size]
                    batch_vectors = encoder.encode(batch)
                    all_embeddings.append(batch_vectors)
                    batch_end = min(batch_start + batch_size, len(values))
                    if (batch_start // batch_size) % 5 == 0 or batch_end == len(values):
                        logger.info("step1g:   db=%s encoding progress: %d/%d",
                                    db_id, batch_end, len(values))
                vectors = np.vstack(all_embeddings)
            else:
                vectors = np.empty((0, 1), dtype=np.float32)
            vectors_path, metadata_path = self.context.artifacts.value_vector_paths(db_id)
            atomic_write_npy(vectors_path, vectors)
            atomic_write_pickle(metadata_path, {"metadata": metadata, "values": values})
            outputs.extend([str(vectors_path), str(metadata_path)])
            processed_count += 1
            self.progress.add_completed_key(db_id)
            logger.info("step1g: [%d/%d] db=%s done, total_values=%d", db_idx, len(db_entries), db_id, len(values))

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )
