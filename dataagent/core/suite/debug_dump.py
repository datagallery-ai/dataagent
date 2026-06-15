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
"""Debug dump of merged configuration to workspace ``.runtime/``."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


def format_settings_yaml(settings: Mapping[str, Any]) -> str:
    """
    Serialize settings with a blank line between each top-level configuration key.

    Args:
        settings: Merged configuration mapping.

    Returns:
        YAML text suitable for writing to ``dataagent_config_*.yaml``.
    """
    parts: list[str] = []
    for key, value in settings.items():
        chunk = yaml.safe_dump(
            {key: value},
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        parts.append(chunk.rstrip("\n"))
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def dump_merged_config(
    settings: Mapping[str, Any],
    *,
    workspace: str | Path,
) -> Path | None:
    """
    Write merged settings to ``<workspace>/.runtime/dataagent_config_<timestamp>.yaml``.

    Called after ``chat()`` / ``astream()`` resolve the effective workspace; not at
    ``ConfigManager.reload()`` time.

    Args:
        settings: Final merged configuration dict.
        workspace: Resolved runtime workspace directory.

    Returns:
        Written file path, or None when ``workspace`` is invalid.
    """
    try:
        workspace_dir = Path(workspace).expanduser().resolve()
    except (TypeError, ValueError) as exc:
        logger.warning("Skipping runtime config dump: invalid workspace: {}", exc)
        return None

    runtime_dir = workspace_dir / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Align with session workspace dir prefix (``DataAgent`` / CLI use UTC ``%Y%m%d_%H%M%S``).
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    target = runtime_dir / f"dataagent_config_{ts}.yaml"
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(format_settings_yaml(settings))
    logger.debug("Wrote runtime configuration dump to {}", target)
    return target
