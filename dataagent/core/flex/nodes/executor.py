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
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from loguru import logger

from dataagent.actions.tools.backfill import ToolArgBackfiller
from dataagent.actions.tools.concurrency import ConcurrencyController
from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolHookRunner, readonly_tool_args
from dataagent.actions.tools.local_tool.sandbox import (
    reset_current_sandbox,
    set_current_sandbox,
)
from dataagent.actions.tools.local_tool.tools import (
    reset_subagent_runtime_context,
    set_file_inspect_workspace,
    set_subagent_runtime_context,
)
from dataagent.actions.tools.schema_validator import ParamsValueError, SchemaValidator
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.framework_adapters.runtime.context import get_stream_writer
from dataagent.core.managers.action_manager import ToolResult
from dataagent.core.managers.action_manager.base import (
    DEFAULT_RETRY_POLICY,
    ERROR_POLICIES,
    ErrorPolicy,
    ErrorType,
    ToolError,
    classify_exception,
)
from dataagent.core.swarm.swarm_config import swarm_enabled, swarm_worker_max_concurrent
from dataagent.core.utils.performance import measure_tool
from dataagent.utils.constants import DEFAULT_MAX_TOOL_RESULT_LENGTH
from dataagent.utils.converter.ir_message_consumer import try_replace_with_ir
from dataagent.utils.converter.result_ir_converter import ResultIRConverter
from dataagent.utils.messages_utils import record_message, truncate_tool_result


@dataclass
class NormalizedToolExecution:
    tool_name: str
    tool_call_id: str
    tool_args: dict[str, Any]
    success: bool
    raw_result: Any = None
    output_text: str = ""
    original_msg: str | None = None
    frontend_msg: str | None = None
    error_text: str | None = None
    error_type: str = ""  # ErrorType 枚举值
    retry_info: dict = field(default_factory=dict)  # {"attempt": 1, "max_retries": 3}
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ToolCallExecutionSetup:
    """Mutable context assembled before a single tool call is executed."""

    tool_name: str
    tool_args: dict[str, Any]
    tool_call_id: str
    progress_finalize: Any
    metadata: dict[str, Any]
    guard_token: Any
    context_token: Any


_BASH_COMMAND_SEPARATORS = re.compile(r"[;&|\n]")
_VARIABLE_ASSIGNMENT = re.compile(r"^[a-zA-Z_]\w*=")


def _extract_base_commands(command_str: str) -> list[str]:
    """从 bash 命令字符串中提取所有基础命令名（不含参数）。

    按 ``;``, ``&&``, ``||``, ``|``, 换行 拆分命令段，
    取每段的首个非变量赋值词作为命令名（含路径时取 basename）。
    """
    segments = _BASH_COMMAND_SEPARATORS.split(command_str)
    commands: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        words = seg.split()
        for word in words:
            if _VARIABLE_ASSIGNMENT.match(word):
                continue
            commands.append(word.rsplit("/", 1)[-1])
            break
    return commands


class Executor(BaseNode):
    def __init__(self, name: str, max_concurrency: int | None = None, **kwargs):
        super().__init__(name, enabled=True, **kwargs)
        self._concurrency = ConcurrencyController(max_concurrency=max_concurrency)
        self._schema_validator = SchemaValidator()
        self._backfiller: ToolArgBackfiller | None = None
        self._max_tool_result_length: int = int(
            self.config.get("max_tool_result_length", DEFAULT_MAX_TOOL_RESULT_LENGTH)
        )
        self._file_node_threshold: int | None = None

    @staticmethod
    def _stringify_tool_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _apply_hook_failure_to_execution(
        execution: NormalizedToolExecution,
        exc: Exception,
        *,
        phase: str,
    ) -> NormalizedToolExecution:
        """Mark a successful tool run as failed when post-hook raises."""
        prefix = "[post-hook]" if phase == "post" else "[hook]"
        message = str(exc)
        if not message.startswith("["):
            message = f"{prefix} {message}"
        execution.success = False
        execution.error_text = message
        execution.error_type = ErrorType.VALIDATION_ERROR.value
        execution.retry_info = {"attempt": 0, "max_retries": 0, "retriable": False}
        return execution

    def reconfig(self, **kwargs):
        max_concurrency = kwargs.get("max_concurrency")
        self._concurrency = ConcurrencyController(max_concurrency=max_concurrency)
        self._schema_validator = SchemaValidator()
        self._backfiller = None

    def _apply_runtime_max_concurrency(self, runtime: Any) -> None:
        """从 runtime 读取 max_concurrency 配置并更新并发控制器

        取 min(自动计算的并发数, YAML配置的并发数)
        """
        if runtime is not None and hasattr(runtime, "env"):
            yaml_max_concurrency = getattr(runtime.env, "max_concurrency", None)
            if yaml_max_concurrency is not None and yaml_max_concurrency > 0:
                self._concurrency.update_max_concurrency(yaml_max_concurrency)

    def _classify_error(self, error: Exception) -> ErrorPolicy:
        """根据错误类型和启发式规则分类错误（使用统一分类函数）"""
        if isinstance(error, ToolError):
            return ERROR_POLICIES.get(error.error_type, DEFAULT_RETRY_POLICY)

        _, policy = classify_exception(error)
        return policy

    def _calculate_backoff(self, policy: ErrorPolicy, attempt: int) -> float:
        """计算退避时间"""
        if policy.backoff_type == "exponential":
            return policy.backoff_base * (2**attempt)
        else:  # fixed
            return policy.backoff_base

    def _reset_context(self, context_token, guard_token) -> None:
        """清理运行时 context 和 sandbox"""
        reset_subagent_runtime_context(context_token)
        if guard_token is not None:
            reset_current_sandbox(guard_token)

    def _build_subagent_progress_emitter(
        self,
        *,
        runtime: Any,
        tool_call_id: str,
        tool_name: str,
    ) -> tuple[Any, Any]:
        """为 subagent 子进程 stderr 进度构建回调（Web 用），并返回 finalize。

        - 若存在 ``runtime.on_subagent_progress``（CLI debug rich renderer 使用），仅透传给该回调，
          不写入任何 stream 事件，避免影响原 CLI 体验。
        - Web 流式：复用 ``type=execution_msg``，并通过 ``extra_msg=subagent_progress`` + ``tool_call_id`` 聚合，
          同时做节流避免刷爆前端。
        """
        base_cb = getattr(runtime, "on_subagent_progress", None) if runtime is not None else None
        if base_cb is not None:

            def _noop_finalize(*_args: Any, **_kwargs: Any) -> None:
                return

            return base_cb, _noop_finalize

        writer = get_stream_writer()
        flush_interval_s = 0.15
        max_buffer_chars = 4096
        buf: list[str] = []
        buf_chars = 0
        last_flush_at = 0.0
        started = False

        def _flush(*, is_final: bool = False) -> None:
            nonlocal buf_chars, last_flush_at, started
            now = time.monotonic()
            if buf:
                content = "".join(buf)
                buf.clear()
                buf_chars = 0
                last_flush_at = now
                started = True
                writer(
                    {
                        "type": "execution_msg",
                        "node_name": self.name,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "extra_msg": "subagent_progress",
                        "content": content,
                    }
                )
            if is_final:
                # Close the code fence if we ever started streaming.
                if started:
                    writer(
                        {
                            "type": "execution_msg",
                            "node_name": self.name,
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "extra_msg": "subagent_progress",
                            "content": "\n```\n",
                        }
                    )
                writer(
                    {
                        "type": "execution_msg",
                        "node_name": self.name,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "extra_msg": "subagent_progress",
                        "content": "",
                        "is_final": True,
                    }
                )

        def on_progress(_tcid: str, hint_text: str) -> None:
            nonlocal buf_chars, last_flush_at, started
            text = str(hint_text or "").strip()
            if not text:
                return
            if not started and not buf:
                # Title + open a fenced code block to preserve line breaks.
                # Using a code fence avoids relying on markdown hard-break rules in the frontend.
                header = f"**{tool_name} 工具执行过程：**\n\n```text\n"
                buf.append(header)
                buf_chars += len(header)
            line = f"↳ {text}\n"
            buf.append(line)
            buf_chars += len(line)
            now = time.monotonic()
            if buf_chars >= max_buffer_chars or (now - last_flush_at) >= flush_interval_s:
                _flush(is_final=False)

        def finalize(*, is_final: bool = True) -> None:
            _flush(is_final=False)
            if is_final:
                _flush(is_final=True)

        return on_progress, finalize

    def _get_tool_schema(self, tool_name: str, runtime: Any = None):
        """获取工具的 Schema，如果工具不存在或没有 schema 则返回 None。"""
        try:
            if runtime is not None:
                tm = runtime.tool_manager
                if tm is not None:
                    return tm.get_schema(tool_name)
        except Exception as e:
            logger.debug(f"Failed to get schema for tool '{tool_name}': {e}")
        return None

    async def _aprocess(self, state: Any, runtime: Any = None) -> dict[str, Any] | FlexState:
        context = get_context_for_flex_state(state, runtime)
        writer = get_stream_writer()
        message = state["messages"][-1]
        if not isinstance(message, AIMessage):
            raise Exception(f"Executor received Invalid message {message}")

        workspace_str: str | None = str(runtime.workspace_dir) if runtime is not None else None

        # 应用 YAML 配置的最大并发数限制
        self._apply_runtime_max_concurrency(runtime)
        # 从 CONTEXT 读取 file_node_threshold，供 _convert_ir 使用
        if runtime is not None and hasattr(runtime, "env"):
            self._file_node_threshold = getattr(runtime.env, "file_node_threshold", None)

        tool_messages = self._build_invalid_tool_messages(message.invalid_tool_calls, writer)

        state_updates: dict[str, Any] = {
            "num_valid_tool_calls": len(message.tool_calls),
            "num_invalid_tool_calls": len(message.invalid_tool_calls),
        }

        # 检查同一轮 tool_calls 中是否有多个 sub_agent_tool 使用同一个显式 sub_id，
        # 这些调用都会被转成 validation error， 因为同一个 sub_id 的subagent不允许被并发调用
        blocked_parallel_tool_calls = self._blocked_sub_agent_tool_executions_this_round(
            message.tool_calls, runtime=runtime
        )

        parallel_tasks: dict[str, asyncio.Task[NormalizedToolExecution]] = {}
        tool_call_specs: dict[str, dict[str, Any]] = {}
        for tool_call in message.tool_calls:
            tool_call_id = str(tool_call["id"])
            tool_name = str(tool_call["name"])
            tool_args = tool_call.get("args", {})
            tool_call_specs[tool_call_id] = {"name": tool_name, "args": tool_args}
            if tool_call_id in blocked_parallel_tool_calls:
                continue
            self._emit_tool_status(
                writer,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_args=tool_args,
                status="start",
            )

            async def wrapped_execute(tc: Any) -> NormalizedToolExecution:
                return await self._execute_tool_call(
                    tc,
                    workspace=workspace_str,
                    user_id=str(state.get("user_id")) if state.get("user_id") is not None else None,
                    session_id=str(state.get("session_id")) if state.get("session_id") is not None else None,
                    sub_id=int(state.get("sub_id")) if state.get("sub_id") is not None else None,
                    runtime=runtime,
                )

            parallel_tasks[tool_call_id] = asyncio.create_task(
                self._concurrency.execute(tool_call_id, tool_name, tool_args, wrapped_execute(tool_call))
            )

        parallel_results: dict[str, NormalizedToolExecution] = {}
        parallel_results.update(blocked_parallel_tool_calls)
        if parallel_tasks:
            task_to_call_id = {task: tool_call_id for tool_call_id, task in parallel_tasks.items()}
            pending_tasks = set(parallel_tasks.values())
            while pending_tasks:
                done, pending_tasks = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    tool_call_id = task_to_call_id[task]
                    tool_spec = tool_call_specs[tool_call_id]
                    try:
                        execution = await task
                    except Exception as exc:
                        policy = self._classify_error(exc)
                        execution = NormalizedToolExecution(
                            tool_name=str(tool_spec["name"]),
                            tool_call_id=tool_call_id,
                            tool_args=dict(tool_spec["args"]),
                            success=False,
                            error_text=str(exc),
                            error_type=policy.error_type.value,
                            retry_info={"attempt": 0, "max_retries": policy.max_retries, "retriable": policy.retriable},
                            metadata={"workspace": workspace_str},
                        )
                    parallel_results[tool_call_id] = execution
                    self._emit_tool_status(
                        writer,
                        tool_call_id=execution.tool_call_id,
                        tool_name=execution.tool_name,
                        tool_args=execution.tool_args,
                        status="success" if execution.success else "error",
                        error_text=execution.error_text,
                        summary=execution.frontend_msg or execution.output_text,
                    )
                    self._emit_tool_execution_output(execution, context, writer)

        for tool_call in message.tool_calls:
            tool_call_id = str(tool_call["id"])
            execution = parallel_results[tool_call_id]
            tool_msg = self._build_tool_message(execution)
            tool_msg = self._maybe_replace_with_ir(tool_msg, context)
            tool_messages.append(tool_msg)

        for tool_message in tool_messages:
            record_message(context, tool_message)
        state_updates["messages"] = tool_messages

        return state_updates

    def _blocked_sub_agent_tool_executions_this_round(
        self, tool_calls: Sequence[Any], *, runtime: Any = None
    ) -> dict[str, NormalizedToolExecution]:
        """Return synthetic failed executions for ``sub_agent_tool`` calls blocked before launch.

        Applies two guardrails for one AIMessage round:

        - Same explicit ``sub_id`` reused by multiple ``sub_agent_tool`` calls (would corrupt one worker dir).
        - Optional ``SWARM.worker_max_concurrent`` ceiling when swarm mode is enabled.

        Returns:
            Map ``tool_call_id`` → ``NormalizedToolExecution`` rows merged from both rules.
        """
        duplicate_blocks = self._blocked_executions_for_duplicate_sub_agent_sub_id_same_round(tool_calls)
        merged: dict[str, NormalizedToolExecution] = dict(duplicate_blocks)
        agent_cfg: dict[str, Any] = {}
        if runtime is not None and hasattr(runtime, "get_all_config"):
            agent_cfg = runtime.get_all_config() or {}
        if not swarm_enabled(agent_cfg):
            return merged
        cap = swarm_worker_max_concurrent(agent_cfg)
        if cap is None:
            return merged
        merged.update(
            self._blocked_executions_for_sub_agent_tools_over_parallel_cap(
                tool_calls,
                duplicate_blocks,
                max_concurrent=cap,
            )
        )
        return merged

    def _blocked_executions_for_duplicate_sub_agent_sub_id_same_round(
        self, tool_calls: Sequence[Any]
    ) -> dict[str, NormalizedToolExecution]:
        """Mark every ``sub_agent_tool`` call whose explicit ``sub_id`` clashes with another call in this round."""
        seen: dict[int, list[Any]] = {}
        for tool_call in tool_calls:
            if str(tool_call.get("name")) != "sub_agent_tool":
                continue
            args = tool_call.get("args", {})
            if not isinstance(args, dict) or args.get("sub_id") is None:
                continue
            try:
                sub_id = int(args["sub_id"])
            except (TypeError, ValueError):
                continue
            seen.setdefault(sub_id, []).append(tool_call)

        results: dict[str, NormalizedToolExecution] = {}
        for sub_id, calls in seen.items():
            if len(calls) < 2:
                continue
            for call in calls:
                call_id = str(call["id"])
                args = call.get("args", {})
                results[call_id] = NormalizedToolExecution(
                    tool_name="sub_agent_tool",
                    tool_call_id=call_id,
                    tool_args=dict(args) if isinstance(args, dict) else {},
                    success=False,
                    error_text=(
                        f"duplicate sub_id {sub_id} in the same executor round; "
                        "create a new subagent for concurrent work instead of reusing this worker."
                    ),
                    error_type=ErrorType.VALIDATION_ERROR.value,
                    retry_info={"attempt": 0, "max_retries": 0, "retriable": False},
                )
        return results

    def _blocked_executions_for_sub_agent_tools_over_parallel_cap(
        self,
        tool_calls: Sequence[Any],
        duplicate_sub_id_blocks: dict[str, NormalizedToolExecution],
        *,
        max_concurrent: int,
    ) -> dict[str, NormalizedToolExecution]:
        """Mark excess ``sub_agent_tool`` calls when this round fans out beyond ``max_concurrent``.

        Walks ``tool_calls`` in order. Calls already listed in ``duplicate_sub_id_blocks``
        are skipped (duplicate-worker guard takes precedence). Among remaining
        ``sub_agent_tool`` entries, only the first ``max_concurrent`` may execute; the rest
        receive validation-style failures without subprocess launch.

        Args:
            tool_calls: Tool calls from the current AIMessage, in planner order.
            duplicate_sub_id_blocks: Blocks from
                ``_blocked_executions_for_duplicate_sub_agent_sub_id_same_round``.
            max_concurrent: Positive ceiling from ``swarm_worker_max_concurrent()`` (caller
                must not pass non-positive values; those are normalized away at config read).
        """
        results: dict[str, NormalizedToolExecution] = {}
        scheduled = 0
        limit = int(max_concurrent)
        for tool_call in tool_calls:
            tool_call_id = str(tool_call.get("id"))
            if tool_call_id in duplicate_sub_id_blocks:
                continue
            if str(tool_call.get("name")) != "sub_agent_tool":
                continue
            scheduled += 1
            if scheduled <= limit:
                continue
            args = tool_call.get("args", {})
            results[tool_call_id] = NormalizedToolExecution(
                tool_name="sub_agent_tool",
                tool_call_id=tool_call_id,
                tool_args=dict(args) if isinstance(args, dict) else {},
                success=False,
                error_text=(
                    f"parallel sub_agent_tool limit exceeded for this round "
                    f"(SWARM.worker_max_concurrent={max_concurrent}); reduce concurrency or sequence calls."
                ),
                error_type=ErrorType.VALIDATION_ERROR.value,
                retry_info={"attempt": 0, "max_retries": 0, "retriable": False},
            )
        return results

    def _build_invalid_tool_messages(self, invalid_tool_calls: Sequence[Any], writer) -> list[ToolMessage]:
        tool_messages: list[ToolMessage] = []
        for invalid_tool in invalid_tool_calls:
            tool_name = invalid_tool.get("name", "unknown")
            error_msg = invalid_tool.get("error", "Unknown error")
            writer(
                {
                    "type": "output_msg",
                    "node_name": self.name,
                    "content": f"\n\n**❌ 调用工具: {tool_name}**\n\n报错: {error_msg}\n\n",
                }
            )
            writer({"type": "break"})
            tool_messages.append(
                ToolMessage(
                    content=f"Error: Invalid tool call - {error_msg}",
                    tool_call_id=str(invalid_tool["id"]),
                    name=tool_name,
                    status="error",
                )
            )
        return tool_messages

    @measure_tool
    async def _execute_tool_call(
        self,
        tool_call: Any,
        workspace: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        sub_id: int | None = None,
        runtime: Any = None,
    ) -> NormalizedToolExecution:
        return await self._execute_tool_call_impl(
            tool_call=tool_call,
            workspace=workspace,
            user_id=user_id,
            session_id=session_id,
            sub_id=sub_id,
            runtime=runtime,
        )

    async def _execute_tool_call_impl(
        self,
        *,
        tool_call: Any,
        workspace: str | None,
        user_id: str | None,
        session_id: str | None,
        sub_id: int | None,
        runtime: Any,
    ) -> NormalizedToolExecution:
        setup = self._setup_tool_call_execution(tool_call, workspace, user_id, session_id, sub_id, runtime)
        tool_args, backfill_changes = self._validate_and_backfill_args(
            tool_name=setup.tool_name,
            tool_args=setup.tool_args,
            tool_call_id=setup.tool_call_id,
            runtime=runtime,
        )
        if backfill_changes:
            setup.metadata["backfill_changes"] = backfill_changes
        self._check_bash_whitelist(setup.tool_name, tool_args, setup.tool_call_id, runtime)

        pre_hooks, post_hooks = self._resolve_tool_hooks(runtime, setup.tool_name)
        hook_inv = self._build_tool_hook_invocation(
            setup=setup,
            tool_args=tool_args,
            runtime=runtime,
            phase="pre",
        )

        try:
            await ToolHookRunner.run_pre_hooks(pre_hooks, hook_inv)
        except Exception as exc:
            self._finalize_tool_progress_safe(setup.progress_finalize)
            self._reset_context(setup.context_token, setup.guard_token)
            return self._failed_execution_from_hook(setup, dict(hook_inv.tool_args), exc, phase="pre")

        execution: NormalizedToolExecution | None = None
        tool_result: ToolResult | None = None
        policy = DEFAULT_RETRY_POLICY
        last_error_type = ErrorType.UNKNOWN

        try:
            tool_result = await self._invoke_manager_tool_async(setup.tool_name, tool_args, runtime=runtime)
            self._reset_context(setup.context_token, setup.guard_token)
            execution = self._normalize_tool_execution(
                tool_name=setup.tool_name,
                tool_call_id=setup.tool_call_id,
                tool_args=tool_args,
                result=tool_result,
                metadata={**setup.metadata, "source": "tool_manager"},
            )
        except Exception as exc:
            last_error = exc
            policy = self._classify_error(exc)
            last_error_type = policy.error_type
            final_max_retries = policy.max_retries

            retry_result, last_error, last_error_type, final_max_retries = await self._retry_tool_execution(
                tool_name=setup.tool_name,
                tool_args=tool_args,
                tool_call_id=setup.tool_call_id,
                context_token=setup.context_token,
                guard_token=setup.guard_token,
                policy=policy,
                last_error=last_error,
                last_error_type=last_error_type,
                final_max_retries=final_max_retries,
                metadata=setup.metadata,
                runtime=runtime,
            )
            if retry_result is not None:
                execution = retry_result
            else:
                logger.debug(
                    f"[Executor] Tool '{setup.tool_name}' failed after {policy.max_retries} retries: {last_error}"
                )
                self._reset_context(setup.context_token, setup.guard_token)
                execution = NormalizedToolExecution(
                    tool_name=setup.tool_name,
                    tool_call_id=setup.tool_call_id,
                    tool_args=tool_args,
                    success=False,
                    error_text=str(last_error),
                    error_type=last_error_type.value,
                    retry_info={
                        "attempt": policy.max_retries,
                        "max_retries": final_max_retries,
                        "retriable": policy.retriable,
                    },
                    metadata=setup.metadata,
                )

        if execution is not None and post_hooks:
            hook_inv.phase = "post"
            hook_inv.tool_args = readonly_tool_args(tool_args)
            hook_inv.tool_result = tool_result
            hook_inv.execution = execution
            try:
                await ToolHookRunner.run_post_hooks(post_hooks, hook_inv)
            except Exception as exc:
                execution = self._apply_hook_failure_to_execution(execution, exc, phase="post")

        self._finalize_tool_progress_safe(setup.progress_finalize)
        return execution

    def _resolve_tool_hooks(self, runtime: Any, tool_name: str) -> tuple[list[Any], list[Any]]:
        """Return pre/post hook lists registered on the tool instance."""
        if runtime is None or not hasattr(runtime, "get_tool"):
            return [], []
        try:
            tool = runtime.get_tool(tool_name)
        except Exception:
            return [], []
        pre = list(getattr(tool, "pre_hooks", None) or [])
        post = list(getattr(tool, "post_hooks", None) or [])
        return pre, post

    def _build_tool_hook_invocation(
        self,
        *,
        setup: _ToolCallExecutionSetup,
        tool_args: dict[str, Any],
        runtime: Any,
        phase: str,
    ) -> ToolHookInvocation:
        """Build per-call hook invocation (shared ``hook_context`` across pre/post)."""
        return ToolHookInvocation(
            tool_name=setup.tool_name,
            tool_call_id=setup.tool_call_id,
            tool_args=readonly_tool_args(tool_args),
            runtime=runtime,
            metadata=dict(setup.metadata),
            phase=phase,  # type: ignore[arg-type]
        )

    def _failed_execution_from_hook(
        self,
        setup: _ToolCallExecutionSetup,
        tool_args: dict[str, Any],
        exc: Exception,
        *,
        phase: str,
    ) -> NormalizedToolExecution:
        """Convert a hook failure into ``NormalizedToolExecution`` (no tool retry)."""
        prefix = "[pre-hook]" if phase == "pre" else "[post-hook]"
        message = str(exc)
        if not message.startswith("["):
            message = f"{prefix} {message}"
        return NormalizedToolExecution(
            tool_name=setup.tool_name,
            tool_call_id=setup.tool_call_id,
            tool_args=tool_args,
            success=False,
            error_text=message,
            error_type=ErrorType.VALIDATION_ERROR.value,
            retry_info={"attempt": 0, "max_retries": 0, "retriable": False},
            metadata=setup.metadata,
        )

    def _finalize_tool_progress_safe(self, progress_finalize: Any) -> None:
        """Finalize subagent progress stream; log and swallow errors to avoid masking tool results.

        Args:
            progress_finalize: Callback returned by :meth:`_build_subagent_progress_emitter`.
        """
        try:
            progress_finalize(is_final=True)
        except Exception:
            logger.debug("Failed to finalize subagent progress stream", exc_info=True)

    async def _retry_tool_execution(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        context_token: Any,
        guard_token: Any,
        policy: ErrorPolicy,
        last_error: Exception,
        last_error_type: ErrorType,
        final_max_retries: int,
        metadata: dict[str, Any],
        runtime: Any = None,
    ) -> tuple[NormalizedToolExecution | None, Exception, ErrorType, int]:
        """按退避策略重试工具执行。

        Returns:
            (result, last_error, last_error_type, final_max_retries)
            其中 result 非 None 表示重试成功，调用方应直接返回。
        """
        _last_error = last_error
        _last_error_type = last_error_type
        _final_max_retries = final_max_retries

        for attempt in range(1, policy.max_retries + 1):
            if not policy.retriable:
                break

            backoff = self._calculate_backoff(policy, attempt - 1)
            logger.debug(f"[Executor] Retry {attempt}/{policy.max_retries} for '{tool_name}' after {backoff}s backoff")
            await asyncio.sleep(backoff)

            try:
                result = await self._invoke_manager_tool_async(tool_name, tool_args, runtime=runtime)
                self._reset_context(context_token, guard_token)
                return (
                    self._normalize_tool_execution(
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        tool_args=tool_args,
                        result=result,
                        metadata={**metadata, "source": "tool_manager", "retry_attempt": attempt},
                        error_type=_last_error_type.value,
                        retry_info={"attempt": attempt, "max_retries": policy.max_retries},
                    ),
                    _last_error,
                    _last_error_type,
                    _final_max_retries,
                )
            except Exception as retry_exc:
                _last_error = retry_exc
                new_policy = self._classify_error(retry_exc)
                _last_error_type = new_policy.error_type
                if not new_policy.retriable or attempt >= new_policy.max_retries:
                    _final_max_retries = new_policy.max_retries
                    break

        return None, _last_error, _last_error_type, _final_max_retries

    def _setup_tool_call_execution(
        self,
        tool_call: Any,
        workspace: str | None,
        user_id: str | None,
        session_id: str | None,
        sub_id: int | None,
        runtime: Any,
    ) -> _ToolCallExecutionSetup:
        """Prepare progress emitter, metadata, sandbox guard, and subagent runtime context for one tool call.

        Args:
            tool_call: Raw tool call dict from the LLM (name, args, id).
            workspace: Workspace directory path, if any.
            user_id: User identifier for subagent context.
            session_id: Session identifier for subagent context.
            sub_id: Sub-agent identifier for subagent context.
            runtime: Per-call Runtime instance.

        Returns:
            Setup bundle consumed by :meth:`_execute_tool_call`.
        """
        tool_name = str(tool_call["name"])
        tool_args = tool_call.get("args", {})
        tool_call_id = str(tool_call["id"])
        progress_cb, progress_finalize = self._build_subagent_progress_emitter(
            runtime=runtime,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        pre_existing_files = ResultIRConverter.snapshot_dir(workspace)
        metadata = {
            "workspace": workspace,
            "pre_existing_files": pre_existing_files,
            "user_id": user_id,
            "session_id": session_id,
            "sub_id": sub_id,
        }

        if workspace:
            set_file_inspect_workspace(workspace)
        guard_token = None
        if runtime is not None:
            guard_token = set_current_sandbox(runtime.sandbox)
            logger.debug(
                "[tool call] {} | workspace={} skills={{{}}}",
                tool_name,
                runtime.sandbox.workspace_root,
                ", ".join(f"{k}: {v}" for k, v in runtime.sandbox.skill_aliases.items()),
            )
        agent_cfg: dict[str, Any] = {}
        if runtime is not None and hasattr(runtime, "get_all_config"):
            agent_cfg = runtime.get_all_config() or {}
        context_token = set_subagent_runtime_context(
            user_id=user_id,
            session_id=session_id,
            sub_id=sub_id,
            progress_callback=progress_cb,
            tool_call_id=tool_call_id,
            agent_config=agent_cfg,
        )
        return _ToolCallExecutionSetup(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            progress_finalize=progress_finalize,
            metadata=metadata,
            guard_token=guard_token,
            context_token=context_token,
        )

    async def _try_execute_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        metadata: dict[str, Any],
        context_token: Any,
        guard_token: Any,
        runtime: Any = None,
    ) -> NormalizedToolExecution:
        result = await self._invoke_manager_tool_async(tool_name, tool_args, runtime=runtime)
        self._reset_context(context_token, guard_token)
        return self._normalize_tool_execution(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
            result=result,
            metadata={**metadata, "source": "tool_manager"},
        )

    def _validate_and_backfill_args(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        runtime: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        """校验工具参数并执行回填

        Args:
            tool_name: 工具名称
            tool_args: 原始工具参数
            tool_call_id: 工具调用 ID（用于错误信息）
            runtime: 运行时上下文

        Returns:
            (回填后的参数, 回填变更列表)

        Raises:
            ParamsValueError: 参数校验失败
        """
        schema = self._get_tool_schema(tool_name, runtime=runtime)
        if schema is None:
            return dict(tool_args), None

        validation_result = self._schema_validator.validate(tool_name, tool_args, schema)

        if not validation_result.valid:
            raise ParamsValueError(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                errors=validation_result.errors,
                message=validation_result.formatted_message,
            )

        # 使用校验后的参数（含类型转换和截断）
        validated_args = validation_result.corrected_args

        backfiller = ToolArgBackfiller()
        backfill_result = backfiller.backfill(tool_name, validated_args, schema)
        backfill_changes = [change.to_dict() for change in backfill_result.changes] if backfill_result.changes else None

        return backfill_result.backfilled_args, backfill_changes

    def _check_bash_whitelist(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        runtime: Any,
    ) -> None:
        """校验 bash 工具的命令是否在白名单内。

        仅对 ``tool_name == "bash"`` 且配置了白名单时生效。
        校验失败时抛出 ``ParamsValueError``（被归类为不可重试的 VALIDATION_ERROR）。

        Raises:
            ParamsValueError: 命令不在白名单中
        """
        if tool_name != "bash" or runtime is None:
            return

        whitelist: list[str] | None = getattr(runtime, "bash_tool_whitelist", None)
        if whitelist is None:
            return

        command_str = str(tool_args.get("command", ""))
        if not command_str.strip():
            return

        base_commands = _extract_base_commands(command_str)
        disallowed = [cmd for cmd in base_commands if cmd not in whitelist]
        if not disallowed:
            return

        allowed_hint = ", ".join(sorted(whitelist))
        raise ParamsValueError(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            errors=[],
            message=(
                f"Bash command whitelist validation failed: "
                f"command(s) {disallowed!r} not in allowed list.\n"
                f"Full command: {command_str}\n"
                f"Allowed commands: [{allowed_hint}]\n"
                f"Hint: Reconstruct the bash call using only allowed commands."
            ),
        )

    async def _invoke_manager_tool_async(
        self, tool_name: str, tool_args: dict[str, Any], runtime: Any = None
    ) -> ToolResult:
        # Use runtime.call_tool (per-call, concurrency-safe)
        if runtime is not None and hasattr(runtime, "call_tool"):
            return await runtime.call_tool(tool_name, **tool_args)
        raise KeyError(f"Tool {tool_name!r} not found: runtime is required for tool execution")

    def _normalize_tool_execution(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        tool_args: dict[str, Any],
        result: Any,
        metadata: dict[str, Any],
        error_type: str = "",
        retry_info: dict | None = None,
    ) -> NormalizedToolExecution:
        if isinstance(result, ToolResult):
            merged_metadata = {**metadata, **result.metadata}
            if not result.success:
                err_type = error_type or (result.error_type.value if result.error_type else "")
                retry = retry_info or {}
                return NormalizedToolExecution(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    tool_args=tool_args,
                    success=False,
                    raw_result=result.data,
                    error_text=result.error or f"Tool '{tool_name}' failed with unknown error.",
                    error_type=err_type,
                    retry_info=retry,
                    metadata=merged_metadata,
                )
            return self._normalize_payload(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_args=tool_args,
                raw_result=result.data,
                metadata=merged_metadata,
            )

        return self._normalize_payload(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
            raw_result=result,
            metadata=metadata,
        )

    def _normalize_payload(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        tool_args: dict[str, Any],
        raw_result: Any,
        metadata: dict[str, Any],
    ) -> NormalizedToolExecution:
        parsed_result: Any = None
        if isinstance(raw_result, str):
            try:
                parsed_result = json.loads(raw_result)
            except Exception:
                parsed_result = None
        elif isinstance(raw_result, dict):
            parsed_result = raw_result

        output_text = "" if raw_result is None else self._stringify_tool_value(raw_result)
        original_msg: str | None = None
        frontend_msg: str | None = None
        normalized_raw_result = raw_result
        success = True
        error_text: str | None = None
        error_type: str = ""
        retry_info: dict = {}

        if isinstance(parsed_result, dict):
            normalized_raw_result = parsed_result.get("data", parsed_result)
            original_value = parsed_result.get("original_msg")
            frontend_value = parsed_result.get("frontend_msg")
            if original_value is not None:
                original_msg = self._stringify_tool_value(original_value)
            if frontend_value is not None:
                frontend_msg = self._stringify_tool_value(frontend_value)
            output_text = original_msg or frontend_msg or output_text

            inner_data = parsed_result.get("data")
            if isinstance(inner_data, dict):
                exit_code = inner_data.get("exit_code")
                if isinstance(exit_code, int) and exit_code != 0:
                    success = False
                    stderr = inner_data.get("stderr", "")
                    stdout = inner_data.get("stdout", "")

                    # 构建错误消息
                    parts = [f"Command exited with code {exit_code}"]
                    if stderr:
                        parts.append(f"[stderr]\n{stderr}")
                    if stdout:
                        parts.append(f"[stdout]\n{stdout}")
                    error_text = "\n".join(parts)

                    # 根据错误消息分类错误
                    error_msg = stderr or stdout or error_text
                    error_type_enum, error_policy = classify_exception(OSError(error_msg))
                    error_type = error_type_enum.value
                    retry_info = {
                        "max_retries": error_policy.max_retries,
                        "retriable": error_policy.retriable,
                    }

        return NormalizedToolExecution(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
            success=success,
            raw_result=normalized_raw_result,
            output_text=output_text,
            original_msg=original_msg,
            frontend_msg=frontend_msg,
            error_text=error_text,
            error_type=error_type,
            retry_info=retry_info,
            metadata=metadata,
        )

    def _emit_tool_status(
        self,
        writer,
        *,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        status: str,
        error_text: str | None = None,
        summary: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "tool_status",
            "node_name": self.name,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "status": status,
        }
        if error_text:
            event["error"] = error_text
        if summary:
            event["summary"] = summary
        writer(event)

    def _emit_tool_execution_output(self, execution: NormalizedToolExecution, context, writer) -> None:
        tool_name = execution.tool_name

        if not execution.success:
            error_str = execution.error_text or f"Tool '{tool_name}' failed with unknown error."
            logger.trace(f"[{self.name}] tool '{tool_name}' (call_id={execution.tool_call_id}) FAILED:\n{error_str}")

            # 构建带错误类型和重试信息的错误消息
            retry_info_str = ""
            if execution.retry_info:
                attempt = execution.retry_info.get("attempt", 0)
                max_retries = execution.retry_info.get("max_retries", 0)
                retry_info_str = f"\n错误类型: {execution.error_type or 'unknown'}"
                if attempt > 0:
                    retry_info_str += f"\n已重试: {attempt}/{max_retries} 次"
                elif max_retries > 0:
                    retry_info_str += f"\n最大重试次数: {max_retries}"
                if not execution.retry_info.get("retriable", False):
                    retry_info_str += " (不可重试)"

            writer(
                {
                    "type": "output_msg",
                    "node_name": self.name,
                    "content": f"**❌ {tool_name} 工具执行失败:**\n\n{error_str}{retry_info_str}\n\n",
                }
            )
            writer({"type": "break"})
            return

        display_output = execution.frontend_msg or execution.output_text
        logger.trace(
            f"[{self.name}] tool '{tool_name}' (call_id={execution.tool_call_id}) succeeded:\n{display_output}"
        )
        writer({"type": "output_msg", "node_name": self.name, "content": f"\n\n**{tool_name} 执行完成**\n\n"})
        writer({"type": "break"})
        writer(
            {
                "type": "execution_msg",
                "node_name": self.name,
                "content": f"**✅ {tool_name} 工具执行结果:**\n\n{display_output}\n\n",
            }
        )

        self._convert_ir(context, execution)

    def _build_tool_message(self, execution: NormalizedToolExecution) -> ToolMessage:
        tool_name = execution.tool_name
        tool_call_id = execution.tool_call_id
        if not execution.success:
            error_str = execution.error_text or f"Tool '{tool_name}' failed with unknown error."

            # 构建带结构化信息的错误消息
            retry_info_str = ""
            if execution.retry_info:
                attempt = execution.retry_info.get("attempt", 0)
                max_retries = execution.retry_info.get("max_retries", 0)
                if attempt > 0:
                    retry_info_str = f" (已重试 {attempt}/{max_retries} 次)"
                elif max_retries > 0:
                    retry_info_str = f" (最大重试次数: {max_retries})"

            content = f"Error executing {tool_name}: {error_str}"
            if execution.error_type:
                content += f"\n错误类型: {execution.error_type}"
            if retry_info_str:
                content += retry_info_str

            return ToolMessage(
                content=content,
                tool_call_id=tool_call_id,
                name=tool_name,
                status="error",
            )
        return ToolMessage(
            content=execution.original_msg or execution.output_text,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="success",
        )

    def _maybe_replace_with_ir(self, tool_msg: ToolMessage, context) -> ToolMessage:
        """如果 ToolMessage content 超长，用 IR 摘要替换；IR 节点不存在则截断兜底。"""
        content = tool_msg.content
        if not isinstance(content, str):
            content = str(content)
        if len(content) < self._max_tool_result_length:
            return tool_msg

        replaced = try_replace_with_ir(tool_msg, context)
        if replaced is not tool_msg:
            return replaced

        truncated = truncate_tool_result(content, max_length=self._max_tool_result_length)
        return tool_msg.model_copy(update={"content": truncated})

    def _convert_ir(self, context, execution: NormalizedToolExecution) -> None:
        action_node_label = f"Action({execution.tool_call_id})"
        try:
            convert_kwargs: dict[str, Any] = {
                "context": context,
                "tool_name": execution.tool_name,
                "tool_call_id": execution.tool_call_id,
                "tool_args": execution.tool_args,
                "result": execution.raw_result,
                "action_node_label": action_node_label,
                "workspace": execution.metadata.get("workspace"),
                "pre_existing_files": execution.metadata.get("pre_existing_files"),
            }
            if self._file_node_threshold is not None:
                convert_kwargs["knowledge_min_length"] = self._file_node_threshold
            created_ir = ResultIRConverter.convert(**convert_kwargs)
            if created_ir:
                logger.debug(f"Executor: created {len(created_ir)} IR node(s) for {execution.tool_name}: {created_ir}")
            else:
                logger.debug(f"Executor: no IR node created for {execution.tool_name}")
        except Exception as ir_err:
            logger.warning(f"Executor: IR conversion failed for {execution.tool_name}, skipping: {ir_err}")
