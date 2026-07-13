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
"""Resource domain models for executable and catalog resources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Resource:
    """One resource definition from merged ``RESOURCES`` configuration."""

    id: str
    name: str
    category: str
    capacity: int = 1
    unit: str = "slot"
    consumption: dict[str, int] = field(default_factory=dict)
    operations: dict[str, str] = field(default_factory=dict)
    transport: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        """Return whether this resource can execute jobs."""
        return self.category == "executable"

    def consumption_for(self, task_type: str) -> int | None:
        """Resolve slot consumption for a task type, falling back to ``*``."""
        normalized = str(task_type or "").strip()
        raw = self.consumption.get(normalized, self.consumption.get("*"))
        return int(raw) if raw is not None else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the resource for diagnostics and catalog APIs."""
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "capacity": {"total": int(self.capacity), "unit": self.unit},
            "consumption": dict(self.consumption),
            "operations": dict(self.operations),
            "transport": _transport_summary(self.transport),
            "metadata": self.metadata,
        }


def _transport_summary(config: dict[str, Any]) -> dict[str, Any]:
    """Return a safe transport summary without leaking secret refs."""
    if not config:
        return {}
    return {
        "type": str(config.get("type") or "").strip(),
        "configured": True,
    }
