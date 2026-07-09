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
"""Built-in local sandbox resource operations (``sandbox.*``)."""

from __future__ import annotations

from typing import Any

from dataagent.actions.resources.sandbox_runner import run_local_sandbox_command
from dataagent.core.resources.operations import ResourceOperationContext, ResourceOperationRegistration


def builtin_sandbox_resource_operations() -> tuple[ResourceOperationRegistration, ...]:
    """Return built-in sandbox operation registrations for local resources."""
    return (
        ResourceOperationRegistration("sandbox.submit", _sandbox_submit, source="builtin"),
        ResourceOperationRegistration("sandbox.poll", _sandbox_poll, source="builtin"),
        ResourceOperationRegistration("sandbox.collect", _sandbox_collect, source="builtin"),
        ResourceOperationRegistration("sandbox.cancel", _sandbox_cancel, source="builtin"),
    )


def _sandbox_submit(arguments: dict[str, Any], context: ResourceOperationContext) -> dict[str, Any]:
    """Execute a local sandbox command synchronously for one resource job."""
    envelope = arguments.get("envelope") if isinstance(arguments.get("envelope"), dict) else {}
    command = str(envelope.get("command") or "").strip()
    timeout_sec = max(1, int(envelope.get("timeout_sec") or 3600))
    workspace_dir = getattr(context.runtime, "workspace_dir", None)
    if workspace_dir is None:
        return {"status": "failed", "exit_code": 1, "error": "workspace_dir is required"}
    if not command:
        return {"status": "failed", "exit_code": 1, "error": "command is required"}
    sandbox = context.runtime.sandbox
    result = run_local_sandbox_command(
        command=command,
        workspace_dir=workspace_dir,
        sandbox=sandbox,
        timeout_sec=timeout_sec,
        cancel_event=context.cancel_event,
    )
    return dict(result)


def _sandbox_poll(arguments: dict[str, Any], context: ResourceOperationContext) -> dict[str, Any]:
    """No-op poll for synchronous local sandbox execution."""
    del context
    return {"job_id": str(arguments.get("job_id") or ""), "status": "completed"}


def _sandbox_collect(arguments: dict[str, Any], context: ResourceOperationContext) -> dict[str, Any]:
    """No-op collect for synchronous local sandbox execution."""
    del context
    return {"job_id": str(arguments.get("job_id") or ""), "status": "completed"}


def _sandbox_cancel(arguments: dict[str, Any], context: ResourceOperationContext) -> dict[str, Any]:
    """No-op cancel hook for synchronous local sandbox execution."""
    del context
    return {"job_id": str(arguments.get("job_id") or ""), "status": "cancelled"}
