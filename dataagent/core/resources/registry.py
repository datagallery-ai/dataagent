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
"""ResourceRegistry: parse and query merged ``RESOURCES`` configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dataagent.core.resources.models import Resource

RESOURCE_CATEGORIES = frozenset({"executable", "non-executable"})
RESOURCE_FIELDS = frozenset({"id", "name", "category", "transport", "operations", "capacity", "consumption"})
RESOURCE_OPERATIONS = frozenset({"submit", "poll", "collect", "cancel"})
TRANSPORT_TYPES = frozenset({"local", "mcp"})


class ResourceRegistry:
    """In-memory catalog built from merged ``RESOURCES`` list entries."""

    def __init__(self, *, resources: list[Resource] | None = None) -> None:
        """Create a registry from parsed resource definitions."""
        self.catalog: dict[str, Resource] = {item.id: item for item in resources or []}

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ResourceRegistry:
        """Build a registry from a merged Agent configuration mapping."""
        raw = config.get("RESOURCES") or []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError("RESOURCES must be a list of resource definitions")
        return cls(resources=resources_from_list(list(raw)))

    def resources(self) -> list[Resource]:
        """Return all resources sorted by id."""
        return [self.catalog[key] for key in sorted(self.catalog)]

    def executable_resources(self) -> list[Resource]:
        """Return executable resources sorted by id."""
        return [resource for resource in self.resources() if resource.executable]

    def resource(self, resource_id: str) -> Resource | None:
        """Look up one resource by id."""
        return self.catalog.get(str(resource_id or "").strip())

    def select_executable(self, *, resource_id: str = "", task_type: str) -> tuple[Resource | None, str]:
        """Select one executable resource for a task type.

        Args:
            resource_id: Optional explicit resource id.
            task_type: Task type used to resolve ``consumption``.

        Returns:
            ``(resource, error_message)``; resource is ``None`` when selection fails.
        """
        normalized_id = str(resource_id or "").strip()
        if normalized_id:
            resource = self.resource(normalized_id)
            if resource is None:
                return None, f"resource not found: {normalized_id}"
            if not resource.executable:
                return None, f"resource is not executable: {normalized_id}"
            if resource.consumption_for(task_type) is None:
                return None, f"resource {normalized_id} does not declare consumption for task type: {task_type}"
            return resource, ""

        candidates = [
            resource for resource in self.executable_resources() if resource.consumption_for(task_type) is not None
        ]
        if not candidates:
            return None, f"no executable resource supports task type: {task_type}"
        if len(candidates) > 1:
            ids = ", ".join(resource.id for resource in candidates)
            return None, f"multiple resources support task type {task_type}; specify resource_id: {ids}"
        return candidates[0], ""

    def with_usage(self, *, resource_usage: dict[str, int] | None = None) -> ResourceRegistry:
        """Return a copy with runtime ``used`` counts applied."""
        resource_usage = resource_usage or {}
        resources = [
            Resource(
                id=resource.id,
                name=resource.name,
                category=resource.category,
                capacity=resource.capacity,
                unit=resource.unit,
                used=int(resource_usage.get(resource.id, resource.used) or 0),
                consumption=dict(resource.consumption),
                operations=dict(resource.operations),
                transport=dict(resource.transport),
                metadata=dict(resource.metadata),
            )
            for resource in self.resources()
        ]
        return ResourceRegistry(resources=resources)


def validate_resources_list(items: list[Any]) -> None:
    """Validate merged ``RESOURCES`` list entries (fail-fast on schema errors).

    Args:
        items: Raw list from merged configuration.

    Raises:
        ValueError: When any entry is invalid.
    """
    for index, raw in enumerate(items):
        label = f"RESOURCES[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} must be an object")
        resource_id = str(raw.get("id") or "").strip()
        if not resource_id:
            raise ValueError(f"{label}.id is required")
        unknown = sorted(set(raw) - RESOURCE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown fields in {label}: {', '.join(unknown)}")
        category = str(raw.get("category") or "").strip()
        if category not in RESOURCE_CATEGORIES:
            raise ValueError(f"{label}.category must be executable or non-executable")
        capacity = raw.get("capacity")
        if not isinstance(capacity, Mapping) or set(capacity) != {"total", "unit"}:
            raise ValueError(f"{label}.capacity requires only total and unit")
        _positive_int(capacity.get("total"), field=f"{label}.capacity.total")
        if not str(capacity.get("unit") or "").strip():
            raise ValueError(f"{label}.capacity.unit is required")
        consumption = raw.get("consumption")
        if not isinstance(consumption, Mapping) or not consumption:
            raise ValueError(f"{label}.consumption must be a non-empty object")
        for task_type, amount in consumption.items():
            if not str(task_type or "").strip():
                raise ValueError(f"{label}.consumption contains an empty task type")
            _positive_int(amount, field=f"{label}.consumption.{task_type}")
        if category == "executable":
            _validate_executable_fields(raw, label=label)
        else:
            _validate_non_executable_fields(raw, label=label)


def resources_from_list(items: list[Any]) -> list[Resource]:
    """Parse and validate a ``RESOURCES`` list into :class:`Resource` objects."""
    validate_resources_list(items)
    out: list[Resource] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        resource_id = str(raw.get("id") or "").strip()
        capacity = raw["capacity"]
        out.append(
            Resource(
                id=resource_id,
                name=str(raw.get("name") or resource_id).strip(),
                category=str(raw["category"]).strip(),
                capacity=int(capacity["total"]),
                unit=str(capacity["unit"]).strip(),
                consumption={str(key): int(value) for key, value in raw["consumption"].items()},
                operations={str(key): str(value) for key, value in (raw.get("operations") or {}).items()},
                transport=dict(raw.get("transport") or {}),
                metadata={"source": f"RESOURCES.{resource_id}"},
            )
        )
    return out


def _validate_non_executable_fields(raw: Mapping[str, Any], *, label: str) -> None:
    """Validate non-executable resources do not declare MCP or lifecycle fields."""
    if raw.get("transport"):
        raise ValueError(f"{label} non-executable resources must not declare transport")
    if raw.get("operations"):
        raise ValueError(f"{label} non-executable resources must not declare operations")


def _validate_executable_fields(raw: Mapping[str, Any], *, label: str) -> None:
    """Validate executable-only fields on one resource definition."""
    transport = raw.get("transport")
    if not isinstance(transport, Mapping) or not str(transport.get("type") or "").strip():
        raise ValueError(f"{label}.transport.type is required for executable resources")
    transport_type = str(transport.get("type") or "").strip().lower()
    if transport_type not in TRANSPORT_TYPES:
        raise ValueError(f"{label}.transport.type must be one of: {', '.join(sorted(TRANSPORT_TYPES))}")
    if transport_type == "mcp" and not str(transport.get("url") or "").strip():
        raise ValueError(f"{label}.transport.url is required for MCP resources")
    operations = raw.get("operations")
    if not isinstance(operations, Mapping) or set(operations) != RESOURCE_OPERATIONS:
        raise ValueError(f"{label}.operations requires submit, poll, collect, and cancel")
    for operation, operation_id in operations.items():
        if not str(operation_id or "").strip():
            raise ValueError(f"{label}.operations.{operation} is required")


def _positive_int(value: Any, *, field: str, default: int | None = None) -> int:
    """Parse a positive integer configuration value."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"{field} must be a positive integer") from None
    if parsed < 1:
        if default is not None:
            return default
        raise ValueError(f"{field} must be a positive integer")
    return parsed
