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

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataagent.utils.constants import MERGED_CONFIG_TOP_LEVEL_KEY_ORDER
from dataagent.utils.runtime_paths import resolve_layout_dir

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)
_REDACTED = "<redacted>"


def _redact_sensitive_values(value: Any, *, key: str = "") -> Any:
    # Runtime dumps must not persist secrets from merged config.
    if key and _SENSITIVE_KEY_RE.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_sensitive_values(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_values(item) for item in value)
    return value


def _write_private_text(target: Path, text: str) -> None:
    # Debug config dumps are written owner-only by default.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    os.chmod(target, 0o600)


def _iter_top_level_keys_for_display(settings: Mapping[str, Any]) -> list[str]:
    """
    Return top-level keys for YAML output: preferred order first, then remaining keys.

    Args:
        settings: Merged configuration mapping.

    Returns:
        Ordered key list for :func:`format_settings_yaml`.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for key in MERGED_CONFIG_TOP_LEVEL_KEY_ORDER:
        if key in settings:
            ordered.append(key)
            seen.add(key)
    for key in settings:
        if key not in seen:
            ordered.append(key)
    return ordered


def format_settings_yaml(settings: Mapping[str, Any]) -> str:
    """
    Serialize settings with a blank line between each top-level configuration key.

    Top-level sections are emitted in :data:`MERGED_CONFIG_TOP_LEVEL_KEY_ORDER` when present;
    any other keys keep their original order from ``settings``.

    Args:
        settings: Merged configuration mapping.

    Returns:
        YAML text suitable for writing to ``dataagent_config_*.yaml``.
    """
    safe_settings = _redact_sensitive_values(settings)
    parts: list[str] = []
    for key in _iter_top_level_keys_for_display(safe_settings):
        chunk = yaml.safe_dump(
            {key: safe_settings[key]},
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

    runtime_dir = resolve_layout_dir(workspace_dir, "runtime_dump_dir", config=settings)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Align with session workspace dir prefix (``DataAgent`` / CLI use UTC ``%Y%m%d_%H%M%S``).
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    target = runtime_dir / f"dataagent_config_{ts}.yaml"
    _write_private_text(target, format_settings_yaml(settings))
    logger.debug("Wrote runtime configuration dump to {}", target)
    return target


def write_merged_config_to_dir(
    settings: Mapping[str, Any],
    *,
    output_dir: str | Path,
) -> Path:
    """
    Write merged settings to ``<output_dir>/dataagent_config_<timestamp>.yaml``.

    Args:
        settings: Final merged configuration dict.
        output_dir: Target directory; created when missing.

    Returns:
        Written file path.

    Raises:
        ValueError: ``output_dir`` exists but is not a directory.
        OSError: Directory creation or file write failed.
    """
    directory = Path(output_dir).expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"--config_output must be a directory, got file: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    target = directory / f"dataagent_config_{ts}.yaml"
    _write_private_text(target, format_settings_yaml(settings))
    return target
