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
"""Plugin business parameters: validation plus dotted-key access.

The section is the Agent configuration's ``plugins.config.nl2sql`` mapping
(JSON); host-level concerns (models, workspaces) live in the host
configuration and are handed to the plugin by the compiled-subagent factory.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nl2sql.constants import DEFAULT_NL2SQL_MODEL_CALL_BUDGET

_LEGACY_SECTIONS = frozenset({
    "MODEL", "CORE", "DATABASE", "SEMANTIC_LAYER", "WORKSPACE", "AGENT_CONFIG", "SESSION_ID",
})
"""Legacy YAML top-level sections; their presence means an unmigrated config."""

_NODE_ORDER = ("perceptor", "generator", "validator", "reflector", "executor", "selector")

_KNOWN_KEYS = frozenset({"model", "model_call_budget", "result_files", "database", "semantic", *_NODE_ORDER})

_CONNECTION_RESERVED = frozenset({"db_id", "dialect", "engine"})

_MIGRATION_HINT = (
    "nl2sql configuration has moved into the Agent configuration: put business parameters "
    "under plugins.config.nl2sql (model = a models alias from the same configuration, "
    "database = {db_id, dialect, engine, ...connection keys}, semantic = semantic-service "
    "credentials, plus optional perceptor/generator/validator/reflector/executor/selector "
    "sections); YAML files are no longer read"
)


class NL2SQLConfig:
    """Validated ``plugins.config.nl2sql`` parameters with dotted-key access."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        raw = dict(data or {})
        legacy = sorted(_LEGACY_SECTIONS & set(raw))
        if legacy:
            raise ValueError(f"legacy section(s) {legacy}: {_MIGRATION_HINT}")
        unknown = sorted(set(raw) - _KNOWN_KEYS)
        if unknown:
            raise ValueError(f"unknown nl2sql parameter(s) {unknown}; known keys: {sorted(_KNOWN_KEYS)}")

        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("nl2sql requires 'model': a model alias from the Agent configuration")

        budget = raw.get("model_call_budget", DEFAULT_NL2SQL_MODEL_CALL_BUDGET)
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ValueError(f"model_call_budget must be a positive integer, got {budget!r}")

        if not isinstance(raw.get("result_files", True), bool):
            raise ValueError(f"result_files must be true or false, got {raw['result_files']!r}")

        database = raw.get("database")
        if not isinstance(database, Mapping):
            raise ValueError("nl2sql requires 'database': {db_id, dialect, engine, ...connection keys}")
        dialect = database.get("dialect")
        if not isinstance(dialect, str) or not dialect.strip():
            raise ValueError("database.dialect is required (e.g. sqlite, mysql, gaussvector)")
        for key in ("db_id", "engine"):
            if key in database and not isinstance(database[key], str):
                raise ValueError(f"database.{key} must be a string, got {database[key]!r}")

        for section in ("semantic", *_NODE_ORDER):
            if section in raw and not isinstance(raw[section], Mapping):
                raise ValueError(f"nl2sql parameter {section!r} must be a mapping, got {raw[section]!r}")

        self._data: dict[str, Any] = raw

    @property
    def model_call_budget(self) -> int:
        """The per-task model-call budget (validated positive integer)."""
        return int(self._data.get("model_call_budget", DEFAULT_NL2SQL_MODEL_CALL_BUDGET))

    @property
    def result_files(self) -> bool:
        """Whether each executed query writes its fetched rows to a session result file."""
        return bool(self._data.get("result_files", True))

    @property
    def enabled_nodes(self) -> list[str]:
        """Node chain enabled by the present node sections (prefix of the canonical order).

        With no node sections configured the full six-node chain runs; otherwise
        the present sections must form a prefix and include the generator.
        """
        present = [name for name in _NODE_ORDER if name in self._data]
        if not present:
            return list(_NODE_ORDER)
        if present != list(_NODE_ORDER[: len(present)]):
            raise ValueError(
                f"enabled nodes {present} must be a prefix of {_NODE_ORDER}; "
                "disable a node by removing it and everything after it"
            )
        if "generator" not in present:
            raise ValueError("nl2sql requires at least the perceptor and generator nodes")
        return present

    def connection_config(self) -> dict[str, Any]:
        """Database connection kwargs: the database section minus its reserved keys."""
        return {key: value for key, value in self.section("database").items() if key not in _CONNECTION_RESERVED}

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value at a dotted key path, or ``default`` when missing."""
        current: Any = self._data
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current

    def section(self, name: str) -> Mapping[str, Any]:
        """Return one top-level configuration section as a mapping."""
        value = self._data.get(name)
        return value if isinstance(value, Mapping) else {}

    def as_dict(self) -> dict[str, Any]:
        """Return the underlying configuration dict (shallow copy)."""
        return dict(self._data)

    @classmethod
    def load(cls, path: str | Path) -> NL2SQLConfig:
        """Load parameters from a JSON file (YAML is no longer supported)."""
        file = Path(path)
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"nl2sql config must be a mapping: {file}")
        return cls(data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NL2SQLConfig(sections={sorted(self._data)})"
