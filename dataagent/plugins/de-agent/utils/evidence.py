"""Session-local, append-only records of semantic service responses."""

import json
import time
from pathlib import Path
from uuid import uuid4

_DIAGNOSTIC_KEYS = frozenset({"diagnostic", "diagnostics", "tooltrace", "reasoningtrace"})


def save_evidence(context, kind: str, query, payload) -> dict:
    """Persist raw evidence without collapsing equally named tables across databases."""
    folder = context.session.outputs / "metadata"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}_{uuid4().hex}.json"
    record = {"kind": kind, "query": query, "payload": payload, "recorded_at_ns": time.time_ns()}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"data": payload, "evidence_path": str(path)}


def read_evidence(context, *, since_ns: int | None = None) -> list[dict]:
    """Read the current session's complete evidence records in retrieval order."""
    records = []
    for path in (context.session.outputs / "metadata").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        # Filesystems can give consecutive writes identical mtimes; persist the write time.
        timestamp = record["recorded_at_ns"]
        if since_ns is not None and timestamp < since_ns:
            continue
        records.append((timestamp, path.name, record))
    records.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in records]


def retrieval_tables(payload: dict) -> list[dict]:
    """Project coarse retrieval to the legacy table names, descriptions and retention periods."""
    tables = []
    seen = set()
    plan = payload.get("dataAccessPlan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return tables
    for table in plan.get("tables") or []:
        if not isinstance(table, dict):
            continue
        database, name = table.get("db"), table.get("table")
        if not database or not name or (database, name) in seen:
            continue
        seen.add((database, name))
        tables.append(
            {
                "table": f"{database}.{name}",
                "description": table.get("description", ""),
                "dataRetentionPeriodDays": table.get("dataRetentionPeriodDays"),
            }
        )
    return tables


def model_evidence(context) -> list[dict]:
    """Return deduplicated business evidence, keeping complete raw responses only on disk."""
    records = []
    seen = set()
    # Keep the latest occurrence: A -> B -> A must still leave A as the latest schema.
    for record in reversed(read_evidence(context)):
        payload = record.get("payload")
        if record.get("kind") == "retrieve":
            payload = {"tables": retrieval_tables(payload)}
        projected = {
            "kind": record.get("kind"),
            "query": record.get("query"),
            "payload": _without_diagnostics(payload),
        }
        key = json.dumps(projected, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            records.append(projected)
    return list(reversed(records))


class ArtifactPathError(ValueError):
    """An artifact path escapes the current session's outputs directory."""


def output_file(context, value: str) -> Path:
    """Resolve one artifact under the current session's output directory."""
    root = context.session.outputs.resolve()
    candidate = Path(value)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not path.is_relative_to(root):
        raise ArtifactPathError(
            f"Artifact must be inside this session's outputs directory: {root}. "
            f"Received: {value}; resolved: {path}. Use a path under {root}."
        )
    return path


def _without_diagnostics(value):
    if isinstance(value, dict):
        return {
            key: _without_diagnostics(item)
            for key, item in value.items()
            if key.replace("_", "").casefold() not in _DIAGNOSTIC_KEYS
        }
    if isinstance(value, list):
        return [_without_diagnostics(item) for item in value]
    return value
