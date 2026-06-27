from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_json, atomic_write_npy, atomic_write_pickle
from ..core.embedding import EmbeddingEncoder
from ..models.step1_models import StepResult
from ..sources.bird_metadata import load_dev_tables, select_databases
from .base import StepBuilder

logger = logging.getLogger(__name__)


class Step1f2BuildValueDescVectorsBuilder(StepBuilder):
    """db 级 enum 聚合 + 向量化 builder。

    依赖 step1f1 在 STEP1F1_COLUMN_STAGING_DIR/{db_id}/ 下产出的列级 staging 文件。
    db 级断点：completed_keys 存 db_id；先 atomic 写产物（enum json / vectors.npy / metadata.pkl）→ 后 add_completed_key。
    """

    step_name = "step1f2_build_value_desc_vectors"

    def _run_impl(self) -> StepResult:
        dev_tables = load_dev_tables(self.context.bird_tables_json)
        db_entries = select_databases(
            dev_tables,
            db_filter=self.context.db_filter,
            limit=self.context.limit,
        )

        encoder = EmbeddingEncoder(
            model_name=config.EMBEDDING_MODEL,
            device=config.EMBEDDING_DEVICE,
        )

        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()
        logger.info("step1f2: %d databases to process, %d already completed", len(db_entries), len(completed))

        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        separator = str(config.STEP1F_TEXT_SEPARATOR)

        for db_idx, entry in enumerate(db_entries, 1):
            db_id = str(entry.get("db_id") or "").strip()
            if not db_id:
                continue
            if not self.context.should_process_db(db_id):
                continue
            if db_id in completed:
                skipped_count += 1
                logger.debug("step1f2: skip db=%s (already completed)", db_id)
                continue

            db_staging_dir = self.context.artifacts.step1f1_column_staging_dir / db_id
            column_payload: Dict[str, Dict[str, Any]] = {}
            vector_metadata: List[Dict[str, Any]] = []
            texts: List[str] = []

            if db_staging_dir.exists():
                staging_files = sorted(db_staging_dir.glob("*.json"))
                for staging_path in staging_files:
                    try:
                        import json

                        with staging_path.open("r", encoding="utf-8") as fp:
                            staged = json.load(fp)
                    except Exception:
                        continue
                    if not isinstance(staged, dict):
                        continue
                    column_id = str(staged.get("column_id") or "").strip()
                    if not column_id:
                        continue
                    table_name = str(staged.get("table_name") or "")
                    column_name = str(staged.get("column_name") or "")
                    data_type = str(staged.get("data_type") or "")
                    coverage = float(staged.get("coverage") or 0.0)
                    distinct_count = int(staged.get("distinct_count") or 0)
                    unmatched_count = int(staged.get("unmatched_count") or 0)
                    validated_values = staged.get("values") or []
                    if not isinstance(validated_values, list) or not validated_values:
                        continue

                    column_payload[column_id] = {
                        "data_type": data_type,
                        "coverage": coverage,
                        "distinct_count": distinct_count,
                        "unmatched_count": unmatched_count,
                        "values": validated_values,
                    }

                    for item in validated_values:
                        if not isinstance(item, dict):
                            continue
                        value = item.get("value")
                        description = item.get("description", "")
                        text = (
                            f"{value}{separator}{description}"
                            if description
                            else f"{value}"
                        )
                        texts.append(text)
                        vector_metadata.append(
                            {
                                "db_id": db_id,
                                "table_name": table_name,
                                "column_name": column_name,
                                "column_id": column_id,
                                "column_type": data_type,
                                "value": value,
                                "description": description,
                                "coverage": coverage,
                                "text": text,
                            }
                        )

            logger.info("step1f2: [%d/%d] db=%s staging_files=%d, encoding %d value vectors...",
                        db_idx, len(db_entries), db_id, len(staging_files) if db_staging_dir.exists() else 0, len(texts))
            vectors = encoder.encode(texts)
            enum_json_path = self.context.artifacts.value_desc_enum_path(db_id)
            vectors_path, metadata_path = self.context.artifacts.value_desc_vector_paths(db_id)

            # 先写产物，再标记 state
            atomic_write_json(enum_json_path, column_payload)
            atomic_write_npy(vectors_path, vectors)
            atomic_write_pickle(metadata_path, {"metadata": vector_metadata, "texts": texts})
            outputs.extend([str(enum_json_path), str(vectors_path), str(metadata_path)])

            processed_count += 1
            self.progress.add_completed_key(db_id)
            completed.add(db_id)
            logger.info("step1f2: [%d/%d] db=%s done, enum_columns=%d, vectors=%d",
                        db_idx, len(db_entries), db_id, len(column_payload), len(texts))

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )
