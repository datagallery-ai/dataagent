# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Filesystem-backed job metadata store under ``{parent_ws}/<jobs_dir>/``."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dataagent.agents.galatea.utils.json_store import read_json_object, write_json_object
from dataagent.core.jobs.models import JobResult
from dataagent.utils.runtime_paths import resolve_jobs_root

_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def now_ms() -> int:
    """Return current UTC epoch time in milliseconds."""
    return int(time.time() * 1000)


def _child_dir_sort_key(path: Path) -> str:
    """Return the sort key for one child directory name under the jobs root."""
    return path.name


def _validate_job_id(job_id: str) -> str:
    # Job IDs are used as directory names under jobs_root.
    value = str(job_id or "").strip()
    if not _SAFE_JOB_ID_RE.fullmatch(value):
        raise ValueError("Invalid job_id")
    return value


class FileJobStore:
    """Persist job control-plane artifacts under one parent workspace root."""

    def __init__(self, parent_workspace: Path, *, config: Mapping[str, Any] | None = None) -> None:
        """Bind the store to a resolved parent Agent workspace directory.

        Args:
            parent_workspace: Absolute path to the main Agent workspace root.
            config: Merged agent config for ``WORKSPACE_POLICY.layout.jobs_dir``.
        """
        self.parent_workspace = Path(parent_workspace).expanduser().resolve()
        self.config = config

    def jobs_root(self) -> Path:
        """Return ``{parent_ws}/<jobs_dir>``."""
        return resolve_jobs_root(parent_workspace=self.parent_workspace, config=self.config)

    def job_dir(self, job_id: str) -> Path:
        """Return ``{parent_ws}/<jobs_dir>/{job_id}``."""
        return self.jobs_root() / _validate_job_id(job_id)

    def job_json_path(self, job_id: str) -> Path:
        """Return the ``job.json`` path for one job."""
        return self.job_dir(job_id) / "job.json"

    def events_jsonl_path(self, job_id: str) -> Path:
        """Return the ``events.jsonl`` path for one job."""
        return self.job_dir(job_id) / "events.jsonl"

    def result_json_path(self, job_id: str) -> Path:
        """Return the ``result.json`` path for one job."""
        return self.job_dir(job_id) / "result.json"

    def list_statuses(self) -> list[dict[str, Any]]:
        """List all persisted ``job.json`` payloads under the jobs root."""
        root = self.jobs_root()
        if not root.exists() or not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for child in sorted(root.iterdir(), key=_child_dir_sort_key):
            if not child.is_dir():
                continue
            try:
                payload = self.read_status(child.name)
            except ValueError:
                # Ignore unexpected directories that are not valid job IDs.
                continue
            if payload:
                out.append(payload)
        return out

    def write_status(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge ``patch`` into ``job.json`` and persist atomically."""
        path = self.job_json_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = read_json_object(path, {})
        current.update(patch)
        current.setdefault("job_id", job_id)
        current["updated_at_ms"] = now_ms()
        write_json_object(path, current)
        return current

    def read_status(self, job_id: str) -> dict[str, Any]:
        """Read ``job.json`` for one job id."""
        path = self.job_json_path(job_id)
        if not path.exists():
            return {"job_id": job_id, "status": "not_found"}
        payload = read_json_object(path, {})
        return payload if payload else {"job_id": job_id, "status": "unknown"}

    def append_event(self, job_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append one JSON event line to ``events.jsonl``."""
        target = self.events_jsonl_path(job_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"time_ms": now_ms(), **event}
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return payload

    def read_events(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], str]:
        """Read incremental events starting at ``cursor``."""
        target = self.events_jsonl_path(job_id)
        if not target.exists():
            return [], str(int(cursor or 0))
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            return [], str(int(cursor or 0))
        try:
            offset = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            offset = 0
        next_offset = min(len(lines), offset + max(1, int(limit or 20)))
        out: list[dict[str, Any]] = []
        for line in lines[offset:next_offset]:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                out.append(payload)
        return out, str(next_offset)

    def write_result(self, result: JobResult | dict[str, Any]) -> None:
        """Write ``result.json`` for a terminal job."""
        payload = result.to_dict() if isinstance(result, JobResult) else dict(result)
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise ValueError("job_id is required")
        path = self.result_json_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_object(path, payload)

    def read_result(self, job_id: str) -> dict[str, Any] | None:
        """Read ``result.json`` when present."""
        target = self.result_json_path(job_id)
        if not target.exists():
            return None
        payload = read_json_object(target, {})
        return payload if payload else None
