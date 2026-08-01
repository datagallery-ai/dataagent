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
"""Scenario instruction helpers for Flex agents (HITL surface removed)."""

from __future__ import annotations

from typing import Any


def resolve_scenario_instructions(config: dict[str, Any] | None, mode: str = "chat") -> str:
    """Resolve ``SCENARIO.{mode}.instructions`` without HITL condition append.

    Args:
        config: Merged Flex YAML configuration dict.
        mode: Scenario key (default ``chat``).

    Returns:
        Final instructions text injected into :class:`~dataagent.core.cbb.agent_env.Env`.
    """
    instructions = ""
    if not isinstance(config, dict):
        return instructions

    scenario = config.get("SCENARIO") or {}
    if isinstance(scenario, dict):
        if mode and isinstance(scenario.get(mode), dict):
            instructions = str(scenario[mode].get("instructions", "") or "").strip()
        if not instructions:
            for scenario_cfg in scenario.values():
                if isinstance(scenario_cfg, dict) and scenario_cfg.get("instructions"):
                    instructions = str(scenario_cfg["instructions"]).strip()
                    break
    return instructions
