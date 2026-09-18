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
"""IR for long-term data task."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from loguru import logger

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPostHookOutcome, ToolPreHookOutcome
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import ContextFactory
from dataagent.core.context.context_ir import ActionNode
from dataagent.core.managers.action_manager.base import ErrorType

# DataTaskIR spike modules for structured field filling and rendering
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import (
    load_template_catalog,
    list_field_templates,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.fill import (
    COMPACT_JSON_INSTRUCTION,
    detect_field_changes,
    fill_field_templates,
    is_field_confirmed,
    write_field_attempt,
    write_field_meta,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import render_constraint_context, render_context
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.json_llm import ainvoke_json_object


def _mark_failed(inv: ToolHookInvocation, reason: str) -> None:
    """Mark the tool execution as failed with a validation error."""
    inv.execution.success = False
    inv.execution.error_text = reason
    inv.execution.error_type = ErrorType.VALIDATION_ERROR.value
    inv.execution.retry_info = {"attempt": 0, "max_retries": 0, "retriable": False}


async def create_ir(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Initialize IR for this task.

    Args:
        inv: Per-call context with ``execution`` set; ``tool_args`` may contain a file path.

    Returns:
        Empty outcome; validation failures are written back to ``inv.execution``.
    """
    hook_name = "create_ir"

    if inv.execution is None or not inv.execution.success:
        logger.debug(
            f"[post_hook] {hook_name} skip. tool={inv.tool_name} call_id={inv.tool_call_id} reason=no_execution",
        )
        return ToolPostHookOutcome()

    # main logic
    runtime = inv.runtime

    if runtime.get_cache("ir_rendered_context"):
        logger.warning(
            f"[post_hook] {hook_name} skip. tool={inv.tool_name} call_id={inv.tool_call_id} reason=IR has been created",
        )
        return ToolPostHookOutcome()

    try:
        catalog = load_template_catalog()
        field_templates = list_field_templates(catalog)
    except Exception:
        logger.exception(
            f"[post_hook] {hook_name} init TEMPLATE IR failed, check format. "
            f"tool={inv.tool_name} call_id={inv.tool_call_id}",
        )
        _mark_failed(inv, "读取TEMPLATE IR失败，检查IR格式")
        return ToolPostHookOutcome()

    # Initialize empty field values
    runtime.set_cache("ir_field_values", [])
    runtime.set_cache("ir_initialized", True)
    runtime.set_cache("ir_rendered_context", "IR init!")
    runtime.set_cache("ir_field_templates", field_templates)
    return ToolPostHookOutcome()


def _resolve_ir_trace_dir(runtime: Runtime) -> str | None:
    """Resolve the offline fill-trace root under the workspace.

    Each update_ir call gets its own ``run_N`` directory so successive updates
    do not overwrite earlier traces. Returns None when no workspace is bound.
    """
    workspace = runtime.workspace_dir
    if workspace is None:
        return None
    run_index = runtime.get_cache("ir_trace_run", 0)
    runtime.set_cache("ir_trace_run", run_index + 1)
    trace_root = Path(workspace) / ".memory" / "ir_trace" / f"run_{run_index}"
    trace_root.mkdir(parents=True, exist_ok=True)
    return str(trace_root)


async def fill_ir_fields(
    inv: ToolHookInvocation,
    *,
    user_query: str,
    tool_evidence: str,
    confirmed_context: str = "",
    current_values: list[dict] | None = None,
    max_concurrency: int = 8,
    reuse_field_ids: set[str] | None = None,
    trace_dir: str | None = None,
) -> list[dict]:
    """Fill IR fields concurrently using the structured field template system.

    Args:
        inv: Per-call context with ``execution`` set.
        user_query: The user's original query.
        tool_evidence: Evidence from tool calls.
        confirmed_context: Additional confirmed context.
        current_values: Optional current values for each field_id.
        max_concurrency: Maximum concurrent field fill calls.

    Returns:
        List of field outputs from fill_field_templates.
    """
    hook_name = "fill_ir_fields"

    if ((inv.execution is None and getattr(inv, "phase", "post") != "pre")
            or (inv.execution is not None and not inv.execution.success)):
        logger.debug(
            f"[post_hook] {hook_name} skip. tool={inv.tool_name} call_id={inv.tool_call_id} reason=no_execution",
        )
        return []

    runtime = inv.runtime
    field_templates = runtime.get_cache("ir_field_templates")
    if not field_templates:
        # 兜底：如果 cache 没有则重新加载
        catalog = load_template_catalog()
        field_templates = list_field_templates(catalog)

    llm = runtime.llm("planner")
    trace_dir = trace_dir or _resolve_ir_trace_dir(runtime)
    logger.info(f"[fill_ir_fields] trace_dir={trace_dir}")

    field_results = await fill_field_templates(
        field_templates,
        user_query=user_query,
        tool_evidence=tool_evidence,
        llm=llm,
        confirmed_context=confirmed_context,
        current_values=current_values,
        max_concurrency=_ir_option(runtime, "max_concurrency", max_concurrency),
        completion_batch_size=_ir_option(runtime, "completion_batch_size", 1),
        completion_policy=_ir_option(runtime, "completion_policy", "always"),
        concise_rationale=_ir_option(runtime, "concise_rationale", False),
        compact_json=_ir_option(runtime, "compact_json", False),
        field_order=_ir_option(runtime, "field_order", "template"),
        trace_dir=trace_dir,
        reuse_field_ids=reuse_field_ids,
    )
    return field_results


def render_ir(field_outputs: list[dict]) -> str:
    """Render filled field outputs into natural language query context.

    Args:
        field_outputs: List of field outputs from fill_ir_fields.

    Returns:
        Rendered context string suitable for NL2SQL queries.
    """
    return render_context(field_outputs)


async def _ir_consistency_gate(
    runtime: Runtime,
    *,
    user_query: str,
    tool_evidence: str,
    field_results: list[dict],
    confirmed_context: str = "",
    trace_dir: str | None = None,
) -> tuple[list[str], bool]:
    """Post-fill consistency gate: cross-check the filled IR against the user query and evidence.

    Uses the planner LLM to detect cross-field conflicts or deviations from the user
    query (e.g. count_entity/grain mismatch, missing window_partitioning in TopN
    tasks). The boolean distinguishes a completed check from a model/parse failure.
    """
    started = time.perf_counter()
    raw = ""
    messages = []
    error = None
    warnings = []
    try:
        field_summary = json.dumps(
            [
                {
                    "field_id": result.get("field_id"),
                    "value": result.get("value"),
                    "unresolved": result.get("unresolved", []),
                }
                for result in field_results
            ],
            ensure_ascii=False,
            default=str,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据仓库特征开发任务的 DataTaskIR 口径一致性审查员，只做检查、不修改任何记录。"
                    "检查已填充的 DataTaskIR 是否存在：(1) 字段间相互冲突；(2) 与用户原始问题明显偏离；"
                    "(3) 计数实体/源表粒度/计数方式不自洽（如计数实体为 session 但聚合基于原始 fact 行，"
                    "或源表在计数实体上不唯一却采用行级计数）；(4) 排序/排名/TopN 任务缺失分区键说明；"
                    "(5) 时间窗口、分组键、去重语义等关键口径与用户问题不一致。"
                    "只报告确有依据的问题，不臆测；无问题则 warnings 返回空数组。"
                    '必须输出 JSON 对象，且必须同时包含 "consistent"（布尔值）和 "warnings"（数组）。'
                    '例如无问题时 {"consistent": true, "warnings": []}；有问题时 consistent 必须为 false。'
                    "不能省略 consistent，也不能用字符串代替布尔值。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "用户原始问题：\n"
                    f"{user_query}\n\n"
                    "已确认上下文：\n"
                    f"{confirmed_context}\n\n"
                    "工具证据：\n"
                    f"{tool_evidence}\n\n"
                    "已填充的 DataTaskIR（字段值 JSON）：\n"
                    f"{field_summary}\n\n"
                    '输出 JSON：{"warnings": [{"field": "字段名", "issue": "问题描述", "suggestion": "修正建议"}], '
                    '"consistent": true/false}'
                    '\n先输出 consistent 布尔判定，再输出 warnings；例如发现问题时：'
                    '{"consistent": false, "warnings": [{"field": "字段名", "issue": "问题描述", "suggestion": "修正建议"}]}。'
                    '无问题则输出 {"consistent": true, "warnings": []}。所有有依据的问题仍须完整列出。'
                ),
            },
        ]
        if _ir_option(runtime, "compact_json", False):
            messages[0]["content"] += COMPACT_JSON_INSTRUCTION
        parsed, raw = await ainvoke_json_object(runtime.llm("planner"), messages)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("warnings"), list):
            raise ValueError("Consistency gate returned invalid warnings")
        if not isinstance(parsed.get("consistent"), bool):
            raise ValueError("Consistency gate omitted its consistency verdict")
        for item in parsed["warnings"]:
            if not isinstance(item, dict) or not isinstance(item.get("issue"), str) or not item["issue"].strip():
                raise ValueError("Consistency gate returned an invalid issue")
            field = item.get("field") or "未指明字段"
            text = f"{field}：{item['issue']}"
            if item.get("suggestion"):
                text += f"（建议：{item['suggestion']}）"
            warnings.append(text)
        if not parsed["consistent"] and not warnings:
            warnings.append("一致性检查报告口径冲突但未给出详情，需核对后再生成 SQL。")
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("[update_ir] consistency gate failed ({}); retry on next update", error)
        warnings.append("IR 一致性检查未完成，不能视为检查通过；需重试检查或明确报告风险。")
    write_field_attempt(trace_dir, "_consistency_gate", 1, messages, raw)
    write_field_meta(trace_dir, "_consistency_gate", {
        "success": error is None, "attempts": 1, "error": error,
        "latency_seconds": time.perf_counter() - started,
        "model_call_seconds": time.perf_counter() - started,
        "output": {"warnings": warnings},
    })
    return warnings, error is None


def get_action_nodes(runtime: Runtime) -> list[ActionNode]:
    """获取当前 runtime 对应 Context 中的全部 ActionNode。"""
    context = ContextFactory.get_context(
        user_id=runtime.user_id,
        session_id=runtime.session_id,
        run_id=runtime.run_id,
        sub_id=runtime.sub_id,
    )
    return [
        node
        for _, _, node in context.state.ir.iter_nodes()
        if isinstance(node, ActionNode)
    ]


async def update_ir(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Update IR for this task.

    Args:
        inv: Per-call context with ``execution`` set; ``tool_args`` may contain a file path.

    Returns:
        Empty outcome; validation failures are written back to ``inv.execution``.
    """
    hook_name = "update_ir"
    logger.info(f"[update_ir] called, tool_name={inv.tool_name}, call_id={inv.tool_call_id}")

    if inv.execution is None or not inv.execution.success:
        logger.debug(
            f"[post_hook] {hook_name} skip. tool={inv.tool_name} call_id={inv.tool_call_id} reason=no_execution",
        )
        return ToolPostHookOutcome()

    runtime = inv.runtime
    policy = _ir_option(runtime, "refresh_policy", "eager")
    if policy not in {"eager", "on_consume"}:
        raise ValueError("DATA_TASK_IR.refresh_policy must be eager or on_consume")
    if policy == "on_consume":
        async with _update_lock(runtime):
            runtime.set_cache("ir_initialized", True)
            runtime.set_cache("ir_dirty", True)
        logger.info("[update_ir] deferred until IR consumer; no model calls")
        return ToolPostHookOutcome()
    async with _update_lock(runtime):
        await _update_ir_locked(inv)
    return ToolPostHookOutcome()


def _update_lock(runtime: Runtime) -> asyncio.Lock:
    lock = runtime.get_cache("ir_update_lock")
    if lock is None:
        lock = asyncio.Lock()
        runtime.set_cache("ir_update_lock", lock)
    return lock


async def ensure_ir_ready(inv: ToolHookInvocation) -> ToolPreHookOutcome:
    """Refresh from current state/evidence before a configured IR consumer runs.

    This pre-hook must be attached to every consumer when on_consume is enabled.
    Check inputs even if no todo completed since the previous consumer. Merely
    enabling the policy must not activate IR when create/update hooks are absent.
    """
    runtime = inv.runtime
    if (_ir_option(runtime, "refresh_policy", "eager") != "on_consume"
            or not runtime.get_cache("ir_initialized")):
        return ToolPreHookOutcome()
    if inv.state is None:
        raise RuntimeError("DataTaskIR consumer pre-hook requires current workflow state")
    async with _update_lock(runtime):
        await _update_ir_locked(inv)
        if runtime.get_cache("ir_input_signature") is None:
            runtime.set_cache("ir_dirty", True)
            raise RuntimeError("DataTaskIR consistency check incomplete; retry consumer before using IR")
    return ToolPreHookOutcome()


def _input_digest(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _ir_option(runtime: Runtime, name: str, default):
    """Keep tuning scoped to the agent configuration, including child agents."""
    getter = getattr(runtime, "get_config", None)
    return getter(f"DATA_TASK_IR.{name}", default) if getter else default


async def _update_ir_locked(inv: ToolHookInvocation) -> None:
    state = inv.state or {}
    runtime = inv.runtime
    started = time.perf_counter()
    action_nodes = get_action_nodes(runtime)

    # 从 context 中提取所有 ToolMessage 的内容作为 tool_evidence
    tool_evidence_parts = []
    tool_evidence_log_parts = []
    white_list = ["metadata_recall", "read_file"]
    for action_node in action_nodes:
        tool_name = action_node.action
        if action_node.success and tool_name in white_list:
            tool_args = action_node.params or {}
            tool_output = action_node.output
            if tool_name == "read_file" and not str(tool_args.get("path", "")).endswith(".md"):
                continue
            tool_evidence_parts.append(json.dumps({
                "tool": tool_name, "params": tool_args, "output": tool_output,
            }, ensure_ascii=False, sort_keys=True, default=str))
            # Format the log from the original values so multiline tool responses
            # remain readable; preserve the model input and its cache signature.
            log_params = json.dumps(tool_args, ensure_ascii=False, indent=2, default=str)
            log_output = tool_output if isinstance(tool_output, str) else json.dumps(
                tool_output, ensure_ascii=False, indent=2, default=str,
            )
            tool_evidence_log_parts.append(
                f"===== 证据 {len(tool_evidence_parts)} =====\n"
                f"工具: {tool_name}\n"
                f"输入参数:\n{log_params}\n"
                f"返回结果:\n{log_output}"
            )

    tool_evidence = "\n\n---\n\n".join(tool_evidence_parts)
    evidence_preview = "\n\n".join(tool_evidence_log_parts) or "（无可用工具证据）"
    logger.info(f"[update_ir] tool_evidence preview: \n{evidence_preview}")

    user_query = state.get("user_query", "")

    # 把instruction加入
    messages = state.get("messages") or []
    first_content = str(messages[0].content) if messages else ""
    instructions = first_content.split('User Context')[-1].split('General Requirement')[0]
    confirmed_context = instructions

    # 后续把hitl的反馈加入（从tool解析）
    # confirmed_context += "\n\n Human feedback: \n\n"

    field_templates = runtime.get_cache("ir_field_templates")
    if not field_templates:
        field_templates = list_field_templates(load_template_catalog())
        runtime.set_cache("ir_field_templates", field_templates)
    semantic_reuse = _ir_option(runtime, "semantic_field_reuse", False) is True
    context_signature = _input_digest([
        user_query, confirmed_context, field_templates,
        {"completion_batch_size": _ir_option(runtime, "completion_batch_size", 1),
         "completion_policy": _ir_option(runtime, "completion_policy", "always"),
         "concise_rationale": _ir_option(runtime, "concise_rationale", False),
         "compact_json": _ir_option(runtime, "compact_json", False),
         "field_order": _ir_option(runtime, "field_order", "template"),
         "semantic_field_reuse": semantic_reuse},
    ])
    input_signature = _input_digest([context_signature, tool_evidence_parts])
    if runtime.get_cache("ir_input_signature") == input_signature:
        runtime.set_cache("ir_dirty", False)
        logger.info("[update_ir] unchanged inputs; skip fill and gate")
        return

    # Publish no old validated snapshot while this refresh is awaiting model calls.
    runtime.set_cache("ir_input_signature", None)
    runtime.set_cache("ir_refresh_failed", True)
    runtime.set_cache("ir_initialized", True)
    current_values = runtime.get_cache("ir_field_values") or []
    trace_dir = _resolve_ir_trace_dir(runtime)
    reuse_ids: set[str] = set()
    if runtime.get_cache("ir_fields_signature") == input_signature:
        # The previous fill completed, but its gate failed. Retry only the gate.
        field_results = current_values
    else:
        previous_parts = runtime.get_cache("ir_evidence_parts")
        append_only = (
            isinstance(previous_parts, list)
            and tool_evidence_parts[:len(previous_parts)] == previous_parts
            and len(tool_evidence_parts) > len(previous_parts)
        )
        if (semantic_reuse
                and runtime.get_cache("ir_context_signature") == context_signature
                and append_only and not runtime.get_cache("ir_gate_warnings")):
            templates_by_id = {template["field_id"]: template for template in field_templates}
            confirmed = [field for field in current_values
                         if field.get("field_id") in templates_by_id
                         and is_field_confirmed(field, templates_by_id[field["field_id"]])]
            if confirmed:
                changed_ids = await detect_field_changes(
                    new_evidence="\n\n---\n\n".join(tool_evidence_parts[len(previous_parts):]),
                    confirmed_fields=confirmed, llm=runtime.llm("planner"), trace_dir=trace_dir,
                )
                reuse_ids = {field["field_id"] for field in confirmed} - changed_ids
        field_results = await fill_ir_fields(
            inv, user_query=user_query, tool_evidence=tool_evidence,
            # Old extracted values are not evidence when their sources/query may
            # have been removed or rewritten. Start fresh in those cases.
            confirmed_context=confirmed_context,
            current_values=(current_values if append_only
                            and runtime.get_cache("ir_context_signature") == context_signature else None),
            reuse_field_ids=reuse_ids, trace_dir=trace_dir,
        )
        runtime.set_cache("ir_field_values", field_results)
        runtime.set_cache("ir_fields_signature", input_signature)
        runtime.set_cache("ir_context_signature", context_signature)
        runtime.set_cache("ir_evidence_parts", tool_evidence_parts)

    gate_warnings, gate_completed = await _ir_consistency_gate(
        runtime,
        user_query=user_query,
        tool_evidence=tool_evidence,
        field_results=field_results,
        confirmed_context=confirmed_context,
        trace_dir=trace_dir,
    )
    runtime.set_cache("ir_gate_warnings", gate_warnings)
    rendered = render_constraint_context(field_results, warnings=gate_warnings)
    logger.info(f"[update_ir] ir_rendered_context: \n{rendered}")
    runtime.set_cache("ir_rendered_context", rendered)
    runtime.set_cache("ir_refresh_failed", False)
    runtime.set_cache("ir_dirty", False)
    if gate_completed:
        runtime.set_cache("ir_input_signature", input_signature)
    logger.info("[update_ir] completed: fields={} reused={} gate_completed={} elapsed={:.3f}s trace={}",
                len(field_results), len(reuse_ids), gate_completed, time.perf_counter() - started, trace_dir)


def get_ir_context(runtime: Runtime) -> str:
    """Return the shared IR contract, including validation warnings, to consumers."""
    if runtime.get_cache("ir_dirty") or runtime.get_cache("ir_refresh_failed"):
        raise RuntimeError("DataTaskIR refresh failed or is in progress; retry update_ir before consuming IR")
    fields = runtime.get_cache("ir_field_values")
    if not fields:
        return ""
    rendered = runtime.get_cache("ir_rendered_context")
    if not rendered or rendered == "IR init!":
        rendered = render_constraint_context(fields, warnings=runtime.get_cache("ir_gate_warnings") or [])
    return rendered


def read_ir(inv: ToolHookInvocation) -> str:
    """Read IR for this task.

    Args:
        inv: Per-call context with ``execution`` set; ``tool_args`` may contain a file path.

    Returns:
        Empty outcome; validation failures are written back to ``inv.execution``.
    """
    hook_name = "read_ir"
    if inv.execution is None or not inv.execution.success:
        logger.debug(
            f"[post_hook] {hook_name} skip. tool={inv.tool_name} call_id={inv.tool_call_id} reason=no_execution",
        )
        return ToolPostHookOutcome()

    # main logic
    runtime = inv.runtime

    if not runtime.get_cache("ir_rendered_context"):
        logger.error(f"[post_hook] {hook_name} failed, task ir not created, use create_ir to init IR.")
        return ""

    return get_ir_context(runtime)
