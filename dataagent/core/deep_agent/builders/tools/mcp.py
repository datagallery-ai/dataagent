# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Build OpenJiuWen MCP server configs from normalized DataAgent YAML."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from dataagent.core.deep_agent.spec import McpServerSpec

if TYPE_CHECKING:
    from openjiuwen.core.foundation.tool import McpServerConfig


def build_mcp_servers(specs: Iterable[McpServerSpec]) -> list[McpServerConfig]:
    """Convert normalized MCP specs to OpenJiuWen server configs."""
    from openjiuwen.core.foundation.tool import McpServerConfig

    return [
        McpServerConfig(
            server_id=spec.server_id,
            server_name=spec.server_name,
            server_path=spec.server_path,
            client_type=spec.client_type,
            params=dict(spec.params),
            auth_headers=dict(spec.auth_headers),
            auth_query_params=dict(spec.auth_query_params),
        )
        for spec in specs
    ]
