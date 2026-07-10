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
"""Read/write ``workspace_catalog.json`` and query subagent workspace summaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.agents.galatea.utils.json_store import read_json_object, write_json_object
from dataagent.core.workspace.frontmatter import JobSummary, SubagentWorkspaceEntry, WorkspaceCatalogDoc
from dataagent.utils.constants import INTERNAL_ARTIFACT_PATH_MARKERS
from dataagent.utils.runtime_paths import resolve_job_subagents_root, resolve_jobs_root

METADATA_DIR = ".metadata"
WORKSPACE_CATALOG_FILE = "workspace_catalog.json"
ARTIFACT_SKIP_DIRS = frozenset({".memory", ".context", ".runtime", ".dataagent", METADATA_DIR})


def is_framework_internal_artifact_path(path: str | Path) -> bool:
    """判断路径是否位于 session 框架内部目录（非用户业务产物）。

    用于指代候选过滤等场景：匹配 workspace 内已知的框架子目录标记，
    而非路径任意段名（避免 ``DATAAGENT_HOME=~/.dataagent`` 下用户文件被误过滤）。

    Args:
        path: 待检查的绝对或相对路径。

    Returns:
        属于框架内部路径时返回 True；空路径返回 False。
    """
    raw = str(path or "").strip()
    if not raw:
        return False
    try:
        normalized = str(Path(raw).expanduser().resolve())
    except (OSError, ValueError):
        normalized = raw
    normalized = normalized.replace("\\", "/")
    if not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return any(marker in normalized for marker in INTERNAL_ARTIFACT_PATH_MARKERS)


def catalog_path(root: str | Path) -> Path:
    """Return ``{root}/.metadata/workspace_catalog.json``."""
    return Path(root).expanduser().resolve() / METADATA_DIR / WORKSPACE_CATALOG_FILE


def _utc_now() -> str:
    """Current UTC timestamp in ISO8601 form."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_doc() -> WorkspaceCatalogDoc:
    """Empty catalog with version 1 and no entries."""
    return WorkspaceCatalogDoc(version=1, subagent_workspace={})


def _job_summary_from_dict(raw: Mapping[str, Any]) -> JobSummary | None:
    """Parse one job summary from catalog JSON; skip invalid rows."""
    job_id = str(raw.get("job_id") or "").strip()
    if not job_id:
        return None
    return JobSummary(
        job_id=job_id,
        agent_id=str(raw.get("agent_id") or ""),
        task=str(raw.get("task") or ""),
    )


def _entry_from_dict(raw: Mapping[str, Any]) -> SubagentWorkspaceEntry:
    """Parse one subagent_workspace entry from catalog JSON."""
    jobs_raw = raw.get("jobs")
    jobs: list[JobSummary] = []
    if isinstance(jobs_raw, list):
        for item in jobs_raw:
            if isinstance(item, dict):
                summary = _job_summary_from_dict(item)
                if summary is not None:
                    jobs.append(summary)
    artifacts_raw = raw.get("artifacts")
    artifacts = [str(item) for item in artifacts_raw] if isinstance(artifacts_raw, list) else []
    return SubagentWorkspaceEntry(
        updated_at=str(raw.get("updated_at") or ""),
        artifacts=artifacts,
        jobs=jobs,
    )


def _doc_from_dict(payload: Mapping[str, Any]) -> WorkspaceCatalogDoc:
    """Deserialize workspace_catalog.json payload."""
    subagent_raw = payload.get("subagent_workspace")
    subagent_workspace: dict[str, SubagentWorkspaceEntry] = {}
    if isinstance(subagent_raw, dict):
        for key, value in subagent_raw.items():
            subagent_id = str(key or "").strip()
            if subagent_id and isinstance(value, dict):
                subagent_workspace[subagent_id] = _entry_from_dict(value)
    return WorkspaceCatalogDoc(
        version=int(payload.get("version") or 1),
        session_id=str(payload.get("session_id") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        subagent_workspace=subagent_workspace,
    )


def _doc_to_dict(doc: WorkspaceCatalogDoc) -> dict[str, Any]:
    """Serialize catalog document for JSON persistence."""
    return {
        "version": doc.version,
        "session_id": doc.session_id,
        "updated_at": doc.updated_at,
        "subagent_workspace": {
            subagent_id: {
                "updated_at": entry.updated_at,
                "artifacts": list(entry.artifacts),
                "jobs": [{"job_id": job.job_id, "agent_id": job.agent_id, "task": job.task} for job in entry.jobs],
            }
            for subagent_id, entry in doc.subagent_workspace.items()
        },
    }


def load_catalog(root: str | Path) -> WorkspaceCatalogDoc:
    """Load catalog from disk; return an empty doc when the file is missing."""
    payload = read_json_object(catalog_path(root), {})
    return _doc_from_dict(payload)


def save_catalog(root: str | Path, doc: WorkspaceCatalogDoc) -> None:
    """Persist catalog atomically."""
    write_json_object(catalog_path(root), _doc_to_dict(doc))


def touch_catalog(root: str | Path, session_id: str) -> None:
    """Refresh top-level session metadata without touching subagent entries."""
    doc = load_catalog(root)
    doc.session_id = str(session_id or "").strip()
    doc.updated_at = _utc_now()
    save_catalog(root, doc)


def register_environment(root: str | Path, subagent_id: str) -> None:
    """Create a catalog entry for a new subagent workspace when absent."""
    normalized_id = str(subagent_id or "").strip()
    if not normalized_id:
        return
    doc = load_catalog(root)
    if normalized_id in doc.subagent_workspace:
        return
    now = _utc_now()
    doc.subagent_workspace[normalized_id] = SubagentWorkspaceEntry(
        updated_at=now,
        artifacts=[],
        jobs=[],
    )
    save_catalog(root, doc)


def append_job(
    root: str | Path,
    subagent_id: str,
    *,
    job_id: str,
    agent_id: str,
    task: str,
) -> None:
    """Append one job summary to the subagent entry, deduplicating by ``job_id``."""
    normalized_id = str(subagent_id or "").strip()
    normalized_job_id = str(job_id or "").strip()
    if not normalized_id or not normalized_job_id:
        return
    doc = load_catalog(root)
    entry = doc.subagent_workspace.get(normalized_id)
    if entry is None:
        entry = SubagentWorkspaceEntry(updated_at=_utc_now(), artifacts=[], jobs=[])
        doc.subagent_workspace[normalized_id] = entry
    if any(job.job_id == normalized_job_id for job in entry.jobs):
        entry.updated_at = _utc_now()
        save_catalog(root, doc)
        return
    entry.jobs.append(
        JobSummary(
            job_id=normalized_job_id,
            agent_id=str(agent_id or ""),
            task=str(task or ""),
        )
    )
    entry.updated_at = _utc_now()
    save_catalog(root, doc)


def scan_artifacts(workspace_dir: Path) -> list[str]:
    """List business artifacts at the root of one subagent workspace directory."""
    root = workspace_dir.expanduser().resolve()
    if not root.is_dir():
        return []
    artifacts: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        name = child.name
        if name.startswith("."):
            if child.is_dir() and name in ARTIFACT_SKIP_DIRS:
                continue
            continue
        if child.is_dir():
            artifacts.append(f"{name}/")
        elif child.is_file():
            artifacts.append(name)
    return artifacts


def refresh_artifacts(
    root: str | Path,
    subagent_id: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Rescan the subagent workspace root and write back ``artifacts``."""
    normalized_id = str(subagent_id or "").strip()
    if not normalized_id:
        return
    parent = Path(root).expanduser().resolve()
    subagents_root = resolve_job_subagents_root(parent_workspace=parent, config=config)
    workspace_dir = (subagents_root / normalized_id).resolve()
    artifacts = scan_artifacts(workspace_dir)
    doc = load_catalog(root)
    entry = doc.subagent_workspace.get(normalized_id)
    if entry is None:
        entry = SubagentWorkspaceEntry(updated_at=_utc_now(), artifacts=[], jobs=[])
        doc.subagent_workspace[normalized_id] = entry
    entry.artifacts = artifacts
    entry.updated_at = _utc_now()
    save_catalog(root, doc)


def _derive_workspace_rel_path(
    *,
    root: Path,
    subagent_id: str,
    config: Mapping[str, Any] | None,
) -> str:
    """Compute workspace_rel_path from subagent_id and layout config."""
    subagents_root = resolve_job_subagents_root(parent_workspace=root, config=config)
    workspace_dir = (subagents_root / subagent_id).resolve()
    return workspace_dir.relative_to(root).as_posix()


def _entry_to_list_item(
    *,
    subagent_id: str,
    entry: SubagentWorkspaceEntry,
    root: Path,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build one search_workspaces list item from a catalog entry."""
    return {
        "subagent_id": subagent_id,
        "workspace_rel_path": _derive_workspace_rel_path(root=root, subagent_id=subagent_id, config=config),
        "updated_at": entry.updated_at,
        "artifacts": list(entry.artifacts),
        "jobs": [{"job_id": job.job_id, "agent_id": job.agent_id, "task": job.task} for job in entry.jobs],
    }


def _format_list_markdown(items: list[dict[str, Any]]) -> str:
    """Render catalog entries as markdown for tool original_msg."""
    if not items:
        return "No subagent workspaces indexed in workspace_catalog.json."
    lines = ["# Subagent workspaces", ""]
    for index, item in enumerate(items, start=1):
        lines.append(f"## {index}. `{item['workspace_rel_path']}`")
        lines.append(f"- subagent_id: `{item['subagent_id']}`")
        lines.append(f"- updated_at: {item.get('updated_at') or 'n/a'}")
        artifacts = item.get("artifacts") or []
        if artifacts:
            lines.append(f"- artifacts: {', '.join(artifacts)}")
        else:
            lines.append("- artifacts: (none)")
        jobs = item.get("jobs") or []
        if jobs:
            lines.append("- jobs:")
            for job in jobs:
                lines.append(f"  - {job.get('job_id')}: {job.get('agent_id')} — {job.get('task')}")
        else:
            lines.append("- jobs: (none)")
        lines.append("")
    return "\n".join(lines).rstrip()


def list_environments(
    root: str | Path,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """List all catalog entries sorted by ``updated_at`` descending."""
    parent = Path(root).expanduser().resolve()
    doc = load_catalog(parent)
    items = [
        _entry_to_list_item(subagent_id=subagent_id, entry=entry, root=parent, config=config)
        for subagent_id, entry in doc.subagent_workspace.items()
    ]
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    markdown = _format_list_markdown(items)
    return {
        "subagent_workspace": items,
        "total_subagent_workspace": len(items),
        "original_msg": markdown,
        "frontend_msg": markdown,
    }


def _scan_jobs_detail(
    *,
    root: Path,
    subagent_id: str,
    config: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Collect job.json rows bound to the given subagent_session_id."""
    jobs_root = resolve_jobs_root(parent_workspace=root, config=config)
    if not jobs_root.is_dir():
        return []
    details: list[dict[str, Any]] = []
    for child in sorted(jobs_root.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        payload = read_json_object(child / "job.json", {})
        if not payload:
            continue
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("subagent_session_id") or "").strip() != subagent_id:
            continue
        details.append(
            {
                "job_id": str(payload.get("job_id") or child.name),
                "agent_id": str(payload.get("agent_id") or ""),
                "task": str(payload.get("task") or ""),
                "status": str(payload.get("status") or ""),
            }
        )
    return details


def inspect_environment(
    root: str | Path,
    subagent_id: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return catalog entry, disk artifacts, and filtered job details for one workspace."""
    normalized_id = str(subagent_id or "").strip()
    parent = Path(root).expanduser().resolve()
    doc = load_catalog(parent)
    entry = doc.subagent_workspace.get(normalized_id)
    subagents_root = resolve_job_subagents_root(parent_workspace=parent, config=config)
    workspace_dir = (subagents_root / normalized_id).resolve()
    disk_artifacts = scan_artifacts(workspace_dir) if workspace_dir.is_dir() else []
    workspace_rel_path = (
        _derive_workspace_rel_path(root=parent, subagent_id=normalized_id, config=config)
        if workspace_dir.is_dir()
        else ""
    )
    catalog_payload: dict[str, Any] | None = None
    if entry is not None:
        catalog_payload = {
            "updated_at": entry.updated_at,
            "artifacts": list(entry.artifacts),
            "jobs": [{"job_id": job.job_id, "agent_id": job.agent_id, "task": job.task} for job in entry.jobs],
        }
    jobs_detail = _scan_jobs_detail(root=parent, subagent_id=normalized_id, config=config)
    data = {
        "subagent_id": normalized_id,
        "workspace_rel_path": workspace_rel_path,
        "catalog": catalog_payload,
        "disk": {"artifacts": disk_artifacts},
        "jobs_detail": jobs_detail,
    }
    markdown_lines = [
        f"# Workspace `{workspace_rel_path or normalized_id}`",
        "",
        f"- subagent_id: `{normalized_id}`",
        f"- disk artifacts: {', '.join(disk_artifacts) if disk_artifacts else '(none)'}",
    ]
    if catalog_payload is not None:
        markdown_lines.append(f"- catalog updated_at: {catalog_payload.get('updated_at') or 'n/a'}")
    if jobs_detail:
        markdown_lines.append("- jobs:")
        for job in jobs_detail:
            markdown_lines.append(f"  - {job.get('job_id')}: {job.get('agent_id')} [{job.get('status')}]")
    markdown = "\n".join(markdown_lines)
    return {
        "data": data,
        "original_msg": markdown,
        "frontend_msg": markdown,
    }


def safe_touch_catalog(root: str | Path, session_id: str) -> None:
    """Call ``touch_catalog`` and log failures without raising."""
    try:
        touch_catalog(root, session_id)
    except Exception as exc:
        logger.warning("workspace catalog touch failed: {}", exc)


def safe_register_environment(root: str | Path, subagent_id: str) -> None:
    """register_environment wrapper that logs and swallows errors."""
    try:
        register_environment(root, subagent_id)
    except Exception as exc:
        logger.warning("workspace catalog register_environment failed: {}", exc)


def safe_append_job(
    root: str | Path,
    subagent_id: str,
    *,
    job_id: str,
    agent_id: str,
    task: str,
) -> None:
    """append_job wrapper that logs and swallows errors."""
    try:
        append_job(root, subagent_id, job_id=job_id, agent_id=agent_id, task=task)
    except Exception as exc:
        logger.warning("workspace catalog append_job failed: {}", exc)


def safe_refresh_artifacts(
    root: str | Path,
    subagent_id: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> None:
    """refresh_artifacts wrapper that logs and swallows errors."""
    try:
        refresh_artifacts(root, subagent_id, config=config)
    except Exception as exc:
        logger.warning("workspace catalog refresh_artifacts failed: {}", exc)
