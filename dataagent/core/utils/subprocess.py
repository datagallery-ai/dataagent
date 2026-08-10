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
from pathlib import Path


def terminate_process_group(pgid: int) -> None:
    """Best-effort kill of a Unix process group (or a single PID on Windows).

    Args:
        pgid: Process group id on Unix (typically the session-leader pid when the
            child was started with ``start_new_session=True``), or the process id
            on Windows.
    """
    if int(pgid or 0) <= 0:
        return
    try:
        if os.name != "nt":
            os.killpg(int(pgid), signal.SIGKILL)
        else:
            os.kill(int(pgid), signal.SIGTERM)
    except OSError:
        pass


def terminate_processes_with_cwd_under(root: Path) -> list[int]:
    """Best-effort kill of processes whose cwd is ``root`` or a subdirectory.

    Used as a shutdown/cancel cleanup for orphan grandchildren (for example
    ``sleep`` started via a nested ``start_new_session`` bash tool) that are no
    longer in the registered subagent process group.

    Args:
        root: Absolute directory that scopes the kill (typically one subagent
            workspace under ``.../subagents/<id>``). Never pass the parent
            workspace root.

    Returns:
        PIDs that were targeted for termination (best-effort; some may already
        have exited).
    """
    try:
        resolved_root = Path(root).expanduser().resolve()
    except OSError:
        return []
    if not resolved_root.is_dir():
        return []

    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []

    my_pid = os.getpid()
    try:
        my_pgid = os.getpgid(0)
    except OSError:
        my_pgid = None
    targeted: list[int] = []
    seen_pgids: set[int] = set()
    for entry in proc_root.iterdir():
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid == my_pid:
            continue
        try:
            cwd_raw = os.readlink(entry / "cwd")
            cwd_path = Path(cwd_raw).resolve()
            cwd_path.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        targeted.append(pid)
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            continue
        # 跳过本进程组：未 setsid 的进程本就由 Ctrl+C 的 SIGINT（前台进程组）覆盖，
        # 不会成为孤儿；若在此处 killpg 自身 pgid，timeout/cancel 路径会把整个
        # ferry 进程一起 SIGKILL 掉。
        if my_pgid is not None and pgid == my_pgid:
            continue
        if pgid in seen_pgids:
            continue
        seen_pgids.add(pgid)
        terminate_process_group(pgid)
    return targeted


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
        with contextlib.suppress(OSError):
            process.kill()
