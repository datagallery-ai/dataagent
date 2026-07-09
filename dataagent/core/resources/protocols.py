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
"""Protocols for resource driver injection into :class:`~dataagent.core.resources.service.ResourceService`."""

from __future__ import annotations

from typing import Any, Protocol


class McpResourceClient(Protocol):
    """Minimal MCP client surface used by :class:`~dataagent.core.resources.service.ResourceService`."""

    def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one remote MCP tool synchronously from a resource job runner thread."""
        ...
