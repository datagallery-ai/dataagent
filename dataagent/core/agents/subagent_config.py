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
"""Shared helpers for resolving ``SUBAGENT_CONFIGS`` yaml entries."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def resolve_subagent_config_path(raw_path: Any) -> Path:
    """Resolve and validate one ``SUBAGENT_CONFIGS`` entry path (absolute only).

    Args:
        raw_path: Path string from a ``SUBAGENT_CONFIGS`` mapping entry.

    Returns:
        Expanded absolute path to the subagent yaml file.

    Raises:
        ValueError: When the path is empty or relative.
    """
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("SUBAGENT_CONFIGS entry requires non-empty 'path'")
    path = Path(str(raw_path).strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"SUBAGENT_CONFIGS path must be absolute (or ~/...); relative paths are not allowed: {raw_path!r}"
        )
    return path


def load_subagent_catalog_metadata(path: Path) -> tuple[str, str]:
    """Load ``AGENT_CONFIG.name`` and ``description`` from a subagent yaml file.

    Args:
        path: Absolute path to the subagent yaml config.

    Returns:
        Tuple of ``(name, description)`` for tool catalog rendering.

    Raises:
        FileNotFoundError: When the yaml file is missing.
        ValueError: When required yaml sections or fields are absent.
    """
    if not path.is_file():
        raise FileNotFoundError(f"SUBAGENT_CONFIGS path does not exist or is not a file: {path}")
    with open(path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"SUBAGENT_CONFIGS yaml root must be a mapping: {path}")
    agent_cfg = payload.get("AGENT_CONFIG")
    if not isinstance(agent_cfg, Mapping):
        raise ValueError(f"SUBAGENT_CONFIGS yaml must contain AGENT_CONFIG section: {path}")
    name = str(agent_cfg.get("name") or "").strip()
    description = str(agent_cfg.get("description") or "").strip()
    if not name:
        raise ValueError(f"SUBAGENT_CONFIGS yaml missing AGENT_CONFIG.name: {path}")
    if not description:
        raise ValueError(f"SUBAGENT_CONFIGS yaml missing AGENT_CONFIG.description: {path}")
    return name, description
