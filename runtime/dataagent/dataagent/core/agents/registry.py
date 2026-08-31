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
"""Resolve ``SUBAGENT_CONFIGS`` entries to stable ``agent_id`` values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import yaml

from dataagent.core.agents.subagent_config import load_subagent_catalog_metadata, resolve_subagent_config_path


@dataclass(frozen=True)
class AgentSpec:
    """One registered subagent specialist."""

    id: str
    name: str
    description: str
    config_path: Path


@dataclass(frozen=True)
class AgentResolution:
    """Result of resolving a caller-provided ``agent_id`` string."""

    spec: AgentSpec | None
    agent_id: str
    matched_by: str = ""
    suggestions: tuple[str, ...] = ()


class AgentRegistry:
    """Map ``agent_id`` to subagent yaml paths from ``SUBAGENT_CONFIGS``."""

    def __init__(self, specs: list[AgentSpec] | None = None) -> None:
        """Create a registry from an optional pre-built spec list."""
        self._specs: dict[str, AgentSpec] = {}
        for spec in specs or []:
            self.register(spec)

    @classmethod
    def from_subagent_configs(cls, entries: Sequence[Any]) -> AgentRegistry:
        """Build a registry from merged ``SUBAGENT_CONFIGS`` entries."""
        registry = cls()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("SUBAGENT_CONFIGS items must be mappings with 'path'")
            path = resolve_subagent_config_path(entry.get("path"))
            payload = _load_yaml_mapping(path)
            agent_id = resolve_agent_id_from_yaml(path, payload)
            name, description = load_subagent_catalog_metadata(path)
            registry.register(
                AgentSpec(
                    id=agent_id,
                    name=name,
                    description=description,
                    config_path=path,
                )
            )
        return registry

    def register(self, spec: AgentSpec) -> None:
        """Register one specialist spec."""
        if not spec.id:
            raise ValueError("agent id is required")
        self._specs[spec.id] = spec

    def get(self, agent_id: str) -> AgentSpec | None:
        """Return a spec by exact id, if present."""
        return self._specs.get(str(agent_id or "").strip())

    def resolve(self, agent_id: str) -> AgentResolution:
        """Resolve an ``agent_id`` with case-insensitive and fuzzy fallback."""
        raw = str(agent_id or "").strip()
        if not raw:
            return AgentResolution(None, "", suggestions=tuple(self._agent_ids()))
        exact = self.get(raw)
        if exact is not None:
            return AgentResolution(exact, exact.id, matched_by="id")

        lowered = raw.lower()
        id_matches = [spec for spec in self._specs.values() if spec.id.lower() == lowered]
        if len(id_matches) == 1:
            spec = id_matches[0]
            return AgentResolution(spec, spec.id, matched_by="id_case_insensitive")

        name_matches = [spec for spec in self._specs.values() if spec.name.lower() == lowered]
        if len(name_matches) == 1:
            spec = name_matches[0]
            return AgentResolution(spec, spec.id, matched_by="name")

        candidates = self._agent_ids()
        suggestions = tuple(get_close_matches(raw, candidates, n=3, cutoff=0.45))
        return AgentResolution(None, raw, suggestions=suggestions or tuple(candidates[:5]))

    def list(self) -> list[AgentSpec]:
        """Return all registered specs sorted by id."""
        return [self._specs[key] for key in sorted(self._specs)]

    def _agent_ids(self) -> list[str]:
        return sorted(str(spec.id) for spec in self._specs.values() if str(spec.id).strip())


def resolve_agent_id_from_yaml(path: Path, payload: Mapping[str, Any]) -> str:
    """Resolve ``agent_id`` using PRD precedence rules."""
    agent_cfg = payload.get("AGENT_CONFIG")
    if isinstance(agent_cfg, Mapping):
        for key in ("id",):
            value = agent_cfg.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    top_level_id = payload.get("id")
    if top_level_id is not None and str(top_level_id).strip():
        return str(top_level_id).strip()
    return path.stem


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"SUBAGENT_CONFIGS yaml root must be a mapping: {path}")
    return dict(payload)
