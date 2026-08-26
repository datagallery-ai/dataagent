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
"""ResourceCapacity: in-memory capacity ledger for resource slot accounting."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from dataagent.resources.catalog.catalog import ResourceCatalog


@dataclass(frozen=True)
class CapacityView:
    """Read-only capacity snapshot for one resource id."""

    id: str
    used: int
    available: int
    total: int
    unit: str


@dataclass(frozen=True)
class ReserveResult:
    """Result of one ``try_reserve`` attempt."""

    ok: bool
    message: str = ""


class ResourceCapacity:
    """Single in-memory ledger for resource slot usage."""

    def __init__(self, catalog: ResourceCatalog) -> None:
        """Bind the ledger to one resource catalog."""
        self._catalog = catalog
        self._lock = Lock()
        self._by_job: dict[str, tuple[str, int]] = {}
        self._used_by_resource: dict[str, int] = {}

    def try_reserve(
        self,
        *,
        resource_id: str,
        task_type: str,
        job_id: str,
        amount: int,
    ) -> ReserveResult:
        """Reserve capacity slots for one job before ``JobService.start``.

        Args:
            resource_id: Target resource id.
            task_type: Task type label used in error messages.
            job_id: Opaque job id allocated by ``JobService.new_job_id``.
            amount: Slot amount required by the task.

        Returns:
            :class:`ReserveResult` with ``ok=False`` when capacity is exhausted.
        """
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return ReserveResult(ok=False, message="job_id is required for capacity reservation")
        normalized_amount = max(0, int(amount))
        with self._lock:
            resource = self._catalog.get(resource_id)
            if resource is None:
                return ReserveResult(ok=False, message=f"resource not found: {resource_id}")
            projected = int(self._used_by_resource.get(resource_id, 0)) + normalized_amount
            if projected > int(resource.capacity):
                return ReserveResult(
                    ok=False,
                    message=(
                        f"resource capacity exhausted: {projected}/{resource.capacity} {resource.unit} allocated; "
                        f"task requires {normalized_amount}"
                    ),
                )
            self._by_job[normalized_job_id] = (resource_id, normalized_amount)
            self._used_by_resource[resource_id] = projected
            return ReserveResult(ok=True)

    def release(self, job_id: str) -> None:
        """Release slots reserved for one job id.

        Args:
            job_id: Job id previously passed to ``try_reserve``.
        """
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return
        with self._lock:
            entry = self._by_job.pop(normalized_job_id, None)
            if entry is None:
                return
            resource_id, amount = entry
            remaining = int(self._used_by_resource.get(resource_id, 0)) - int(amount)
            if remaining > 0:
                self._used_by_resource[resource_id] = remaining
            else:
                self._used_by_resource.pop(resource_id, None)

    def snapshot(self) -> list[CapacityView]:
        """Return used/available counts for all catalog resources."""
        with self._lock:
            views: list[CapacityView] = []
            for resource in self._catalog.list():
                used = int(self._used_by_resource.get(resource.id, 0))
                total = int(resource.capacity)
                views.append(
                    CapacityView(
                        id=resource.id,
                        used=used,
                        available=max(0, total - used),
                        total=total,
                        unit=resource.unit,
                    )
                )
            return views
