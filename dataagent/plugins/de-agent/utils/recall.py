"""Preload semantic discovery once per original query and session."""

import hashlib
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4
from weakref import WeakValueDictionary

from utils.evidence import retrieval_tables, save_evidence
from utils.feedback import filter_candidates

_LOCKS = WeakValueDictionary()
_LOCKS_GUARD = Lock()


def load_semantic_context(context, query: str) -> dict:
    """Reuse successful query results; serialize concurrent misses without caching failures."""
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    path = context.session.outputs / ".semantic" / f"{digest}.json"
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path), Lock())
    with lock:
        cached = _read_cache(path, query)
        if cached is not None:
            return {**cached, "tables": filter_candidates(context, cached["tables"])}
        client = context.semantic_client
        if client is None:
            raise ValueError("de-agent requires the Agent's semantic configuration")
        payload = client.semantic_retrieve(query)
        saved = save_evidence(context, "retrieve", query, payload)
        tables = filter_candidates(context, retrieval_tables(payload))
        if not tables:
            raise ValueError("Semantic retrieval returned no usable table candidates")
        result = {"query": query, "tables": tables, "evidence_path": saved.get("evidence_path")}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return result


def _read_cache(path, query):
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict) or cached.get("query") != query or not cached.get("tables"):
        return None
    evidence_path = cached.get("evidence_path")
    if not isinstance(evidence_path, str) or not Path(evidence_path).is_file():
        return None
    return cached
