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
"""Subprocess runner for Job-path subagent execution (Phase A, no SWARM writes)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from dataagent.actions.tools.local_tool.sandbox import Sandbox, get_current_sandbox, set_current_sandbox
from dataagent.actions.tools.local_tool.tools import (
    _coerce_flex_state_dict_from_payload,
    _run_subprocess_async,
    _synthetic_worker_result_dict,
)
from dataagent.core.context.message_history import read_messages_file, serialize_message
from dataagent.core.swarm.worker_result import worker_result_from_payload
from dataagent.core.utils.subprocess import terminate_process_tree_async
from dataagent.utils.runtime_paths import FLEX_PERSISTENCE_ROOT_ENV, resolve_user_root

_CANCEL_POLL_INTERVAL_SEC = 0.2


@dataclass(frozen=True)
class JobSubagentOutcome:
    """Parsed subagent subprocess result for the Job collect contract."""

    original_msg: Any
    frontend_msg: str
    state: dict[str, Any] | None
    status: str
    error: str = ""


class SubagentSubprocessRunner:
    """Run ``sub_agent_entry`` for Job-path subagents without touching SWARM storage."""

    async def run(
        self,
        *,
        query: str,
        config_path: Path,
        workspace_dir: Path,
        subagent_session_id: str,
        user_id: str,
        parent_session_id: str,
        sub_id: int,
        timeout: int,
        sandbox: Sandbox,
        cancel_event: Event | None = None,
        progress_callback: Any = None,
        tool_call_id: str | None = None,
        reuse_workspace: bool = False,
    ) -> JobSubagentOutcome:
        """Launch the subagent subprocess and parse stdout into collect fields.

        Args:
            query: Task/query forwarded to the child agent.
            config_path: Absolute path to the subagent yaml config.
            workspace_dir: Subagent workspace root under ``subagents/{id}/``.
            subagent_session_id: Opaque workspace id allocated for this job.
            user_id: Parent user id.
            parent_session_id: Parent session id passed via CLI ``--session-id``.
            sub_id: Numeric worker id required by ``sub_agent_entry``.
            timeout: Subprocess timeout in seconds.
            sandbox: Sandbox used to wrap the child command.
            cancel_event: Optional job cancel event.
            progress_callback: Optional stderr progress callback.
            tool_call_id: Optional tool call id for progress parsing.
            reuse_workspace: When true, hydrate messages/state from the existing workspace.

        Returns:
            Parsed collect-compatible fields. Never writes ``workers/.memory``.
        """
        token = set_current_sandbox(sandbox)
        initial_state_file: Path | None = None
        resolved_workspace = Path(workspace_dir).expanduser().resolve()
        try:
            initial_state_file = _prepare_job_initial_state_file(
                workspace_dir=resolved_workspace,
                subagent_session_id=subagent_session_id,
                user_id=user_id,
                parent_session_id=parent_session_id,
                sub_id=sub_id,
                query=query,
                reuse_workspace=reuse_workspace,
            )
            env = dict(os.environ)
            env[FLEX_PERSISTENCE_ROOT_ENV] = str(resolved_workspace)
            sub_agent_session_id = f"subagent_{parent_session_id}_{sub_id}"
            sub_agent_log_path = (
                resolve_user_root(user_id=user_id) / "logs" / f"{sub_agent_session_id}_{sub_id}.log"
            ).resolve()
            env["DATAAGENT_LOG_FILE"] = str(sub_agent_log_path)
            env["DATAAGENT_LOG_PROCESS_NAME"] = "subagent"
            cmd = [
                sys.executable,
                "-m",
                "dataagent.actions.tools.local_tool.sub_agent_entry",
                "--query",
                query,
                "--config",
                os.fspath(config_path),
                "--user-id",
                user_id,
                "--session-id",
                parent_session_id,
                "--sub-id",
                str(sub_id),
                "--initial-state-file",
                str(initial_state_file),
            ]
            completed = await self._run_with_cancel(
                cmd=cmd,
                timeout=timeout,
                env=env,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                tool_call_id=tool_call_id,
            )
            return _parse_job_subagent_completed(
                completed=completed,
                parent_session_id=parent_session_id,
                worker_sub_id=sub_id,
            )
        finally:
            from dataagent.actions.tools.local_tool.sandbox import reset_current_sandbox

            reset_current_sandbox(token)
            if initial_state_file is not None:
                with contextlib.suppress(OSError):
                    initial_state_file.unlink()
                with contextlib.suppress(OSError):
                    initial_state_file.parent.rmdir()

    async def _run_with_cancel(
        self,
        *,
        cmd: list[str],
        timeout: int,
        env: dict[str, str],
        cancel_event: Event | None,
        progress_callback: Any,
        tool_call_id: str | None,
    ) -> dict[str, Any]:
        """Run subprocess and honour ``cancel_event`` while the child is running."""
        if cancel_event is None:
            try:
                return await _run_subprocess_async(
                    cmd,
                    timeout=timeout,
                    env=env,
                    progress_callback=progress_callback,
                    tool_call_id=tool_call_id,
                )
            except TimeoutError:
                return {"stdout": "", "stderr": "subagent subprocess timed out", "returncode": -1}

        if progress_callback and tool_call_id:
            return await _run_cancellable_subprocess_async(
                cmd=cmd,
                timeout=timeout,
                env=env,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                tool_call_id=tool_call_id,
            )

        return await _run_cancellable_subprocess_async(
            cmd=cmd,
            timeout=timeout,
            env=env,
            cancel_event=cancel_event,
            progress_callback=None,
            tool_call_id=None,
        )


async def _run_cancellable_subprocess_async(
    *,
    cmd: list[str],
    timeout: int,
    env: dict[str, str],
    cancel_event: Event,
    progress_callback: Any,
    tool_call_id: str | None,
) -> dict[str, Any]:
    """Execute a subprocess and terminate it when ``cancel_event`` is set or timeout elapses."""
    if progress_callback and tool_call_id:
        try:
            return await _run_subprocess_async(
                cmd,
                timeout=timeout,
                env=env,
                progress_callback=progress_callback,
                tool_call_id=tool_call_id,
            )
        except TimeoutError:
            return {"stdout": "", "stderr": "subagent subprocess timed out", "returncode": -1}

    sandbox = get_current_sandbox()
    original_cmd = cmd
    wrapped_cmd = sandbox.wrap_command(cmd, env=env)
    cwd = None
    if wrapped_cmd is not original_cmd:
        cwd = None

    process = await asyncio.create_subprocess_exec(
        *wrapped_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
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
                return {"stdout": "", "stderr": "subagent job cancelled", "returncode": -1}
            if time.monotonic() >= deadline:
                await terminate_process_tree_async(process)
                communicate_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate_task
                return {"stdout": "", "stderr": "subagent subprocess timed out", "returncode": -1}
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SEC)
        stdout_bytes, stderr_bytes = communicate_task.result()
    except asyncio.CancelledError:
        await terminate_process_tree_async(process)
        raise
    return {
        "stdout": stdout_bytes.decode("utf-8", errors="replace").strip(),
        "stderr": stderr_bytes.decode("utf-8", errors="replace").strip(),
        "returncode": process.returncode,
    }


def _prepare_job_initial_state_file(
    *,
    workspace_dir: Path,
    subagent_session_id: str,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    query: str,
    reuse_workspace: bool = False,
) -> Path:
    """Create an initial state file for a cold start or hydrated workspace reuse."""
    resolved_workspace = Path(workspace_dir).expanduser().resolve()
    messages_payload: list[dict[str, Any]] = []
    base_state: dict[str, Any] = {}
    next_run_id = 0
    if reuse_workspace:
        messages_payload, base_state, next_run_id = _load_job_workspace_hydrate_state(resolved_workspace)

    payload = {
        **base_state,
        "messages": messages_payload,
        "user_query": query,
        "complete": False,
        "user_id": user_id,
        "session_id": str(subagent_session_id or "").strip() or f"subagent_{parent_session_id}_{sub_id}",
        "run_id": int(next_run_id),
        "sub_id": int(sub_id),
        "workspace": str(resolved_workspace),
    }
    tmp_dir = resolved_workspace / ".runtime" / "job_state"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"initial_state_{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _load_job_workspace_hydrate_state(workspace_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Load persisted messages and snapshot fields from a job subagent workspace.

    Args:
        workspace_dir: Resolved subagent workspace root under ``subagents/{id}/``.

    Returns:
        Tuple of serialized messages, optional base state fields, and the next ``run_id``.
    """
    mem_dir = workspace_dir / ".memory"
    messages_path = mem_dir / "messages.json"
    messages = read_messages_file(messages_path) if messages_path.is_file() else []
    messages_payload = [serialize_message(message) for message in messages]

    base_state: dict[str, Any] = {}
    snapshot_path = mem_dir / "snapshot.json"
    if snapshot_path.is_file():
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            snapshot_raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(snapshot_raw, dict):
                snap = snapshot_raw.get("user_snapshot") if "user_snapshot" in snapshot_raw else snapshot_raw
                if isinstance(snap, dict):
                    for key in ("user_snapshot", "user_profile", "enable_portrait"):
                        if key in snapshot_raw:
                            base_state[key] = snapshot_raw[key]
                    prior_run_id = snap.get("run_id")
                    if isinstance(prior_run_id, int) and prior_run_id >= 0:
                        return messages_payload, base_state, prior_run_id + 1

    next_run_id = 1 if messages_payload else 0
    return messages_payload, base_state, next_run_id


def _parse_job_subagent_completed(
    *,
    completed: dict[str, Any],
    parent_session_id: str,
    worker_sub_id: int,
) -> JobSubagentOutcome:
    """Parse child stdout JSON without SWARM persistence side effects."""
    if int(completed.get("returncode") or 0) != 0 and not str(completed.get("stdout") or "").strip():
        message = str(completed.get("stderr") or "subagent subprocess failed").strip()
        failed = _synthetic_worker_result_dict(
            sub_id=worker_sub_id,
            parent_session_id=parent_session_id,
            status="failed",
            final_answer="",
            error=message,
        )
        return JobSubagentOutcome(
            original_msg=failed,
            frontend_msg=message,
            state=None,
            status="failed",
            error=message,
        )

    stdout = completed.get("stdout") or ""
    stripped = stdout.strip()
    parsed: Any = None
    if stripped:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

    if parsed is None:
        rc = int(completed.get("returncode") or 0)
        hint = stripped or str(completed.get("stderr") or "subagent stdout was not valid JSON")
        status = "failed" if rc != 0 else "completed"
        return JobSubagentOutcome(
            original_msg=hint,
            frontend_msg=hint,
            state=None,
            status=status,
            error="" if status == "completed" else hint,
        )

    if not isinstance(parsed, dict):
        hint = stripped
        return JobSubagentOutcome(original_msg=hint, frontend_msg=hint, state=None, status="completed")

    if parsed.get("error"):
        err = str(parsed.get("error"))
        msg = str(parsed.get("assistant_reply") or parsed.get("frontend_msg") or f"子 Agent 执行失败：{err}")
        failed = _synthetic_worker_result_dict(
            sub_id=worker_sub_id,
            parent_session_id=parent_session_id,
            status="failed",
            final_answer="",
            error=err,
        )
        return JobSubagentOutcome(
            original_msg=failed,
            frontend_msg=msg,
            state=None,
            status="failed",
            error=err,
        )

    worker_result_payload = parsed.get("worker_result")
    if isinstance(worker_result_payload, dict):
        worker_result = worker_result_from_payload(worker_result_payload)
        flex_raw = parsed.get("subagent_final_state")
        if flex_raw is None:
            flex_raw = parsed.get("original_msg", "")
        flex_state = _coerce_flex_state_dict_from_payload(flex_raw)
        assistant_reply = (
            str(parsed.get("assistant_reply") or parsed.get("frontend_msg") or "").strip() or worker_result.final_answer
        )
        return JobSubagentOutcome(
            original_msg=worker_result.to_dict(),
            frontend_msg=assistant_reply,
            state=flex_state,
            status="completed",
        )

    assistant_reply = str(parsed.get("assistant_reply") or parsed.get("frontend_msg") or "").strip()
    flex_raw = parsed.get("subagent_final_state")
    if flex_raw is None:
        flex_raw = parsed.get("original_msg", "")
    flex_state = _coerce_flex_state_dict_from_payload(flex_raw)
    wr = _synthetic_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=parent_session_id,
        status="success",
        final_answer=assistant_reply,
        error=None,
        resumed=False,
    )
    return JobSubagentOutcome(
        original_msg=wr,
        frontend_msg=assistant_reply or wr["final_answer"],
        state=flex_state,
        status="completed",
    )


def derive_job_sub_id(subagent_session_id: str) -> int:
    """Derive a stable positive ``sub_id`` for ``sub_agent_entry`` from a session id."""
    digest = int.from_bytes(subagent_session_id.encode("utf-8"), "big", signed=False)
    return int(digest % 900_000) + 100_000
