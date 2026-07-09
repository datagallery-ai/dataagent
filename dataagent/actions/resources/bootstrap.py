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
"""Default resource driver wiring for :class:`~dataagent.core.cbb.runtime.Runtime`."""

from __future__ import annotations

from collections.abc import Callable

from dataagent.actions.resources.mcp_adapter import build_mcp_resource_client
from dataagent.actions.resources.sandbox_ops import builtin_sandbox_resource_operations
from dataagent.core.resources.models import Resource
from dataagent.core.resources.operations import ResourceOperationRegistry
from dataagent.core.resources.protocols import McpResourceClient


def default_resource_operation_registry() -> ResourceOperationRegistry:
    """Create a registry preloaded with built-in ``sandbox.*`` operations."""
    registry = ResourceOperationRegistry()
    for registration in builtin_sandbox_resource_operations():
        registry.register(registration)
    return registry


def default_mcp_client_factory() -> Callable[[Resource], McpResourceClient]:
    """Return the default MCP client factory for executable MCP resources."""
    return build_mcp_resource_client
