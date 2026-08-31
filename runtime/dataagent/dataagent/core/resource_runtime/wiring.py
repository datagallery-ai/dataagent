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
"""Single wiring entry for resource catalog, capacity, resolve, and coordinator."""

from __future__ import annotations

from typing import Any

from dataagent.core.jobs.service import JobService
from dataagent.core.resource_runtime.coordinator import ResourceJobCoordinator
from dataagent.core.resource_runtime.mcp import default_mcp_client_factory
from dataagent.core.resource_runtime.operations.operations import ResourceOperationRegistry
from dataagent.core.resource_runtime.sandbox.sandbox_ops import builtin_sandbox_resource_operations
from dataagent.resources.capacity.ledger import ResourceCapacity
from dataagent.resources.catalog.catalog import ResourceCatalog
from dataagent.resources.resolve.prepare import ResourceResolve


def build_default_operation_registry() -> ResourceOperationRegistry:
    """Create a registry preloaded with built-in ``sandbox.*`` operations."""
    registry = ResourceOperationRegistry()
    for registration in builtin_sandbox_resource_operations():
        registry.register(registration)
    return registry


def build_resource_coordinator(
    *,
    job_service: JobService,
    runtime: Any,
    config: dict[str, Any],
) -> ResourceJobCoordinator:
    """Build one workspace-scoped :class:`ResourceJobCoordinator` from merged config.

    Args:
        job_service: Workspace-scoped job service used for resource job execution.
        runtime: Active :class:`~dataagent.core.cbb.runtime.Runtime`.
        config: Merged Agent configuration containing ``RESOURCES``.

    Returns:
        A new coordinator wired with catalog, capacity, resolve, and driver registry.
    """
    catalog = ResourceCatalog.from_config(config)
    capacity = ResourceCapacity(catalog)
    resolve = ResourceResolve(catalog)
    return ResourceJobCoordinator(
        catalog=catalog,
        capacity=capacity,
        resolve=resolve,
        job_service=job_service,
        runtime=runtime,
        operation_registry=build_default_operation_registry(),
        mcp_client_factory=default_mcp_client_factory(),
    )
