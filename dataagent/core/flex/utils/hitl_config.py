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
"""Human-in-the-loop (HITL) configuration helpers for Flex agents."""

from __future__ import annotations

from typing import Any

from loguru import logger

from dataagent.utils.constants import HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX


def normalize_human_feedback_conditions(raw: Any) -> list[str]:
    """Normalize ``human_feedback_conditions`` from YAML to a list of non-empty strings.

    Supports:
    - ``list[str]``: each item becomes one condition (stripped; empty items dropped).
    - ``str``: treated as a single condition paragraph (not split on commas).

    Args:
        raw: Value from YAML ``SCENARIO.{mode}.human_feedback_conditions``.

    Returns:
        Normalized condition strings; empty when ``raw`` is missing or invalid.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    logger.warning(
        "human_feedback_conditions must be a list or string; got {} — ignoring",
        type(raw).__name__,
    )
    return []


def resolve_human_feedback_conditions(config: dict[str, Any] | None, mode: str = "chat") -> list[str]:
    """Read ``human_feedback_conditions`` from ``SCENARIO.{mode}``.

    Args:
        config: Merged Flex YAML configuration dict.
        mode: Scenario key (default ``chat``).

    Returns:
        Normalized condition list; empty when unset or invalid.
    """
    if not isinstance(config, dict):
        return []
    scenario = config.get("SCENARIO") or {}
    if not isinstance(scenario, dict):
        return []
    scenario_mode = scenario.get(mode) if mode else None
    if not isinstance(scenario_mode, dict):
        for value in scenario.values():
            if isinstance(value, dict) and value.get("human_feedback_conditions") is not None:
                return normalize_human_feedback_conditions(value.get("human_feedback_conditions"))
        return []
    return normalize_human_feedback_conditions(scenario_mode.get("human_feedback_conditions"))


def format_human_feedback_conditions_block(conditions: list[str]) -> str:
    """Format scenario HITL conditions as instructions text for the Planner.

    Args:
        conditions: Normalized ``human_feedback_conditions`` entries.

    Returns:
        Instruction block appended to ``SCENARIO.{mode}.instructions`` when HITL is enabled.
    """
    return "\n".join(f"{condition}，{HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX}" for condition in conditions)


def append_human_feedback_conditions_to_instructions(instructions: str, conditions: list[str]) -> str:
    """Append formatted HITL conditions to scenario instructions.

    Args:
        instructions: Base ``SCENARIO.{mode}.instructions`` text.
        conditions: Normalized ``human_feedback_conditions`` entries.

    Returns:
        Combined instructions text; unchanged when ``conditions`` is empty.
    """
    if not conditions:
        return instructions
    block = format_human_feedback_conditions_block(conditions)
    base = str(instructions or "").strip()
    if base:
        return f"{base}\n\n{block}"
    return block


def is_human_feedback_enabled(config: dict[str, Any] | None) -> bool:
    """Return whether HITL infrastructure should be active for this agent config.

    ``enable_human_feedback`` is the global HITL switch. Scenario-level
    ``human_feedback_conditions`` only append guidance after this switch is enabled.

    Args:
        config: Merged Flex YAML configuration dict.

    Returns:
        True when HITL tool registration and ``HumanFeedbackNode`` should be enabled.
    """
    if not isinstance(config, dict):
        return False
    if config.get("enable_human_feedback") is True:
        return True
    agent_cfg = config.get("AGENT_CONFIG") or {}
    return isinstance(agent_cfg, dict) and agent_cfg.get("enable_human_feedback") is True


def resolve_scenario_instructions(config: dict[str, Any] | None, mode: str = "chat") -> str:
    """Resolve ``SCENARIO.{mode}.instructions`` and append HITL conditions when enabled.

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

    if is_human_feedback_enabled(config):
        conditions = resolve_human_feedback_conditions(config, mode=mode)
        instructions = append_human_feedback_conditions_to_instructions(instructions, conditions)
    return instructions
