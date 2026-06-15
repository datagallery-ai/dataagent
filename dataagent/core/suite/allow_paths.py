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
"""Effective workspace allow-path resolution for Suite subagent access."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dataagent.core.managers.action_manager.manager import ToolManager


def effective_workspace_allow_paths(
    settings: Mapping[str, Any],
    activated_suites: Sequence[Mapping[str, str]] | None = None,
) -> list[str]:
    """
    Return read-only allow roots for ``sub_agent_tool`` and subagent path checks.

    Combines explicit ``WORKSPACE.allow_path`` from user YAML with each activated
    Suite ``root`` (injection granularity: ``suite.root`` only). Does not mutate
    ``settings`` and is not shown in planner ``allow_path_lines``.

    Args:
        settings: Merged Agent configuration (``ConfigManager.settings``).
        activated_suites: ``ConfigManager.activated_suites`` metadata list.

    Returns:
        De-duplicated absolute path strings (user entries first, then suite roots).
    """
    paths: list[str] = list(ToolManager.workspace_allow_path_list(settings))
    seen: set[str] = set()
    for item in paths:
        try:
            seen.add(str(Path(str(item)).expanduser().resolve()))
        except (OSError, ValueError):
            seen.add(str(item).strip())

    for entry in activated_suites or ():
        if not isinstance(entry, Mapping):
            continue
        raw_root = entry.get("root")
        if raw_root is None or not str(raw_root).strip():
            continue
        try:
            resolved = str(Path(str(raw_root)).expanduser().resolve())
        except (OSError, ValueError):
            resolved = str(raw_root).strip()
        if resolved in seen:
            continue
        paths.append(resolved)
        seen.add(resolved)
    return paths
