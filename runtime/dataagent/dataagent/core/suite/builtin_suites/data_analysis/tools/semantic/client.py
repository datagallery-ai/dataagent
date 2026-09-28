from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.utils.messages_utils import write_result_to_workspace
from dataagent.utils.runtime_paths import resolve_layout_dir

DEFAULT_BASE_URL = "http://localhost:31000/api/semantic"
DEFAULT_TIMEOUT_SEC = 180.0


def resolve_base_url() -> str:
    """Resolve the semantic service base URL from environment or the default."""
    return str(os.environ.get("SEMANTIC_SERVICE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def resolve_timeout_sec() -> float:
    """Resolve the semantic service HTTP timeout in seconds from environment or the default."""
    raw = str(os.environ.get("SEMANTIC_SERVICE_TIMEOUT_SEC") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SEC
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def semantic_retrieve(
    query: str,
    *,
    base_url: str | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Call semantic-service unified retrieval and return a SemanticBundle dict."""
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query is required")

    timeout = resolve_timeout_sec() if timeout_sec is None else max(1.0, float(timeout_sec))
    client = SemanticServiceClient(base_url or resolve_base_url(), timeout=timeout)
    try:
        payload = client.semantic_retrieve(normalized_query)
    finally:
        client.client.close()
    if isinstance(payload, dict):
        return payload
    from dataagent.core.errors import DataAgentError

    raise DataAgentError(
        source="tool",
        component="semantic-service",
        fact="语义服务返回非对象 payload",
    )


def _current_workspace() -> Path:
    guard = get_current_sandbox()
    workspace = guard.workspace_root
    if workspace is None:
        raise RuntimeError("workspace is required to write semantic download")
    return Path(workspace)


def _runtime_config() -> dict[str, Any] | None:
    try:
        from dataagent.core.framework_adapters.runtime.context import get_current_runtime

        runtime = get_current_runtime()
    except Exception:
        return None
    getter = getattr(runtime, "get_all_config", None) if runtime is not None else None
    if not callable(getter):
        return None
    config = getter()
    return config if isinstance(config, dict) else None


def semantic_download(scene_name: str) -> dict[str, Any]:
    """Download ontology JSON and persist it like other long tool results.

    Writes through ``write_result_to_workspace`` to
    ``{workspace}/.dataagent/tool_outputs/semantic_download_<timestamp>.txt``
    (same layout as Executor IR dumps for ``semantic_retrieve``). Returns a
    short receipt only so the model does not copy the document body.

    Args:
        scene_name: Scene name passed to GET ontology/define/json/download.
    """
    normalized_scene = str(scene_name or "").strip()
    if not normalized_scene:
        raise ValueError("scene_name is required")
    workspace = _current_workspace()

    timeout = resolve_timeout_sec()
    client = SemanticServiceClient(resolve_base_url(), timeout=timeout)
    try:
        payload = client.semantic_download(normalized_scene)
    finally:
        client.client.close()
    if not isinstance(payload, dict):
        from dataagent.core.errors import DataAgentError

        raise DataAgentError(
            source="tool",
            component="semantic-service",
            fact="语义服务 download 返回非对象 payload",
        )
    code = payload.get("code")
    if code is not None and str(code) not in {"200", "0"}:
        from dataagent.core.errors import DataAgentError

        raise DataAgentError(
            source="tool",
            component="semantic-service",
            fact=str(payload.get("message") or payload.get("msg") or f"download failed code={code}"),
        )
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    encoded = json.dumps(body, ensure_ascii=False)
    config = _runtime_config()
    output_dir = resolve_layout_dir(workspace, "tool_outputs_dir", config=config)
    get_current_sandbox().authorize_write(
        output_dir / "semantic_download.txt",
        operation="semantic_download",
    )
    target = write_result_to_workspace(
        encoded + "\n",
        "semantic_download",
        workspace,
        config=config,
    )
    size = target.stat().st_size
    receipt = {
        "ok": True,
        "scene_name": normalized_scene,
        "path": str(target),
        "bytes": size,
        "top_level_keys": list(body.keys()),
    }
    return {
        "original_msg": json.dumps(receipt, ensure_ascii=False),
        "frontend_msg": f"semantic_download wrote {target} ({size} bytes)",
        "data": receipt,
    }
