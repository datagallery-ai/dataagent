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
"""Job envelope build/finalize helpers for submit lifecycle tools (P0.5)."""

from __future__ import annotations

from typing import Any

INTERNAL_JOB_ENVELOPE_KEY = "_job_envelope"
SUBMIT_SUBAGENT_TOOL = "submit_subagent"
SUBMIT_RESOURCE_JOB_TOOL = "submit_resource_job"
SUBMIT_JOB_TOOLS = frozenset({SUBMIT_SUBAGENT_TOOL, SUBMIT_RESOURCE_JOB_TOOL})

_COMMON_PROTECTED_FIELDS = frozenset({"kind", "timeout_sec", "parent_tool_call_id"})
_RESOURCE_PROTECTED_FIELDS = _COMMON_PROTECTED_FIELDS | {
    "type",
    "resource_id",
    "command",
    "sandbox_request",
    "out_kind",
    "inputs",
    "outputs",
    "receipt_ids",
    "script_artifact",
}
_PROTECTED_FIELDS = {
    SUBMIT_SUBAGENT_TOOL: _COMMON_PROTECTED_FIELDS | {"agent_id", "task", "workspace_rel_path"},
    SUBMIT_RESOURCE_JOB_TOOL: _RESOURCE_PROTECTED_FIELDS,
}

DEFAULT_RESOURCE_JOB_TIMEOUT_SEC = 3600


def build_base_job_envelope(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    *,
    parent_tool_call_id: str = "",
) -> dict[str, Any] | None:
    """Build the core-owned baseline envelope from LLM-visible submit tool arguments.

    Args:
        tool_name: Registered submit tool name.
        tool_args: Validated LLM-visible arguments for one tool call.
        parent_tool_call_id: Optional parent tool call id from the executor.

    Returns:
        Baseline envelope dict, or ``None`` when ``tool_name`` is not a submit tool.
    """
    name = str(tool_name or "").strip()
    args = tool_args if isinstance(tool_args, dict) else {}
    parent_id = str(parent_tool_call_id or "").strip()
    if name == SUBMIT_SUBAGENT_TOOL:
        envelope: dict[str, Any] = {
            "kind": "agent",
            "agent_id": str(args.get("agent_id") or "").strip(),
            "task": str(args.get("task") or "").strip(),
            "timeout_sec": _positive_int(args.get("timeout_sec"), 600),
        }
        workspace_rel_path = _workspace_rel_path_from_args(args)
        if workspace_rel_path:
            envelope["workspace_rel_path"] = workspace_rel_path
        if parent_id:
            envelope["parent_tool_call_id"] = parent_id
        return _normalize_subagent_workspace_fields(envelope)

    if name == SUBMIT_RESOURCE_JOB_TOOL:
        envelope = {
            "kind": "resource",
            "type": str(args.get("task_type") or "resource").strip() or "resource",
            "timeout_sec": _positive_int(args.get("timeout_sec"), DEFAULT_RESOURCE_JOB_TIMEOUT_SEC),
        }
        sandbox_request = _dict(args.get("sandbox_request"))
        if sandbox_request:
            # Preserve mandatory sandbox defaults even when callers provide overrides.
            sandbox_overrides = {}
            for key, value in sandbox_request.items():
                if key not in {"enabled", "backend"}:
                    sandbox_overrides[key] = value
            envelope["sandbox_request"] = {
                "enabled": True,
                "backend": "best-effort",
                **sandbox_overrides,
            }
        _set_non_empty_string(envelope, "resource_id", args.get("resource_id"))
        _set_non_empty_string(envelope, "command", args.get("command"))
        _set_non_empty_string(envelope, "out_kind", args.get("out_kind"))
        _set_dict_list(envelope, "inputs", args.get("inputs"))
        _set_dict_list(envelope, "outputs", args.get("outputs"))
        _set_string_list(envelope, "receipt_ids", args.get("receipt_ids"))
        script_artifact = _dict(args.get("script_artifact"))
        if script_artifact:
            envelope["script_artifact"] = script_artifact
        if parent_id:
            envelope["parent_tool_call_id"] = parent_id
        return envelope

    return None


def finalize_job_envelope(
    tool_name: str,
    base_envelope: dict[str, Any],
    candidate_envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge plugin candidate fields and validate protected core ownership.

    Args:
        tool_name: Registered submit tool name.
        base_envelope: Core-built baseline envelope.
        candidate_envelope: Injector/plugin candidate envelope.

    Returns:
        Final envelope with empty optional fields removed.

    Raises:
        ValueError: When protected fields are overwritten or required fields are missing.
    """
    name = str(tool_name or "").strip()
    if name not in SUBMIT_JOB_TOOLS:
        raise ValueError(f"Unsupported submit tool for job envelope: {name or '<empty>'}")
    base = (
        _normalize_subagent_workspace_fields(dict(base_envelope or {}))
        if name == SUBMIT_SUBAGENT_TOOL
        else dict(base_envelope or {})
    )
    candidate_raw = dict(candidate_envelope) if isinstance(candidate_envelope, dict) else dict(base)
    candidate = (
        _normalize_subagent_workspace_fields(candidate_raw) if name == SUBMIT_SUBAGENT_TOOL else dict(candidate_raw)
    )
    for field in _PROTECTED_FIELDS[name]:
        if field in base and field in candidate and candidate.get(field) != base[field]:
            raise ValueError(f"Job envelope field is owned by core and cannot be overwritten: {field}")

    merged = _merge_final_job_envelope(name, base, candidate)

    if name == SUBMIT_SUBAGENT_TOOL:
        if merged.get("kind") != "agent":
            raise ValueError("Subagent job envelope kind must be agent")
        if not str(merged.get("agent_id") or "").strip():
            raise ValueError("Subagent job envelope requires agent_id")
        if not str(merged.get("task") or "").strip():
            raise ValueError("Subagent job envelope requires task")
    else:
        if merged.get("kind") != "resource":
            raise ValueError("Resource job envelope kind must be resource")
        if not str(merged.get("type") or "").strip():
            raise ValueError("Resource job envelope requires type")

    return _drop_empty_optional_fields(merged)


def _merge_final_job_envelope(
    tool_name: str,
    base_envelope: dict[str, Any],
    candidate_envelope: dict[str, Any],
) -> dict[str, Any]:
    """Merge plugin extras onto the core baseline while preserving protected fields."""
    protected = _PROTECTED_FIELDS[tool_name]
    merged = dict(candidate_envelope)
    for field in protected:
        if field in base_envelope:
            merged[field] = base_envelope[field]
    return merged


def envelope_from_tool_context(context: Any) -> dict[str, Any]:
    """Read a finalized job envelope from tool execution context.

    Args:
        context: :class:`~dataagent.actions.tools.context.ToolExecutionContext`
            or a plain dict with optional ``job_envelope`` key.

    Returns:
        Envelope dict, or empty dict when unavailable.
    """
    raw = context.get("job_envelope") if isinstance(context, dict) else getattr(context, "job_envelope", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _workspace_rel_path_from_args(args: dict[str, Any]) -> str:
    """Resolve Ferry ``workspace_rel_path`` from args, accepting Galatea ``workspace_dir`` alias."""
    rel = str(args.get("workspace_rel_path") or "").strip()
    if rel:
        return rel
    return str(args.get("workspace_dir") or "").strip()


def _normalize_subagent_workspace_fields(envelope: dict[str, Any]) -> dict[str, Any]:
    """Normalize Galatea ``workspace_dir`` into Ferry ``workspace_rel_path``."""
    normalized = dict(envelope)
    rel = str(normalized.get("workspace_rel_path") or "").strip()
    legacy = str(normalized.get("workspace_dir") or "").strip()
    normalized.pop("workspace_dir", None)
    if not rel and legacy:
        rel = legacy
    if rel:
        normalized["workspace_rel_path"] = rel
    else:
        normalized.pop("workspace_rel_path", None)
    return normalized


def _drop_empty_optional_fields(envelope: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional envelope entries while preserving numeric zeros."""
    return {key: value for key, value in envelope.items() if value not in (None, "", [], {})}


def _positive_int(value: Any, default: int) -> int:
    """Parse a positive integer with fallback."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _dict(value: Any) -> dict[str, Any]:
    """Coerce a mapping value."""
    return dict(value) if isinstance(value, dict) else {}


def _set_non_empty_string(target: dict[str, Any], key: str, value: Any) -> None:
    """Set a string field when the normalized value is non-empty."""
    normalized = str(value or "").strip()
    if normalized:
        target[key] = normalized


def _set_dict_list(target: dict[str, Any], key: str, value: Any) -> None:
    """Set a list-of-dicts field when non-empty."""
    normalized = [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if normalized:
        target[key] = normalized


def _set_string_list(target: dict[str, Any], key: str, value: Any) -> None:
    """Set a list-of-strings field when non-empty."""
    normalized = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
    if normalized:
        target[key] = normalized
