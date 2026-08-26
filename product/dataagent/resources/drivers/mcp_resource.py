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
"""MCP transport metadata and ``secret_ref`` resolution for resource drivers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def resolve_secret_ref(value: Any) -> str:
    """Resolve a header value that may use ``secret_ref: env:<VAR>``.

    Args:
        value: Plain string or mapping ``{"secret_ref": "env:VAR_NAME"}``.

    Returns:
        Resolved secret string, or ``str(value)`` for plain scalars.

    Raises:
        ValueError: When ``secret_ref`` scheme is unsupported or env var is missing.
    """
    if isinstance(value, Mapping):
        secret_ref = str(value.get("secret_ref") or "").strip()
        if not secret_ref:
            raise ValueError("secret_ref value is required when header value is an object")
        if secret_ref.startswith("env:"):
            var_name = secret_ref[len("env:") :].strip()
            if not var_name:
                raise ValueError("secret_ref env: requires a variable name")
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(f"environment variable is not set: {var_name}")
            return str(env_value)
        raise ValueError(f"unsupported secret_ref scheme: {secret_ref}")
    return str(value or "")


def resolve_transport_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Resolve ``transport.headers`` values for MCP HTTP connections.

    Args:
        headers: Raw header mapping from resource transport configuration.

    Returns:
        Plain string header mapping suitable for httpx/MCP clients.
    """
    resolved: dict[str, str] = {}
    for key, value in (headers or {}).items():
        header_name = str(key or "").strip()
        if not header_name:
            continue
        resolved[header_name] = resolve_secret_ref(value)
    return resolved


def resolve_mcp_transport(resource_id: str, transport: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one MCP transport block into plain connection fields.

    Args:
        resource_id: Stable resource id used for error messages.
        transport: Executable resource transport mapping.

    Returns:
        Dict with ``url``, ``headers``, and ``timeout_sec``.

    Raises:
        ValueError: When transport is missing required MCP fields.
    """
    url = str(transport.get("url") or "").strip()
    if not url:
        raise ValueError(f"resource {resource_id} MCP transport requires url")
    headers = resolve_transport_headers(
        transport.get("headers") if isinstance(transport.get("headers"), Mapping) else {}
    )
    timeout_sec = max(1, int(transport.get("timeout_sec") or 30))
    return {"url": url, "headers": headers, "timeout_sec": timeout_sec}
