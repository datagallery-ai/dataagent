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
"""Resource operation registry for driver ids such as ``sandbox.submit``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

from dataagent.resources.catalog.models import Resource

ResourceOperation = Callable[[dict[str, Any], "ResourceOperationContext"], Any]


@dataclass(frozen=True)
class ResourceOperationRegistration:
    """One registered resource operation implementation."""

    id: str
    operation: ResourceOperation
    source: str = "builtin"
    override: bool = False


@dataclass(frozen=True)
class ResourceOperationContext:
    """Execution context passed to resource operation handlers."""

    runtime: Any
    resource: Resource
    cancel_event: Event


class ResourceOperationRegistry:
    """Map operation ids (``sandbox.submit``) to Python callables."""

    def __init__(self) -> None:
        """Create an empty operation registry."""
        self._operations: dict[str, ResourceOperationRegistration] = {}

    def register(self, registration: ResourceOperationRegistration) -> None:
        """Register one operation handler.

        Args:
            registration: Operation registration metadata and callable.

        Raises:
            ValueError: When the id is empty or already registered without override.
            TypeError: When the operation is not callable.
        """
        operation_id = str(registration.id or "").strip()
        if not operation_id:
            raise ValueError("resource operation id is required")
        if not callable(registration.operation):
            raise TypeError(f"resource operation must be callable: {operation_id}")
        existing = self._operations.get(operation_id)
        if existing is not None and not registration.override:
            raise ValueError(f"resource operation already registered: {operation_id}")
        self._operations[operation_id] = registration

    def invoke(self, operation_id: str, arguments: dict[str, Any], context: ResourceOperationContext) -> Any:
        """Invoke a registered operation by id.

        Args:
            operation_id: Registered operation id string.
            arguments: Operation-specific arguments.
            context: Runtime/resource/cancel context.

        Raises:
            ValueError: When the operation id is unknown.
        """
        normalized = str(operation_id or "").strip()
        registration = self._operations.get(normalized)
        if registration is None:
            raise ValueError(f"resource operation is not registered: {normalized}")
        return registration.operation(dict(arguments or {}), context)

    def registrations(self) -> list[ResourceOperationRegistration]:
        """Return all registered operations."""
        return list(self._operations.values())
