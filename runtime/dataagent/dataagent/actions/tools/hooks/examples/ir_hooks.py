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

from pathlib import Path

from loguru import logger

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPostHookOutcome
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
    fill_field_templates,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import render_context
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
    runtime.set_cache("ir_field_values", {})
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
    current_values: dict[str, dict] = None,
    max_concurrency: int = 8,
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

    if inv.execution is None or not inv.execution.success:
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
    trace_dir = _resolve_ir_trace_dir(runtime)
    logger.info(f"[fill_ir_fields] trace_dir={trace_dir}")

    field_results = await fill_field_templates(
        field_templates,
        user_query=user_query,
        tool_evidence=tool_evidence,
        llm=llm,
        confirmed_context=confirmed_context,
        current_values=current_values,
        max_concurrency=max_concurrency,
        trace_dir=trace_dir,
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
) -> list[str]:
    """Post-fill consistency gate: cross-check the filled IR against the user query and evidence.

    Uses the planner LLM to detect cross-field conflicts or deviations from the user
    query (e.g. count_entity/grain mismatch, missing window_partitioning in TopN
    tasks). Returns human-readable warnings; the caller decides how to surface them.
    """
    try:
        import json as _json

        rendered = render_ir(field_results)
        evidence_budget = tool_evidence[-6000:] if len(tool_evidence) > 6000 else tool_evidence
        field_summary = _json.dumps(
            [
                {
                    "field_id": result.get("field_id"),
                    "value": result.get("output", {}).get("value"),
                    "unresolved": result.get("output", {}).get("unresolved", []),
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
                ),
            },
            {
                "role": "user",
                "content": (
                    "用户原始问题：\n"
                    f"{user_query}\n\n"
                    "工具证据（节选，用于核对口径依据）：\n"
                    f"{evidence_budget}\n\n"
                    "已填充的 DataTaskIR（字段值 JSON）：\n"
                    f"{field_summary}\n\n"
                    "IR 渲染后的口径约束文本：\n"
                    f"{rendered}\n\n"
                    '输出 JSON：{"warnings": [{"field": "字段名", "issue": "问题描述", "suggestion": "修正建议"}], '
                    '"consistent": true/false}'
                ),
            },
        ]
        parsed, _ = await ainvoke_json_object(runtime.llm("planner"), messages)
        warnings = []
        if parsed and isinstance(parsed.get("warnings"), list):
            for item in parsed["warnings"]:
                if isinstance(item, dict) and item.get("issue"):
                    field = item.get("field") or "未指明字段"
                    suggestion = item.get("suggestion")
                    text = f"{field}：{item['issue']}"
                    if suggestion:
                        text += f"（建议：{suggestion}）"
                    warnings.append(text)
        return warnings
    except Exception:
        logger.exception("[update_ir] consistency gate failed, skip gate warnings")
        return []


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

    # main logic
    state = inv.state
    runtime = inv.runtime
    action_nodes = get_action_nodes(runtime)

    # 从 context 中提取所有 ToolMessage 的内容作为 tool_evidence
    tool_evidence_parts = []
    white_list = ["metadata_recall", "read_file"]
    for action_node in action_nodes:
        tool_name = action_node.action
        if action_node.success and tool_name in white_list:
            tool_args = action_node.params
            tool_output = action_node.output
            if tool_name == "read_file" and not tool_args["path"].endswith(".md"):
                continue
            tool_evidence_parts.append(f"Tool name:[{tool_name}]\n{tool_output}")

    tool_evidence = "\n\n---\n\n".join(tool_evidence_parts)
    logger.info(f"[update_ir] tool_evidence preview: \n{tool_evidence}")

    user_query = state.get("user_query", "")

    # 把instruction加入
    instructions = state['messages'][0].content.split('User Context')[-1].split('General Requirement')[0]
    confirmed_context = instructions

    # 后续把hitl的反馈加入（从tool解析）
    # confirmed_context += "\n\n Human feedback: \n\n"

    current_values = runtime.get_cache("ir_field_values", {})
    field_results = await fill_ir_fields(
        inv,
        user_query=user_query,
        tool_evidence=tool_evidence,
        confirmed_context=confirmed_context,
        current_values=current_values,
    )

    # Extract successful outputs and update runtime cache
    successful_outputs = {}
    for result in field_results:
        if result.get("success"):
            field_id = result.get("field_id")
            successful_outputs[field_id] = result.get("output")

    rendered = render_ir(field_results)
    gate_warnings = await _ir_consistency_gate(
        runtime,
        user_query=user_query,
        tool_evidence=tool_evidence,
        field_results=field_results,
    )
    if gate_warnings:
        rendered += (
            "\n\n【口径一致性风险提示（未确认，生成 SQL 时需谨慎）】\n"
            "以下口径间存在潜在冲突或与用户问题的偏离，SQL Agent 不得擅自按某一口径定案，"
            "需按用户原始问题推导，或明确报告冲突：\n"
            + "\n".join(f"- {warning}" for warning in gate_warnings)
        )
    runtime.set_cache("ir_field_values", field_results)
    runtime.set_cache("ir_rendered_context", rendered)
    logger.info(f"[update_ir] ir_rendered_context: \n{rendered}")
    return ToolPostHookOutcome()


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

    return runtime.get_cache("ir_rendered_context")
