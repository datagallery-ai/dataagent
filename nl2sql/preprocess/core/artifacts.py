from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from ... import config
except ImportError:
    import config


@dataclass(frozen=True)
class ArtifactLayout:
    workspace_root: Path
    log_root: Path

    @classmethod
    def from_config(cls) -> "ArtifactLayout":
        workspace_root = Path(config.STEP1_PREPROCESS_WORKSPACE_DIR)
        log_root = Path(config.STEP1_PREPROCESS_LOG_DIR)
        return cls(workspace_root=workspace_root, log_root=log_root)

    @property
    def progress_dir(self) -> Path:
        return self.log_root

    @property
    def summary_path(self) -> Path:
        return Path(config.STEP1_PREPROCESS_SUMMARY_PATH)

    @property
    def rest_cache_dir(self) -> Path:
        return Path(config.STEP1_CACHE_DIR)

    @property
    def column_vector_store_dir(self) -> Path:
        return Path(config.STEP1_COLUMN_VECTOR_STORE_DIR)

    @property
    def join_relations_dir(self) -> Path:
        return Path(config.STEP1_JOIN_RELATIONS_DIR)

    @property
    def lsh_indexes_dir(self) -> Path:
        return Path(config.STEP1_LSH_INDEX_DIR)

    @property
    def value_desc_enum_dir(self) -> Path:
        return Path(config.STEP1_VALUE_DESC_ENUM_DIR)

    @property
    def value_desc_vectors_dir(self) -> Path:
        return Path(config.STEP1_VALUE_DESC_VECTOR_DIR)

    @property
    def value_vector_store_dir(self) -> Path:
        return Path(config.STEP1_VALUE_VECTOR_STORE_DIR)

    @property
    def log_file_path(self) -> Path:
        return Path(config.STEP1_PREPROCESS_RUN_LOG_PATH)

    @property
    def state_dir(self) -> Path:
        # 平铺布局：log/preprocess/state/<step_name>.json
        return self.log_root / str(config.STEP1_PREPROCESS_STATE_DIR_NAME)

    def step_progress_path(self, step_name: str) -> Path:
        return self.state_dir / f"{step_name}.json"

    def join_relation_path(self, db_id: str) -> Path:
        return self.join_relations_dir / f"{db_id}.json"

    def column_vector_paths(self, db_id: str) -> tuple[Path, Path]:
        base = self.column_vector_store_dir / db_id
        return base / "vectors.npy", base / "metadata.pkl"

    def value_desc_vector_paths(self, db_id: str) -> tuple[Path, Path]:
        base = self.value_desc_vectors_dir / db_id
        return base / "vectors.npy", base / "metadata.pkl"

    def value_vector_paths(self, db_id: str) -> tuple[Path, Path]:
        base = self.value_vector_store_dir / db_id
        return base / "vectors.npy", base / "metadata.pkl"

    def lsh_index_path(self, db_id: str) -> Path:
        return self.lsh_indexes_dir / db_id / "lsh_index.pkl"

    def value_desc_enum_path(self, db_id: str) -> Path:
        return self.value_desc_enum_dir / f"{db_id}.json"

    # ===== step1b 列级 staging =====
    @property
    def step1b_staging_dir(self) -> Path:
        return Path(config.STEP1B_STAGING_DIR)

    def step1b_column_staging_path(self, db_id: str, table: str, column: str) -> Path:
        safe_table = str(table).replace("/", "_").replace(":", "_")
        safe_col = str(column).replace("/", "_").replace(":", "_")
        return self.step1b_staging_dir / db_id / f"{safe_table}__{safe_col}.json"

    # ===== step1f1 列级 staging（仅枚举列产出文件） =====
    @property
    def step1f1_column_staging_dir(self) -> Path:
        return Path(config.STEP1F1_COLUMN_STAGING_DIR)

    def step1f1_column_staging_path(self, db_id: str, table: str, column: str) -> Path:
        safe_table = str(table).replace("/", "_").replace(":", "_")
        safe_col = str(column).replace("/", "_").replace(":", "_")
        return self.step1f1_column_staging_dir / db_id / f"{safe_table}__{safe_col}.json"

    # ===== step1h 题目级 staging + few-shot 输出 =====
    @property
    def step1h_question_staging_dir(self) -> Path:
        return Path(config.STEP1H_QUESTION_STAGING_DIR)

    def step1h_question_staging_path(self, question_id: str) -> Path:
        safe = str(question_id).replace("/", "_").replace(":", "_")
        return self.step1h_question_staging_dir / f"{safe}.json"

    @property
    def few_shot_output_path(self) -> Path:
        return Path(config.FEW_SHOT_PATH)
