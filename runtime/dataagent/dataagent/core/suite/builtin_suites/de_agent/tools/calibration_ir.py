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
from __future__ import annotations

from typing import Any

from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext

# 复用 ir_hooks 中已存在的构造块（非新抽取的公共函数）
from dataagent.actions.tools.hooks.examples.ir_hooks import (
    _ir_consistency_gate,
    _resolve_ir_trace_dir,
    get_action_nodes,
    render_ir,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.fill import (
    _has_meaningful_leaf,
    fill_field_templates,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import (
    list_field_templates,
    load_template_catalog,
)

# update_ir 使用的证据白名单，保持一致
_EVIDENCE_WHITE_LIST = ["metadata_recall", "read_file"]


def _collect_tool_evidence(runtime: Any) -> str:
    """从当前 context 的 ActionNode 中收集工具证据（与 update_ir 同口径）。"""
    parts: list[str] = []
    for action_node in get_action_nodes(runtime):
        tool_name = action_node.action
        if not (action_node.success and tool_name in _EVIDENCE_WHITE_LIST):
            continue
        if tool_name == "read_file" and not action_node.params.get("path", "").endswith(".md"):
            continue
        parts.append(f"Tool name:[{tool_name}]\n{action_node.output}")
    return "\n\n---\n\n".join(parts)


async def freeze_calibration_to_ir(
    calibration_tables: str,
    user_query: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Fold the calibration freeze table (口径冻结表) you just wrote into the DataTaskIR.

    Call this tool RIGHT AFTER you finish writing the calibration freeze table
    (口径冻结表 / SQL 前置契约), and BEFORE calling ``wrapped_nl2sql_sub_agent_tool``.
    It feeds your frozen calibration into the task-level DataTaskIR as the highest-priority
    confirmed business calibration, so downstream SQL generation and delivery checks
    follow exactly the calibration you froze. If the calibration changes later
    (e.g. after user feedback), call this tool again with the updated table to refresh IR.

    **Note**: This tool requires DataTaskIR to be enabled. IR is initialized when you call
    ``create_plan``. If IR is not enabled in your config (no ``create_ir`` hook), this tool
    will skip execution and return a notice.

    Args:
        calibration_tables (str): The FILLED calibration freeze table content (Markdown),
            i.e. the concrete frozen calibration for THIS task (not the blank template).
            Include every applicable freeze table with real field roles / business rules;
            for tables that do not apply, keep the "不适用 + 原因" note.
        user_query (str): The user's original feature-development requirement text, used to
            keep each IR field aligned with what the user actually asked for.

    Returns:
        dict[str, str]: ``original_msg`` (IR update summary + rendered calibration constraints
        for LLM reasoning) and ``frontend_msg`` (brief UI summary).
    """
    runtime = _tool_context.runtime
    if runtime is None:
        raise RuntimeError(
            "freeze_calibration_to_ir: runtime is unavailable; this tool must run within an active session."
        )

    # Check if IR has been initialized
    ir_initialized = runtime.get_cache("ir_initialized", False)
    if not ir_initialized:
        notice = "DataTaskIR 未启用，freeze_calibration_to_ir 工具跳过执行。\n"
        logger.info(f"[freeze_calibration_to_ir] {notice}")
        return {
            "original_msg": notice,
            "frontend_msg": notice,
        }

    calibration_text = str(calibration_tables or "").strip()
    if not calibration_text:
        raise ValueError("calibration_tables must not be empty; write the calibration freeze table first.")

    # 冻结表即最高优先级的已确认口径
    confirmed_context = calibration_text
    tool_evidence = _collect_tool_evidence(runtime)

    # --- 以下编排逐段内联，等价于 update_ir 的填充/渲染/回写，但以冻结表为 confirmed_context ---
    field_templates = runtime.get_cache("ir_field_templates")
    if not field_templates:
        # 兜底：IR 未初始化（create_ir 未触发）时，直接从模板目录加载
        catalog = load_template_catalog()
        field_templates = list_field_templates(catalog)
        runtime.set_cache("ir_field_templates", field_templates)

    llm = runtime.llm("planner")
    trace_dir = _resolve_ir_trace_dir(runtime)
    logger.info(f"[freeze_calibration_to_ir] trace_dir={trace_dir}, evidence_len={len(tool_evidence)}")

    current_values = runtime.get_cache("ir_field_values", {})
    field_results = await fill_field_templates(
        field_templates,
        user_query=user_query,
        tool_evidence=tool_evidence,
        llm=llm,
        confirmed_context=confirmed_context,
        current_values=current_values,
        trace_dir=trace_dir,
    )

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
    logger.info(f"[freeze_calibration_to_ir] ir_rendered_context:\n{rendered}")

    # field_results 是各字段的 output（{field_id, value, rationale, ...}），不含 success 键；
    # “已填充”以 value 是否含实质内容判定（value 为空/全 null 表示证据不足未填）。
    filled_count = sum(1 for result in field_results if _has_meaningful_leaf(result.get("value")))
    summary = (
        f"口径冻结表已融入 DataTaskIR（{filled_count}/{len(field_results)} 个字段填入了确定口径）。"
        + ("存在口径一致性风险提示，见下方渲染文本。" if gate_warnings else "")
    )
    return {
        "original_msg": f"{summary}\n\n【DataTaskIR 已确认口径约束】\n{rendered}",
        "frontend_msg": f"\n\nfreeze_calibration_to_ir 工具执行完成\n\n{summary}",
    }
