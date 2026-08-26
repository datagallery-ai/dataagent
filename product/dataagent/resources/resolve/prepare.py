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
"""ResourceResolve: prepare structured submit plans for the coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dataagent.resources.catalog.catalog import ResourceCatalog
from dataagent.resources.catalog.models import Resource
from dataagent.resources.drivers.mcp_resource import resolve_mcp_transport


def _build_allocation(resource_id: str, *, task_type: str, amount: int, unit: str) -> dict[str, Any]:
    """Build allocation metadata for a resource job without importing core."""
    return {
        "resource": {
            "id": str(resource_id or "").strip(),
            "task_type": str(task_type or "").strip(),
            "amount": int(amount),
            "unit": str(unit or "").strip(),
        }
    }


@dataclass(frozen=True)
class DriverBinding:
    """Resolved driver binding for one executable resource."""

    transport_type: str
    operation_ids: dict[str, str]
    mcp_url: str = ""
    mcp_headers: dict[str, str] | None = None
    mcp_timeout_sec: int = 30


@dataclass(frozen=True)
class SubmitPlan:
    """Structured submit request produced for the coordinator."""

    agent_id: str
    task: str
    allocation: dict[str, Any]
    metadata: dict[str, Any]
    driver: DriverBinding
    amount: int


class ResourceResolve:
    """Prepare submit DTOs from catalog entries and merged envelopes."""

    def __init__(self, catalog: ResourceCatalog) -> None:
        """Bind resolve helpers to one catalog."""
        self._catalog = catalog

    @staticmethod
    def driver_binding_for(resource: Resource) -> DriverBinding:
        """Resolve transport and operation bindings for one resource.

        Args:
            resource: Executable resource definition.

        Returns:
            :class:`DriverBinding` with plain MCP connection fields when applicable.

        Raises:
            ValueError: When transport configuration is invalid.
        """
        transport_type = str((resource.transport or {}).get("type") or "").strip().lower()
        operation_ids = {key: str(value) for key, value in resource.operations.items()}
        if transport_type == "mcp":
            resolved = resolve_mcp_transport(resource.id, resource.transport)
            return DriverBinding(
                transport_type=transport_type,
                operation_ids=operation_ids,
                mcp_url=str(resolved["url"]),
                mcp_headers=dict(resolved["headers"]),
                mcp_timeout_sec=int(resolved["timeout_sec"]),
            )
        return DriverBinding(transport_type=transport_type, operation_ids=operation_ids)

    def prepare_submit(
        self,
        *,
        resource: Resource,
        envelope: dict[str, Any],
        amount: int,
    ) -> SubmitPlan:
        """Build one :class:`SubmitPlan` from a selected resource and envelope.

        Args:
            resource: Selected executable resource.
            envelope: Finalized submit envelope from the coordinator.
            amount: Reserved slot amount.

        Returns:
            Structured plan for ``JobService.start``.
        """
        task_type = str(envelope.get("type") or "").strip()
        allocation = _build_allocation(
            resource.id,
            task_type=task_type,
            amount=int(amount),
            unit=resource.unit,
        )
        normalized_command = str(envelope.get("command") or "").strip()
        normalized_task = normalized_command or task_type
        metadata = {
            "job_kind": "resource",
            "resource_id": resource.id,
            "resource_category": resource.category,
            "job_envelope": dict(envelope),
        }
        return SubmitPlan(
            agent_id=f"resource:{resource.id}",
            task=normalized_task,
            allocation=allocation,
            metadata=metadata,
            driver=self.driver_binding_for(resource),
            amount=int(amount),
        )
