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
"""Quarantined subprocess bridge for recalls and pending Job migration."""

import asyncio
import contextlib
import contextvars
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.actions.tools.local_tool.agent_status_handler import (
    extract_subagent_status,
    reset_subagent_status,
)
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.common_utils.outbound_tls import ENV_PRESERVE_ON_MISSING
from dataagent.core.context.message_history import serialize_message
from dataagent.core.errors import DataAgentError
from dataagent.core.swarm.swarm_config import swarm_enabled
from dataagent.core.swarm.worker_lock import acquire_worker_lock, release_worker_lock
from dataagent.core.swarm.worker_memory import (
    load_worker_messages,
    load_worker_subagent_state,
    persist_worker_messages,
    persist_worker_state,
    worker_has_persisted_assets,
)
from dataagent.core.swarm.worker_metadata import compute_next_worker_run_id, upsert_worker_metadata
from dataagent.core.swarm.worker_result import (
    build_busy_result,
    build_timeout_result,
    parse_subagent_stdout,
)
from dataagent.core.swarm.worker_result import (
    worker_session_id as compute_worker_session_id,
)
from dataagent.core.utils.subprocess import terminate_process_tree_async
from dataagent.utils.constants import (
    DEFAULT_SESSION_ID,
    DEFAULT_SUBAGENT_PROCESS_TIMEOUT,
    DEFAULT_USER_ID,
    WORKER_LOCK_TTL_GRACE_SECONDS,
)
from dataagent.utils.log.dataagent_logger import get_log_context
from dataagent.utils.runtime_paths import resolve_user_root

_subagent_runtime_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "subagent_runtime_context",
    default=None,
)
_PROCESS_TIMEOUT_CLEANUP_SECONDS = 1


def set_subagent_runtime_context(
    *,
    user_id: str | None,
    session_id: str | None,
    sub_id: int | None,
    parent_user_query: str | None = None,
    run_id: int | None = None,
    progress_callback: Any | None = None,  # Callable[[str, str], None]
    tool_call_id: str | None = None,
    agent_config: dict[str, Any] | None = None,
    parent_workspace: str | Path | None = None,
    otel_config: dict[str, Any] | None = None,
) -> contextvars.Token:
    """Set per-tool-call runtime identity for sub-agent launching."""
    return _subagent_runtime_context.set(
        {
            "user_id": None if user_id is None else str(user_id).strip(),
            "session_id": None if session_id is None else str(session_id).strip(),
            "sub_id": sub_id,
            "parent_user_query": None if parent_user_query is None else str(parent_user_query),
            "run_id": run_id,
            "progress_callback": progress_callback,
            "tool_call_id": tool_call_id,
            "agent_config": dict(agent_config) if isinstance(agent_config, dict) else {},
            "parent_workspace": None if parent_workspace is None else str(parent_workspace),
            "otel_config": otel_config,
        }
    )


def _subagent_agent_config() -> dict[str, Any]:
    """Return per-agent config from the pending subagent runtime context."""
    ctx = _subagent_runtime_context.get()
    if not isinstance(ctx, dict):
        return {}
    agent_cfg = ctx.get("agent_config")
    return agent_cfg if isinstance(agent_cfg, dict) else {}


@dataclass
class _SubagentCompletedOutcome:
    """Parsed subprocess stdout before mapping to the internal recall result contract."""

    worker_result: dict[str, Any]
    agent_state: dict[str, Any] | None
    assistant_reply: str
    sub_id: int
    raw_stdout_for_llm: str | None = None


def _failed_worker_result_dict(
    *,
    sub_id: int,
    parent_session_id: str,
    message: str,
    source: str = "tool",
    status: str = "failed",
) -> dict[str, Any]:
    """Build a failed ``worker_result`` dict with structured public error."""
    error = DataAgentError(
        source=source,
        fact=f"{message}；sub_id={int(sub_id)}",
        component="subagent",
    )
    sid = int(sub_id)
    return {
        "sub_id": sid,
        "parent_session_id": parent_session_id,
        "worker_session_id": compute_worker_session_id(parent_session_id, sid),
        "status": status,
        "final_answer": "",
        "artifacts": [],
        "tool_calls_count": 0,
        "iteration_count": 0,
        "error": error.to_dict(),
        "resumed": False,
    }


def _coerce_agent_state_dict_from_payload(raw: Any) -> dict[str, Any] | None:
    """Parse ``subagent_final_state`` into a final agent-state dictionary."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        structured = _extract_structured_json(raw)
        return structured if isinstance(structured, dict) else None
    return None


def _handle_subagent_completed(
    *,
    completed: dict[str, Any],
    resolved_user_id: str,
    resolved_session_id: str,
    worker_sub_id: int,
    cfg_path: Path,
    query: str,
    last_run_id_executed: int,
) -> _SubagentCompletedOutcome:
    """Parse child stdout JSON, persist swarm assets when applicable, return structured outcome.

    Timeout is classified by the caller via ``TimeoutError`` / ``timed_out`` before this
    function runs. Stderr is logged for diagnosis and never used to guess error codes.
    """
    stderr = completed.get("stderr") or ""
    if str(stderr).strip():
        logger.debug(f"[subagent:{worker_sub_id}] stderr captured ({len(stderr)} chars)")
        for line in str(stderr).strip().splitlines()[:30]:
            logger.debug(f"[subagent:{worker_sub_id}] {line}")
        truncated = str(stderr).strip().splitlines()
        if len(truncated) > 30:
            logger.debug(f"[subagent:{worker_sub_id}] ... ({len(truncated) - 30} more lines omitted)")

    if completed.get("timed_out"):
        timeout = int(completed.get("timeout") or 1)
        timeout_result = build_timeout_result(
            sub_id=worker_sub_id,
            parent_session_id=resolved_session_id,
            timeout=timeout,
        )
        wr_dict = timeout_result.to_dict()
        if swarm_enabled(_subagent_agent_config()):
            upsert_worker_metadata(
                user_id=resolved_user_id,
                parent_session_id=resolved_session_id,
                worker_session_id=timeout_result.worker_session_id,
                sub_id=worker_sub_id,
                config_path=os.fspath(cfg_path),
                query=query,
                worker_result=wr_dict,
                status="timeout",
                error=timeout_result.error.fact if timeout_result.error else None,
                last_run_id_executed=int(last_run_id_executed),
            )
        return _SubagentCompletedOutcome(
            wr_dict,
            None,
            timeout_result.error.fact if timeout_result.error else "timeout",
            worker_sub_id,
        )

    parsed = parse_subagent_stdout(
        str(completed.get("stdout") or ""),
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        returncode=int(completed.get("returncode") or 0),
    )
    wr_dict = parsed.worker_result.to_dict()
    persistence = None
    stdout = str(completed.get("stdout") or "").strip()
    if stdout:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            raw = json.loads(stdout)
            if isinstance(raw, dict):
                persistence = raw.get("worker_persistence")
    if parsed.from_child_payload:
        _apply_worker_persistence(
            user_id=resolved_user_id,
            parent_session_id=resolved_session_id,
            sub_id=worker_sub_id,
            worker_session_id=parsed.worker_result.worker_session_id,
            config_path=os.fspath(cfg_path),
            query=query,
            worker_result=wr_dict,
            worker_persistence=persistence,
            last_run_id_executed=int(last_run_id_executed),
        )
        return _SubagentCompletedOutcome(
            wr_dict,
            _coerce_agent_state_dict_from_payload(parsed.final_state_raw),
            parsed.assistant_reply,
            int(parsed.worker_result.sub_id),
        )

    return _SubagentCompletedOutcome(
        wr_dict,
        None,
        parsed.assistant_reply or (parsed.worker_result.error.fact if parsed.worker_result.error else "failed"),
        worker_sub_id,
        raw_stdout_for_llm=parsed.raw_stdout,
    )


def _subagent_outcome_to_public_tool_dict(outcome: _SubagentCompletedOutcome) -> dict[str, Any]:
    """Map a parsed subprocess outcome to the internal recall result contract.

    Failed worker results raise ``DataAgentError`` so LocalToolWrapper produces a failed ToolResult.
    """
    worker_result = outcome.worker_result if isinstance(outcome.worker_result, dict) else None
    if worker_result is not None and worker_result.get("status") != "success":
        raw_error = worker_result.get("error")
        if isinstance(raw_error, dict):
            raise DataAgentError.from_dict(raw_error)
        raise DataAgentError(
            source="tool",
            component="subagent",
            fact="子 Agent 失败但缺少 error",
        )
    if outcome.raw_stdout_for_llm is not None:
        raise DataAgentError(
            source="internal",
            fact=str(outcome.assistant_reply or "invalid subagent stdout"),
            component="subagent",
        )
    return {
        "original_msg": outcome.worker_result,
        "frontend_msg": outcome.assistant_reply or str(outcome.worker_result.get("final_answer") or ""),
        "state": outcome.agent_state,
        "sub_id": outcome.sub_id,
    }


def reset_subagent_runtime_context(token: contextvars.Token) -> None:
    """Reset per-tool-call runtime identity for sub-agent launching."""
    _subagent_runtime_context.reset(token)


def get_subagent_runtime_context() -> dict[str, Any]:
    """Get the current sub-agent runtime identity context."""
    context = _subagent_runtime_context.get()
    return dict(context) if isinstance(context, dict) else {}


def _resolve_tool_file_path(path_value: str, arg_name: str) -> str:
    guard = get_current_sandbox()
    normalized_path = str(path_value or "").strip()
    if not normalized_path:
        raise ValueError(f"{arg_name} must not be empty.")
    if "\n" in normalized_path or "\r" in normalized_path:
        raise ValueError(f"{arg_name} must be a file path, not inline table content.")
    aliased_path = guard.resolve_prompt_path_alias(normalized_path)
    if aliased_path is not None:
        return str(aliased_path.resolve())
    return str(guard.resolve_requested_path(normalized_path, guard.workspace_root))


def _resolve_and_authorize(
    path_value: str,
    arg_name: str,
    *,
    operation: str,
    mode: str = "read",
) -> Path:
    """Resolve a user-supplied path and authorize the access in one step."""
    guard = get_current_sandbox()
    p = Path(_resolve_tool_file_path(path_value, arg_name))
    if mode == "write":
        guard.authorize_write(p, operation=operation)
    else:
        guard.authorize_read(p, operation=operation)
    return p


async def _terminate_process_tree_async(process: asyncio.subprocess.Process) -> None:
    """Backward-compatible alias for :func:`~dataagent.core.utils.subprocess.terminate_process_tree_async`."""
    await terminate_process_tree_async(process)


async def _wait_for_subprocess_async(
    awaitable: Any,
    process: asyncio.subprocess.Process,
    *,
    timeout: int | float,
) -> Any:
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()

    await _terminate_process_tree_async(process)
    done, _ = await asyncio.wait({task}, timeout=_PROCESS_TIMEOUT_CLEANUP_SECONDS)
    if task not in done:
        task.cancel()
        logger.warning("Timed out while cleaning up subprocess after timeout: pid={}", process.pid)
    raise TimeoutError


async def _run_subprocess_async(
    cmd: list[str],
    *,
    timeout: int,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    progress_callback=None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    sandbox = get_current_sandbox()
    original_cmd = cmd
    cmd = sandbox.wrap_command(cmd, cwd=cwd, env=env)
    if cmd is not original_cmd:
        cwd = None  # cwd handled by bwrap --chdir
        logger.debug("[sandbox] wrapped cmd: {}", cmd[:5])

    # Windows: CREATE_NEW_PROCESS_GROUP enables process-tree termination via taskkill /T.
    # CREATE_NO_WINDOW suppresses the flashing console window on Windows.
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    try:
        if progress_callback and tool_call_id:
            # 实时读取 stderr 推送进度，同时避免超长无换行输出触发 StreamReader.readline() 限制。
            stdout_stream = process.stdout
            stderr_stream = process.stderr
            if stdout_stream is None or stderr_stream is None:
                raise RuntimeError("Subprocess stdout/stderr pipes are not available.")
            # 64KiB: 常见 pipe 读取分块，平衡吞吐/调度开销并避免 readline() 单行长度限制问题
            chunk_size = 64 * 1024
            stdout_chunks: list[bytes] = []
            stderr_lines: list[str] = []

            async def _drain_stdout() -> None:
                while True:
                    chunk = await stdout_stream.read(chunk_size)
                    if not chunk:
                        break
                    stdout_chunks.append(chunk)

            async def _drain_stderr() -> None:
                pending = b""
                while True:
                    chunk = await stderr_stream.read(chunk_size)
                    if not chunk:
                        break
                    pending += chunk
                    parts = pending.split(b"\n")
                    pending = parts.pop()
                    for line in parts:
                        decoded = line.decode("utf-8", errors="replace")
                        extract_subagent_status(decoded, tool_call_id, progress_callback)
                        stderr_lines.append(decoded)

                if pending:
                    decoded = pending.decode("utf-8", errors="replace")
                    extract_subagent_status(decoded, tool_call_id, progress_callback)
                    stderr_lines.append(decoded)

            await _wait_for_subprocess_async(
                asyncio.gather(_drain_stdout(), _drain_stderr(), process.wait()),
                process,
                timeout=timeout,
            )

            stdout_bytes = b"".join(stdout_chunks)
            stderr_bytes = "\n".join(stderr_lines).encode("utf-8")
        else:
            # 无回调：保持原有的阻塞式读取
            stdout_bytes, stderr_bytes = await _wait_for_subprocess_async(
                process.communicate(),
                process,
                timeout=timeout,
            )
    finally:
        # 清理去重缓存和各 handler 的 per-tool-call 状态
        if tool_call_id:
            reset_subagent_status(tool_call_id)

    return {
        "stdout": stdout_bytes.decode("utf-8", errors="replace").strip(),
        "stderr": stderr_bytes.decode("utf-8", errors="replace").strip(),
        "returncode": process.returncode,
    }


def _extract_structured_json(raw: Any) -> dict[str, Any] | None:
    """Extract structured JSON object from model/sub-agent output.

    Supports dict passthrough, plain JSON text, and markdown fenced json blocks.
    """
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        logger.warning(f"Unexpected error during JSON parsing: {e}")

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        block = fenced.group(1).strip()
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except Exception as e:
            logger.warning(f"Unexpected error during JSON parsing: {e}")

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        candidate = match.group(0)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None

    return None


def _resolve_subagent_identity() -> tuple[str, str, dict[str, Any]]:
    """Resolve ``user_id`` / ``session_id`` and return the subagent runtime context dict.

    Emits log warnings and substitutes parent-agent defaults when either identifier is missing
    from ``get_subagent_runtime_context()``.
    """
    subagent_context = get_subagent_runtime_context()
    raw_user_id = subagent_context.get("user_id")
    raw_session_id = subagent_context.get("session_id")
    resolved_user_id = str(raw_user_id).strip() if raw_user_id is not None else ""
    resolved_session_id = str(raw_session_id).strip() if raw_session_id is not None else ""
    default_user_id = DEFAULT_USER_ID
    default_session_id = DEFAULT_SESSION_ID
    if not resolved_user_id:
        logger.warning(
            "invoke_subagent_process: runtime context 缺少 user_id，已回退为默认值 %r（与主 Agent 默认一致）",
            default_user_id,
        )
        resolved_user_id = default_user_id
    if not resolved_session_id:
        logger.warning(
            "invoke_subagent_process: runtime context 缺少 session_id，已回退为默认值 %r（与主 Agent 默认一致）",
            default_session_id,
        )
        resolved_session_id = default_session_id
    return resolved_user_id, resolved_session_id, subagent_context


async def _sub_agent_run_subprocess_and_collect_outcome(
    *,
    query: str,
    cfg_path: Path,
    resolved_user_id: str,
    resolved_session_id: str,
    worker_sub_id: int,
    swarm_on: bool,
    reuse_worker_state: bool,
    next_run_id: int,
    parent_user_query: str | None,
    timeout: int,
    progress_callback: Any,
    tool_call_id: str | None,
) -> _SubagentCompletedOutcome:
    """Create initial state, run the sub-agent subprocess, and parse stdout into an outcome."""
    initial_state_file: Path | None = None
    try:
        initial_state_file = _prepare_worker_initial_state_file(
            user_id=resolved_user_id,
            parent_session_id=resolved_session_id,
            sub_id=worker_sub_id,
            query=query,
            swarm_on=swarm_on,
            reuse_worker_state=reuse_worker_state,
            next_run_id=next_run_id,
            parent_user_query=parent_user_query,
        )
        env = dict(os.environ)
        sub_agent_session_id = f"subagent_{resolved_session_id}_{worker_sub_id}"
        sub_agent_log_path = (
            resolve_user_root(user_id=resolved_user_id) / "logs" / f"{sub_agent_session_id}.log"
        ).resolve()
        env["DATAAGENT_LOG_FILE"] = str(sub_agent_log_path)
        env["DATAAGENT_LOG_PROCESS_NAME"] = "subagent"
        env[ENV_PRESERVE_ON_MISSING] = "1"
        cmd = [
            sys.executable,
            "-m",
            "dataagent.actions.tools.local_tool.sub_agent_entry",
            "--query",
            query,
            "--config",
            os.fspath(cfg_path),
            "--user-id",
            resolved_user_id,
            "--session-id",
            resolved_session_id,
            "--sub-id",
            str(worker_sub_id),
            "--initial-state-file",
            str(initial_state_file),
        ]
        parent_trace_id = get_log_context().get("trace_id") or uuid.uuid4().hex
        cmd.extend(["--trace-id", str(parent_trace_id)])
        completed = await _run_subprocess_async(
            cmd,
            timeout=timeout,
            env=env,
            progress_callback=progress_callback,
            tool_call_id=tool_call_id,
        )
        return _handle_subagent_completed(
            completed=completed,
            resolved_user_id=resolved_user_id,
            resolved_session_id=resolved_session_id,
            worker_sub_id=worker_sub_id,
            cfg_path=cfg_path,
            query=query,
            last_run_id_executed=next_run_id,
        )
    finally:
        if initial_state_file is not None:
            with contextlib.suppress(OSError):
                initial_state_file.unlink()
            with contextlib.suppress(OSError):
                initial_state_file.parent.rmdir()


def _subagent_timeout_payload(
    *,
    worker_sub_id: int,
    resolved_session_id: str,
    resolved_user_id: str,
    cfg_path: Path,
    query: str,
    timeout: int,
    swarm_on: bool,
    next_run_id: int,
) -> dict[str, Any]:
    """Build the tool dict and optional swarm metadata after a subprocess timeout."""
    timeout_result = build_timeout_result(sub_id=worker_sub_id, parent_session_id=resolved_session_id, timeout=timeout)
    if swarm_on:
        upsert_worker_metadata(
            user_id=resolved_user_id,
            parent_session_id=resolved_session_id,
            worker_session_id=timeout_result.worker_session_id,
            sub_id=worker_sub_id,
            config_path=os.fspath(cfg_path),
            query=query,
            worker_result=timeout_result.to_dict(),
            status="timeout",
            error=timeout_result.error.fact if timeout_result.error else None,
            last_run_id_executed=int(next_run_id),
        )
    msg = f"子 Agent 执行超时（>{timeout} 秒），已终止子进程。"
    return {
        "original_msg": timeout_result.to_dict(),
        "frontend_msg": msg,
        "state": None,
        "sub_id": worker_sub_id,
    }


def _subagent_startup_failure_payload(
    *,
    worker_sub_id: int,
    resolved_session_id: str,
    exc: Exception,
) -> dict[str, Any]:
    """Build the tool dict when the sub-agent subprocess fails to run or is interrupted."""
    msg = f"子 Agent 启动失败：{exc}"
    wr = _failed_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        source="tool",
        message=str(exc),
    )
    return {
        "original_msg": wr,
        "frontend_msg": msg,
        "state": None,
        "sub_id": worker_sub_id,
    }


async def invoke_subagent_process(
    query: str,
    config_path: str | Path,
    sub_id: int | None = None,
    timeout: int = DEFAULT_SUBAGENT_PROCESS_TIMEOUT,
) -> dict[str, Any]:
    """
    Starts a sub Agent in a separate subprocess and returns the result of a single-turn chat.

    Args:
      - query: User query to be passed to the sub Agent.
      - config_path: Absolute path under the workspace root to the sub Agent YAML config
        (same path rules as ``read_file``).
      - sub_id: Optional worker folder id. When omitted, the parent allocates a random id.
        When the model supplies an id, that id is always used as the worker folder name
        (cold start when ``workers/<sub_id>/`` has no persisted assets yet; hydrate history
        when swarm is enabled and ``.memory`` artifacts exist).
      - timeout: Timeout in seconds for the subprocess execution (default: __SUBAGENT_PROCESS_TIMEOUT__).

    Returns:
      - ``original_msg``: ``worker_result`` dict (JSON in ToolMessage) for the planner.
      - ``frontend_msg``: Subagent-facing answer text (``final_answer`` / ``assistant_reply``).
      - ``state``: Final graph-state dict for programmatic wrappers; may be ``None``.
      - ``sub_id``: Allocated or reused worker folder id.

      On subprocess stdout JSON parse failure, ``original_msg`` is the raw stdout string (warning logged).
    """
    # Path 入参视为内部调用（已解析的包内固定配置），跳过工作区白名单校验；
    # str 入参为模型/用户输入，仍走 _resolve_and_authorize。
    cfg_path = (
        config_path
        if isinstance(config_path, Path)
        else _resolve_and_authorize(config_path, "config_path", operation="invoke_subagent_process", mode="read")
    )
    if not cfg_path.is_file():
        raise FileNotFoundError(f"子 Agent 配置文件不存在: {cfg_path}")

    resolved_user_id, resolved_session_id, subagent_context = _resolve_subagent_identity()
    worker_path_kwargs = _subagent_worker_path_kwargs()

    swarm_on = swarm_enabled(_subagent_agent_config())
    worker_sub_id = _resolve_worker_sub_id_for_call(
        user_id=resolved_user_id,
        parent_session_id=resolved_session_id,
        requested_sub_id=sub_id,
        swarm_on=swarm_on,
    )
    reuse_worker_state = swarm_on and worker_has_persisted_assets(
        user_id=resolved_user_id,
        parent_session_id=resolved_session_id,
        sub_id=worker_sub_id,
        **worker_path_kwargs,
    )
    if swarm_on and sub_id is not None and not reuse_worker_state:
        logger.warning(
            "invoke_subagent_process: requested sub_id={} has no persisted worker assets under "
            "workers/<sub_id>/.memory/; cold-starting a new worker in this folder.",
            worker_sub_id,
        )
    next_run_id = compute_next_worker_run_id(
        user_id=resolved_user_id,
        parent_session_id=resolved_session_id,
        sub_id=worker_sub_id,
        reuse_worker_state=reuse_worker_state,
    )
    parent_run_id = subagent_context.get("run_id")
    if parent_run_id is not None and not swarm_on:
        next_run_id = int(parent_run_id)
    lock = None
    if swarm_on:
        lock = acquire_worker_lock(
            user_id=resolved_user_id,
            parent_session_id=resolved_session_id,
            sub_id=worker_sub_id,
            query=query,
            ttl_seconds=int(timeout) + WORKER_LOCK_TTL_GRACE_SECONDS,
            **worker_path_kwargs,
        )
        if lock is None:
            busy_result = build_busy_result(sub_id=worker_sub_id, parent_session_id=resolved_session_id)
            raise busy_result.error or DataAgentError(source="tool", component="subagent")

    try:
        outcome = await _sub_agent_run_subprocess_and_collect_outcome(
            query=query,
            cfg_path=cfg_path,
            resolved_user_id=resolved_user_id,
            resolved_session_id=resolved_session_id,
            worker_sub_id=worker_sub_id,
            swarm_on=swarm_on,
            reuse_worker_state=reuse_worker_state,
            next_run_id=next_run_id,
            parent_user_query=subagent_context.get("parent_user_query"),
            timeout=timeout,
            progress_callback=subagent_context.get("progress_callback"),
            tool_call_id=subagent_context.get("tool_call_id"),
        )
        return _subagent_outcome_to_public_tool_dict(outcome)
    except TimeoutError:
        _subagent_timeout_payload(
            worker_sub_id=worker_sub_id,
            resolved_session_id=resolved_session_id,
            resolved_user_id=resolved_user_id,
            cfg_path=cfg_path,
            query=query,
            timeout=timeout,
            swarm_on=swarm_on,
            next_run_id=next_run_id,
        )
        raise
    except DataAgentError:
        raise
    except Exception as e:  # pragma: no cover - 极端系统错误
        payload = _subagent_startup_failure_payload(
            worker_sub_id=worker_sub_id,
            resolved_session_id=resolved_session_id,
            exc=e,
        )
        error_payload = (
            payload.get("original_msg", {}).get("error") if isinstance(payload.get("original_msg"), dict) else None
        )
        if isinstance(error_payload, dict):
            raise DataAgentError.from_dict(error_payload) from e
        raise DataAgentError(source="tool", component="subagent") from e
    finally:
        if lock is not None:
            release_worker_lock(lock)


def _subagent_worker_path_kwargs() -> dict[str, Any]:
    """Return ``parent_workspace`` / ``config`` for swarm worker path helpers."""
    ctx = get_subagent_runtime_context()
    agent_cfg = _subagent_agent_config()
    kwargs: dict[str, Any] = {"config": agent_cfg or None}
    parent_workspace = ctx.get("parent_workspace") if isinstance(ctx, dict) else None
    if parent_workspace:
        kwargs["parent_workspace"] = parent_workspace
    return kwargs


def _allocate_unique_worker_sub_id(*, user_id: str, parent_session_id: str) -> int:
    """Return a random 6-digit worker id whose session folder does not yet exist."""
    from dataagent.utils.runtime_paths import resolve_worker_root

    path_kwargs = _subagent_worker_path_kwargs()
    for _ in range(100):
        candidate = int.from_bytes(os.urandom(4), "big") % 900000 + 100000
        if not resolve_worker_root(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=candidate,
            **path_kwargs,
        ).exists():
            return candidate
    raise RuntimeError("Unable to generate a unique subagent id.")


def _resolve_worker_sub_id_for_call(
    *,
    user_id: str,
    parent_session_id: str,
    requested_sub_id: int | None,
    swarm_on: bool,
) -> int:
    """Resolve the worker folder id for one internal subprocess invocation.

    If the model omits ``sub_id``, allocate a random unused folder id. If it supplies one,
    always use that integer (new worker when nothing is persisted yet under ``workers/<sub_id>/``).
    """
    if not swarm_on:
        if requested_sub_id is not None:
            return int(requested_sub_id)
        return _allocate_unique_worker_sub_id(user_id=user_id, parent_session_id=parent_session_id)

    if requested_sub_id is None:
        return _allocate_unique_worker_sub_id(user_id=user_id, parent_session_id=parent_session_id)

    return int(requested_sub_id)


def _prepare_worker_initial_state_file(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    query: str,
    swarm_on: bool,
    reuse_worker_state: bool,
    next_run_id: int,
    parent_user_query: str | None = None,
) -> Path:
    """Create a workspace-visible initial-state file readable by the child process.

    When swarm mode is enabled and ``reuse_worker_state`` is true, the payload merges
    disk-backed ``subagent_state.json`` plus serialized ``messages``. The parent also
    injects identity seeds (including ``run_id``) so children never resurrect stale
    ``run_id`` values from historical snapshots alone.

    The caller deletes the returned path after launching the subprocess.
    """
    base_state: dict[str, Any] = {}
    messages_payload: list[dict[str, Any]] = []
    if swarm_on and reuse_worker_state:
        path_kwargs = _subagent_worker_path_kwargs()
        base_state = load_worker_subagent_state(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=sub_id,
            **path_kwargs,
        )
        messages = (
            load_worker_messages(
                user_id=user_id,
                parent_session_id=parent_session_id,
                sub_id=sub_id,
                **path_kwargs,
            )
            or []
        )
        messages_payload = [serialize_message(message) for message in messages]

    worker_sess = compute_worker_session_id(parent_session_id, sub_id)
    payload = {
        **base_state,
        "messages": messages_payload,
        "user_query": query,
        "complete": False,
        "user_id": user_id,
        "session_id": worker_sess,
        "run_id": int(next_run_id),
        "sub_id": int(sub_id),
        "parent_user_query": str(parent_user_query or ""),
        "_parent_session_id": parent_session_id,
        "_parent_run_id": int(next_run_id),
    }
    # Propagate OTel config so the sub-agent creates its own OtelEventRecorder
    ctx = get_subagent_runtime_context()
    otel_config = ctx.get("otel_config") if isinstance(ctx, dict) else None
    if otel_config:
        payload["__otel_config"] = otel_config
    workspace_root = get_current_sandbox().workspace_root
    if workspace_root is not None:
        tmp_dir = Path(workspace_root) / ".dataagent_tmp" / "subagents" / uuid.uuid4().hex
        tmp_dir.mkdir(parents=True, exist_ok=False)
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="dataagent_subagent_state_"))
    path = tmp_dir / f"initial_state_{sub_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _apply_worker_persistence(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    worker_session_id: str,
    config_path: str,
    query: str,
    worker_result: dict[str, Any],
    worker_persistence: Any,
    last_run_id_executed: int,
) -> None:
    """Apply child-provided persistence data when ``SWARM.enable`` is true.

    Messages and ``subagent_state.json`` are overwritten in full after each successful
    subprocess that reaches this helper; ``metadata.json`` records the executed
    ``last_run_id`` alongside planner-facing hints.
    """
    if not swarm_enabled(_subagent_agent_config()):
        return
    path_kwargs = _subagent_worker_path_kwargs()
    payload = worker_persistence if isinstance(worker_persistence, dict) else {}
    messages = payload.get("messages")
    if isinstance(messages, list):
        persist_worker_messages(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=sub_id,
            messages=messages,
            **path_kwargs,
        )
    state = payload.get("state")
    persist_worker_state(
        user_id=user_id,
        parent_session_id=parent_session_id,
        sub_id=sub_id,
        state=state if isinstance(state, dict) else {},
        **path_kwargs,
    )
    upsert_worker_metadata(
        user_id=user_id,
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id,
        sub_id=sub_id,
        config_path=config_path,
        query=query,
        worker_result=worker_result,
        status=str(worker_result.get("status") or "failed"),
        error=worker_result.get("error"),
        last_run_id_executed=int(last_run_id_executed),
        **path_kwargs,
    )


# Keep the callable description synchronized with its runtime default.
invoke_subagent_process.__doc__ = (invoke_subagent_process.__doc__ or "").replace(
    "__SUBAGENT_PROCESS_TIMEOUT__", str(DEFAULT_SUBAGENT_PROCESS_TIMEOUT)
)


def _resolve_bound_llm_model_name(*, tool_config: dict[str, Any] | None = None) -> str | None:
    """Resolve the MODEL registry key from the bound tool configuration."""
    bound = str((tool_config or {}).get("llm_model") or "").strip()
    return bound or None
