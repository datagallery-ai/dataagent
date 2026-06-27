from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from datasketch import MinHash, MinHashLSH
from ...common.atomic_io import atomic_write_pickle
from ..models.step1_models import StepResult
from ..sources.bird_metadata import load_dev_tables, load_sqlite_tables, select_databases, sqlite_path
from .base import StepBuilder

try:
    from ... import config
except ImportError:
    import config

logger = logging.getLogger(__name__)


class Step1eLshIndexBuilder(StepBuilder):
    step_name = "step1e_build_lsh_indexes"

    def _run_impl(self) -> StepResult:
        import sqlite3

        dev_tables = load_dev_tables(self.context.bird_tables_json)
        db_entries = select_databases(dev_tables, db_filter=self.context.db_filter, limit=self.context.limit)
        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()
        logger.info("step1e: %d databases to process, %d already completed",
                    len(db_entries), len(completed))

        for entry in db_entries:
            db_id = str(entry.get("db_id") or "").strip()
            if not db_id:
                continue
            if not self.context.force_rerun and db_id in completed:
                skipped_count += 1
                logger.debug("step1e: skip db=%s (already completed)", db_id)
                continue
            db_path = sqlite_path(self.context.bird_db_dir, db_id)
            if not db_path.exists():
                raise FileNotFoundError(f"sqlite database not found: {db_path}")
            db_name = Path(db_path).stem
            logger.info("step1e: building LSH index for db=%s", db_id)
            lsh = MinHashLSH(threshold=config.STEP1E_LSH_THRESHOLD, num_perm=config.STEP1E_LSH_NUM_PERM)
            minhashes: Dict[str, MinHash] = {}
            records: Dict[str, Dict[str, Any]] = {}

            # timeout=30：当 sqlite 文件被其他进程持锁时，最多等 30 秒后抛 OperationalError，
            # 避免在持锁场景下无限等待。正常单进程独占下不会触发等待。
            logger.info("step1e:   db=%s connecting to sqlite (timeout=30s) ...", db_id)
            with sqlite3.connect(str(db_path), timeout=30) as conn:
                columns = _get_columns(conn)
                total_cols = len(columns)
                logger.info("step1e:   db=%s columns=%d, loading distinct values...", db_id, total_cols)
                for col_idx, (table_name, column_name) in enumerate(columns, 1):
                    col_values = _get_distinct_values(
                        conn, table_name, column_name, limit=int(config.STEP1E_LSH_SAMPLE_LIMIT)
                    )
                    for value in col_values:
                        key = f"{db_name}.{table_name}.{column_name}:{value}"
                        mh = _create_minhash(value, num_perm=config.STEP1E_LSH_NUM_PERM, k=config.STEP1E_LSH_K)
                        try:
                            lsh.insert(key, mh)
                            minhashes[key] = mh
                            records[key] = {
                                "matched_column": f"{db_name}.{table_name}.{column_name}",
                                "matched_value": value,
                            }
                        except ValueError:
                            continue
                    pct = col_idx * 100 // total_cols
                    logger.info(
                        "step1e:   db=%s [%d/%d %d%%] %s.%s (+%d values, total=%d)",
                        db_id, col_idx, total_cols, pct,
                        table_name, column_name, len(col_values), len(minhashes),
                    )
            output_path = self.context.artifacts.lsh_index_path(db_id)
            atomic_write_pickle(
                output_path,
                {
                    "lsh": lsh,
                    "minhashes": minhashes,
                    "records": records,
                    "meta": {
                        "database": db_name,
                        "params": {
                            "num_perm": config.STEP1E_LSH_NUM_PERM,
                            "threshold": config.STEP1E_LSH_THRESHOLD,
                            "k": config.STEP1E_LSH_K,
                        },
                        "total_values": len(minhashes),
                        "build_time": datetime.now().isoformat(),
                    },
                },
            )
            outputs.append(str(output_path))
            processed_count += 1
            self.progress.add_completed_key(db_id)
            logger.info("step1e:   db=%s done, total_values=%d, output=%s",
                        db_id, len(minhashes), output_path)

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )


def _create_minhash(value: str, num_perm: int, k: int) -> MinHash:
    minhash = MinHash(num_perm=num_perm)
    text = str(value or "")
    max_iter = max(1, len(text) - k + 1)
    for idx in range(max_iter):
        shingle = text[idx: idx + k]
        minhash.update(shingle.encode("utf-8"))
    return minhash


def _quote_ident(name: str) -> str:
    return '"' + str(name or "").replace('"', '""') + '"'


def _get_columns(conn: Any) -> List[tuple[str, str]]:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    columns: List[tuple[str, str]] = []
    for row in tables:
        table_name = str(row[0]) if row else ""
        if not table_name:
            continue
        try:
            cursor = conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})")
            for col in cursor:
                column_name = str(col[1] or "")
                if column_name:
                    columns.append((table_name, column_name))
        except Exception:
            continue
    return columns


def _get_distinct_values(conn: Any, table: str, column: str, limit: int | None = None) -> List[str]:
    sql = f"SELECT DISTINCT {_quote_ident(column)} FROM {_quote_ident(table)} WHERE {_quote_ident(column)} IS NOT NULL"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        cursor = conn.execute(sql)
        return [str(row[0]) for row in cursor if row and row[0] is not None]
    except Exception:
        return []
