# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Windows Suite local tool: execute a Windows cmd.exe command."""

from __future__ import annotations

import asyncio
import locale
import os
import subprocess
from typing import Any

from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.tools import (
    _build_shell_env,
    _expand_skill_aliases_in_shell_command,
    _terminate_process_tree_async,
)


def _ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("The cmd tool is only available on Windows.")


def _decode_output(raw: bytes) -> str:
    """Decode subprocess output bytes using smart encoding detection.

    Tries UTF-8 first (strict); if that fails, falls back to the system
    preferred encoding (e.g. GBK/CP936 on Chinese Windows).
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(), errors="replace")


# Seconds to wait after killing a timed-out process before cancelling the task.
_PROCESS_TIMEOUT_CLEANUP_SECONDS = 1


async def _run_cmd_subprocess(
    command: str,
    *,
    comspec: str,
    timeout: int,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a cmd.exe command via ``create_subprocess_shell``.

    Unlike ``create_subprocess_exec``, ``create_subprocess_shell`` passes the
    command string directly to the shell, avoiding Python's ``list2cmdline``
    which would double-escape quotes and break paths like
    ``python "C:\\path\\file.py"``.
    """
    cmd_line = f"{comspec} /d /c {command}"

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    process = await asyncio.create_subprocess_shell(
        cmd_line,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        creationflags=creationflags,
    )

    try:
        task = asyncio.ensure_future(process.communicate())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            stdout_bytes, stderr_bytes = task.result()
        else:
            await _terminate_process_tree_async(process)
            done, _ = await asyncio.wait({task}, timeout=_PROCESS_TIMEOUT_CLEANUP_SECONDS)
            if task not in done:
                task.cancel()
            raise TimeoutError
    except TimeoutError as exc:
        raise RuntimeError(
            f"Command timed out after {timeout}s. Consider splitting into smaller steps or increasing timeout.\n"
            f"Command: {command!r}"
        ) from exc

    stdout = _decode_output(stdout_bytes).strip()
    stderr = _decode_output(stderr_bytes).strip()
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": process.returncode,
    }


async def win_cmd(command: str, purpose: str, timeout: str | int = 600) -> dict[str, Any]:
    """Execute a Windows cmd.exe command in the current workspace.

    Use this tool when:
    - Running Windows shell commands explicitly (dir, copy, where, etc.)
    - Executing .bat/.cmd scripts
    - You need deterministic cmd.exe semantics on Windows

    IMPORTANT — cmd.exe quoting rules differ from bash:
    - cmd.exe does NOT support nested double quotes.  Commands like
      ``python -c "import sqlite3; conn=sqlite3.connect('...'); ..."`` will
      fail because cmd.exe cannot correctly parse the inner quotes.
    - File paths with spaces can be safely quoted with double quotes,
      e.g. ``python "C:\\My Path\\script.py"``.  For literal quotes inside
      a quoted argument, use ``""`` (doubled double quotes) — cmd.exe does
      NOT support backslash-escaped quotes (``\\"``).

    Args:
        command (str): Complete cmd command as a single string.
        purpose (str): Brief description of why this command is being run (required, non-empty).
        timeout (str | int): Maximum seconds to wait before killing the process. Defaults to 600.

    Returns:
        dict[str, Any]: Tool-style output with original_msg/frontend_msg/data.
    """
    normalized_purpose = str(purpose or "").strip()
    if not normalized_purpose:
        raise ValueError("'purpose' is required and must not be empty.")

    cmd_text = str(command or "").strip()
    if not cmd_text:
        raise ValueError("'command' is required and must not be empty.")

    if isinstance(timeout, str):
        timeout = int(timeout)

    _ensure_windows()

    guard = get_current_sandbox()
    cwd: str | None = None
    env = _build_shell_env()
    if guard.workspace_root is not None:
        cwd = str(guard.workspace_root)
    if guard.skill_aliases:
        cmd_text = _expand_skill_aliases_in_shell_command(cmd_text)

    # Ensure Python subprocesses output UTF-8 so we can decode correctly.
    env["PYTHONUTF8"] = "1"

    comspec = env.get("COMSPEC", r"C:\Windows\System32\cmd.exe")

    result = await _run_cmd_subprocess(
        cmd_text,
        comspec=comspec,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )

    stdout = result["stdout"]
    stderr = result["stderr"]
    exit_code = result["returncode"]
    parts: list[str] = []
    if stdout:
        parts.append(stdout)

    if stderr:
        parts.append(f"[stderr]\n{stderr}")

    if exit_code != 0:
        parts.append(f"[exit code: {exit_code}]")

    original_msg = "\n".join(parts) or "(no output)"
    status_label = "succeeded" if exit_code == 0 else f"failed (exit code {exit_code})"
    frontend_msg = f"cmd {status_label} — {normalized_purpose}"
    return {
        "original_msg": original_msg,
        "frontend_msg": frontend_msg,
        "data": {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "command": cmd_text,
            "cwd": cwd,
            "comspec": comspec,
        },
    }
