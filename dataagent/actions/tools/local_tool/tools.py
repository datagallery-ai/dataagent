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
import asyncio
import contextlib
import contextvars
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.agent_status_handler import (
    extract_subagent_status,
    reset_subagent_status,
)
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.sql_reader import load_table
from dataagent.core.context.message_history import serialize_message
from dataagent.core.managers.llm_manager import llm_manager
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
    worker_result_from_payload,
)
from dataagent.core.swarm.worker_result import (
    worker_session_id as compute_worker_session_id,
)
from dataagent.utils.constants import (
    DEFAULT_BASH_TIMEOUT,
    DEFAULT_DIFF_MAX_CHARS,
    DEFAULT_GLOB_MAX_RESULTS,
    DEFAULT_GREP_HEAD_LIMIT,
    DEFAULT_GREP_TIMEOUT,
    DEFAULT_READ_MAX_FILE_SIZE,
    DEFAULT_READ_MAX_OUTPUT_BYTES,
    DEFAULT_SESSION_ID,
    DEFAULT_SKIP_DIRS,
    DEFAULT_SUBAGENT_TOOL_TIMEOUT,
    DEFAULT_USER_ID,
    WORKER_LOCK_TTL_GRACE_SECONDS,
)
from dataagent.utils.fix_md_image_path import fix_markdown_image_paths, load_images_as_json
from dataagent.utils.formatting_utils import get_available_chinese_font
from dataagent.utils.runtime_paths import dataagent_package_root

_subagent_runtime_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "subagent_runtime_context",
    default=None,
)


def set_subagent_runtime_context(
    *,
    user_id: str | None,
    session_id: str | None,
    sub_id: int | None,
    progress_callback: Any | None = None,  # Callable[[str, str], None]
    tool_call_id: str | None = None,
    agent_config: dict[str, Any] | None = None,
) -> contextvars.Token:
    """Set per-tool-call runtime identity for sub-agent launching."""
    return _subagent_runtime_context.set(
        {
            "user_id": None if user_id is None else str(user_id).strip(),
            "session_id": None if session_id is None else str(session_id).strip(),
            "sub_id": sub_id,
            "progress_callback": progress_callback,
            "tool_call_id": tool_call_id,
            "agent_config": dict(agent_config) if isinstance(agent_config, dict) else {},
        }
    )


def _subagent_agent_config() -> dict[str, Any]:
    """Return per-Agent config from subagent contextvars (set by Flex executor)."""
    ctx = _subagent_runtime_context.get()
    if not isinstance(ctx, dict):
        return {}
    agent_cfg = ctx.get("agent_config")
    return agent_cfg if isinstance(agent_cfg, dict) else {}


@dataclass
class _SubagentCompletedOutcome:
    """Parsed subprocess stdout before mapping to planner-visible ``sub_agent_tool`` keys."""

    worker_result: dict[str, Any]
    flex_state: dict[str, Any] | None
    assistant_reply: str
    sub_id: int
    raw_stdout_for_llm: str | None = None


def _synthetic_worker_result_dict(
    *,
    sub_id: int,
    parent_session_id: str,
    status: str,
    final_answer: str,
    error: str | None,
    resumed: bool = False,
    artifacts: list[str] | None = None,
    tool_calls_count: int = 0,
    iteration_count: int = 0,
) -> dict[str, Any]:
    """Build a JSON-serializable ``worker_result``-shaped dict for ToolMessage payloads."""
    sid = int(sub_id)
    return {
        "sub_id": sid,
        "parent_session_id": parent_session_id,
        "worker_session_id": compute_worker_session_id(parent_session_id, sid),
        "status": status,
        "final_answer": final_answer,
        "artifacts": list(artifacts or []),
        "tool_calls_count": int(tool_calls_count),
        "iteration_count": int(iteration_count),
        "error": error,
        "resumed": bool(resumed),
    }


def _coerce_flex_state_dict_from_payload(raw: Any) -> dict[str, Any] | None:
    """Parse ``subagent_final_state`` / legacy ``original_msg`` into a Flex final-state dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        structured = _extract_structured_json(raw)
        return structured if isinstance(structured, dict) else None
    return None


def _subagent_completed_outcome_when_stdout_not_json(
    *,
    stripped: str,
    rc: int,
    stderr: str,
    worker_sub_id: int,
    resolved_session_id: str,
) -> _SubagentCompletedOutcome:
    """Return an outcome when the child wrote empty or non-JSON stdout."""
    if rc != 0:
        msg = f"子 Agent 子进程异常退出（code={rc}）。"
        if stderr:
            msg += f"\n错误输出：\n{stderr}"
        wr = _synthetic_worker_result_dict(
            sub_id=worker_sub_id,
            parent_session_id=resolved_session_id,
            status="failed",
            final_answer="",
            error=msg,
        )
        return _SubagentCompletedOutcome(wr, None, msg, worker_sub_id)
    if stripped:
        logger.warning("sub_agent stdout is not valid JSON; forwarding raw stdout to the model.")
        hint = "子 Agent stdout 不是合法 JSON；原始输出见工具结果正文。"
        wr = _synthetic_worker_result_dict(
            sub_id=worker_sub_id,
            parent_session_id=resolved_session_id,
            status="failed",
            final_answer="",
            error="invalid_subagent_stdout_json",
        )
        return _SubagentCompletedOutcome(wr, None, hint, worker_sub_id, raw_stdout_for_llm=stripped)
    msg = "子 Agent 执行完成，但未返回任何内容。"
    wr = _synthetic_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        status="failed",
        final_answer="",
        error=msg,
    )
    return _SubagentCompletedOutcome(wr, None, msg, worker_sub_id)


def _subagent_completed_outcome_when_top_level_not_object(
    *,
    stripped: str,
    worker_sub_id: int,
    resolved_session_id: str,
) -> _SubagentCompletedOutcome:
    """Return an outcome when parsed JSON exists but its top-level value is not an object."""
    logger.warning("sub_agent stdout JSON is not an object; forwarding raw stdout to the model.")
    hint = "子 Agent stdout JSON 顶层不是对象；原始输出见工具结果正文。"
    wr = _synthetic_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        status="failed",
        final_answer="",
        error="invalid_subagent_stdout_shape",
    )
    return _SubagentCompletedOutcome(wr, None, hint, worker_sub_id, raw_stdout_for_llm=stripped)


def _subagent_completed_outcome_when_payload_error(
    *,
    parsed: dict[str, Any],
    resolved_user_id: str,
    resolved_session_id: str,
    worker_sub_id: int,
    cfg_path: Path,
    query: str,
    last_run_id_executed: int,
) -> _SubagentCompletedOutcome:
    """Return an outcome when the child payload reports a top-level ``error`` field."""
    err = parsed.get("error")
    msg = parsed.get("assistant_reply") or parsed.get("frontend_msg") or f"子 Agent 执行失败：{err}"
    failed_result = _synthetic_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        status="failed",
        final_answer="",
        error=str(err),
    )
    if swarm_enabled(_subagent_agent_config()):
        upsert_worker_metadata(
            user_id=resolved_user_id,
            parent_session_id=resolved_session_id,
            worker_session_id=failed_result["worker_session_id"],
            sub_id=worker_sub_id,
            config_path=os.fspath(cfg_path),
            query=query,
            worker_result=failed_result,
            status="failed",
            error=str(err),
            last_run_id_executed=int(last_run_id_executed),
        )
    return _SubagentCompletedOutcome(failed_result, None, msg, worker_sub_id)


def _subagent_completed_outcome_from_worker_result_branch(
    *,
    parsed: dict[str, Any],
    worker_result_payload: dict[str, Any],
    resolved_user_id: str,
    resolved_session_id: str,
    worker_sub_id: int,
    cfg_path: Path,
    query: str,
    last_run_id_executed: int,
) -> _SubagentCompletedOutcome:
    """Return an outcome when the child included a structured ``worker_result`` object."""
    worker_result = worker_result_from_payload(worker_result_payload)
    flex_raw = parsed.get("subagent_final_state")
    if flex_raw is None:
        flex_raw = parsed.get("original_msg", "")
    flex_state = _coerce_flex_state_dict_from_payload(flex_raw)
    assistant_reply = (
        str(parsed.get("assistant_reply") or parsed.get("frontend_msg") or "").strip() or worker_result.final_answer
    )
    wr_dict = worker_result.to_dict()
    _apply_worker_persistence(
        user_id=resolved_user_id,
        parent_session_id=resolved_session_id,
        sub_id=worker_sub_id,
        worker_session_id=worker_result.worker_session_id,
        config_path=os.fspath(cfg_path),
        query=query,
        worker_result=wr_dict,
        worker_persistence=parsed.get("worker_persistence"),
        last_run_id_executed=int(last_run_id_executed),
    )
    return _SubagentCompletedOutcome(wr_dict, flex_state, assistant_reply, int(worker_result.sub_id))


def _subagent_completed_outcome_synthetic_success(
    *,
    parsed: dict[str, Any],
    worker_sub_id: int,
    resolved_session_id: str,
) -> _SubagentCompletedOutcome:
    """Return a success-shaped outcome when no ``worker_result`` object is present."""
    assistant_reply = str(parsed.get("assistant_reply") or parsed.get("frontend_msg") or "").strip()
    flex_raw = parsed.get("subagent_final_state")
    if flex_raw is None:
        flex_raw = parsed.get("original_msg", "")
    flex_state = _coerce_flex_state_dict_from_payload(flex_raw)
    wr = _synthetic_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        status="success",
        final_answer=assistant_reply,
        error=None,
        resumed=False,
    )
    return _SubagentCompletedOutcome(wr, flex_state, assistant_reply or wr["final_answer"], worker_sub_id)


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

    Internal keys mirror the subprocess protocol (``worker_result``, ``subagent_final_state``,
    ``assistant_reply``). ``original_msg`` / ``frontend_msg`` are assigned only in
    ``sub_agent_tool`` for ``LocalToolWrapper`` / Executor consumption.
    """
    stdout = completed.get("stdout") or ""
    stderr = completed.get("stderr") or ""
    rc = int(completed.get("returncode") or 0)
    stripped = stdout.strip()

    parsed: Any = None
    if stripped:
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None

    if parsed is None:
        return _subagent_completed_outcome_when_stdout_not_json(
            stripped=stripped,
            rc=rc,
            stderr=stderr,
            worker_sub_id=worker_sub_id,
            resolved_session_id=resolved_session_id,
        )

    if not isinstance(parsed, dict):
        return _subagent_completed_outcome_when_top_level_not_object(
            stripped=stripped,
            worker_sub_id=worker_sub_id,
            resolved_session_id=resolved_session_id,
        )

    if parsed.get("error"):
        return _subagent_completed_outcome_when_payload_error(
            parsed=parsed,
            resolved_user_id=resolved_user_id,
            resolved_session_id=resolved_session_id,
            worker_sub_id=worker_sub_id,
            cfg_path=cfg_path,
            query=query,
            last_run_id_executed=last_run_id_executed,
        )

    worker_result_payload = parsed.get("worker_result")
    if isinstance(worker_result_payload, dict):
        return _subagent_completed_outcome_from_worker_result_branch(
            parsed=parsed,
            worker_result_payload=worker_result_payload,
            resolved_user_id=resolved_user_id,
            resolved_session_id=resolved_session_id,
            worker_sub_id=worker_sub_id,
            cfg_path=cfg_path,
            query=query,
            last_run_id_executed=last_run_id_executed,
        )

    return _subagent_completed_outcome_synthetic_success(
        parsed=parsed,
        worker_sub_id=worker_sub_id,
        resolved_session_id=resolved_session_id,
    )


def _subagent_outcome_to_public_tool_dict(outcome: _SubagentCompletedOutcome) -> dict[str, Any]:
    """Map internal parse outcome to the Executor-facing ``sub_agent_tool`` return dict."""
    if outcome.raw_stdout_for_llm is not None:
        return {
            "original_msg": outcome.raw_stdout_for_llm,
            "frontend_msg": outcome.assistant_reply,
            "state": outcome.flex_state,
            "sub_id": outcome.sub_id,
        }
    return {
        "original_msg": outcome.worker_result,
        "frontend_msg": outcome.assistant_reply or str(outcome.worker_result.get("final_answer") or ""),
        "state": outcome.flex_state,
        "sub_id": outcome.sub_id,
    }


def reset_subagent_runtime_context(token: contextvars.Token) -> None:
    """Reset per-tool-call runtime identity for sub-agent launching."""
    _subagent_runtime_context.reset(token)


def get_subagent_runtime_context() -> dict[str, Any]:
    """Get the current sub-agent runtime identity context."""
    context = _subagent_runtime_context.get()
    return dict(context) if isinstance(context, dict) else {}


def llm_analyzer(text: str = "", task: str = "", output_path: str = "", text_path: str = "") -> dict[str, str]:
    """Performs task (e.g. semantic analysis) on text using LLM.

    Args:
        text (str): Input text to analyze.
        task (str): Task to perform.
        output_path (str): Absolute path under the workspace root to write the analysis result.
        text_path (str): Optional absolute workspace path (or ``skill/...``) to a file
            whose contents are analyzed instead of inline text.

    Returns:
        str: Saved analysis file path.
    """
    output_path = _resolve_tool_file_path(output_path, "output_path")
    analysis_input = (
        _load_analysis_content(text_path, allow_empty=True, arg_name="text_path") if text_path else str(text)
    )
    if not str(analysis_input or "").strip():
        raise ValueError("Either text or text_path must be provided.")
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    llm = llm_manager.get_default_llm()
    response = llm.invoke([{"role": "user", "content": f"Perform task on text: {task}\nText: {analysis_input}"}])
    total_tokens = response.usage_metadata["total_tokens"]
    result = response.content.split("</think>")[-1].strip()
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)
    return {
        "original_msg": output_path,
        "frontend_msg": f"\n\nllm_analyzer 工具执行完成\n\n分析结果已保存到：`{output_path}`",
        "tokens_used": total_tokens,
    }


def file_saver(content: str, file_path: str) -> dict[str, str]:
    """Saves text content to a file for later use.
    This tool must not overwrite or interfere with results saved by other tools.

    Args:
        content (str): Text content to save.
        file_path (str): Absolute path under the workspace root where the file is saved.
    """
    resolved_path = Path(_resolve_tool_file_path(file_path, "file_path"))
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(content, encoding="utf-8")
    return {"original_msg": "", "frontend_msg": f"File saved at {resolved_path}."}


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


def _build_shell_env() -> dict[str, str]:
    return dict(os.environ)


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

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
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

            try:
                await asyncio.wait_for(
                    asyncio.gather(_drain_stdout(), _drain_stderr()),
                    timeout=timeout,
                )
                await process.wait()
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise TimeoutError from exc

            stdout_bytes = b"".join(stdout_chunks)
            stderr_bytes = "\n".join(stderr_lines).encode("utf-8")
        else:
            # 无回调：保持原有的阻塞式读取
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise TimeoutError from exc
    finally:
        # 清理去重缓存和各 handler 的 per-tool-call 状态
        if tool_call_id:
            reset_subagent_status(tool_call_id)

    return {
        "stdout": stdout_bytes.decode("utf-8", errors="replace").strip(),
        "stderr": stderr_bytes.decode("utf-8", errors="replace").strip(),
        "returncode": process.returncode,
    }


def _expand_skill_aliases_in_shell_command(command: str) -> str:
    pattern = re.compile(r"(?<![\w.-])(skill/[A-Za-z0-9_.-]+(?:/[^\s;&|<>\"']*)?)")

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        sandbox = get_current_sandbox()
        resolved = sandbox.resolve_prompt_path_alias(token)
        return str(resolved) if resolved is not None else token

    return pattern.sub(_replace, command)


def _load_plot_source_dataframe(src_data_path: str) -> pd.DataFrame:
    normalized_source = str(src_data_path or "").strip()
    if not normalized_source:
        raise ValueError("src_data_path must not be empty.")

    if "\n" not in normalized_source and "\r" not in normalized_source:
        resolved_source_path = Path(_resolve_tool_file_path(normalized_source, "src_data_path"))
        if resolved_source_path.is_file():
            return pd.read_csv(resolved_source_path)
        raise FileNotFoundError(f"src_data_path does not exist: {resolved_source_path}")

    inline_source = normalized_source.replace("\r\n", "\n").replace("\r", "\n")
    try:
        df = pd.read_csv(StringIO(inline_source), sep=None, engine="python")
    except Exception:
        df = None
    if df is not None and not df.empty and len(df.columns) > 0:
        return df

    try:
        df = pd.read_csv(StringIO(inline_source), sep=r"\s+", engine="python")
    except Exception:
        df = None
    if df is not None and not df.empty and len(df.columns) > 0:
        return df

    raise ValueError("src_data_path must be a CSV file path or inline tabular text.")


def _load_analysis_content(path_value: str, allow_empty: bool = False, arg_name: str = "analysis_path") -> str:
    normalized_path = str(path_value or "").strip()
    if not normalized_path:
        if allow_empty:
            return ""
        raise ValueError(f"{arg_name} must not be empty.")
    resolved_analysis_path = Path(_resolve_tool_file_path(normalized_path, arg_name))
    if not resolved_analysis_path.is_file():
        raise FileNotFoundError(f"{arg_name} does not exist: {resolved_analysis_path}")
    return resolved_analysis_path.read_text(encoding="utf-8")


def _extract_sql_from_response(content: str) -> str:
    text = str(content or "").split("</think>")[-1].strip()
    for pattern in (r"```mysql\n([\s\S]*?)\n```", r"```sql\n([\s\S]*?)\n```", r"```\n([\s\S]*?)\n```"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    if text:
        return text
    raise ValueError("LLM did not return SQL content.")


def natural_language_to_plot(
    query: str,
    src_data_path: str,
    sql_command_path: str,
    script_path: str,
    image_path: str,
    json_path: str,
    analysis_path: str = "",
) -> dict[str, str]:
    """Converts natural language visualization requests to matplotlib code and executes it.

    Important: To ensure the uniqueness of image paths across different calls, it is mandatory to add a timestamp suffix
    to the image_path. This is a required measure; otherwise, the files will be overwritten.

    This function uses LLM to:
    1. generate self-contained matplotlib code for common plot types including line plots, bar charts,
    and scatter plots.
    2. Please save the generated results as a **JSON array file** at `json_path`. Each element in the array should
    be an object containing two keys: `image_path` and `description`. Each object represents a single,
    independent result. When new results are generated, they must be appended as new objects to the end of
    this array, and the whole array should be written back to the file (no JSONL, only a single JSON array).

    Args:
        query (str): Natural language description of desired visualization. Example:
            "Create a line plot showing sales trends over 12 months"
        src_data_path (str): Absolute path under the workspace to the source CSV (or inline tabular text as documented).
        sql_command_path (str): Absolute workspace path to a .sql file used to describe columns for plotting.
        When generating code, you must strictly use the column names defined in this SQL command, rather than any other
        assumed fields in the source file.
        script_path (str): Absolute workspace path for the generated Python script (.py extension recommended).
        image_path (str): Absolute workspace path for saving generated images (file or directory as used by the tool).
            Use a timestamp suffix to ensure distinct paths across repeated calls. For example
            "image_name_04151120.png" indicates the image was generated on April 15th at 11:20 AM.
        json_path (str): Absolute workspace path for the plot-description **JSON array**
            file; entries are merged across calls.
        analysis_path (str): Optional absolute workspace path to a saved analysis file to guide visualization code.

    Returns:
        str: JSON description of generated plots.
    """
    df = _load_plot_source_dataframe(src_data_path)
    data_preview = df.head().to_string()
    columns_info = ", ".join(df.columns)
    # Use resolved absolute path in prompt so generated code uses it and does not depend on cwd
    normalized_src = str(src_data_path or "").strip()
    if "\n" not in normalized_src and "\r" not in normalized_src:
        src_path_for_prompt = str(Path(_resolve_tool_file_path(normalized_src, "src_data_path")).resolve())
    else:
        src_path_for_prompt = "(inline data)"
    sql_command_path = _resolve_tool_file_path(sql_command_path, "sql_command_path")
    script_path = _resolve_tool_file_path(script_path, "script_path")
    image_path = _resolve_tool_file_path(image_path, "image_path")
    json_path = _resolve_tool_file_path(json_path, "json_path")
    with open(sql_command_path, encoding="utf-8") as f:
        sql_command = f.read()
    script_dir = os.path.dirname(script_path)
    if script_dir:
        os.makedirs(script_dir, exist_ok=True)
    image_dir = os.path.dirname(image_path)
    if image_dir:
        os.makedirs(image_dir, exist_ok=True)
    json_dir = os.path.dirname(json_path)
    if json_dir:
        os.makedirs(json_dir, exist_ok=True)
    llm = llm_manager.get_default_llm()
    analysis_content = _load_analysis_content(analysis_path, allow_empty=True)
    chinese_font = get_available_chinese_font()
    system_prompt = f"""You are a matplotlib visualization code generation assistant. Follow these STRICT rules:
1. Generate ONLY executable Python code using matplotlib.pyplot
2. NEVER include any explanations, comments or natural language
3. Always start with:
   import matplotlib.pyplot as plt
   import seaborn as sns
   import numpy as np
   import json
   import os
   from matplotlib import font_manager
   import matplotlib as mpl

   sns.set_style('whitegrid')
   plt.rcParams['font.sans-serif'] = ['{chinese_font}', 'Microsoft YaHei', 'PingFang SC', 'WenQuanYi Zen Hei']
   plt.rcParams['axes.unicode_minus'] = False

   chinese_font_prop = font_manager.FontProperties(family='{chinese_font}')
4.Prior to plotting, you must use the quantile winsorization method to handle outliers, keeping the data within the 1%
to 99% range. Please detail this processing step in the code.
5. Create sample data if needed (use numpy if necessary)
6. Implement the visualization requested in the user query precisely
7. For Chinese text in plots, use fontproperties parameter:
   - plt.title('中文标题')
   - plt.xlabel('中文标签')
   - plt.ylabel('中文标签')
   - plt.legend()
8. To make the grid settings take effect, please add plt.grid(True) before saving the plot.
9. Save each plot immediately after creating it:
   - Before saving, ensure that the directory for '{image_path}' exists; if not, create it using:
     os.makedirs(os.path.dirname(image_path), exist_ok=True)
   - save as '{image_path}'
   - SAVE EACH PLOT IMMEDIATELY AFTER CREATING IT, DO NOT WAIT UNTIL ALL PLOTS ARE CREATED
10. Manage plot descriptions using a JSON array file at '{json_path}':
   - At the beginning of the script, initialize an empty list variable `json_description = []`
   - For each plot, create a dictionary with keys: "image_path" and "description"
   - "image_path": absolute path of the plot image (e.g., "{image_path}")
   - "description": meaning of this plot
   - Append each dictionary to the `json_description` list
   - After all plots are created, if the file '{json_path}' already exists and is non-empty, load the existing JSON
     array from this file (use json.load); if loading fails, treat the existing list as empty
   - Extend the existing list with the new `json_description` list and OVERWRITE '{json_path}' with a single JSON array
     using json.dump(..., ensure_ascii=False)
   - IMPORTANT: The content of '{json_path}' MUST ALWAYS be a single valid JSON array
11. Ensure all strings are properly terminated with matching quotes
12. Use ' ```python ' to start and use ' ``` ' to end to let the user know there is the python code.
13. At the end of the script, ensure the variable `json_description` contains all the JSON descriptions created in
    this script
14. Print "successful!" at the end
15. STRICTLY PROHIBITED: Do NOT include any font installation code
16. STRICTLY PROHIBITED: Do NOT define any functions
17. STRICTLY PROHIBITED: Do NOT use complex font detection logic (no font_manager._rebuild, no font list iteration)
18. Use simple font setup as shown above
19. CRITICAL: When using plt.legend(), ALWAYS include prop=chinese_font_prop parameter to ensure Chinese characters
display correctly in legends

Common visualization types to support:
- Line plots (plt.plot)
- Bar charts (plt.bar)
- Scatter plots (plt.scatter)
- Histograms (plt.hist)
- Pie charts (plt.pie)
- Heatmaps (plt.imshow)

Failure to follow these rules will result in syntax errors."""
    user_prompt = f"""
User Request:
{query}

Source Data Path (use this exact absolute path in your code for reading the CSV; do not use a relative path):
{src_path_for_prompt}

SQL Command to extract the Source Data:
{sql_command}

Column Info:
{columns_info}

Data preview:
{data_preview}

Previous analysis results:
{analysis_content}"""
    response = llm.invoke([{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
    total_tokens = response.usage_metadata["total_tokens"]
    pattern = r"```python([\s\S]*?)```"
    match = re.search(pattern, response.content)
    if match is None:
        raise ValueError("LLM did not return a ```python ...``` code block for plotting.")
    extracted_code = match.group(1).strip()
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(extracted_code)
    local_vars = {}
    exec(extracted_code, local_vars, local_vars)
    json_description = local_vars.get("json_description", [])
    if isinstance(json_description, str):
        try:
            json_description = json.loads(json_description)
        except Exception:
            json_description = []
    if isinstance(json_description, dict):
        json_description = [json_description]
    if not isinstance(json_description, list):
        json_description = []
    logger.trace(f"=== JSON description ==={json_description}")

    def _json_list_to_markdown(json_description: list) -> str:
        if not json_description:
            return "（暂无结果）"
        md_lines = ["| 图片路径 | 描述 |", "|---|---|"]
        for item in json_description:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except Exception:
                    continue
            if not isinstance(item, dict):
                continue
            image_path = item.get("image_path", "")
            description = item.get("description", "")
            md_lines.append(f"| ![]({image_path}) | {description} |")
        return "\n".join(md_lines)

    json_description_md = _json_list_to_markdown(json_description)
    frontend_msg_md = (
        "\n\n正在执行 natural_language_to_plot 工具生成的代码，代码如下:\n\n"
        + f"```python\n{extracted_code}\n```\n\n"
        + "natural_language_to_plot 工具执行完成，图片已经生成。"
    )
    return {"original_msg": json_description_md, "frontend_msg": frontend_msg_md, "tokens_used": total_tokens}


def natural_language_to_sql(
    query: str,
    data_schema: str,
    sql_save_path: str,
    csv_save_path: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str]:
    """Generate and execute SQL script from natural language query. **Generate exactly one SQL query** per call.

    This tool can only be invoked **once** in the entire execution plan.
    When planning, you must ensure that the tool is used exactly one time.

    Args:
        query (str): Natural language query or SQL template.
        data_schema (str): Detailed schema of the source data tables, including their columns,
            column descriptions (as detailed as possible, explicitly mentioning data types such as
            Unix timestamp, DATE, or DATETIME), and the join keys.
            Provide as much detail as possible for each column so that the tool can generate and execute SQL correctly.
            Example:
            table_name_1: [
                { "column_name": "col_name_1", "column_description": "description of col_name_1" },
                { "column_name": "col_name_2", "column_description": "description of col_name_2" }
            ];
            table_name_2: [
                { "column_name": "col_name_1", "column_description": "description of col_name_1" },
                { "column_name": "col_name_2", "column_description": "description of col_name_2" }
            ];
            joins: table_name_1.col_name_1 = table_name_2.col_name_2
        sql_save_path (str): Absolute workspace path to save the generated SQL script (with .sql extension).
        csv_save_path (str): Absolute workspace path to save query results as CSV (with .csv extension).

    Returns:
        str: First few lines of execution results.
    """
    sql_save_path = _resolve_tool_file_path(sql_save_path, "sql_save_path")
    csv_save_path = _resolve_tool_file_path(csv_save_path, "csv_save_path")
    sql_dir = os.path.dirname(sql_save_path)
    if sql_dir:
        os.makedirs(sql_dir, exist_ok=True)
    csv_dir = os.path.dirname(csv_save_path)
    if csv_dir:
        os.makedirs(csv_dir, exist_ok=True)
    llm = llm_manager.get_default_llm()
    sql_system_prompt = f"""You are an expert MySQL database engineer. Follow these STRICT rules:
1. Generate ONLY executable MySQL SQL code
2. NEVER include explanations, comments or natural language
3. Use DISTINCT in SELECT only when necessary to eliminate duplicates
4. Use proper MySQL syntax and functions
5. Implement the user query logic precisely
6. Include proper JOINs using the provided schema relationships
7. Add appropriate WHERE/ORDER BY/LIMIT clauses
8. Generate ONLY the SELECT query - NO export commands
9. Use '```mysql' to start and '```' to end the code block
10. CRITICAL: When using DISTINCT with ORDER BY, you MUST use column aliases from SELECT in ORDER BY
11. Example: If SELECT has "FROM_UNIXTIME(o.payTime) AS payTime", ORDER BY must be "payTime" NOT "o.payTime"
12. Handle date/time conversions explicitly (e.g., FROM_UNIXTIME for timestamps)
13. Use table aliases for clarity with multi-table queries
14. Escape reserved words with backticks
15. Handle NULL values appropriately with COALESCE or IFNULL
16. Optimize for performance (avoid SELECT *)
17. ONLY use columns that exist in the provided schema
18. Verify table and column names match the schema exactly
19. CRITICAL: Before generating SQL, validate that all referenced columns exist in the provided schema
20. If a column mentioned in the user query does not exist, DO NOT include it in the SQL
21. For timestamp fields, explicitly use FROM_UNIXTIME() for conversion
22. When joining tables, always use the exact column names specified in the schema

USER QUERY: {query}
DATABASE SCHEMA: {data_schema}
"""
    sql_user_prompt = f"""User Query:
{query}

Table Schema (JSON):
{data_schema}"""
    sql_response = llm.invoke(
        [{"role": "system", "content": sql_system_prompt}, {"role": "user", "content": sql_user_prompt}]
    )
    total_tokens = sql_response.usage_metadata["total_tokens"]
    sql = _extract_sql_from_response(sql_response.content)
    logger.trace(f"Generated SQL:\n{sql}\n")
    with open(sql_save_path, "w", encoding="utf-8") as f_sql:
        f_sql.write(sql)
    try:
        df = load_table(sql, _tool_context=_tool_context)
    except Exception as e:
        if "Unknown column" in str(e):
            invalid_col_match = re.search(r"Unknown column '(.+?)'", str(e))
            if not invalid_col_match:
                raise ValueError(f"Failed to parse invalid column from database error: {e}") from e
            invalid_col = invalid_col_match.group(1)
            logger.trace(f"⚠️ 错误：检测到无效列名 '{invalid_col}'")
            correction_system_prompt = "你是一个SQL修正助手，请根据数据库模式修正SQL中的列名错误"
            correction_user_prompt = f"""
检测到无效列名 '{invalid_col}'。请根据以下模式生成修正后的SQL：
DATABASE SCHEMA: {data_schema}

请严格使用模式中存在的列名重新生成SQL，移除无效列 '{invalid_col}'。
"""
            correction_response = llm.invoke(
                [
                    {"role": "system", "content": correction_system_prompt},
                    {"role": "user", "content": correction_user_prompt},
                ]
            )
            corrected_sql = _extract_sql_from_response(correction_response.content)
            logger.trace(f"修正后的SQL:\n{corrected_sql}")
            with open(sql_save_path, "w", encoding="utf-8") as f_sql:
                f_sql.write(corrected_sql)
            df = load_table(corrected_sql, _tool_context=_tool_context)
        else:
            raise e
    df.to_csv(csv_save_path, index=False)
    frontend_msg_md = (
        f"\n\nnatural_language_to_sql 工具执行完成\n\n"
        f"SQL 文件已保存到：`{sql_save_path}`\n\n"
        f"CSV 结果已保存到：`{csv_save_path}`\n\n"
        f"生成的SQL语句如下:\n```sql\n{sql}\n```"
    )
    return {
        "original_msg": f"SQL 执行完成，结果已保存到：{csv_save_path}",
        "frontend_msg": frontend_msg_md,
        "tokens_used": total_tokens,
    }


def report_generator(
    query: str,
    output_path: str,
    analysis_path: str,
    images_path: str = "",
) -> dict[str, str]:
    """Based on the provided analysis and images, generate a detailed Markdown-formatted report.

    Running **llm_analyzer** and **natural_language_to_plot** beforehand is recommended.

    Args:
        query (str): User's request for the report, which may include specific focus areas or analysis points.
        output_path (str): Absolute workspace path for the output Markdown file.
        analysis_path (str): Absolute workspace path to a saved analysis file (e.g. from
            llm_analyzer).
        images_path (str): Absolute workspace path to a JSONL file with image paths and descriptions (optional).

    Returns:
        str: Generated Markdown report.
    """
    output_path = _resolve_tool_file_path(output_path, "output_path")
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    if images_path:
        images_path = _resolve_tool_file_path(images_path, "images_path")
    images = load_images_as_json(images_path) if images_path else ""
    report_analysis = _load_analysis_content(analysis_path)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    llm = llm_manager.get_default_llm()
    system_prompt = f"""You are a professional MD report generation assistant. Follow these rules:
1. Generate well-structured MD documents based only on the provided analysis inputs and \
images, ensuring a minimum of 1,000 words.
2. Organize content structure reasonably, including titles, sections, image insertion, etc.
3. For Markdown image syntax only: use paths in ![desc](path) that are relative to the report file \
(output_path), for portability. This exception applies only inside the MD body — do not infer \
relative paths for filesystem tool arguments elsewhere.
4. Add appropriate descriptions and explanations for each image.
5. Maintain professionalism and readability of the report.
6. If the Query explicitly specifies the language for the report, generate the report in that \
language; otherwise, use the language of the Query itself.
7. The report generation time should be the current time {current_time} and clearly stated in the report.
8. The data period covered in the report should be based on the actual data provided, not arbitrarily assigned.
9. The analysis input is loaded from a saved analysis file. Treat it as the single source of truth.
10. Do not fabricate numbers, statistics, indicators, or conclusions that are not grounded in \
the provided analysis or images.
MD format requirements:
1. Insert images using ![description](path) format.
2. Use lists, tables and other elements appropriately to enhance readability."""
    user_prompt = f"""Please generate an MD report based on the following data:
<query>{query}</query>
<analysis>{report_analysis}</analysis>
<images>{images}</images>"""
    response = llm.invoke([{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
    result = fix_markdown_image_paths(response.content.split("</think>")[-1].strip(), output_path, True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)
    return {
        "original_msg": result,
        "frontend_msg": f"\n\nreport_generator 工具执行完成\n\n{result}",
        "tokens_used": response.usage_metadata["total_tokens"],
    }


def metrics_calculator(metrics: str, data_path: str) -> dict[str, str]:
    """Calculate all required metrics directly from CSV data using LLM reasoning.

    Args:
        metrics (str): Natural language description of all metrics to calculate.
        data_path (str): Absolute workspace path to the CSV file used for calculation.

    Returns:
        str: JSON string of calculated metrics.
    """
    try:
        resolved_data_path = Path(_resolve_tool_file_path(data_path, "data_path"))
        data = pd.read_csv(resolved_data_path).to_markdown()
    except Exception as e:
        raise ValueError("No data provided.") from e
    llm = llm_manager.get_default_llm()
    system_prompt = """You are an expert data analyst and calculator.
Follow these rules strictly:
1. Compute the numerical values of the requested metrics based on <metrics> and <data>.
2. If a metric cannot be computed, output a JSON object with an "error" field and a brief reason.
3. Pay close attention to column names, data types, and units.
4. The final answer MUST be in JSONL format, each metric per line.

Input format:
<metrics>...</metrics>
<data>...</data>

Output format:
{"metric": "deposit", "value": 10, "unit": "CNY"}
{"metric": "profit", "error": "not enough data"}
"""
    user_prompt = f"""<metrics>{metrics}</metrics>
<data>{data}</data>"""
    response = llm.invoke([{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
    result = response.content.split("</think>")[-1].strip()
    return {
        "original_msg": result,
        "frontend_msg": f"\n\nmetrics_calculator 工具执行完成\n\n{result}",
        "tokens_used": response.usage_metadata["total_tokens"],
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


async def nl2sql_sub_agent_tool(
    query: str,
    sql_filename: str,
    csv_filename: str,
    *,
    _tool_context: ToolExecutionContext,
):
    """Convert natural language query to SQL. One SQL query at a time.
    SQL and CSV files are saved under the current Agent session workspace (set when
    the run starts)

    A good query should explicitly describe:
    - business goal and statistical intent
    - entity definitions and metric formulas
    - required joins and matching keys
    - filters, grouping granularity, aggregations, and sorting
    - intermediate computation logic
    - output fields and final result format

    Args:
        - query (str): Natural language query.
        - sql_filename: Filename for the generated SQL (with ``.sql`` extension), saved under the session workspace.
        - csv_filename: Filename for query results (with ``.csv`` extension), saved under the session workspace.

    """
    runtime = _tool_context.runtime
    if runtime is None or runtime.workspace_dir is None:
        raise RuntimeError(
            "nl2sql_sub_agent_tool: session workspace is unavailable; "
            "set initial_state.workspace (or chat(workspace=...)) before calling this tool."
        )
    workspace = str(Path(runtime.workspace_dir).expanduser().resolve())
    raw_source_config = _tool_context.tool_config.get("source_config_path")
    if raw_source_config:
        logger.debug(f"use source_config_path from user config yaml : {raw_source_config}")
        source_config_path = Path(str(raw_source_config)).expanduser().resolve()
    else:
        source_config_path = dataagent_package_root() / "agents" / "nl2sql" / "nl2sql_agent.yaml"
    user_prompt_path = dataagent_package_root() / "agents" / "nl2sql" / "prompts" / "user"
    with source_config_path.open(encoding="utf-8") as f:
        source_config = yaml.safe_load(f) or {}
    ws_config = source_config.setdefault("WORKSPACE", {})
    shutil.copytree(user_prompt_path, workspace, dirs_exist_ok=True)
    ws_config["path"] = workspace
    temp_config = _build_nl2sql_sub_agent_config(
        source_config,
        config_manager=_tool_context.config_manager,
        tool_config=_tool_context.tool_config,
    )
    guard = get_current_sandbox()
    temp_root = guard.workspace_root or Path.cwd().resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="nl2sql_sub_agent_",
        dir=temp_root,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        yaml.safe_dump(temp_config, temp_file, allow_unicode=False, sort_keys=False)
        temp_config_path = temp_file.name
    try:
        res = await sub_agent_tool(query=query, config_path=temp_config_path)
    finally:
        Path(temp_config_path).unlink(missing_ok=True)

    worker_payload = res.get("original_msg")
    if isinstance(worker_payload, dict) and worker_payload.get("error"):
        err = worker_payload.get("error")
        return {
            "original_msg": f"nl2sql_sub_agent_tool 工具执行失败：{err}",
            "frontend_msg": f"nl2sql_sub_agent_tool 工具执行失败：{err}",
        }

    sub_state = res.get("state")
    if not isinstance(sub_state, dict):
        logger.warning(
            f"nl2sql_sub_agent_tool: expected dict state from sub_agent_tool, got {type(sub_state).__name__}"
        )
        return res
    if sub_state.get("error"):
        return {
            "original_msg": f"nl2sql_sub_agent_tool 工具执行失败：{sub_state['error']}",
            "frontend_msg": f"nl2sql_sub_agent_tool 工具执行失败：{sub_state['error']}",
        }
    sql = sub_state.get("sql", "")
    try:
        import sqlglot

        dialect = source_config["DATABASE"]["engine"]
        sql = sqlglot.parse_one(sql, read=dialect).sql(pretty=True)
    except Exception:
        try:
            import sqlparse

            sql = sqlparse.format(sql, reindent=True, keyword_case="upper")
        except Exception:
            logger.warning("SQL cannot be reformatted.")

    columns = sub_state.get("columns") or []
    rows = sub_state.get("rows") or []

    sql_save_path = os.path.join(workspace, sql_filename)
    csv_save_path = os.path.join(workspace, csv_filename)
    sql_path = _resolve_and_authorize(sql_save_path, "sql_save_path", operation="nl2sql_sub_agent", mode="write")
    csv_path = _resolve_and_authorize(csv_save_path, "csv_save_path", operation="nl2sql_sub_agent", mode="write")
    sql_path.write_text(f"{sql}\n", encoding="utf-8")
    pd.DataFrame(rows, columns=columns if columns else None).to_csv(csv_path, index=False, encoding="utf-8-sig")
    frontend_msg_md = (
        f"\n\n nl2sql_sub_agent_tool 工具执行完成\n\n"
        f"SQL 文件已保存到：`{str(sql_path)}`\n\n"
        f"CSV 结果已保存到：`{str(csv_path)}`\n\n"
        f"生成的SQL语句如下:\n```sql\n{sql}\n```"
    )
    return {
        "original_msg": f"SQL 执行完成，SQL 文件已保存到：{str(sql_path)}，查询结果已保存到：{str(csv_path)}",
        "frontend_msg": frontend_msg_md,
    }


def _resolve_sub_agent_tool_identity() -> tuple[str, str, dict[str, Any]]:
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
            "sub_agent_tool: runtime context 缺少 user_id，已回退为默认值 %r（与主 Agent 默认一致）",
            default_user_id,
        )
        resolved_user_id = default_user_id
    if not resolved_session_id:
        logger.warning(
            "sub_agent_tool: runtime context 缺少 session_id，已回退为默认值 %r（与主 Agent 默认一致）",
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
        )
        env = dict(os.environ)
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


def _sub_agent_tool_busy_payload(*, worker_sub_id: int, resolved_session_id: str) -> dict[str, Any]:
    """Build the standard tool dict when this worker id is already executing elsewhere."""
    busy_result = build_busy_result(sub_id=worker_sub_id, parent_session_id=resolved_session_id)
    busy_msg = f"subagent {worker_sub_id} 正在运行，本次未启动新的子进程；请创建新的 subagent 执行该任务"
    return {
        "original_msg": busy_result.to_dict(),
        "frontend_msg": busy_msg,
        "state": None,
        "sub_id": worker_sub_id,
    }


def _sub_agent_tool_timeout_payload(
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
            worker_result=timeout_result,
            status="timeout",
            error=timeout_result.error,
            last_run_id_executed=int(next_run_id),
        )
    msg = f"子 Agent 执行超时（>{timeout} 秒），已终止子进程。"
    return {
        "original_msg": timeout_result.to_dict(),
        "frontend_msg": msg,
        "state": None,
        "sub_id": worker_sub_id,
    }


def _sub_agent_tool_startup_failure_payload(
    *,
    worker_sub_id: int,
    resolved_session_id: str,
    exc: Exception,
) -> dict[str, Any]:
    """Build the tool dict when the sub-agent subprocess fails to run or is interrupted."""
    msg = f"子 Agent 启动失败：{exc}"
    wr = _synthetic_worker_result_dict(
        sub_id=worker_sub_id,
        parent_session_id=resolved_session_id,
        status="failed",
        final_answer="",
        error=str(exc),
    )
    return {
        "original_msg": wr,
        "frontend_msg": msg,
        "state": None,
        "sub_id": worker_sub_id,
    }


async def sub_agent_tool(
    query: str,
    config_path: str | Path,
    sub_id: int | None = None,
    timeout: int = DEFAULT_SUBAGENT_TOOL_TIMEOUT,
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
      - timeout: Timeout in seconds for the subprocess execution (default: __SUBAGENT_TIMEOUT__).

    Returns:
      - ``original_msg``: ``worker_result`` dict (JSON in ToolMessage) for the planner.
      - ``frontend_msg``: Subagent-facing answer text (``final_answer`` / ``assistant_reply``).
      - ``state``: Flex final graph state dict for programmatic wrappers (e.g. nl2sql); may be ``None``.
      - ``sub_id``: Allocated or reused worker folder id.

      On subprocess stdout JSON parse failure, ``original_msg`` is the raw stdout string (warning logged).
    """
    # Path 入参视为内部调用（已解析的包内固定配置），跳过工作区白名单校验；
    # str 入参为模型/用户输入，仍走 _resolve_and_authorize。
    cfg_path = (
        config_path
        if isinstance(config_path, Path)
        else _resolve_and_authorize(config_path, "config_path", operation="sub_agent_tool", mode="read")
    )
    if not cfg_path.is_file():
        raise FileNotFoundError(f"子 Agent 配置文件不存在: {cfg_path}")

    resolved_user_id, resolved_session_id, subagent_context = _resolve_sub_agent_tool_identity()

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
    )
    if swarm_on and sub_id is not None and not reuse_worker_state:
        logger.warning(
            "sub_agent_tool: requested sub_id={} has no persisted worker assets under "
            "workers/<sub_id>/.memory/; cold-starting a new worker in this folder.",
            worker_sub_id,
        )
    next_run_id = compute_next_worker_run_id(
        user_id=resolved_user_id,
        parent_session_id=resolved_session_id,
        sub_id=worker_sub_id,
        reuse_worker_state=reuse_worker_state,
    )
    lock = acquire_worker_lock(
        user_id=resolved_user_id,
        parent_session_id=resolved_session_id,
        sub_id=worker_sub_id,
        query=query,
        ttl_seconds=int(timeout) + WORKER_LOCK_TTL_GRACE_SECONDS,
    )
    if lock is None:
        return _sub_agent_tool_busy_payload(worker_sub_id=worker_sub_id, resolved_session_id=resolved_session_id)

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
            timeout=timeout,
            progress_callback=subagent_context.get("progress_callback"),
            tool_call_id=subagent_context.get("tool_call_id"),
        )
        return _subagent_outcome_to_public_tool_dict(outcome)
    except TimeoutError:
        return _sub_agent_tool_timeout_payload(
            worker_sub_id=worker_sub_id,
            resolved_session_id=resolved_session_id,
            resolved_user_id=resolved_user_id,
            cfg_path=cfg_path,
            query=query,
            timeout=timeout,
            swarm_on=swarm_on,
            next_run_id=next_run_id,
        )
    except Exception as e:  # pragma: no cover - 极端系统错误
        return _sub_agent_tool_startup_failure_payload(
            worker_sub_id=worker_sub_id,
            resolved_session_id=resolved_session_id,
            exc=e,
        )
    finally:
        release_worker_lock(lock)


def _allocate_unique_worker_sub_id(*, user_id: str, parent_session_id: str) -> int:
    """Return a random 6-digit worker id whose session folder does not yet exist."""
    from dataagent.utils.runtime_paths import resolve_worker_root

    for _ in range(100):
        candidate = int.from_bytes(os.urandom(4), "big") % 900000 + 100000
        if not resolve_worker_root(user_id=user_id, parent_session_id=parent_session_id, sub_id=candidate).exists():
            return candidate
    raise RuntimeError("Unable to generate a unique subagent id.")


def _resolve_worker_sub_id_for_call(
    *,
    user_id: str,
    parent_session_id: str,
    requested_sub_id: int | None,
    swarm_on: bool,
) -> int:
    """Resolve the worker folder id for one ``sub_agent_tool`` call.

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
        base_state = load_worker_subagent_state(user_id=user_id, parent_session_id=parent_session_id, sub_id=sub_id)
        messages = load_worker_messages(user_id=user_id, parent_session_id=parent_session_id, sub_id=sub_id) or []
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
    }
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
    payload = worker_persistence if isinstance(worker_persistence, dict) else {}
    messages = payload.get("messages")
    if isinstance(messages, list):
        persist_worker_messages(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=sub_id,
            messages=messages,
        )
    state = payload.get("state")
    persist_worker_state(
        user_id=user_id,
        parent_session_id=parent_session_id,
        sub_id=sub_id,
        state=state if isinstance(state, dict) else {},
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
    )


def get_ontology_description_tool(*, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """
    Fetch the entity and relation structure description for the current scene directly.
    Call this tool AT MOST ONCE and FIRST.

    Use when:
    - You need to understand the overall ontology structure, entity types, or relation definitions.

    Returns:
        A dict containing ontology metadata — entity types, attributes, and relations.
    """
    from dataagent.actions.gym.ontology_env import OntologyEnv

    env = OntologyEnv(config_manager=_tool_context.config_manager)
    return env.get_ontology_description()


def get_business_procedure_tool(keywords: list[str], *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """
    Fetch the entity and relation structure description for the current scene directly.
    Call this tool AT MOST ONCE and FIRST.

    Use when:
    - You need to understand the overall ontology structure, entity types, or relation definitions.
    Args:
    keywords: list[str],可以输入多个关键词（短词）, 其中有一个匹配到对应的业务逻辑则会返回
    Returns:
        A dict containing ontology metadata — entity types, attributes, and relations.
    """
    from dataagent.actions.gym.ontology_env import OntologyEnv

    env = OntologyEnv(config_manager=_tool_context.config_manager)
    return env.get_business_procedure(keywords)


async def ontology_sub_agent_query_tool(
    query: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Ontology 子代理统一入口工具。

    功能说明：
    - 将自然语言查询委托给 ontology sub-agent，并返回结构化 JSON 结果。
    - 只传 `query`，子代理 `config_path` 由工具内部固定。
    - 自动继承主 Agent 的 `ONTOLOGY.scene`（通过 `SCENE` 环境透传）。

    使用约束：
    - 仅用于本体/图谱查询任务。
    - 该函数本身只负责单次子查询。
    - 尽可能同时下发多个本工具的调用。

    Args:
        query: 单个自然语言查询字符串。

    Returns:
        dict[str, Any]: 标准工具返回，`original_msg` 优先为结构化结果，`frontend_msg` 为展示文本。
    """
    fixed_config_path = (dataagent_package_root() / "core" / "flex" / "examples" / "ontology_sub_agent.yaml").resolve()

    previous_scene = os.environ.get("SCENE")
    configured_scene = _tool_context.config_manager.get("ONTOLOGY.scene")
    if configured_scene:
        os.environ["SCENE"] = str(configured_scene)
    try:
        res = await sub_agent_tool(query=query, config_path=fixed_config_path)
        state = res.get("state")
        if isinstance(state, dict):
            messages = state.get("messages", [])
            if isinstance(messages, list) and messages:
                return {"original_msg": messages[-1], "frontend_msg": messages[-1]}
        # 无法解析为预期格式，原样返回 sub_agent_tool 的结果
        return res
    finally:
        if previous_scene is None:
            os.environ.pop("SCENE", None)
        else:
            os.environ["SCENE"] = previous_scene


async def bash(command: str, purpose: str | None = None, timeout: int = DEFAULT_BASH_TIMEOUT) -> dict[str, Any]:
    """Executes a given bash command and returns its output.

    The working directory is set to the workspace root. Shell state does not
    persist between calls.

    IMPORTANT: Avoid using this tool to run ``find``, ``grep``, ``cat``,
    ``head``, ``tail``, ``sed``, ``awk``, or ``echo`` commands unless
    explicitly instructed or after verifying that a dedicated tool cannot
    accomplish the task. Instead use the appropriate dedicated tool:

    - File search: Use glob (NOT find or ls)
    - Content search: Use grep (NOT grep/rg in shell)
    - Read files: Use read_file (NOT cat/head/tail)
    - Edit files: Use edit_file (NOT sed/awk)
    - Write files: Use write_file (NOT echo >/cat <<EOF)

    Instructions:
    - Prefer non-interactive commands. The shell does NOT support real-time
      interactive input (e.g. ``read``, ``input()``, prompts). Use default
      values, command-line arguments, or environment variables instead.
    - Always quote file paths that contain spaces with double quotes.
    - When issuing multiple independent commands, chain them with ``&&`` in a
      single call. Use ``;`` only when you don't care if earlier commands fail.
    - For long-running tasks, consider reducing timeout or splitting into steps.
    - If a command fails due to missing dependencies, install them first.

    Args:
        command (str): The command to execute.
        purpose (str | None): Clear, concise description of what this command does (optional).
        timeout (int): Optional timeout in seconds (default __BASH_TIMEOUT__).
    """
    normalized_purpose = str(purpose or "").strip()

    if not command or not str(command).strip():
        raise ValueError("'command' is required and must not be empty.")

    guard = get_current_sandbox()
    cwd: str | None = None
    env = _build_shell_env()
    if guard.workspace_root is not None:
        cwd = str(guard.workspace_root)
    if guard.skill_aliases:
        command = _expand_skill_aliases_in_shell_command(command)

    try:
        result = await _run_subprocess_async(
            ["/bin/bash", "-lc", command],
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"Command timed out after {timeout}s. Consider splitting into smaller steps or increasing timeout.\n"
            f"Command: {command!r}"
        ) from exc

    stdout = result["stdout"]
    stderr = result["stderr"]
    exit_code = result["returncode"]

    # Build original_msg so LLM can reason about success/failure without inspecting `data`.
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if exit_code != 0:
        parts.append(f"[exit code: {exit_code}]")
    original_msg = "\n".join(parts) or "(no output)"

    status_label = "succeeded" if exit_code == 0 else f"failed (exit code {exit_code})"
    frontend_msg = f"bash {status_label}" + (f" — {normalized_purpose}" if normalized_purpose else "")

    return {
        "original_msg": original_msg,
        "frontend_msg": frontend_msg,
        "data": {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        },
    }


def apply_patch(diff: str, purpose: str | None = None) -> dict[str, Any]:
    """Apply a unified diff patch to existing files.

    Use this tool when:
    - Modifying specific lines in files
    - Refactoring code safely
    - Updating multiple files in a controlled manner

    Args:
        diff (str): A valid unified diff string (git-style). ``+++`` file paths must be
            absolute under the workspace root (or resolve under it the same way as ``write_file``).
        purpose (str | None): Brief description of why this patch is being applied (optional).

    Returns:
        dict[str, Any]: Tool-style output with:
            - original_msg: Summary of applied/failed files for LLM reasoning.
            - frontend_msg: Brief execution summary for UI display.
            - data: A dict containing ``applied`` and ``failed`` file lists.
    """
    normalized_purpose = str(purpose or "").strip()

    files: dict[str, list[str]] = {}
    current_file: str | None = None

    for line in diff.splitlines():
        if line.startswith("+++ "):
            current_file = line[4:].strip().replace("b/", "")
            files.setdefault(current_file, [])
        elif current_file and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            files[current_file].append(line)

    applied: list[str] = []
    failed: list[dict[str, str]] = []

    for file_path, lines in files.items():
        try:
            p = _resolve_and_authorize(file_path, "file_path", operation="apply_patch", mode="write")
        except PermissionError as e:
            failed.append({"path": file_path, "error": str(e)})
            continue
        if not p.exists():
            failed.append({"path": file_path, "error": "File not found"})
            continue
        try:
            original = p.read_text(encoding="utf-8", errors="replace").splitlines()
            new_content: list[str] = []
            idx = 0
            for patch_line in lines:
                if patch_line.startswith(" "):
                    new_content.append(original[idx])
                    idx += 1
                elif patch_line.startswith("-"):
                    idx += 1
                elif patch_line.startswith("+"):
                    new_content.append(patch_line[1:])
            p.write_text("\n".join(new_content), encoding="utf-8")
            applied.append(file_path)
            logger.trace(f"apply_patch: patched {file_path}")
        except Exception as e:
            logger.warning(f"apply_patch: failed to patch {file_path}: {e}")
            failed.append({"path": file_path, "error": str(e)})

    original_msg_parts = [f"Applied ({len(applied)}): {applied}"]
    if failed:
        original_msg_parts.append(f"Failed ({len(failed)}): {failed}")
    original_msg = "\n".join(original_msg_parts)
    frontend_msg = (
        "\n\napply_patch 工具执行完成"
        + (f" — {normalized_purpose}" if normalized_purpose else "")
        + f"\n\n成功应用: {len(applied)} 个文件，失败: {len(failed)} 个文件"
    )
    return {
        "original_msg": original_msg,
        "frontend_msg": frontend_msg,
        "data": {"applied": applied, "failed": failed},
    }


def edit_file(
    path: str,
    op: str,
    anchor: str,
    text: str,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Edit an existing file using anchor-based operations.

    Performs targeted modifications in files by locating a literal anchor
    string and applying an operation relative to it.

    Usage:
    - Use absolute paths under the workspace root. Never edit files in skill
      directories or read-only roots.
    - You must use read_file at least once in the conversation before editing.
      This tool will error if you attempt an edit without reading the file.
    - When editing text from read_file output, ensure you preserve the exact
      indentation (tabs/spaces) as it appears AFTER the line number prefix.
      The line number prefix format is: ``N\\tline``. Everything after the tab
      is the actual file content to match. Never include any part of the line
      number prefix in the anchor or text.
    - ALWAYS prefer editing existing files. NEVER write new files unless
      explicitly required.
    - The edit will FAIL if ``anchor`` is not found in the file. Provide enough
      surrounding context to make the anchor unique.
    - Use ``replace_all`` for renaming strings across the file (e.g. renaming
      a variable).
    - Original file encoding (BOM) and line ending style (LF/CRLF) are
      automatically preserved.
    - Keep each ``text`` argument short — ideally under 50 lines. For larger
      changes, split into multiple edit_file calls with different anchors.
      This keeps tool calls fast and reduces the chance of errors.

    Args:
        path (str): Absolute path under the workspace root to the file to modify.
        op (str): One of: ``replace_first``, ``replace_all``, ``insert_before``, ``insert_after``.
        anchor (str): The literal text to locate in the file (must be unique for replace_first).
        text (str): The text to replace with or insert (must be different from anchor for replacements).
        purpose (str | None): Brief description of why this file is being modified (optional).
    """
    normalized_purpose = str(purpose or "").strip()

    valid_ops = {"replace_first", "replace_all", "insert_before", "insert_after"}
    if op not in valid_ops:
        raise ValueError(f"Invalid op: {op!r}. Must be one of: {', '.join(sorted(valid_ops))}.")

    p = _resolve_and_authorize(path, "path", operation="edit_file", mode="write")
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if not anchor:
        raise ValueError("'anchor' cannot be empty.")

    text = str(text).replace("\r\n", "\n")

    # Decode with BOM detection (utf-16le / utf-8 BOM). Detect line-ending style.
    encoding = "utf-8"
    with p.open("rb") as bf:
        head = bf.read(4096)
        if head.startswith(b"\xff\xfe"):
            encoding = "utf-16le"
        elif head.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8"
    has_crlf = b"\r\n" in head

    # Read and normalize to LF for editing.
    original = p.read_text(encoding=encoding, errors="replace").replace("\r\n", "\n")

    if op == "replace_all":
        if anchor not in original:
            raise ValueError(f"Anchor not found in: {path!r}")
        new_text = original.replace(anchor, text)
    else:
        idx = original.find(anchor)
        if idx == -1:
            raise ValueError(f"Anchor not found in: {path!r}")
        if op == "replace_first":
            new_text = original[:idx] + text + original[idx + len(anchor) :]
            # Smart trailing-newline cleanup when deleting (text is empty):
            # if the anchor was a full line, remove the leftover blank line.
            if not text and idx > 0 and original[idx - 1] == "\n":
                end = idx + len(anchor)
                if end < len(original) and original[end] == "\n":
                    new_text = original[:idx] + original[end + 1 :]
        elif op == "insert_before":
            new_text = original[:idx] + text + original[idx:]
        else:  # insert_after
            new_text = original[: idx + len(anchor)] + text + original[idx + len(anchor) :]

    changed = new_text != original
    if changed:
        # Restore original line-ending style before writing back.
        write_text = new_text.replace("\n", "\r\n") if has_crlf else new_text
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, write_text.encode(encoding, errors="replace"))
        logger.trace(f"edit_file: updated {path} (op={op})")

    message = "File updated" if changed else "No changes made"
    diff_text = _compute_unified_diff(original, new_text, path) if changed else "(no changes)"
    return {
        "original_msg": f"{message}: {path}\n\n{diff_text}",
        "frontend_msg": (
            "\n\nedit_file 工具执行完成"
            + (f" — {normalized_purpose}" if normalized_purpose else "")
            + f"\n\n{message}: {path}"
        ),
        "data": {
            "changed": changed,
            "path": str(p.resolve()),
            "encoding": encoding,
            "size_bytes": int(p.stat().st_size),
            "mtime_ns": int(getattr(p.stat(), "st_mtime_ns", int(p.stat().st_mtime * 1_000_000_000))),
        },
    }


def inspect_file(path: str) -> dict[str, Any]:
    """Inspect a file's metadata: size, line count, and last-modified time.

    Use this tool when:
    - You need to check file size before reading large files
    - You want to verify a file exists and get its basic info
    - You need to decide how many bytes/lines to read safely

    Args:
        path (str): Absolute path under the workspace (or ``skill/...``) to the file to inspect.

    Returns:
        dict[str, Any]: Tool-style output with:
            - original_msg: Human-readable file metadata for LLM reasoning.
            - frontend_msg: Brief inspection summary for UI display.
            - data: A dict containing ``path``, ``size_bytes``, ``line_count``, ``last_modified``.
    """
    p = _resolve_and_authorize(path, "path", operation="inspect_file", mode="read")
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    stat = p.stat()
    size_bytes = stat.st_size
    last_modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    try:
        content = p.read_bytes()
        line_count = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
    except Exception:
        line_count = -1

    original_msg = (
        f"path: {p.resolve()}\nsize_bytes: {size_bytes}\nline_count: {line_count}\nlast_modified: {last_modified}"
    )
    data: dict[str, Any] = {
        "path": str(p.resolve()),
        "size_bytes": size_bytes,
        "line_count": line_count,
        "last_modified": last_modified,
    }

    lineage = _lookup_file_lineage(str(p.resolve()))
    if lineage:
        data["lineage"] = lineage
        lineage_lines = [f"{k}: {v}" for k, v in lineage.items() if v]
        original_msg += "\n" + "\n".join(lineage_lines)

    return {
        "original_msg": original_msg,
        "frontend_msg": f"\n\ninspect_file 工具执行完成\n\n{original_msg}",
        "data": data,
    }


# ---------------------------------------------------------------------------
# File tool limits
# ---------------------------------------------------------------------------


def _atomic_write(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically, preserving permissions when possible.

    Uses a secure temporary file in the same directory (same filesystem) so that
    ``os.replace`` is guaranteed to be atomic.  If the target already exists its
    permission bits are copied to the new file; on failure we fall back to a
    plain write.
    """
    fd = None
    tmp_path: Path | None = None
    # Capture original permissions before writing (None for new files).
    original_mode: int | None = None
    if target.exists():
        try:
            original_mode = target.stat().st_mode
        except OSError as exc:
            logger.warning("Failed to read permissions for {}: {}", target, exc)

    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.write(fd, data)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        logger.warning("Failed to write temp file for {}, falling back to direct write", target)
        target.write_bytes(data)
        return
    finally:
        os.close(fd)

    if original_mode is not None:
        try:
            os.chmod(tmp_path, original_mode)
        except OSError as exc:
            logger.warning("Failed to preserve permissions for {}: {}", target, exc)

    try:
        os.replace(tmp_path, target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        logger.warning("Atomic replace failed for {}, falling back to direct write", target)
        target.write_bytes(data)


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    """Add line-number prefixes (compact ``N\\tline`` format) for model consumption."""
    lines = content.split("\n")
    return "\n".join(f"{i + start_line}\t{line}" for i, line in enumerate(lines))


def _compute_unified_diff(
    old_content: str | None,
    new_content: str,
    file_path: str,
    max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> str:
    """Return a truncated unified diff between *old_content* and *new_content*."""
    old_lines = (old_content or "").splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=file_path, tofile=file_path)
    result = "".join(diff)
    if not result:
        return "(no changes)"
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... (diff truncated)"
    return result


def read_file(
    path: str, purpose: str | None = None, offset: int | None = 1, limit: int | None = None
) -> dict[str, Any]:
    """Read a file from the local filesystem.

    Reads a file and returns its content. You can access any file directly by
    using this tool. If the user provides a path to a file, assume that path is
    valid. It is okay to read a file that does not exist; an error will be
    returned.

    Usage:
    - Use absolute paths under the workspace root for workspace files. For
      additional read-only roots listed in the task context, use their absolute
      host paths. For skill resources, use ``skill/<name>/...`` paths.
    - By default it reads the entire file from line 1. For large files, use
      ``offset`` and ``limit`` to read only the relevant section.
    - When you already know which part of the file you need, only read that
      part — this is important for larger files.
    - Results are returned with line numbers (``N\\tline``) starting at 1.
    - This tool can only read text files, not directories. To list a directory,
      use the bash tool with ``ls``.
    - If you read a file that exists but has empty contents you will receive a
      system reminder warning in place of file contents.
    - Binary files and files exceeding the size budget will be rejected with a
      clear error message.

    Args:
        path (str): Absolute path under the workspace root, read-only root, or
            ``skill/<name>/...`` for skill assets.
        purpose (str | None): Brief description of why this file is being read (optional).
        offset (int | None): The line number to start reading from (1-based). Only provide
            if the file is too large to read at once.
        limit (int | None): The number of lines to read. Only provide if the file is too
            large to read at once.
    """
    normalized_purpose = str(purpose or "").strip()

    p = _resolve_and_authorize(path, "path", operation="read_file", mode="read")
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    resolved_path = str(p.resolve())

    start_line = 1 if offset is None else int(offset)
    if start_line < 1:
        raise ValueError("offset must be >= 1.")
    if limit is not None and int(limit) <= 0:
        raise ValueError("limit must be a positive integer or None.")
    limit_lines = None if limit is None else int(limit)

    try:
        st = p.stat()
        total_bytes = int(st.st_size)
    except FileNotFoundError as err:
        raise FileNotFoundError(f"File not found: {path}") from err

    # Cap returned content size to avoid blowing up the context window.
    if start_line == 1 and limit_lines is None and total_bytes > DEFAULT_READ_MAX_FILE_SIZE:
        raise ValueError(
            f"File too large to read at once ({total_bytes} bytes, ~{total_bytes // 4} tokens). "
            "Use offset/limit to read a smaller range, or use grep to search for specific content."
        )

    # Detect encoding via BOM, then stream by lines.
    encoding = "utf-8"
    with p.open("rb") as bf:
        head = bf.read(4096)
        if b"\x00" in head:
            raise ValueError("This tool can only read text files; the target appears to be binary.")
        if head.startswith(b"\xff\xfe"):
            encoding = "utf-16le"
        elif head.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8"

    collected: list[str] = []
    collected_bytes = 0
    truncated = False

    # Stream file line-by-line, reading only the requested range (+1 line to detect truncation).
    # We do NOT scan to EOF; total_lines is therefore unknown.
    with p.open("r", encoding=encoding, errors="replace", newline="") as tf:
        # Skip BOM for UTF-8 explicitly (TextIO will keep it in content)
        if encoding == "utf-8":
            first = tf.read(1)
            if first != "\ufeff":
                tf.seek(0)

        current_line_no = 0
        lines_collected = 0

        for line in tf:
            current_line_no += 1
            if current_line_no < start_line:
                continue

            if limit_lines is not None and lines_collected >= limit_lines:
                # We already have the requested number of lines; one extra line indicates truncation.
                truncated = True
                break

            # Normalize line endings for stable output.
            rendered = line.replace("\r\n", "\n").replace("\r", "\n")
            rendered_bytes = len(rendered.encode("utf-8"))

            # Enforce output size budget.
            if collected_bytes + rendered_bytes > DEFAULT_READ_MAX_OUTPUT_BYTES:
                truncated = True
                break

            collected.append(rendered)
            collected_bytes += rendered_bytes
            lines_collected += 1

        # If we didn't hit limit_lines but did hit byte budget, truncated already True and loop broke.
        # If limit_lines is None, truncation only reflects the output budget.

    content = "".join(collected)

    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    numbered = _add_line_numbers(content, start_line)
    return {
        "original_msg": numbered,
        "frontend_msg": (
            "\n\nread_file 工具执行完成"
            + (f" — {normalized_purpose}" if normalized_purpose else "")
            + f"\n\n已读取: {path}（{collected_bytes} 字节，{line_count} 行，从第 {start_line} 行开始"
            + (f"，最多 {limit_lines} 行" if limit_lines is not None else "")
            + ("，已截断）" if truncated else "）")
        ),
        "data": {
            "path": resolved_path,
            "total_bytes": total_bytes,
            "num_lines": line_count,
            "truncated": truncated,
        },
    }


def write_file(path: str, content: str, purpose: str | None = None) -> dict[str, Any]:
    """Write a file to the local filesystem.

    Writes a file to the workspace. If the file already exists it will be
    overwritten; parent directories are created automatically.

    Usage:
    - Use absolute paths under the workspace root. Never write into skill
      directories or read-only roots.
    - This tool will overwrite the existing file if there is one at the
      provided path.
    - If this is an existing file, you MUST use the read_file tool first to
      read the file's contents. This tool will fail if you did not read the
      file first.
    - Prefer the edit_file tool for modifying existing files — it only sends
      the diff. Only use this tool to create new files or for complete rewrites.
    - NEVER create documentation files (*.md) or README files unless explicitly
      requested by the user.
    - Keep ``content`` concise — ideally under 300 lines. For larger files,
      write a short skeleton first, then use edit_file to add sections
      incrementally. This avoids slow, token-heavy tool calls.

    Args:
        path (str): Absolute path under the workspace root for the file to create or overwrite.
        content (str): The content to write to the file.
        purpose (str | None): Brief description of why this file is being created/updated (optional).
    """
    normalized_purpose = str(purpose or "").strip()

    p = _resolve_and_authorize(path, "path", operation="write_file", mode="write")
    p.parent.mkdir(parents=True, exist_ok=True)

    normalized_content = str(content).replace("\r\n", "\n")

    data = normalized_content.encode("utf-8")

    # Capture old content for diff on updates (best-effort).
    old_content: str | None = None
    is_update = p.exists()
    if is_update:
        with contextlib.suppress(Exception):
            old_content = p.read_text(encoding="utf-8", errors="replace")

    # Atomic write with permission preservation.
    _atomic_write(p, data)
    logger.trace(f"write_file: wrote {len(data)} bytes to {p.resolve()}")

    summary = f"File written: {p.resolve()} ({len(data)} bytes)"
    if is_update and old_content is not None:
        summary += "\n\n" + _compute_unified_diff(old_content, normalized_content, path)
    st = p.stat()
    return {
        "original_msg": summary,
        "frontend_msg": (
            "\n\nwrite_file 工具执行完成"
            + (f" — {normalized_purpose}" if normalized_purpose else "")
            + f"\n\n已写入: {p.resolve()}（{len(data)} 字节）"
        ),
        "data": {
            "path": str(p.resolve()),
            "size_bytes": len(data),
            "type": "update" if is_update else "create",
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        },
    }


def glob(
    glob_pattern: str, target_directory: str | None = None, max_results: int = DEFAULT_GLOB_MAX_RESULTS
) -> dict[str, Any]:
    """Fast file pattern matching tool that works with any codebase size.

    - Supports glob patterns like ``"**/*.py"`` or ``"src/**/*.ts"``
    - Returns matching file paths sorted by modification time (newest first)
    - Use this tool when you need to find files by name patterns
    - Patterns not starting with ``**/`` are automatically prepended with
      ``**/`` for recursive matching
    - Common VCS and build directories (.git, node_modules, __pycache__,
      etc.) are automatically excluded

    Args:
        glob_pattern (str): The glob pattern to match files against.
        target_directory (str | None): The directory to search in. If not specified,
            the workspace root will be used.
        max_results (int): Maximum number of paths to return (default __GLOB_MAX_RESULTS__).
    """
    pattern = str(glob_pattern or "").strip()
    if not pattern:
        raise ValueError("'glob_pattern' is required and must not be empty.")

    base = target_directory or str(get_current_sandbox().workspace_root)
    base_path = _resolve_and_authorize(base, "target_directory", operation="glob", mode="read")
    if not base_path.exists():
        raise FileNotFoundError(f"Directory not found: {base}")
    if not base_path.is_dir():
        raise ValueError(f"target_directory must be a directory: {base}")

    # Recursive search. Normalize patterns without **/ prefix for user convenience.
    effective_pattern = pattern if pattern.startswith("**/") or pattern.startswith("**\\") else f"**/{pattern}"

    matches: list[Path] = []
    for p in base_path.glob(effective_pattern):
        # Skip files inside hidden/VCS directories.
        if any(part in DEFAULT_SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            matches.append(p)
            if len(matches) >= int(max_results):
                break

    # Sort by mtime desc (most recently modified first).
    matches.sort(key=lambda x: x.stat().st_mtime_ns if x.exists() else 0, reverse=True)

    # Return relative paths to save tokens.
    resolved_base = base_path.resolve()
    paths = []
    for p in matches:
        try:
            paths.append(str(p.resolve().relative_to(resolved_base)))
        except ValueError:
            paths.append(str(p.resolve()))

    truncated = len(matches) >= int(max_results)
    msg = "\n".join(paths) if paths else "(no matches)"
    if truncated:
        msg += "\n(Results truncated. Consider a more specific pattern or path.)"

    return {
        "original_msg": msg,
        "frontend_msg": f"\n\nglob 工具执行完成\n\n匹配到 {len(paths)} 个文件",
        "data": {
            "target_directory": str(resolved_base),
            "glob_pattern": pattern,
            "effective_pattern": effective_pattern,
            "max_results": int(max_results),
            "paths": paths,
            "truncated": truncated,
        },
    }


_GREP_NO_MATCHES = "(no matches)"
_GREP_TRUNCATION_SUFFIX = "\n(Results truncated. Consider a more specific pattern or glob filter.)"
_SYSTEM_GREP_BIN: str | None = shutil.which("grep")

# file_type → extensions for ``grep --include`` filtering.
_GREP_FILE_TYPE_MAP: dict[str, tuple[str, ...]] = {
    "py": (".py", ".pyi"),
    "js": (".js", ".mjs", ".cjs", ".jsx"),
    "ts": (".ts", ".tsx"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "c": (".c", ".h"),
    "cpp": (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    "md": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "html": (".html", ".htm"),
    "css": (".css",),
    "sh": (".sh", ".bash", ".zsh"),
    "sql": (".sql",),
    "xml": (".xml",),
    "txt": (".txt",),
}


def _grep_system_grep(
    *,
    grep_bin: str,
    pattern: str,
    root_path: Path,
    output_mode: str,
    head_limit: int,
    before: int,
    after: int,
    context: int,
    case_insensitive: bool,
    file_type: str | None,
    glob_pattern: str | None,
) -> dict[str, Any]:
    file_type_exts = _GREP_FILE_TYPE_MAP.get(file_type.lower()) if file_type else None

    args: list[str] = [grep_bin, "-E", "-r", "-H", "-I"]
    if case_insensitive:
        args.append("-i")
    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")
    else:
        args.append("-n")
        if context > 0:
            args += ["-C", str(context)]
        else:
            if before > 0:
                args += ["-B", str(before)]
            if after > 0:
                args += ["-A", str(after)]
    for d in DEFAULT_SKIP_DIRS:
        args.append(f"--exclude-dir={d}")
    for ext in file_type_exts or ():
        args.append(f"--include=*{ext}")
    if glob_pattern:
        args.append(f"--include={glob_pattern}")
    args += ["--", pattern, str(root_path)]

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=DEFAULT_GREP_TIMEOUT)
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(
            f"grep timed out after {DEFAULT_GREP_TIMEOUT} seconds. Try a more specific pattern or path."
        ) from err
    if proc.returncode == 2:
        raise RuntimeError(proc.stderr.strip() or "grep failed")

    raw_lines = proc.stdout.splitlines()
    truncated = head_limit > 0 and len(raw_lines) > head_limit
    if truncated:
        raw_lines = raw_lines[:head_limit]

    abs_prefix = str(root_path.resolve()) + os.sep
    output_lines = [line[len(abs_prefix) :] if line.startswith(abs_prefix) else line for line in raw_lines]
    output = "\n".join(output_lines) if output_lines else _GREP_NO_MATCHES
    if truncated:
        output += _GREP_TRUNCATION_SUFFIX

    resolved = root_path.resolve()
    return {
        "original_msg": output,
        "frontend_msg": f"\n\ngrep 工具执行完成\n\n在 {resolved} 中搜索完成",
        "data": {
            "pattern": pattern,
            "path": str(resolved),
            "glob": glob_pattern,
            "output_mode": output_mode,
            "head_limit": head_limit,
            "exit_code": proc.returncode,
            "truncated": truncated,
        },
    }


def grep(
    pattern: str,
    path: str | None = None,
    glob_pattern: str | None = None,
    output_mode: str = "files_with_matches",
    head_limit: int = DEFAULT_GREP_HEAD_LIMIT,
    before: int = 0,
    after: int = 0,
    context: int = 0,
    case_insensitive: bool = False,
    file_type: str | None = None,
) -> dict[str, Any]:
    """A powerful content search tool backed by the system ``grep`` binary.

    ALWAYS use this tool for content search tasks. NEVER invoke ``grep`` as a
    bash command directly — this tool has been optimized for correct
    permissions and access.

    Usage:
    - Supports POSIX ERE regex (e.g. ``"log.*Error"``, ``"function +[A-Za-z_]+"``)
    - Filter files with glob_pattern (e.g. ``"*.js"``, ``"*.tsx"``) or
      file_type (e.g. ``"py"``, ``"js"``, ``"rust"``)
    - Output modes: ``"files_with_matches"`` shows only file paths (default),
      ``"content"`` shows matching lines, ``"count"`` shows match counts
    - Results are capped by head_limit (default __GREP_HEAD_LIMIT__). Pass 0 for unlimited
      (use sparingly — large result sets waste context).

    Args:
        pattern (str): The regular expression pattern to search for in file contents.
        path (str | None): File or directory to search in. Defaults to workspace root.
        glob_pattern (str | None): Glob pattern to filter files (e.g. ``"*.py"``).
        output_mode (str): ``"files_with_matches"`` (default), ``"content"``, or ``"count"``.
        head_limit (int): Limit output to first N entries (default __GREP_HEAD_LIMIT__).
        before (int): Lines of context before each match (``-B``). Requires ``output_mode="content"``.
        after (int): Lines of context after each match (``-A``). Requires ``output_mode="content"``.
        context (int): Lines of context before and after each match (``-C``). Overrides before/after.
        case_insensitive (bool): Case-insensitive search (``-i``). Default false.
        file_type (str | None): File type filter (e.g. ``"py"``, ``"js"``).
    """
    if not pattern:
        raise ValueError("'pattern' is required and must not be empty.")
    if not _SYSTEM_GREP_BIN:
        raise RuntimeError("system grep is not available; install grep or add it to PATH.")

    search_root = path or str(get_current_sandbox().workspace_root)
    root_path = _resolve_and_authorize(search_root, "path", operation="grep", mode="read")
    if not root_path.exists():
        raise FileNotFoundError(f"Path not found: {search_root}")

    return _grep_system_grep(
        grep_bin=_SYSTEM_GREP_BIN,
        pattern=pattern,
        root_path=root_path,
        output_mode=output_mode,
        head_limit=head_limit,
        before=before,
        after=after,
        context=context,
        case_insensitive=case_insensitive,
        file_type=file_type,
        glob_pattern=glob_pattern,
    )


# Patch tool docstrings with actual constant values so that bind_tools
# transmits real numbers to the model context instead of Python identifiers.
sub_agent_tool.__doc__ = (sub_agent_tool.__doc__ or "").replace(
    "__SUBAGENT_TIMEOUT__", str(DEFAULT_SUBAGENT_TOOL_TIMEOUT)
)
bash.__doc__ = (bash.__doc__ or "").replace("__BASH_TIMEOUT__", str(DEFAULT_BASH_TIMEOUT))
glob.__doc__ = (glob.__doc__ or "").replace("__GLOB_MAX_RESULTS__", str(DEFAULT_GLOB_MAX_RESULTS))
grep.__doc__ = (grep.__doc__ or "").replace("__GREP_HEAD_LIMIT__", str(DEFAULT_GREP_HEAD_LIMIT))


def request_human_feedback(reason: str, pending_action: str = "") -> str:
    """
    Request human feedback before continuing.

    Use this tool when:
    1. important data may be deleted or modified
    2. reliable progress is blocked by one or more clarification points
    3. you are unsure whether the current action matches user intent
    4. the next step may have serious consequences
    5. additional user input is required

    For tasks, call this tool as soon as reliable progress requires user feedback, clarification,
    confirmation, additional context, or further guidance. Follow the planner system prompt for the detailed
    trigger rules. Ask for the smallest blocking point first when possible. Do not collapse multiple
    independent clarification points into one vague request; if several points must be asked together,
    list them explicitly. Do not guess business meaning or silently choose among plausible interpretations
    when human input is required.

    Args:
        reason: Why human input is required before continuing.
        pending_action: The action to take after confirmation.

    Returns:
        str: A message indicating that human feedback is pending.
    """
    return "Waiting for human feedback..."


# ── inspect_file 溯源扩展 ──────────────────────────────────────────────────────
# executor 在每次工具调用前通过 set_file_inspect_workspace 注入当前 workspace，
# inspect_file 据此定位 file_metadata.json 并附加文件溯源信息。

_file_inspect_workspace: contextvars.ContextVar[str] = contextvars.ContextVar("file_inspect_workspace", default="")


def set_file_inspect_workspace(workspace: str) -> None:
    """由 executor 在每次工具调用前设置 workspace，供 inspect_file 溯源查询使用。"""
    _file_inspect_workspace.set(workspace)


def _lookup_file_lineage(abs_path: str) -> dict[str, Any] | None:
    """在 file_metadata.json 中查找文件的溯源记录。"""
    workspace = _file_inspect_workspace.get()
    if not workspace:
        return None
    metadata_path = _resolve_lineage_metadata_path(workspace)
    if metadata_path is None or not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = payload.get("files", {})
    if not isinstance(files, dict):
        return None
    try:
        rel = str(Path(abs_path).relative_to(Path(workspace).resolve())).replace("\\", "/")
    except ValueError:
        return None
    record = files.get(rel)
    return record if isinstance(record, dict) else None


def _resolve_lineage_metadata_path(workspace: str) -> Path | None:
    """从 workspace 路径推导 file_metadata.json 的位置。

    路径结构：``~/.dataagent/{user_id}/{session_id}/`` →
    ``~/.dataagent/{user_id}/.memory/file_metadata.json``
    """
    try:
        from dataagent.utils.runtime_paths import dataagent_home

        ws = Path(workspace).expanduser().resolve()
        home = dataagent_home()
        rel = ws.relative_to(home)
        parts = rel.parts
        if parts:
            return home / parts[0] / ".memory" / "file_metadata.json"
    except (ValueError, ImportError):
        pass
    return None


def _resolve_bound_llm_model_name(*, tool_config: dict[str, Any] | None = None) -> str | None:
    """Resolve the MODEL registry key from injected ``ToolExecutionContext.tool_config``."""
    bound = str((tool_config or {}).get("llm_model") or "").strip()
    return bound or None


def _build_nl2sql_sub_agent_config(
    source_config: dict[str, Any],
    *,
    config_manager: Any,
    tool_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    temp_config = copy.deepcopy(source_config)

    # 用当前主 Agent 的 DATABASE 和 METAVISOR 配置做内存级覆盖
    runtime_database = config_manager.get("DATABASE")
    runtime_metavisor = config_manager.get("METAVISOR")
    if isinstance(runtime_database, dict) and runtime_database:
        temp_config["DATABASE"] = copy.deepcopy(runtime_database)
    if isinstance(runtime_metavisor, dict) and runtime_metavisor:
        temp_config["METAVISOR"] = copy.deepcopy(runtime_metavisor)

    bound_llm_model_name = _resolve_bound_llm_model_name(tool_config=tool_config)
    if bound_llm_model_name:
        runtime_model = config_manager.get(f"MODEL.{bound_llm_model_name}")
        if isinstance(runtime_model, dict) and runtime_model:
            runtime_model_copy = copy.deepcopy(runtime_model)
            runtime_params = runtime_model_copy.get("params")
            if isinstance(runtime_params, dict):
                runtime_params["temperature"] = 0.0
            temp_config["MODEL"] = {
                bound_llm_model_name: runtime_model_copy,
            }
    agent_tools = [i.get("function", "") for i in config_manager.get("TOOLS", {}).get("local_functions", {})]
    if "search_metric_instance" in agent_tools or "search_tables_with_typename" in agent_tools:
        temp_config["CORE"]["perceptor"]["user_schema"] = "schema_schemair"
    if "search_udf_function_by_name_keyword" in agent_tools:
        temp_config["CORE"]["perceptor"]["user_evidence"] = "schema_udf_basic"

    return temp_config


tool_mapping = {
    "natural_language_to_sql": natural_language_to_sql,
    "llm_analyzer": llm_analyzer,
    "natural_language_to_plot": natural_language_to_plot,
    "report_generator": report_generator,
    "metrics_calculator": metrics_calculator,
    "file_saver": file_saver,
    "sub_agent_tool": sub_agent_tool,
    "bash": bash,
    "apply_patch": apply_patch,
    "edit_file": edit_file,
    "inspect_file": inspect_file,
    "read_file": read_file,
    "write_file": write_file,
    "request_human_feedback": request_human_feedback,
    "nl2sql_sub_agent_tool": nl2sql_sub_agent_tool,
}
