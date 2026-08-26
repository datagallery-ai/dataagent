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
"""Write oversized tool results to workspace files and return an inline preview to the model.

Adapted from Galatea's truncator hook; uses Ferry's ToolHookInvocation and writes to
``workspace_dir/tool-results/``.
"""

from __future__ import annotations

from loguru import logger

from dataagent.actions.tools.hooks.base import (
    ToolHookInvocation,
    ToolPostHookOutcome,
)

DEFAULT_INLINE_THRESHOLD_BYTES = 32768
DEFAULT_PREVIEW_BYTES = 2000


async def truncator(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Write oversized tool results to a workspace file and replace the inline content with a preview.

    When a tool returns more than ``DEFAULT_INLINE_THRESHOLD_BYTES`` of output the
    hook persists the full payload to ``<workspace>/tool-results/<call_id>.txt`` and
    replaces ``inv.execution.output_text`` with a short header, relative file path,
    and a truncated preview.

    Args:
        inv: Per-call hook context; ``execution`` is expected to be non-None and
            ``success``-ful.

    Returns:
        Empty outcome.

    Raises:
        OSError: When the workspace file cannot be written; propagated as a hook failure
            by the Executor.
    """
    if inv.execution is None or not inv.execution.success:
        return ToolPostHookOutcome()

    payload = _result_to_bytes(inv.execution.output_text)
    if len(payload) <= DEFAULT_INLINE_THRESHOLD_BYTES:
        return ToolPostHookOutcome()

    runtime = inv.runtime
    if runtime is None or runtime.workspace_dir is None:
        return ToolPostHookOutcome()

    output_dir = runtime.workspace_dir / "tool-results"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{inv.tool_call_id or inv.tool_name}.txt"
    target_path = output_dir / filename
    target_path.write_bytes(payload)
    relative = target_path.relative_to(runtime.workspace_dir).as_posix()

    preview = _build_preview(payload, DEFAULT_PREVIEW_BYTES)
    inv.execution.output_text = _format_truncated_message(len(payload), relative, preview)
    inv.execution.original_msg = None

    logger.debug(
        "[truncator] tool={} call_id={} size={} saved_to={}",
        inv.tool_name,
        inv.tool_call_id,
        len(payload),
        relative,
    )
    return ToolPostHookOutcome()


def _result_to_bytes(result: str) -> bytes:
    """Encode the tool result text to UTF-8 bytes safely."""
    if not result:
        return b""
    return result.encode("utf-8", errors="replace")


def _build_preview(payload: bytes, preview_bytes: int) -> str:
    """Return a human-readable preview of the payload up to *preview_bytes*."""
    if len(payload) <= preview_bytes:
        return payload.decode("utf-8", errors="replace")
    snippet = payload[:preview_bytes]
    newline_index = snippet.rfind(b"\n")
    if newline_index > 0:
        snippet = snippet[:newline_index]
    return snippet.decode("utf-8", errors="replace")


def _format_truncated_message(total_bytes: int, relative_path: str, preview: str) -> str:
    """Build the inline message shown to the model after truncation."""
    return (
        "The original tool result exceeds the inline size limit and has been replaced with a preview.\n"
        f"Full tool result file: {relative_path}\n"
        "If the full tool result is required, read the saved file in smaller sections.\n"
        "Tool result preview:\n"
        f"{preview}"
    )
