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
"""Local sandbox command execution for resource jobs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from threading import Event

from dataagent.actions.tools.local_tool.sandbox import Sandbox, reset_current_sandbox, set_current_sandbox
from dataagent.core.utils.subprocess import terminate_process_tree_async

_CANCEL_POLL_INTERVAL_SEC = 0.2


def run_local_sandbox_command(
    *,
    command: str,
    workspace_dir: Path,
    sandbox: Sandbox,
    timeout_sec: int,
    cancel_event: Event,
) -> dict[str, object]:
    """Execute one shell command in the workspace sandbox with cancel support.

    Args:
        command: Shell command executed via ``/bin/bash -lc``.
        workspace_dir: Parent workspace directory used as subprocess cwd.
        sandbox: Active sandbox used to wrap the command.
        timeout_sec: Maximum runtime in seconds.
        cancel_event: Job cancel event propagated from :class:`JobService`.

    Returns:
        Result payload with ``status``, ``exit_code``, ``stdout``, and ``stderr``.
    """
    token = set_current_sandbox(sandbox)
    cwd = str(Path(workspace_dir).expanduser().resolve())
    cmd = ["/bin/bash", "-lc", command]
    try:
        completed = asyncio.run(
            _run_cancellable_command_async(
                cmd=cmd,
                cwd=cwd,
                timeout=max(1, int(timeout_sec)),
                cancel_event=cancel_event,
                sandbox=sandbox,
            )
        )
    finally:
        reset_current_sandbox(token)

    returncode = int(completed.get("returncode", 1))
    stdout = str(completed.get("stdout") or "")
    stderr = str(completed.get("stderr") or "")
    if cancel_event.is_set():
        return {
            "status": "cancelled",
            "exit_code": 130,
            "stdout": stdout,
            "stderr": stderr,
            "summary": "cancelled",
            "error": "cancelled",
        }
    if returncode != 0:
        summary = stderr.strip() or stdout.strip() or f"command failed with exit code {returncode}"
        return {
            "status": "failed",
            "exit_code": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "error": summary,
        }
    return {
        "status": "completed",
        "exit_code": 0,
        "stdout": stdout,
        "stderr": stderr,
        "summary": stdout.strip(),
        "outputs": [],
        "metrics": {},
    }


async def _run_cancellable_command_async(
    *,
    cmd: list[str],
    cwd: str,
    timeout: int,
    cancel_event: Event,
    sandbox: Sandbox,
) -> dict[str, object]:
    """Run a wrapped subprocess and honour ``cancel_event`` while it is active."""
    wrapped_cmd = sandbox.wrap_command(cmd, cwd=cwd, env=None)
    process_cwd = None if wrapped_cmd is not cmd else cwd
    process = await asyncio.create_subprocess_exec(
        *wrapped_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=process_cwd,
        start_new_session=os.name != "nt",
    )
    communicate_task = asyncio.create_task(process.communicate())
    deadline = time.monotonic() + max(1, int(timeout))
    try:
        while not communicate_task.done():
            if cancel_event.is_set():
                await terminate_process_tree_async(process)
                communicate_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate_task
                return {"stdout": "", "stderr": "resource job cancelled", "returncode": -1}
            if time.monotonic() >= deadline:
                await terminate_process_tree_async(process)
                communicate_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate_task
                return {"stdout": "", "stderr": "resource job timed out", "returncode": -1}
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SEC)
        stdout_bytes, stderr_bytes = communicate_task.result()
    except asyncio.CancelledError:
        await terminate_process_tree_async(process)
        raise
    return {
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "returncode": int(process.returncode or 0),
    }
