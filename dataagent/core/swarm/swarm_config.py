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
"""Central helpers for YAML ``SWARM`` subtree flags used by Flex subagents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _swarm_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the SWARM mapping from an explicit config dict."""
    if not config:
        return {}
    swarm = config.get("SWARM")
    return swarm if isinstance(swarm, Mapping) else {}


def swarm_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Return whether swarm-mode persistence and planner worker assets are active.

    When the ``SWARM.enable`` key is absent from merged YAML, this defaults to
    ``False`` so existing deployments keep subagent disk reuse unless explicitly
    disabled.

    Args:
        config: Per-Agent merged configuration dict (e.g. from ``runtime.get_all_config()``).
    """
    swarm = _swarm_section(config)
    if "enable" not in swarm:
        return False
    return bool(swarm.get("enable"))


def swarm_worker_max_concurrent(config: Mapping[str, Any] | None = None) -> int | None:
    """Return the max parallel ``sub_agent_tool`` calls per executor round, or ``None`` for no cap.

    If ``SWARM.worker_max_concurrent`` is **not** set in merged YAML, returns ``None`` so the
    executor does not apply an extra subagent-specific concurrency ceiling (other limits,
    e.g. general tool concurrency, still apply). When set, it must parse as a positive
    integer; ``0``, negative values, or non-integers are treated as ``None`` (no cap).

    Args:
        config: Per-Agent merged configuration dict (e.g. from ``runtime.get_all_config()``).
    """
    raw = _swarm_section(config).get("worker_max_concurrent")
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n
