from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def now_beijing_iso() -> str:
    """Return an ISO-8601 timestamp in the Beijing timezone."""
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


WORKFLOW_TYPE = "data_analysis"
TOP_STATUS_RUNNING = "running"
TOP_STATUS_SILENT = "silent"
STEP_STATUS_PENDING = "pending"
STEP_STATUS_READY = "ready"
STEP_STATUS_IN_PROGRESS = "in_progress"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_COMPLETED = "completed"
STEP_STATUS_SILENT = "silent"
MAX_STEP_RETRIES = 3


def active_workflow_path(workspace_dir: Path) -> Path:
    """Return the path to the active workflow pointer file in a workspace."""
    return Path(workspace_dir) / ".metadata" / "active_workflow.json"


def workflow_status_path(workspace_dir: Path, run_id: str) -> Path:
    """Return the path to a workflow run's ``workflow_status.json`` file."""
    return workflow_dir(workspace_dir, run_id) / "workflow_status.json"


def workflow_events_path(workspace_dir: Path, run_id: str) -> Path:
    """Return the path to a workflow run's ``events.jsonl`` audit log."""
    return workflow_dir(workspace_dir, run_id) / "events.jsonl"


def workflow_dir(workspace_dir: Path, run_id: str) -> Path:
    """Return the metadata directory for a specific workflow run."""
    return Path(workspace_dir) / ".metadata" / "workflows" / str(run_id)


class DataAnalysisWorkflowController:
    def __init__(self, workspace_dir: Path | str):
        self.workspace_dir = Path(workspace_dir)

    def create_workflow(
        self,
        *,
        user_query: str,
        input_refs: list[str],
        steps: list[dict[str, Any]],
        run_id: str = "",
        shared_input_publication_id: str = "",
    ) -> dict[str, Any]:
        """Initialize, persist, and activate a new data analysis workflow."""
        clean_steps = _validate_steps(steps)
        clean_input_refs = _validate_input_refs(input_refs)
        workflow_run_id = str(run_id or f"data_analysis_{uuid.uuid4().hex[:12]}").strip()
        if not workflow_run_id:
            raise ValueError("run_id is required")
        created_at = now_beijing_iso()
        workflow_steps = []
        for index, step_def in enumerate(clean_steps):
            workflow_steps.append(
                {
                    "id": step_def["id"],
                    "status": STEP_STATUS_READY if index == 0 else STEP_STATUS_PENDING,
                    "target": step_def["target"],
                    "target_version": 1,
                    "owner_type": "subagent",
                    "owner_id": step_def["owner_id"],
                    "job_id": "",
                    "receipt": _empty_receipt(),
                    "created_at": created_at,
                    "updated_at": created_at,
                    "started_at": "",
                    "completed_at": "",
                    "failed_at": "",
                    "failure_phase": "",
                    "failure_reason": "",
                    "silenced_at": "",
                    "target_updated_at": "",
                    "target_update_reason": "",
                    "retry_count": 0,
                    "last_retry_at": "",
                    "last_retry_reason": "",
                }
            )
        status = {
            "schema_version": 1,
            "workflow_type": WORKFLOW_TYPE,
            "run_id": workflow_run_id,
            "status": TOP_STATUS_RUNNING,
            "user_query": str(user_query or "").strip(),
            "input_refs": clean_input_refs,
            "shared_input_publication_id": str(shared_input_publication_id or "").strip(),
            "current_step_id": clean_steps[0]["id"],
            "created_at": created_at,
            "updated_at": created_at,
            "silenced_at": "",
            "silence_reason": "",
            "steps": workflow_steps,
        }
        self._save_status(status)
        self._write_active_pointer(status)
        self._append_event(workflow_run_id, "workflow_created", {"current_step_id": clean_steps[0]["id"]})
        return deepcopy(status)

    def load_active_running_workflow(self) -> dict[str, Any] | None:
        """Load the currently active running data analysis workflow, if one exists."""
        pointer = _read_json(active_workflow_path(self.workspace_dir))
        if not isinstance(pointer, dict):
            return None
        if str(pointer.get("workflow_type") or "") != WORKFLOW_TYPE:
            return None
        run_id = str(pointer.get("run_id") or "").strip()
        if not run_id:
            return None
        status = self.load_workflow(run_id)
        if not status or str(status.get("status") or "") != TOP_STATUS_RUNNING:
            return None
        return status

    def load_workflow(self, run_id: str) -> dict[str, Any] | None:
        """Load workflow status for ``run_id``, or return ``None`` if missing or invalid."""
        payload = _read_json(workflow_status_path(self.workspace_dir, run_id))
        if not isinstance(payload, dict):
            return None
        if str(payload.get("workflow_type") or "") != WORKFLOW_TYPE:
            return None
        return self._with_runtime_defaults(payload)

    def mark_current_step_submitted(self, job_id: str) -> dict[str, Any]:
        """Mark the current ready step as in progress with the given subagent job id."""
        clean_job_id = str(job_id or "").strip()
        if not clean_job_id:
            raise ValueError("job_id is required")
        status = self._require_active_running()
        step = _current_step(status)
        if step["status"] != STEP_STATUS_READY:
            raise ValueError(f"current step `{step['id']}` is not ready")
        now = now_beijing_iso()
        step["status"] = STEP_STATUS_IN_PROGRESS
        step["job_id"] = clean_job_id
        _clear_step_failure(step)
        step["started_at"] = now
        step["updated_at"] = now
        status["updated_at"] = now
        self._save_status(status)
        self._append_event(status["run_id"], "step_submitted", {"step_id": step["id"], "job_id": clean_job_id})
        return deepcopy(status)

    def mark_current_step_failed(
        self,
        *,
        job_id: str,
        phase: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record a failure for the current in-progress step and optionally silence the workflow."""
        clean_job_id = str(job_id or "").strip()
        clean_phase = str(phase or "").strip()
        clean_reason = str(reason or "").strip()
        if not clean_job_id:
            raise ValueError("job_id is required")
        if not clean_phase:
            raise ValueError("failure phase is required")
        if not clean_reason:
            raise ValueError("failure reason is required")
        status = self._require_active_running()
        step = _current_step(status)
        if step["status"] != STEP_STATUS_IN_PROGRESS:
            raise ValueError(f"current step `{step['id']}` is not in progress")
        if str(step.get("job_id") or "").strip() != clean_job_id:
            raise ValueError("job_id does not match current step")

        now = now_beijing_iso()
        step["failure_phase"] = clean_phase
        step["failure_reason"] = clean_reason
        step["failed_at"] = now
        step["updated_at"] = now
        failure_payload = {
            "step_id": step["id"],
            "job_id": clean_job_id,
            "failure_phase": clean_phase,
            "failure_reason": clean_reason,
        }
        exhausted = int(step.get("retry_count") or 0) >= MAX_STEP_RETRIES
        if exhausted:
            step["status"] = STEP_STATUS_SILENT
            step["silenced_at"] = now
            status["status"] = TOP_STATUS_SILENT
            status["current_step_id"] = ""
            status["silenced_at"] = now
            status["silence_reason"] = "retry_limit_exhausted"
            silence_payload = {
                "reason": "retry_limit_exhausted",
                **failure_payload,
            }
        else:
            step["status"] = STEP_STATUS_FAILED

        status["updated_at"] = now
        self._save_status(status)
        if exhausted:
            self._clear_active_pointer(status["run_id"])
        self._append_event(status["run_id"], "step_failed", failure_payload)
        if exhausted:
            self._append_event(status["run_id"], "workflow_silenced", silence_payload)
        return deepcopy(status)

    def complete_current_step(
        self,
        job_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete the current step with a receipt and advance to the next step or finish."""
        clean_job_id = str(job_id or "").strip()
        if not clean_job_id:
            raise ValueError("job_id is required")
        clean_receipt = validate_receipt(receipt)
        status = self._require_active_running()
        step = _current_step(status)
        if step["status"] != STEP_STATUS_IN_PROGRESS:
            raise ValueError(f"current step `{step['id']}` is not in progress")
        if str(step.get("job_id") or "").strip() != clean_job_id:
            raise ValueError("job_id does not match current step")

        now = now_beijing_iso()
        step["status"] = STEP_STATUS_COMPLETED
        step["receipt"] = clean_receipt
        step["completed_at"] = now
        step["updated_at"] = now

        next_step = _next_step(status, step["id"])
        if next_step is None:
            status["status"] = TOP_STATUS_SILENT
            status["current_step_id"] = ""
            status["silenced_at"] = now
            status["silence_reason"] = "workflow_completed"
            self._clear_active_pointer(status["run_id"])
            event_type = "workflow_completed"
        else:
            next_step["status"] = STEP_STATUS_READY
            next_step["updated_at"] = now
            status["current_step_id"] = next_step["id"]
            event_type = "step_completed"

        status["updated_at"] = now
        self._save_status(status)
        self._append_event(
            status["run_id"],
            event_type,
            {"step_id": step["id"], "job_id": clean_job_id, "receipt": clean_receipt},
        )
        return deepcopy(status)

    def update_step_target(self, step_id: str, target: str, reason: str) -> dict[str, Any]:
        """Update a step's target before it starts or after a non-terminal failure."""
        clean_step_id = str(step_id or "").strip()
        clean_target = str(target or "").strip()
        clean_reason = str(reason or "").strip()
        if not clean_target:
            raise ValueError("target is required")
        if not clean_reason:
            raise ValueError("reason is required")
        status = self._require_active_running()
        step = _step_by_id(status, clean_step_id)
        if step["status"] in {STEP_STATUS_IN_PROGRESS, STEP_STATUS_COMPLETED}:
            raise ValueError(f"cannot update target for {step['status']} step `{clean_step_id}`")

        now = now_beijing_iso()
        step["target"] = clean_target
        step["target_version"] = int(step.get("target_version") or 1) + 1
        step["target_updated_at"] = now
        step["target_update_reason"] = clean_reason
        step["updated_at"] = now
        status["updated_at"] = now
        self._save_status(status)
        self._append_event(
            status["run_id"],
            "step_target_updated",
            {"step_id": clean_step_id, "target_version": step["target_version"], "reason": clean_reason},
        )
        return deepcopy(status)

    def retry_current_step(self, reason: str) -> dict[str, Any]:
        """Reset a failed current step to ready so it can be resubmitted."""
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("reason is required")
        status = self._require_active_running()
        step = _current_step(status)
        if step["status"] != STEP_STATUS_FAILED:
            raise ValueError(f"current step `{step['id']}` is not failed")
        if int(step.get("retry_count") or 0) >= MAX_STEP_RETRIES:
            raise ValueError(f"current step `{step['id']}` has exhausted its retry limit")

        previous_job_id = str(step.get("job_id") or "").strip()
        now = now_beijing_iso()
        step["status"] = STEP_STATUS_READY
        step["job_id"] = ""
        step["receipt"] = _empty_receipt()
        step["started_at"] = ""
        step["completed_at"] = ""
        _clear_step_failure(step)
        step["updated_at"] = now
        step["silenced_at"] = ""
        step["retry_count"] = int(step.get("retry_count") or 0) + 1
        step["last_retry_at"] = now
        step["last_retry_reason"] = clean_reason
        status["status"] = TOP_STATUS_RUNNING
        status["current_step_id"] = step["id"]
        status["silenced_at"] = ""
        status["silence_reason"] = ""
        status["updated_at"] = now
        self._save_status(status)
        self._write_active_pointer(status)
        self._append_event(
            status["run_id"],
            "step_retry_requested",
            {"step_id": step["id"], "previous_job_id": previous_job_id, "reason": clean_reason},
        )
        return deepcopy(status)

    def silence_workflow(self, reason: str) -> dict[str, Any]:
        """Manually silence the active workflow and clear the active pointer."""
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("reason is required")
        status = self._require_active_running()
        now = now_beijing_iso()
        status["status"] = TOP_STATUS_SILENT
        status["silenced_at"] = now
        status["silence_reason"] = clean_reason
        current_step_id = str(status.get("current_step_id") or "").strip()
        if current_step_id:
            step = _step_by_id(status, current_step_id)
            if step["status"] != STEP_STATUS_COMPLETED:
                step["status"] = STEP_STATUS_SILENT
                step["silenced_at"] = now
                step["updated_at"] = now
        status["current_step_id"] = ""
        status["updated_at"] = now
        self._save_status(status)
        self._clear_active_pointer(status["run_id"])
        self._append_event(status["run_id"], "workflow_silenced", {"reason": clean_reason})
        return deepcopy(status)

    def _require_active_running(self) -> dict[str, Any]:
        status = self.load_active_running_workflow()
        if status is None:
            raise ValueError("no running data analysis workflow")
        return status

    def _save_status(self, status: dict[str, Any]) -> None:
        path = workflow_status_path(self.workspace_dir, str(status.get("run_id") or ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_active_pointer(self, status: dict[str, Any]) -> None:
        path = active_workflow_path(self.workspace_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "workflow_type": WORKFLOW_TYPE,
            "run_id": str(status.get("run_id") or ""),
            "state_path": workflow_status_path(self.workspace_dir, str(status.get("run_id") or "")).as_posix(),
            "updated_at": now_beijing_iso(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _clear_active_pointer(self, run_id: str) -> None:
        path = active_workflow_path(self.workspace_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "workflow_type": WORKFLOW_TYPE,
            "run_id": "",
            "previous_run_id": str(run_id or ""),
            "state_path": "",
            "updated_at": now_beijing_iso(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        path = workflow_events_path(self.workspace_dir, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schema_version": 1,
            "workflow_type": WORKFLOW_TYPE,
            "run_id": run_id,
            "type": event_type,
            "created_at": now_beijing_iso(),
            "payload": payload if isinstance(payload, dict) else {},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _with_runtime_defaults(self, status: dict[str, Any]) -> dict[str, Any]:
        status.setdefault("shared_input_publication_id", "")
        steps = status.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    step["receipt"] = _coerce_step_receipt(step)
                    step.setdefault("failed_at", "")
                    step.setdefault("failure_phase", "")
                    step.setdefault("failure_reason", "")
                    step.setdefault("retry_count", 0)
        return status


def _validate_steps(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            raise ValueError(f"step at index {index} must be a dict")
        step_id = str(item.get("id") or "").strip()
        owner_id = str(item.get("owner_id") or "").strip()
        target = str(item.get("target") or "").strip()
        if not step_id:
            raise ValueError(f"step at index {index} requires id")
        if step_id in seen:
            raise ValueError(f"duplicate step_id: {step_id}")
        if not owner_id:
            raise ValueError(f"owner_id for step `{step_id}` is required")
        if not target:
            raise ValueError(f"target for step `{step_id}` is required")
        clean.append({"id": step_id, "owner_id": owner_id, "target": target})
        seen.add(step_id)
    if not clean:
        raise ValueError("steps are required")
    return clean


def _validate_input_refs(input_refs: list[str]) -> list[str]:
    if not isinstance(input_refs, list):
        raise ValueError("input_refs must be a list")
    clean: list[str] = []
    seen: set[str] = set()
    for item in input_refs:
        ref = str(item or "").strip()
        if not ref or ref in seen:
            continue
        clean.append(ref)
        seen.add(ref)
    if not clean:
        raise ValueError("input_refs is required")
    return clean


def _empty_receipt() -> dict[str, Any]:
    return {"summary": "", "artifacts": []}


def validate_receipt(value: Any) -> dict[str, Any]:
    """Validate and normalize a step completion receipt with summary and artifacts."""
    if not isinstance(value, dict):
        raise ValueError("receipt must be a dict")
    summary = value.get("summary")
    if not isinstance(summary, str):
        raise ValueError("receipt.summary must be a string")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("receipt.artifacts must be a list")
    if not artifacts:
        raise ValueError("receipt.artifacts must contain at least one artifact")
    clean_artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"receipt.artifacts[{index}] must be a dict")
        kind = str(artifact.get("kind") or "file").strip().lower()
        if kind == "clickhouse_table":
            uri = str(artifact.get("uri") or "").strip()
            name = str(artifact.get("name") or "").strip()
            database, table = _clickhouse_table_from_uri(uri)
            canonical_name = f"{database}.{table}"
            if name and name != canonical_name:
                raise ValueError(f"receipt.artifacts[{index}].name must match ClickHouse URI table `{canonical_name}`")
            if uri in seen:
                continue
            clean_artifacts.append({"kind": "clickhouse_table", "uri": uri, "name": canonical_name})
            seen.add(uri)
            continue
        if kind != "file":
            raise ValueError(f"receipt.artifacts[{index}].kind is unsupported: {kind}")
        path = str(artifact.get("path") or "").strip()
        if not path:
            raise ValueError(f"receipt.artifacts[{index}].path is required")
        artifact_type = str(artifact.get("type") or "").strip() or _artifact_type_from_path(path)
        if path in seen:
            continue
        clean_artifacts.append({"path": path, "type": artifact_type})
        seen.add(path)
    return {"summary": summary.strip(), "artifacts": clean_artifacts}


def _clickhouse_table_from_uri(uri: str) -> tuple[str, str]:
    prefix = "clickhouse://"
    value = str(uri or "").strip()
    if not value.startswith(prefix):
        raise ValueError("ClickHouse table artifact uri must use clickhouse://<database>/<table>")
    remainder = value[len(prefix) :]
    if any(marker in remainder for marker in ("?", "#", "@")):
        raise ValueError("ClickHouse table artifact uri must not contain query, fragment, or credentials")
    parts = remainder.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("ClickHouse table artifact uri must use clickhouse://<database>/<table>")
    database, table = parts
    if not _is_clickhouse_identifier(database) or not _is_clickhouse_identifier(table):
        raise ValueError("ClickHouse table artifact uri contains an invalid database or table name")
    return database, table


def _is_clickhouse_identifier(value: str) -> bool:
    return bool(value) and value.replace("_", "a").isalnum() and not value[0].isdigit()


def _clear_step_failure(step: dict[str, Any]) -> None:
    step["failed_at"] = ""
    step["failure_phase"] = ""
    step["failure_reason"] = ""


def _coerce_step_receipt(step: dict[str, Any]) -> dict[str, Any]:
    receipt = step.get("receipt")
    if isinstance(receipt, dict):
        try:
            return validate_receipt(receipt)
        except ValueError:
            return _empty_receipt()
    summary = str(step.get("receipt_ref") or "").strip()
    artifacts = []
    for path in _string_list(step.get("output_paths")):
        artifacts.append({"path": path, "type": _artifact_type_from_path(path)})
    return {"summary": summary, "artifacts": artifacts}


def _artifact_type_from_path(path: str) -> str:
    suffix = Path(str(path or "")).suffix.lower().lstrip(".")
    return suffix or "file"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    clean: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        clean.append(text)
        seen.add(text)
    return clean


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _current_step(status: dict[str, Any]) -> dict[str, Any]:
    step_id = str(status.get("current_step_id") or "").strip()
    if not step_id:
        raise ValueError("workflow has no current step")
    return _step_by_id(status, step_id)


def _step_by_id(status: dict[str, Any], step_id: str) -> dict[str, Any]:
    steps = status.get("steps")
    if not isinstance(steps, list):
        raise ValueError("workflow steps are missing")
    for step in steps:
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise ValueError(f"workflow step `{step_id}` not found")


def _next_step(status: dict[str, Any], step_id: str) -> dict[str, Any] | None:
    steps = status.get("steps")
    if not isinstance(steps, list):
        raise ValueError("workflow steps are missing")
    for index, step in enumerate(steps):
        if isinstance(step, dict) and step.get("id") == step_id:
            for candidate in steps[index + 1 :]:
                if isinstance(candidate, dict):
                    return candidate
            return None
    raise ValueError(f"workflow step `{step_id}` not found")
