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
(YAML); host-level concerns (models, workspaces) live in the host
configuration and are handed to the plugin by the compiled-subagent factory.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dataagent import read_yaml
from nl2sql.constants import DEFAULT_NL2SQL_MODEL_CALL_BUDGET, DEFAULT_NL2SQL_PARSE_RETRIES

_NODE_ORDER = ("perceptor", "generator", "validator", "reflector", "executor", "selector")

_BIRD_NODE_ORDER = ("perceptor", "generator", "bird-validator", "selector")

_KNOWN_KEYS = frozenset(
    {
        "model",
        "model_call_budget",
        "emit_reasoning_tag",
        "result_files",
        "database",
        "nodes",
        "scenario",
        *_NODE_ORDER,
        "bird-validator",
    }
)

_CONNECTION_RESERVED = frozenset({"db_id", "dialect", "engine"})


def _validate_bird_options(options: Mapping[str, Any]) -> None:
    # Node modules import NL2SQLConfig during package initialization.
    from nl2sql.nodes.checkers.checkers import AVAILABLE_CHECKERS, DEFAULT_CHECKERS

    unknown = set(options) - {
        "checkers",
        "repair_attempts",
        "preview_limit",
        "sql_security_enabled",
    }
    if unknown:
        raise ValueError(f"unknown bird-validator options: {sorted(unknown)}")
    checkers = options.get("checkers", DEFAULT_CHECKERS)
    if not isinstance(checkers, (list, tuple)):
        raise ValueError("bird-validator.checkers must be an ordered list of checker names")
    for name in checkers:
        if name not in AVAILABLE_CHECKERS:
            raise ValueError(
                f"unknown bird-validator.checkers entry {name!r}; choose from {AVAILABLE_CHECKERS}"
            )
    if len(set(checkers)) != len(checkers):
        raise ValueError("bird-validator.checkers must not contain duplicate names")
    for key, minimum in (("repair_attempts", 1), ("preview_limit", -1)):
        if key in options and (type(options[key]) is not int or options[key] < minimum):
            raise ValueError(f"bird-validator.{key} must be an integer >= {minimum}")
    if not isinstance(options.get("sql_security_enabled", False), bool):
        raise ValueError("bird-validator.sql_security_enabled must be true or false")


class NL2SQLConfig:
    """Validated ``plugins.config.nl2sql`` parameters with dotted-key access."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        raw = dict(data or {})
        unknown = sorted(set(raw) - _KNOWN_KEYS)
        if unknown:
            raise ValueError(
                f"unknown nl2sql parameter(s) {unknown}; known keys: {sorted(_KNOWN_KEYS)}"
            )

        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("nl2sql requires 'model': a model alias from the Agent configuration")

        budget = raw.get("model_call_budget", DEFAULT_NL2SQL_MODEL_CALL_BUDGET)
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ValueError(f"model_call_budget must be a positive integer, got {budget!r}")

        if not isinstance(raw.get("result_files", True), bool):
            raise ValueError(f"result_files must be true or false, got {raw.get('result_files')!r}")

        scenario = raw.get("scenario")
        if scenario is not None and (not isinstance(scenario, str) or not scenario.strip()):
            raise ValueError(f"scenario must be a dotted scenario name, got {scenario!r}")

        database = raw.get("database")
        if not isinstance(database, Mapping):
            raise ValueError(
                "nl2sql requires 'database': {db_id, dialect, engine, ...connection keys}"
            )
        dialect = database.get("dialect")
        if not isinstance(dialect, str) or not dialect.strip():
            raise ValueError("database.dialect is required (e.g. sqlite, mysql, postgres)")
        for key in ("db_id", "engine"):
            if key in database and not isinstance(database.get(key), str):
                raise ValueError(f"database.{key} must be a string, got {database.get(key)!r}")

        for section in ("bird-validator", *_NODE_ORDER):
            if section in raw and not isinstance(raw.get(section), Mapping):
                raise ValueError(
                    f"nl2sql parameter {section!r} must be a mapping, got {raw.get(section)!r}"
                )

        if not isinstance(raw.get("validator", {}).get("semantic_check", True), bool):
            raise ValueError("validator.semantic_check must be true or false")
        if not isinstance(raw.get("emit_reasoning_tag", False), bool):
            raise ValueError("emit_reasoning_tag must be true or false")
        if not isinstance(raw.get("generator", {}).get("emit_reasoning_tag", False), bool):
            raise ValueError("generator.emit_reasoning_tag must be true or false")

        retries = raw.get("generator", {}).get("parse_retries", DEFAULT_NL2SQL_PARSE_RETRIES)
        if type(retries) is not int or retries < 0:
            raise ValueError("generator.parse_retries must be a non-negative integer")

        nodes = raw.get("nodes")
        if nodes is not None:
            if not isinstance(nodes, (list, tuple)) or len(nodes) < 2:
                raise ValueError("nodes must include at least perceptor and generator")
            if tuple(nodes) == _BIRD_NODE_ORDER:
                conflicts = {"validator", "reflector", "executor"}.intersection(raw)
                if conflicts:
                    raise ValueError(
                        f"bird-validator chain cannot use node sections {sorted(conflicts)}"
                    )
                if raw.get("selector", {}).get("sel_retries", 0) != 0:
                    raise ValueError("bird-validator chain requires selector.sel_retries=0")
            elif list(nodes) != list(_NODE_ORDER[: len(nodes)]):
                raise ValueError(
                    f"nodes must be a prefix of {_NODE_ORDER} or exactly {_BIRD_NODE_ORDER}"
                )
        if "bird-validator" in raw:
            if tuple(nodes or ()) != _BIRD_NODE_ORDER:
                raise ValueError(f"bird-validator section requires nodes={_BIRD_NODE_ORDER}")
            _validate_bird_options(raw["bird-validator"])
        self._data: dict[str, Any] = raw

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NL2SQLConfig(sections={sorted(self._data)})"

    @property
    def model_call_budget(self) -> int:
        """The per-task model-call budget (validated positive integer)."""
        return int(self._data.get("model_call_budget", DEFAULT_NL2SQL_MODEL_CALL_BUDGET))

    @property
    def result_files(self) -> bool:
        """Whether each executed query writes its fetched rows to a session result file."""
        return bool(self._data.get("result_files", True))

    @property
    def scenario(self) -> str | None:
        """The dotted scenario-pack name to assemble (``None`` = base behavior, no pack)."""
        value = self._data.get("scenario")
        return value if isinstance(value, str) and value.strip() else None

    @property
    def enabled_nodes(self) -> list[str]:
        """Return the explicit node chain, or infer a legacy prefix from its sections.

        Without an explicit list or any node sections the full six-node chain runs.
        The BIRD graph requires an explicit four-node list.
        """
        explicit = self._data.get("nodes")
        if explicit is not None:
            return list(explicit)
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

    @property
    def executes_sql(self) -> bool:
        """Whether the selected graph returns executed candidate results."""
        return any(node in {"executor", "bird-validator"} for node in self.enabled_nodes)

    @classmethod
    def load(cls, path: str | Path) -> NL2SQLConfig:
        """Load parameters from a strict YAML mapping (.yaml or .yml)."""
        file = Path(path)
        data = read_yaml(file)
        if not isinstance(data, dict):
            raise ValueError(f"nl2sql config must be a mapping: {file}")
        return cls(data)

    def connection_config(self) -> dict[str, Any]:
        """Database connection kwargs: the database section minus its reserved keys."""
        return {
            key: value
            for key, value in self.section("database").items()
            if key not in _CONNECTION_RESERVED
        }

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value at a dotted key path, or ``default`` when missing."""
        current: Any = self._data
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current.get(part)
        return current

    def section(self, name: str) -> Mapping[str, Any]:
        """Return one top-level configuration section as a mapping."""
        value = self._data.get(name)
        return value if isinstance(value, Mapping) else {}

    def as_dict(self) -> dict[str, Any]:
        """Return the underlying configuration dict (shallow copy)."""
        return dict(self._data)
