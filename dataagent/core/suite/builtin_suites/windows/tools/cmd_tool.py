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
import contextlib
import locale
import os
import re
import subprocess
import tempfile
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


def _fix_cmd_quotes(command: str) -> str:
    """Replace C-style escaped quotes (``\\"``) with cmd.exe doubled quotes (``""``).

    LLMs commonly generate ``python -c "print(\\"hello\\")"`` where ``\\"`` is
    a C-style escape.  But cmd.exe does NOT treat ``\\"`` as an escape — the
    ``"`` closes the quoted section, causing special characters like ``>``,
    ``<``, ``|`` in the unquoted section to be interpreted as operators.

    Replacing ``\\"`` with ``""`` (cmd.exe's native escape for a literal quote
    inside a quoted section) preserves the semantics for both cmd.exe and the
    originating language.

    This follows the Windows CRT argument-parsing rule: only an **odd** number
    of backslashes before a ``"`` means the last backslash escapes the quote;
    an even number means the backslashes are literal and the ``"`` is a closing
    quote.  For example:

    * ``\\"` → 1 backslash (odd) → escape → replace with ``""``
    * ``\\\\"` → 3 backslashes (odd) → last one escapes → replace with ``\\\\""``
    * ``\\\\"` → 2 backslashes (even) → literal ``\\`` + closing ``"`` → keep as-is
    """

    def _replace(match: re.Match) -> str:
        backslashes = match.group(1)
        if len(backslashes) % 2 == 1:
            # Odd: last \ escapes the " → replace \" with ""
            return backslashes[:-1] + '""'
        # Even: literal backslashes + closing quote → keep as-is
        return match.group(0)

    return re.sub(r'(\\+)"', _replace, command)


# Regex: match ``python -c "..."`` including doubled-quote escapes (``""``)
# inside the string.  The pattern captures the python executable and the
# content between the outer quotes, allowing ``""`` (cmd.exe literal quote)
# as the only form of embedded quote.
_PYTHON_C_DQUOTE_RE = re.compile(
    r'(python(?:\.exe)?)\s+-c\s+"((?:[^"]|"")*)"',
    re.IGNORECASE,
)
_PYTHON_C_SQUOTE_RE = re.compile(
    r"(python(?:\.exe)?)\s+-c\s+'((?:[^'])*)'",
    re.IGNORECASE,
)


def _fix_python_c_multiline(command: str) -> tuple[str, list[str]]:
    """Detect multi-line ``python -c "..."`` and rewrite to a temp-file exec.

    cmd.exe ``/c`` treats newlines as command separators, so a multi-line
    ``python -c`` command is silently broken.  This function extracts the
    Python code, writes it to a temp file, and rewrites the command to
    ``python "<tempfile>"``.

    Returns:
        A tuple of (rewritten_command, list_of_temp_file_paths_to_clean_up).
    """
    temp_files: list[str] = []

    def _rewrite_dquote(m: re.Match) -> str:
        python_exe = m.group(1)
        code = m.group(2)
        if "\n" not in code:
            return m.group(0)  # single-line, no rewrite needed
        # Restore doubled quotes to single quotes for the Python source file.
        code = code.replace('""', '"')
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="dataagent_cmd_",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(code)
            tmp_name = tmp.name
        temp_files.append(tmp_name)
        return f'{python_exe} "{tmp_name}"'

    def _rewrite_squote(m: re.Match) -> str:
        python_exe = m.group(1)
        code = m.group(2)
        if "\n" not in code:
            return m.group(0)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="dataagent_cmd_",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(code)
            tmp_name = tmp.name
        temp_files.append(tmp_name)
        return f'{python_exe} "{tmp_name}"'

    result = _PYTHON_C_DQUOTE_RE.sub(_rewrite_dquote, command)
    result = _PYTHON_C_SQUOTE_RE.sub(_rewrite_squote, result)
    return result, temp_files


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

    The tool automatically fixes two common cmd.exe quoting pitfalls:

    1. **Backslash-escaped quotes** — ``\\"`` inside a quoted section is
       replaced with ``""`` (cmd.exe's native literal-quote escape).  This
       means ``python -c "print(\\"hello\\")"`` works correctly without
       manual adjustment.

    2. **Multi-line ``python -c``** — when the code passed to ``python -c``
       contains newlines, the tool writes the code to a temporary file and
       rewrites the command to ``python <tempfile>``, avoiding cmd.exe's
       newline-as-command-separator behaviour.

    File paths with spaces can be safely quoted with double quotes,
    e.g. ``python "C:\\My Path\\script.py"``.

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

    # Fix 1: Replace \" with "" for cmd.exe compatibility.
    # LLMs commonly generate python -c "...\\"...\\"" which cmd.exe misparses.
    cmd_text = _fix_cmd_quotes(cmd_text)

    # Fix 2: Rewrite multi-line python -c to temp-file execution.
    # cmd.exe /c treats newlines as command separators, breaking multi-line
    # python -c commands.  This rewrites them to python <tempfile>.
    cmd_text, _temp_files = _fix_python_c_multiline(cmd_text)

    # Ensure Python subprocesses output UTF-8 so we can decode correctly.
    env["PYTHONUTF8"] = "1"

    comspec = env.get("COMSPEC", r"C:\Windows\System32\cmd.exe")

    try:
        result = await _run_cmd_subprocess(
            cmd_text,
            comspec=comspec,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    finally:
        # Clean up temp files created by _fix_python_c_multiline.
        # Placed in finally so files are removed even on timeout / cancellation.
        for tmp_path in _temp_files:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

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
