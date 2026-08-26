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
"""Parse tool hook callables from YAML ``TOOLS`` entries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class ToolHookLists:
    """Resolved pre/post hook callables for a tool or MCP/A2A server/agent."""

    pre: list[Callable[..., Any]] = field(default_factory=list)
    post: list[Callable[..., Any]] = field(default_factory=list)


def load_tool_hooks_from_config(hooks_cfg: Any) -> ToolHookLists:
    """Load hook callables from a ``hooks: {pre: [...], post: [...]}`` mapping.

    Args:
        hooks_cfg: YAML ``hooks`` dict or ``None``.

    Returns:
        ``ToolHookLists`` with importable callables; invalid entries are skipped with a warning.
    """
    if not isinstance(hooks_cfg, dict):
        return ToolHookLists()

    from dataagent.utils.import_utils import import_callable_from_spec

    result = ToolHookLists()
    for phase, target in (("pre", result.pre), ("post", result.post)):
        raw_list = hooks_cfg.get(phase)
        if not raw_list:
            continue
        if not isinstance(raw_list, list):
            logger.warning(f"[tool_hooks] hooks.{phase} must be a list, got {type(raw_list).__name__}")
            continue
        for item in raw_list:
            spec = str(item or "").strip()
            if not spec:
                continue
            try:
                target.append(import_callable_from_spec(spec))
            except Exception as e:
                logger.warning(f"[tool_hooks] Failed to load hook {spec!r} for phase {phase}: {e}")
    return result


def attach_hooks_to_tool(tool: Any, hook_lists: ToolHookLists) -> None:
    """Attach resolved hook lists to a ``BaseTool`` instance.

    Args:
        tool: Tool wrapper instance.
        hook_lists: Parsed pre/post callables.
    """
    tool.pre_hooks = list(hook_lists.pre)
    tool.post_hooks = list(hook_lists.post)
