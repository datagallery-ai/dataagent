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
"""Shared async subprocess helpers for core job and resource runners."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess


async def terminate_process_tree_async(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its process group when still running.

    On Unix, sends ``SIGKILL`` to the process group (requires the process
    to have been created with ``start_new_session=True``).

    On Windows, uses ``taskkill /T /F /PID`` to kill the entire process
    tree (requires the process to have been created with
    ``CREATE_NEW_PROCESS_GROUP`` flag).

    Args:
        process: Async subprocess handle created with ``start_new_session`` on Unix
            or ``CREATE_NEW_PROCESS_GROUP`` on Windows.
    """
    if process.returncode is not None:
        return

    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            # Windows: use taskkill /T (tree) /F (force) to kill the entire
            # process tree, not just the parent process.  This avoids leaving
            # orphaned child processes when the parent was a shell (cmd.exe / bash).
            subprocess.run(
                [
                    os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"),
                    "/T",
                    "/F",
                    "/PID",
                    str(process.pid),
                ],
                capture_output=True,
                timeout=5,
            )
    except (subprocess.TimeoutExpired, OSError):
        # Fallback: try a plain kill on the parent process
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
