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
"""Agent post-hook: run the delivery's trial compute automatically.

Registered under ``HOOKS.nodes.planner.post`` in de_agent's ``hooks.yaml``.
When a turn completes with SQL deliverables in the workspace, this hook
trial-runs the INSERT into a scratch table and reports the data quality check —
the agent does not have to (and should not) call ``trial_compute_table`` by hand.

Why an agent post-hook rather than a prompt instruction:
the trial compute talks to DataOps and can take minutes; doing it inside the
actor loop would stall every turn. Running it at turn end keeps the loop fast
while still guaranteeing the check happens before delivery is finalized.

Safety properties:
  - Never raises. Any failure is logged; the turn's result is left untouched.
  - Runs at most once per run (``run_id``) via the runtime cache, so a retried
    or re-rendered turn does not re-run an expensive DataOps job.
  - Only fires for turns that actually produced a deliverable (``insert_*.sql``
    or ``create_*.sql``) and completed normally.
  - Data quality warnings are reported as text; they never mark the turn failed.

Why **node-level** (not ``HOOKS.agent.post``):
    The report has to reach the frontend on **both** paths:
  - non-streaming (chat / REST / A2A non-stream) reads ``state["messages"][-1]``
  - streaming (A2A SSE / Web) reads ``output_msg`` stream events

A node-level post-hook runs inside ``node.aprocess()``, i.e. **inside**
langgraph's runnable context, so ``get_stream_writer()`` resolves and the
``output_msg`` event actually reaches the client. An agent-level post-hook runs
*after* the graph has finished — ``get_stream_writer()`` then raises
``RuntimeError("Called get_config outside of a runnable context")`` and the
stream push fails silently.

The report is **merged into** the last AI message rather than appended as a
new one: the non-streaming path reads ``messages[-1]``, so appending would
replace the agent's own delivery summary with the report.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.actions.tools.local_tool.dataops_quality_check import render_metrics_table
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.framework_adapters.runtime.context import get_stream_writer

# Runtime cache key holding the set of run_ids that already trial-computed.
_TRIAL_COMPUTE_DONE_KEY = "dataops_trial_compute_done_runs"

# Message role used to surface the report. Prefixed so it is identifiable in the
# transcript as machine-generated rather than agent-authored prose.
_REPORT_PREFIX = "【自动数据试算】"


def dataops_quality_check_post(
    state: FlexState,
    runtime: Runtime,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    """Trial-run the delivered DML and append a data quality report to the turn.

    Args:
        state: The node's return delta (node-level post-hook) **or** the full
            graph state (agent-level post-hook). Handled by :func:`_run_hook`.
        runtime: Per-invocation runtime, used for workspace and cache access.
        original_state: The node's **input** state, supplied by
            ``BaseNode.aprocess`` for node-level post-hooks. It carries the
            fields the node delta lacks (``run_id``, ``messages`` history, ...).

    Returns:
        The (possibly extended) state. On any failure the input state is
        returned unchanged.
    """
    try:
        return _run_hook(state, runtime, original_state=original_state)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[trial_compute_post] hook failed (ignored): {exc}")
        return state


def _run_hook(
    state: FlexState,
    runtime: Runtime,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    # 节点级 post-hook 收到的是**节点返回的增量**（只有 messages/complete/
    # num_turns/curr_iter），不含 run_id 等上下文字段；agent 级收到的是完整
    # 图状态。两者都要能跑，所以下面按「哪个有值用哪个」取值。
    #
    # 注意 messages 的形态差异：
    #   - 节点级增量的 messages 是**单条** AIMessage（planner 返回的形态）；
    #   - 完整图状态的 messages 是**列表**。
    # 合并时必须保持各自的形态，否则 reducer 会把消息重复累加。
    full_state: FlexState = state
    if original_state is not None:
        merged_context: dict[str, Any] = dict(original_state)
        for key, value in dict(state).items():
            if value is not None:
                merged_context[key] = value
        full_state = merged_context  # type: ignore[assignment]

    if not full_state.get("complete"):
        logger.debug("[trial_compute_post] skip: turn not complete")
        return state

    run_id = full_state.get("run_id", 0)
    if _already_ran(runtime, run_id):
        logger.debug("[trial_compute_post] skip: run_id={} already trial-computed", run_id)
        return state

    workspace = _resolve_workspace(runtime)
    if workspace is None:
        logger.debug("[trial_compute_post] skip: no workspace_dir")
        return state

    dml_path = _find_dml(workspace)
    if dml_path is None:
        logger.debug("[trial_compute_post] skip: no insert_*.sql in {}", workspace)
        return state

    _mark_ran(runtime, run_id)
    logger.info("[trial_compute_post] trial-computing {} (run_id={})", dml_path.name, run_id)

    result = _invoke_trial_compute(dml_path=dml_path, runtime=runtime)
    report = _render_report(dml_path=dml_path, result=result)
    if not report:
        return state

    logger.info("[trial_compute_post] {}", report.splitlines()[0])

    # --- 1) 并入最后一条 AI 消息的 content --------------------------------
    # 非流式链路（chat / REST / A2A non-stream）由 ``state["messages"][-1]``
    # 取最终回复（见 rest_api/service.py::_format_result）。所以**不能**把报告
    # 追加成一条新 AIMessage —— 那样会顶掉 agent 自己的交付说明。
    # 正确做法是并入最后一条 AI 消息的 content：既保留交付说明，又带上表格。
    merged_state = _merge_report_into_last_message(full_state, report)
    # 只把「变更后的 messages」交回去：节点级 hook 的返回值是**增量**，
    # 若把 full_state 整体返回，run_id/complete 等字段会被 reducer 重复累加。
    # messages 的形态要跟 state 里原本的一致（单条 AIMessage vs 列表）。
    new_messages = merged_state.get("messages")
    result_delta: dict[str, Any] = {
        **{k: v for k, v in state.items() if k != "messages"},
        "messages": new_messages,
    }

    # --- 2) 推一条 output_msg 到流式前端 ---------------------------------
    # 流式链路（A2A SSE / Web）靠 stream 事件渲染，state 的变更不会被 yield。
    # 这里必须额外推一条 output_msg，前端才能看到这张 Markdown 表格。
    _emit_report_to_stream(report)
    return result_delta  # type: ignore[return-value]


def _merge_report_into_last_message(state: FlexState, report: str) -> FlexState:
    """把报告并入最后一条 AI 消息的 content，保持 messages[-1] 仍是最终回复。

    ``messages`` 的形态要保持不变：

    - **节点级** hook 拿到的是节点返回的增量，其 ``messages`` 是**单条**
      ``AIMessage``（BaseNode.aprocess 之后才会拼成列表）；
    - **agent 级** hook 拿到的是完整图状态，其 ``messages`` 是**列表**。

    两种形态都原样返回，避免把单条消息变成列表（或反之）导致 reducer
    把消息重复累加。

    找不到可并入的 AI 消息时，退回追加一条新 AIMessage（保证报告不丢）。
    """
    messages = state.get("messages")

    # --- 形态 1：单条 AIMessage（节点级增量）-----------------------------
    if isinstance(messages, AIMessage):
        return {**state, "messages": _merge_into_message(messages, report)}

    # --- 形态 2：列表（agent 级完整状态）----------------------------------
    if isinstance(messages, list):
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, AIMessage):
                continue
            new_messages = list(messages)
            new_messages[index] = _merge_into_message(message, report)
            return {**state, "messages": new_messages}
        # 列表里没有 AI 消息 —— 追加一条（下游仍能取到报告）
        return {**state, "messages": [*messages, AIMessage(content=report)]}

    # --- 形态 3：没有 messages --------------------------------------------
    return {**state, "messages": AIMessage(content=report)}


def _merge_into_message(message: AIMessage, report: str) -> AIMessage:
    """把报告追加到一条 AI 消息的 content 末尾，保留其余字段。"""
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    merged_content = f"{content}\n\n{report}" if content.strip() else report
    return AIMessage(
        content=merged_content,
        additional_kwargs=dict(message.additional_kwargs or {}),
        response_metadata=dict(message.response_metadata or {}),
        id=message.id,
        name=message.name,
        tool_calls=list(message.tool_calls or []),
        invalid_tool_calls=list(message.invalid_tool_calls or []),
        usage_metadata=message.usage_metadata,
    )


def _emit_report_to_stream(report: str) -> None:
    """把报告推给流式前端（best-effort，失败不影响交付）。

    只有**节点级** hook 里调用才有效：此时处于 langgraph 的 runnable context
    之内，``get_stream_writer()`` 能解析到 config，事件才会被 yield 给前端。

    若本函数在 agent 级 post-hook（图已跑完）里被调用，langgraph 会抛
    ``RuntimeError("Called get_config outside of a runnable context")``；
    框架层的 ``get_stream_writer`` 会把它兜成 no-op，所以这里必须显式
    捕获并记日志 —— 否则问题会被完全静默掉。
    """
    try:
        writer = get_stream_writer()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trial_compute_post] stream writer unavailable: {exc}")
        return
    if writer is None:
        logger.debug("[trial_compute_post] stream writer is None, report not pushed to stream")
        return
    try:
        writer(
            {
                "type": "output_msg",
                "node_name": "DataAgent",
                "content": report,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trial_compute_post] failed to emit report to stream: {exc}")


# ---------------------------------------------------------------------------
# Async bridging — agent post-hooks are sync, the tool is a coroutine
# ---------------------------------------------------------------------------


def _invoke_trial_compute(*, dml_path: Path, runtime: Runtime) -> dict[str, Any]:
    """Run the async trial compute from sync hook context.

    Uses ``asyncio.run`` directly when no loop is running; otherwise isolates the
    coroutine in a worker thread so we never touch the caller's loop. Mirrors the
    guard used by ``verify._run_dataops_validation_round``.
    """
    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.core.suite.builtin_suites.de_agent.tools.dataops_trial_compute_tool import (
        trial_compute_table,
    )

    # Mirror the YAML-declared tool config so thresholds/exclude-fields match
    # what an explicit tool call would use.
    tool_config: dict[str, Any] = {}
    try:
        raw = runtime.config_manager.get("TOOLS.local_functions")
        entries = raw if isinstance(raw, list) else []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == "trial_compute_table":
                tool_config = entry.get("config") or {}
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trial_compute_post] tool_config lookup failed, using defaults: {exc}")

    ctx = ToolExecutionContext(runtime=runtime, tool_config=tool_config)
    coroutine = trial_compute_table(
        target_table=_guess_target_table(dml_path),
        dml_path=str(dml_path),
        _tool_context=ctx,
    )

    try:
        asyncio.get_running_loop()
        in_running_loop = True
    except RuntimeError:
        in_running_loop = False

    if in_running_loop:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coroutine).result()
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------


def _resolve_workspace(runtime: Runtime) -> Path | None:
    raw = getattr(runtime, "workspace_dir", None)
    # Only a real str/Path is a usable workspace. Test doubles (MagicMock) expose
    # a truthy path-like attribute whose __fspath__ yields a junk relative path
    # ("MagicMock/mock.workspace_dir/<id>"), which would make the hook scan a
    # bogus directory instead of the real one.
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError as exc:
        logger.debug(f"[trial_compute_post] unusable workspace_dir {raw}: {exc}")
        return None


def _find_dml(workspace: Path) -> Path | None:
    """Locate the deliverable INSERT, preferring the most recently modified file.

    ``organize_workspace`` mirrors deliverables into ``sql/``；但它挂在 agent 级
    post-hook（图的收尾），而本 hook 挂在**节点级**（planner post）—— 节点级
    先于 agent 级执行，所以此刻 ``sql/`` 里可能是**上一轮**留下的旧副本。

    因此不能只按目录优先级（sql/ 优先）取第一个 —— 那样可能挑到陈旧的副本。
    改为在所有候选里取**最新修改**的那个：本轮 agent 写出的 insert_*.sql
    一定比上一轮拷贝进 sql/ 的更新。
    """
    candidates: list[Path] = []
    for directory in (workspace / "sql", workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            name = path.name.lower()
            if name.startswith("insert_") and name.endswith(".sql") and path.is_file():
                candidates.append(path)

    if not candidates:
        return None

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    # 最新修改优先；mtime 相同则退回 sql/ 优先（保持原有偏好）。
    return max(candidates, key=lambda p: (_mtime(p), p.parent.name == "sql"))


def _guess_target_table(dml_path: Path) -> str:
    """Best-effort target table for reporting: the DDL's ``CREATE TABLE db.tbl``.

    Purely cosmetic — the scratch rewrite is derived from the DML itself.
    """
    try:
        text = dml_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\w.-]+(?:\.[`\w.-]+)+)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return ""
    # Strip the backticks of each dotted segment: `biads`.`x` -> biads.x
    return ".".join(part.strip("`") for part in m.group(1).split("."))


# ---------------------------------------------------------------------------
# Run guard
# ---------------------------------------------------------------------------


def _already_ran(runtime: Runtime, run_id: Any) -> bool:
    done = _done_runs(runtime)
    return run_id in done


def _mark_ran(runtime: Runtime, run_id: Any) -> None:
    done = _done_runs(runtime)
    done.add(run_id)
    try:
        runtime.set_cache(_TRIAL_COMPUTE_DONE_KEY, done)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trial_compute_post] failed to persist run guard: {exc}")


def _done_runs(runtime: Runtime) -> set:
    try:
        raw = runtime.get_cache(_TRIAL_COMPUTE_DONE_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trial_compute_post] run guard unavailable: {exc}")
        return set()
    return set(raw) if isinstance(raw, (set, frozenset, list, tuple)) else set()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _render_report(*, dml_path: Path, result: dict[str, Any] | None) -> str:
    """Render the trial compute result as a user-facing text block.

    The quality metrics are rendered as a **Markdown table** so the frontend
    can display them as a table (not as a wall of text). The table is built by
    the shared :func:`render_metrics_table` helper, so the post-hook and the
    standalone ``dataops_validate_data`` tool produce **identical** tables.

    Returns an empty string when there is nothing worth reporting, so the hook
    stays silent instead of adding noise to every turn.
    """
    if not result:
        return ""

    if result.get("skipped"):
        # 优雅结束：优先用工具给的人话，退回到 reason 兜底。
        reason = result.get("message") or result.get("reason") or result.get("summary") or "unknown"
        return f"{_REPORT_PREFIX}已跳过（{reason}）。"

    if not result.get("passed"):
        lines = [f"{_REPORT_PREFIX}试算失败，SQL 未能成功执行。"]
        # ``error`` 是收敛过的失败原因；``summary`` 是状态行（含同样的原因）。
        summary = result.get("error") or result.get("summary")
        if summary:
            lines.append(f"- 原因：{summary}")
        analysis = result.get("log_analysis")
        if isinstance(analysis, dict):
            if analysis.get("error_type"):
                lines.append(f"- 错误类型：{analysis['error_type']}")
            suggestions = analysis.get("suggestions") or []
            for suggestion in suggestions[:3]:
                lines.append(f"- 建议：{suggestion}")
        lines.append("请按上述诊断修改 `insert_*.sql` 后重新交付。")
        return "\n".join(lines)

    quality = result.get("quality_check")
    if quality is None:
        # Trial ran fine; check was skipped (non-INSERT, no dataops resource, ...).
        return f"{_REPORT_PREFIX}试算通过；本次未执行数据正确性校验（无可校验数据或资源不可用）。"

    if not quality.get("sampled", True):
        # 校验没跑起来（提交失败/超时）—— 不能报成「全部通过」，
        # 也不能报成「0 处告警」，那两种说法都会让人以为数据已经查过。
        reason = quality.get("error") or "未知原因"
        return f"{_REPORT_PREFIX}试算通过，但数据正确性校验未能执行（{reason}），本次未产出质量结论。"

    # --- 有指标：渲染 Markdown 表格（前端直接渲染成表格）----------------
    details = quality.get("details") or {}
    warnings = quality.get("warnings") or []
    failed_checks = quality.get("failed_checks") or []
    completed_checks = quality.get("completed_checks") or []
    checks = "、".join(quality.get("checks") or []) or "—"
    sample_size = quality.get("sample_size")

    lines: list[str] = []
    if quality.get("ok"):
        lines.append(
            f"{_REPORT_PREFIX}试算通过，数据正确性校验全部通过。"
            f"（校验项：{checks}；采样 {sample_size} 行）"
        )
    elif failed_checks:
        # 部分完成：跑出来的指标照常展示，失败的校验项单独说明。
        # 早先这里因为「一条挂了 = 整个校验没跑成」而只报一句「未能执行」，
        # 已经算出来的指标全丢了 —— 用户看不到任何结果。
        done = "、".join(completed_checks) or "无"
        lines.append(
            f"{_REPORT_PREFIX}试算通过，数据正确性校验**部分完成**"
            f"（已完成的校验项：{done}；采样 {sample_size} 行）。"
        )
        if warnings:
            lines.append(f"已完成部分发现 {len(warnings)} 处告警。")
    else:
        lines.append(
            f"{_REPORT_PREFIX}试算通过，但数据正确性校验发现 {len(warnings)} 处告警"
            f"（校验项：{checks}；采样 {sample_size} 行）。"
        )

    if details:
        lines.append("")
        lines.append(
            render_metrics_table(details, warnings, quality.get("failed_metrics") or {})
        )

    if failed_checks:
        lines.append("")
        lines.append("未完成的校验项：")
        for item in failed_checks:
            lines.append(f"- {item.get('name')}：{item.get('reason') or item.get('status')}")
        lines.append("以上校验项未产出结果（超时或执行失败），其余结果如上表所示。")

    if warnings:
        lines.append("")
        lines.append("告警明细：")
        lines.extend(f"- {warning}" for warning in warnings[:20])
        lines.append("请检查对应字段的口径（是否漏 JOIN、INNER JOIN 丢数据、分组粒度错误等）后再交付。")
    return "\n".join(lines)
