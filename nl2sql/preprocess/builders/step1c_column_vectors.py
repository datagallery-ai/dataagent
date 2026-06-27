from __future__ import annotations

import logging
from typing import Any, Dict, List

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_npy, atomic_write_pickle, read_json
from ..core.embedding import EmbeddingEncoder
from ..models.step1_models import StepResult
from .base import StepBuilder

logger = logging.getLogger(__name__)


class Step1cColumnVectorsBuilder(StepBuilder):
    step_name = "step1c_build_column_vectors"

    def _run_impl(self) -> StepResult:
        columns_info_path = self.context.artifacts.rest_cache_dir / "table_columns_info_cache.json"
        columns_info_cache: Dict[str, Dict[str, list[Dict[str, Any]]]] = read_json(columns_info_path, default={})
        encoder = EmbeddingEncoder(model_name=config.EMBEDDING_MODEL, device=config.EMBEDDING_DEVICE)

        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        db_ids = [db_id for db_id in columns_info_cache if self.context.should_process_db(db_id)]
        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()
        logger.info("step1c: %d databases to process, %d already completed", len(db_ids), len(completed))

        for db_idx, db_id in enumerate(db_ids, 1):
            table_map = columns_info_cache[db_id]
            if not self.context.force_rerun and db_id in completed:
                skipped_count += 1
                logger.debug("step1c: skip db=%s (already completed)", db_id)
                continue
            texts: List[str] = []
            metadata: List[Dict[str, Any]] = []
            for table_name, columns in table_map.items():
                for column in columns or []:
                    column_name = str(column.get("column_name") or "").strip()
                    column_id = str(column.get("column_id") or "").strip()
                    desc_short = str(column.get("column_description_short") or column.get("desc_short") or "").strip()
                    separator = str(config.STEP1C_TEXT_SEPARATOR)
                    text = f"{column_name}{separator}{desc_short}" if desc_short else column_name
                    texts.append(text)
                    metadata.append(
                        {
                            "db_id": db_id,
                            "table_name": table_name,
                            "column_name": column_name,
                            "column_id": column_id,
                            "text": text,
                        }
                    )
            logger.info("step1c: [%d/%d] db=%s encoding %d column vectors...",
                        db_idx, len(db_ids), db_id, len(texts))
            vectors = encoder.encode(texts)
            vectors_path, metadata_path = self.context.artifacts.column_vector_paths(db_id)
            atomic_write_npy(vectors_path, vectors)
            atomic_write_pickle(metadata_path, {"metadata": metadata, "texts": texts})
            outputs.extend([str(vectors_path), str(metadata_path)])
            processed_count += 1
            self.progress.add_completed_key(db_id)
            logger.info("step1c: [%d/%d] db=%s done, vectors=%d", db_idx, len(db_ids), db_id, len(texts))

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )
