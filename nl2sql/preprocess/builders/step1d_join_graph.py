from __future__ import annotations

import json
from collections import deque
from typing import Any, Dict, List, Set, Tuple

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_json
from ..models.step1_models import StepResult
from ..sources.bird_metadata import load_dev_tables, load_foreign_keys, select_databases, sqlite_path
from .base import StepBuilder


class Step1dJoinGraphBuilder(StepBuilder):
    step_name = "step1d_build_join_graph"

    def _run_impl(self) -> StepResult:
        dev_tables = load_dev_tables(self.context.bird_tables_json)
        db_entries = select_databases(dev_tables, db_filter=self.context.db_filter, limit=self.context.limit)
        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        for entry in db_entries:
            db_id = str(entry.get("db_id") or "").strip()
            if not db_id:
                continue
            progress_key = db_id
            if not self.context.force_rerun and progress_key in self.progress.completed_keys():
                skipped_count += 1
                continue

            db_path = sqlite_path(self.context.bird_db_dir, db_id)
            if not db_path.exists():
                raise FileNotFoundError(f"sqlite database not found: {db_path}")

            table_names = [str(name or "").strip() for name in (entry.get("table_names_original") or []) if str(name or "").strip()]
            edges: List[Dict[str, Any]] = []
            adjacency: Dict[str, List[Dict[str, Any]]] = {table_name: [] for table_name in table_names}
            for table_name in table_names:
                for fk in load_foreign_keys(db_path, table_name):
                    target_table = str(fk.get("target_table") or "").strip()
                    source_column = str(fk.get("source_column") or "").strip()
                    target_column = str(fk.get("target_column") or "").strip()
                    if not target_table or not source_column or not target_column:
                        continue
                    expression = f"{db_id}.{table_name}.{source_column} = {db_id}.{target_table}.{target_column}"
                    step = {
                        "source": f"{db_id}.{table_name}@bird",
                        "target": f"{db_id}.{target_table}@bird",
                        "type": "JOIN",
                        "expression": json.dumps([expression], ensure_ascii=False),
                        "intent": "",
                    }
                    edges.append(step)
                    adjacency.setdefault(table_name, []).append({"next_table": target_table, "step": step})
                    reverse_expression = f"{db_id}.{target_table}.{target_column} = {db_id}.{table_name}.{source_column}"
                    reverse_step = {
                        "source": f"{db_id}.{target_table}@bird",
                        "target": f"{db_id}.{table_name}@bird",
                        "type": "JOIN",
                        "expression": json.dumps([reverse_expression], ensure_ascii=False),
                        "intent": "",
                    }
                    adjacency.setdefault(target_table, []).append({"next_table": table_name, "step": reverse_step})

            payload = {
                "db_id": db_id,
                "edges": edges,
                "adjacency": adjacency,
            }
            output_path = self.context.artifacts.join_relation_path(db_id)
            atomic_write_json(output_path, payload)
            outputs.append(str(output_path))
            processed_count += 1
            self.progress.add_completed_key(progress_key)

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )


def resolve_join_paths(payload: Dict[str, Any], db_table1: str, db_table2: str, max_depth: int | None = None) -> List[List[Dict[str, Any]]]:
    db_id_1, table1 = _split_db_table(db_table1)
    db_id_2, table2 = _split_db_table(db_table2)
    if not table1 or not table2 or db_id_1 != db_id_2:
        return []

    if max_depth is None:
        max_depth = int(config.STEP1D_MAX_JOIN_DEPTH)

    adjacency = payload.get("adjacency") or {}
    if table1 not in adjacency or table2 not in adjacency:
        return []

    results: List[List[Dict[str, Any]]] = []
    queue: deque[Tuple[str, List[Dict[str, Any]], Set[str]]] = deque()
    queue.append((table1, [], {table1}))

    while queue:
        current, steps, visited = queue.popleft()
        if len(steps) > max_depth:
            continue
        if current == table2 and steps:
            results.append(steps)
            continue
        for item in adjacency.get(current, []):
            next_table = str(item.get("next_table") or "").strip()
            step = item.get("step") or {}
            if not next_table or next_table in visited:
                continue
            queue.append((next_table, steps + [step], visited | {next_table}))
    return results


def _split_db_table(db_table: str) -> Tuple[str, str]:
    raw = str(db_table or "")
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    parts = raw.split(".", 1)
    if len(parts) != 2:
        return "", ""
    return parts[0].strip(), parts[1].strip()
