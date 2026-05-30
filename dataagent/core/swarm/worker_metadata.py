# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""负责 subagent worker 的 metadata 的读存"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataagent.core.swarm.worker_io import atomic_write_json
from dataagent.utils.constants import MAX_WORKER_METADATA_ARTIFACTS
from dataagent.utils.runtime_paths import resolve_session_root, resolve_worker_memory_dir


@dataclass
class WorkerMetadata:
    """Lightweight asset card for one persisted subagent worker.

    ``metadata.json`` is the discovery surface used by the main planner. It keeps
    identity, status, last query / last answer, artifacts, and error information while
    leaving full history in ``messages.json``. Artifact paths accumulate across
    invocations up to ``MAX_WORKER_METADATA_ARTIFACTS``.

    Field ``last_run_id`` stores the Flex ``run_id`` used by the **last completed**
    subprocess ``chat()`` for this worker (assigned by the parent, not read back
    from ``subagent_state.json``). Busy paths skip metadata updates entirely.
    """

    sub_id: int
    user_id: str
    parent_session_id: str
    worker_session_id: str
    config_path: str
    agent_name: str
    status: str
    created_at: str
    last_invoked_at: str
    last_run_id: int
    last_query: str
    last_answer: str
    artifacts: list[str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert the metadata record to a JSON-serializable dictionary."""
        return asdict(self)


def load_worker_metadata(*, user_id: str, parent_session_id: str, sub_id: int) -> WorkerMetadata | None:
    """Load a single worker's ``metadata.json`` if it exists and is valid."""
    path = (
        resolve_worker_memory_dir(user_id=user_id, parent_session_id=parent_session_id, sub_id=sub_id) / "metadata.json"
    )
    return _read_metadata_file(path)


def compute_next_worker_run_id(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    reuse_worker_state: bool,
) -> int:
    """Return the Flex ``run_id`` seed for the next subprocess ``chat()`` invocation.

    Fresh worker folders (including reuse-miss reassignments) always start at
    ``0``. When ``reuse_worker_state`` is true, the next id is
    ``metadata.last_run_id + 1`` (or ``0`` if metadata is missing).
    """
    if not reuse_worker_state:
        return 0
    existing = load_worker_metadata(user_id=user_id, parent_session_id=parent_session_id, sub_id=sub_id)
    if existing is None:
        return 0
    return int(existing.last_run_id) + 1


def upsert_worker_metadata(
    *,
    user_id: str,
    parent_session_id: str,
    worker_session_id: str,
    sub_id: int,
    config_path: str,
    query: str,
    worker_result: Any | None,
    status: str,
    last_run_id_executed: int,
    error: str | None = None,
) -> WorkerMetadata:
    """Create or update one worker metadata asset.

    The parent process calls this after success, failure, or timeout for a run
    that actually started (busy skips metadata entirely). ``last_run_id_executed``
    must match the ``run_id`` injected into the child's initial state for that run.
    """
    memory_dir = resolve_worker_memory_dir(user_id=user_id, parent_session_id=parent_session_id, sub_id=sub_id)
    path = memory_dir / "metadata.json"
    now = _now()
    existing = _read_metadata_file(path)
    result = _as_dict(worker_result)
    prior_artifacts = list(existing.artifacts) if existing else []
    incoming_artifacts = [str(item) for item in result.get("artifacts") or []]
    merged_artifacts = _merge_worker_metadata_artifacts(prior_artifacts, incoming_artifacts)
    metadata = WorkerMetadata(
        sub_id=int(sub_id),
        user_id=user_id,
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id,
        config_path=config_path,
        agent_name=Path(config_path).stem,
        status=status,
        created_at=existing.created_at if existing else now,
        last_invoked_at=now,
        last_run_id=int(last_run_id_executed),
        last_query=query,
        last_answer=str(result.get("final_answer") or ""),
        artifacts=merged_artifacts,
        error=error if error is not None else result.get("error"),
    )
    atomic_write_json(path, metadata.to_dict())
    return metadata


def list_worker_metadata(*, user_id: str, parent_session_id: str) -> list[WorkerMetadata]:
    """Discover current-session workers by scanning ``workers/*/.memory/metadata.json``."""
    workers_dir = resolve_session_root(user_id=user_id, session_id=parent_session_id) / "workers"
    if not workers_dir.exists():
        return []
    items: list[WorkerMetadata] = []
    for path in workers_dir.glob("*/.memory/metadata.json"):
        metadata = _read_metadata_file(path)
        if metadata is not None:
            items.append(metadata)
    items.sort(key=lambda item: item.last_invoked_at, reverse=True)
    return items


def build_worker_metadata_context(
    *,
    user_id: str,
    parent_session_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Build planner-safe metadata context for system prompt injection.

    Only lightweight fields are returned (per design): ``sub_id``, ``last_query``,
    ``last_answer``, ``artifacts``, ``error``. Records are sorted by most recent
    invocation and truncated by ``limit`` to avoid prompt growth.
    """
    records: list[dict[str, Any]] = []
    for item in list_worker_metadata(user_id=user_id, parent_session_id=parent_session_id)[: int(limit)]:
        records.append(
            {
                "sub_id": item.sub_id,
                "last_query": item.last_query,
                "last_answer": item.last_answer,
                "artifacts": item.artifacts,
                "error": item.error,
            }
        )
    return records


def _merge_worker_metadata_artifacts(prior: list[str], incoming: list[str]) -> list[str]:
    """Merge artifact paths for ``metadata.json``: dedupe in encounter order, cap length.

    Paths already listed in ``prior`` keep their relative order; new paths from
    ``incoming`` append when not yet seen. If the merged list exceeds
    ``MAX_WORKER_METADATA_ARTIFACTS``, trailing entries are kept (newer paths tend
    to appear later after repeated merges).
    """
    merged: list[str] = []
    seen: set[str] = set()
    for raw in prior + incoming:
        path = str(raw).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        merged.append(path)
    limit = int(MAX_WORKER_METADATA_ARTIFACTS)
    if limit > 0 and len(merged) > limit:
        return merged[-limit:]
    return merged


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalize dict-like worker result objects for metadata extraction."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {}


def _read_metadata_file(path: Path) -> WorkerMetadata | None:
    """Parse one metadata file, returning ``None`` for missing or invalid data."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        legacy_last_run = payload.get("last_run_id")
        if legacy_last_run is None and "total_invocations" in payload:
            legacy_last_run = max(0, int(payload.get("total_invocations", 0) or 0) - 1)
        return WorkerMetadata(
            sub_id=int(payload["sub_id"]),
            user_id=str(payload["user_id"]),
            parent_session_id=str(payload["parent_session_id"]),
            worker_session_id=str(payload["worker_session_id"]),
            config_path=str(payload.get("config_path") or ""),
            agent_name=str(payload.get("agent_name") or ""),
            status=str(payload.get("status") or ""),
            created_at=str(payload.get("created_at") or ""),
            last_invoked_at=str(payload.get("last_invoked_at") or ""),
            last_run_id=int(legacy_last_run or 0),
            last_query=str(payload.get("last_query") or ""),
            last_answer=str(payload.get("last_answer") or payload.get("last_summary") or ""),
            artifacts=[str(item) for item in payload.get("artifacts") or []],
            error=None if payload.get("error") is None else str(payload.get("error")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()
