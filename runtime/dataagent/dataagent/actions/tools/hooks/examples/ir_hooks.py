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
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPostHookOutcome, ToolPreHookOutcome
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import session as ir_session
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.fill import (
    COMPACT_JSON_INSTRUCTION,
    detect_field_changes,
    field_structural_errors,
    fill_field_templates,
    is_field_confirmed,
    write_field_attempt,
    write_field_meta,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.json_llm import ainvoke_json_object
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import render_constraint_context, render_context
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import (
    list_field_templates,
    load_template_catalog,
)
from dataagent.actions.tools.semantic_tool.metadata_recall import HISTORY_SQL_MARKER
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import ContextFactory
from dataagent.core.context.context_ir import ActionNode
from dataagent.core.managers.action_manager.base import ErrorType


class IRRefreshError(RuntimeError):
    """Extraction/validation failure eligible for IR-only fallback.

    Configuration, task selection, cancellation and storage errors must propagate.
    """


def ir_failure_policy(runtime: Runtime) -> str:
    policy = _ir_option(runtime, "failure_policy", "fallback_without_ir")
    if policy not in {"fallback_without_ir", "strict"}:
        raise ValueError("DataTaskIR 配置错误：failure_policy 必须为 fallback_without_ir 或 strict")
    return policy


def ir_consistency_policy(runtime: Runtime) -> str:
    policy = _ir_option(runtime, "consistency_policy", "warn")
    if policy not in {"warn", "enforce"}:
        raise ValueError("DataTaskIR 配置错误：consistency_policy 必须为 warn 或 enforce")
    return policy


async def _refresh_with_policy(inv: ToolHookInvocation) -> None:
    """Run under the update lock; downgrade only typed IR inference failures."""
    runtime = inv.runtime
    policy = ir_failure_policy(runtime)
    ir_consistency_policy(runtime)
    try:
        await _update_ir_locked(inv)
    except IRRefreshError as exc:
        if policy == "strict":
            raise
        signature = runtime.get_cache("ir_attempt_signature")
        fallback = {"signature": signature, "reason": str(exc)}
        if runtime.get_cache("ir_fallback") != fallback:
            ir_session.record_fallback(runtime, fallback)
            logger.warning("[DataTaskIR fallback_without_ir] tool={} signature={} reason={}",
                           inv.tool_name, signature, exc)
        runtime.set_cache("ir_fallback", fallback)
        runtime.set_cache("ir_blocked_input", {"signature": signature, "message": str(exc)})
        runtime.set_cache("ir_input_signature", None)
        runtime.set_cache("ir_rendered_context", "")
        runtime.set_cache("ir_dirty", True)
        runtime.set_cache("ir_refresh_failed", True)
    # Capture this invocation's result: another concurrent tool may later refresh IR.
    if hasattr(inv, "hook_context"):
        inv.hook_context["ir_fallback"] = runtime.get_cache("ir_fallback")


async def record_ir_diagnostics(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Keep fallback diagnostics in execution metadata, outside model-visible results."""
    execution = inv.execution
    fallback = getattr(inv, "hook_context", {}).get("ir_fallback", inv.runtime.get_cache("ir_fallback"))
    if not fallback or execution is None:
        return ToolPostHookOutcome()
    if hasattr(execution, "metadata"):
        execution.metadata["data_task_ir"] = {"status": "fallback_without_ir", **fallback}
    return ToolPostHookOutcome()


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

    ir_session.ensure_task_bound(runtime, inv.state)
    if runtime.get_cache("ir_turn_suppressed"):
        return ToolPostHookOutcome()

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
    if not runtime.get_cache("ir_field_values"):
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
    turn_id = runtime.get_cache("ir_trace_turn")
    if turn_id is None:
        turn_id = uuid.uuid4().hex
        runtime.set_cache("ir_trace_turn", turn_id)
    memory = ir_session.memory_dir(runtime) if ir_session.enabled(runtime) else Path(workspace) / ".memory"
    trace_root = memory / "ir_trace" / turn_id / f"run_{run_index}"
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
    field_ids: set[str] | None = None,
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
    if field_ids is not None:
        field_templates = [field for field in field_templates if field["field_id"] in field_ids]
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
        consistency_contract = "\n".join(load_template_catalog().get("consistency_contract", []))
        messages = [
            {
                "role": "system",
                "content": (
                    "你是数据仓库特征开发任务的 DataTaskIR 口径一致性审查员，只做检查、不修改任何记录。"
                    "检查已填充的 DataTaskIR 是否存在：(1) 字段间相互冲突；(2) 与用户原始问题明显偏离；"
                    "(3) 计数实体/源表粒度/计数方式不自洽（如计数实体为 session 但聚合基于原始 fact 行，"
                    "用户要求统计去重实体却按明细行计数）；(4) 排序/排名/TopN 任务缺失分区键说明；"
                    "(5) 时间窗口、分组键、去重语义等关键口径与用户问题不一致；"
                    "(6) 用户原话或证据中已经明确、且属于本 IR 表达范围的要求被遗漏。"
                    "IR 允许部分填写，null、空数组或未填写本身不是不一致，不以填满模板为检查目标。"
                    "区分证据已明确但 IR 漏记、与输入本来没有提供信息；后者不得要求模型猜测补齐。"
                    "未涉及的操作不要求补充或追问；仅当未知口径影响当前任务正确实现时报告待澄清问题，"
                    "并明确缺少的是哪项任务所需证据，不得建议无依据的业务值。"
                    "只报告确有依据的问题，不臆测；无问题则 warnings 返回空数组。"
                    "每个问题必须定位到六个顶层 field_id 中的相关字段，并指出具体冲突值和依据。"
                    f"\n模板一致性协议：\n{consistency_contract}\n"
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
        logger.warning("[update_ir] consistency gate failed ({}); applying configured failure policy", error)
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
    ir_session.ensure_task_bound(runtime, inv.state)
    if runtime.get_cache("ir_turn_suppressed"):
        return await record_ir_diagnostics(inv)
    if policy not in {"eager", "on_consume"}:
        raise ValueError("DATA_TASK_IR.refresh_policy must be eager or on_consume")
    if policy == "on_consume":
        async with _update_lock(runtime):
            runtime.set_cache("ir_initialized", True)
            runtime.set_cache("ir_dirty", True)
        logger.info("[update_ir] deferred until IR consumer; no model calls")
        return ToolPostHookOutcome()
    async with _update_lock(runtime):
        await _refresh_with_policy(inv)
    return await record_ir_diagnostics(inv)


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
    if runtime.get_cache("ir_enabled") is False:
        return ToolPreHookOutcome()
    ir_session.ensure_task_bound(runtime, inv.state)
    if runtime.get_cache("ir_turn_suppressed"):
        return ToolPreHookOutcome()
    if not runtime.get_cache("ir_initialized"):
        if not runtime.get_cache("ir_enabled"):
            return ToolPreHookOutcome()
        runtime.set_cache("ir_initialized", True)
    if inv.state is None:
        raise RuntimeError("DataTaskIR consumer pre-hook requires current workflow state")
    async with _update_lock(runtime):
        await _refresh_with_policy(inv)
        if runtime.get_cache("ir_input_signature") is None and not runtime.get_cache("ir_fallback"):
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


def _block_ir_refresh(runtime: Runtime, signature: str, reasons: list[str]) -> None:
    message = (
        "DataTaskIR 尚未通过本轮检查；当前输入的自动检查/修复已结束。"
        "不能使用本次或旧版本 IR 作为已确认约束；请核验以下诊断，必要时补充证据或澄清。\n"
        + "\n".join(reasons)
    )
    runtime.set_cache("ir_blocked_input", {"signature": signature, "message": message})
    runtime.set_cache("ir_gate_warnings", reasons)
    runtime.set_cache("ir_dirty", True)
    runtime.set_cache("ir_refresh_failed", True)
    raise IRRefreshError(message)


def _structural_issues(templates: list[dict], fields: list[dict]) -> list[str]:
    by_id = {t["field_id"]: t for t in templates}
    return [
        f"{field['field_id']}：数据结构异常 {', '.join(errors)}（不是要求补齐未知业务值）"
        for field in fields if field.get("field_id") in by_id
        if (errors := field_structural_errors(by_id[field["field_id"]], field))
    ]


def _extraction_failed(runtime: Runtime, signature: str) -> None:
    failures = runtime.get_cache("ir_extraction_failures") or {"signature": signature, "count": 0}
    if failures["signature"] != signature:
        failures = {"signature": signature, "count": 0}
    failures["count"] += 1
    runtime.set_cache("ir_extraction_failures", failures)
    runtime.set_cache("ir_fields_signature", None)
    runtime.set_cache("ir_dirty", True)
    if failures["count"] >= 2:
        _block_ir_refresh(runtime, signature, ["字段提取连续失败；请检查模型服务或补充证据。"])
    raise IRRefreshError("本轮 DataTaskIR 提取失败；旧快照不能作为当前 SQL 约束。")


async def _check_and_repair_ir(inv, fields, *, user_query, tool_evidence, confirmed_context,
                               input_signature, field_templates, trace_dir):
    """Check structure, then observe warnings or enforce one bounded semantic repair."""
    runtime = inv.runtime
    if issues := _structural_issues(field_templates, fields):
        _block_ir_refresh(runtime, input_signature, issues)
    warnings, completed = await _ir_consistency_gate(
        runtime, user_query=user_query, tool_evidence=tool_evidence,
        field_results=fields, confirmed_context=confirmed_context, trace_dir=trace_dir,
    )
    if completed and warnings and ir_consistency_policy(runtime) == "warn":
        logger.warning("[DataTaskIR consistency_warning] IR 存在一致性问题，按 warn 策略继续注入；"
                       "不触发定向修复或降级。signature={} trace={}\n{}",
                       input_signature, trace_dir, "\n".join(warnings))
        return fields, warnings, completed
    if completed and warnings:
        # An issue can reference multiple fields. Diagnostics are suggestions,
        # not evidence: the extractor must check them against the original inputs.
        affected = {t["field_id"] for t in field_templates
                    if re.search(r"\b" + re.escape(t["field_id"]) + r"\b", "\n".join(warnings))}
        if affected and runtime.get_cache("ir_repaired_input") != input_signature:
            runtime.set_cache("ir_repaired_input", input_signature)
            repair_dir = _resolve_ir_trace_dir(runtime)
            logger.info("[update_ir] one targeted repair: fields={} trace={}", sorted(affected), repair_dir)
            try:
                repaired = await fill_ir_fields(
                    inv, user_query=user_query, tool_evidence=tool_evidence,
                    confirmed_context=confirmed_context + "\n\n待核验的审查意见（不是新增业务证据）：\n"
                    + "\n".join(warnings)
                    + "\n仅依据原问题和工具证据修正涉及字段；审查可能误判，不照抄建议、不补猜测值。",
                    current_values=fields, field_ids=affected, trace_dir=repair_dir,
                )
            except ValueError:
                # Keep configuration errors outside the inference fallback boundary.
                raise
            except Exception as exc:
                _block_ir_refresh(runtime, input_signature, [f"定向字段修复失败（{type(exc).__name__}）。", *warnings])
            replacements = {f["field_id"]: f for f in repaired if f.get("field_id") in affected}
            if set(replacements) != affected or any(f.get("_extraction_error") for f in replacements.values()):
                _block_ir_refresh(runtime, input_signature, ["定向字段修复未完成。", *warnings])
            updated = [replacements.get(f["field_id"], f) for f in fields]
            # Gate inputs omit rationale. Rewording explanations is not a repair
            # and must not buy another chance for the same semantic verdict.
            def gate_values(values):
                return [{"field_id": f.get("field_id"), "value": f.get("value"),
                         "unresolved": f.get("unresolved", [])} for f in values]
            if _input_digest(gate_values(updated)) == _input_digest(gate_values(fields)):
                _block_ir_refresh(runtime, input_signature, ["定向修复没有改变待检查的字段内容。", *warnings])
            fields = updated
            runtime.set_cache("ir_field_values", fields)
            if issues := _structural_issues(field_templates, fields):
                _block_ir_refresh(runtime, input_signature, issues)
            warnings, completed = await _ir_consistency_gate(
                runtime, user_query=user_query, tool_evidence=tool_evidence,
                field_results=fields, confirmed_context=confirmed_context, trace_dir=repair_dir,
            )
        if completed and warnings:
            _block_ir_refresh(runtime, input_signature, warnings)
    if not completed:
        failures = runtime.get_cache("ir_gate_failures") or {"signature": input_signature, "count": 0}
        if failures["signature"] != input_signature:
            failures = {"signature": input_signature, "count": 0}
        failures["count"] += 1
        runtime.set_cache("ir_gate_failures", failures)
        if failures["count"] >= 2:
            _block_ir_refresh(runtime, input_signature, warnings)
    return fields, warnings, completed


async def _update_ir_locked(inv: ToolHookInvocation) -> None:
    state = inv.state or {}
    runtime = inv.runtime
    started = time.perf_counter()
    managed = ir_session.enabled(runtime)
    ir_session.ensure_task_bound(runtime, state)
    action_nodes = ir_session.current_actions(runtime, get_action_nodes(runtime))

    # 从 context 中提取所有 ToolMessage 的内容作为 tool_evidence
    tool_evidence_parts = ir_session.inherited_evidence(runtime) if managed else []
    white_list = ["metadata_recall", "read_file"]
    for action_node in action_nodes:
        tool_name = action_node.action
        if action_node.success and tool_name in white_list:
            tool_args = action_node.params or {}
            tool_output = action_node.output
            if tool_name == "read_file":
                path = str(tool_args.get("path", "")).replace("\\", "/")
                # Semantic service spills long enum results to workspace .txt.
                # Include that exact artifact family, not arbitrary text/SQL files.
                enum_dump = False
                if runtime.workspace_dir and re.fullmatch(
                    r"get_table_schema_enum_values_.+_[0-9a-f]{8}\.txt", Path(path).name,
                ):
                    root = Path(runtime.workspace_dir).resolve()
                    enum_dump = (root / path).resolve().parent == root
                if (not path.endswith(".md") and not enum_dump) or "calibration_tables" in path:
                    continue
            # Historical SQL is reference material, not evidence for new task constraints.
            if tool_name == "metadata_recall" and HISTORY_SQL_MARKER in str(tool_args.get("query") or "").lower():
                logger.info("[update_ir] history-SQL recall evidence excluded from IR fill")
                continue
            tool_evidence_parts.append(json.dumps({
                "tool": tool_name, "params": tool_args, "output": tool_output,
            }, ensure_ascii=False, sort_keys=True, default=str))

    # Both modes treat an identical reread as the same input. Changed output
    # replaces the previous evidence and permits a fresh inference attempt.
    latest = {}
    for part in tool_evidence_parts:
        item = json.loads(part)
        latest[_input_digest([item["tool"], item["params"]])] = part
    tool_evidence_parts = list(latest.values())
    if managed:
        ir_session.record_evidence(runtime, tool_evidence_parts)
    # Log exactly the deduplicated evidence sent to the model, including inherited inputs.
    tool_evidence_log_parts = []
    inherited = set(ir_session.inherited_evidence(runtime)) if managed else set()
    for index, part in enumerate(tool_evidence_parts, 1):
        item = json.loads(part)
        source = "所选任务历史快照" if part in inherited else "本轮工具"
        output = item["output"] if isinstance(item["output"], str) else json.dumps(item["output"], ensure_ascii=False)
        tool_evidence_log_parts.append(
            f"===== 证据 {index}（{source}）=====\n工具: {item['tool']}\n"
            f"输入参数:\n{json.dumps(item['params'], ensure_ascii=False, indent=2)}\n返回结果:\n{output}"
        )
    tool_evidence = "\n\n---\n\n".join(tool_evidence_parts)
    evidence_preview = "\n\n".join(tool_evidence_log_parts) or "（无可用工具证据）"
    logger.info(f"[update_ir] tool_evidence preview: \n{evidence_preview}")

    user_query = state.get("user_query", "")

    # 把instruction加入
    messages = state.get("messages") or []
    first_content = next((str(message.content) for message in messages
                          if getattr(message, "type", None) != "tool"), "")
    instructions = first_content.split('User Context')[-1].split('General Requirement')[0]
    confirmed_context = instructions
    if managed:
        user_query = ir_session.query_context(runtime)
        confirmed_context = ir_session.confirmed_context(runtime, state)
    else:
        feedback = [text for message in messages if (text := ir_session.feedback_content(message))]
        if feedback:
            confirmed_context += "\n\n用户澄清：\n" + "\n".join(feedback)

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
         "consistency_policy": ir_consistency_policy(runtime),
         "semantic_field_reuse": semantic_reuse},
        # Relative dates must not silently reuse yesterday's interpretation.
        datetime.now().date().isoformat(),
    ])
    input_signature = _input_digest([context_signature, tool_evidence_parts])
    runtime.set_cache("ir_attempt_signature", input_signature)
    blocked = runtime.get_cache("ir_blocked_input")
    if blocked and blocked["signature"] == input_signature:
        logger.info("[update_ir] unchanged blocked inputs; no model calls")
        raise IRRefreshError(blocked["message"])
    runtime.set_cache("ir_fallback", None)
    if runtime.get_cache("ir_input_signature") == input_signature:
        if managed and runtime.get_cache("ir_session")["snapshot"]["status"] != "committed":
            ir_session.commit(runtime, runtime.get_cache("ir_field_values") or [], tool_evidence_parts, input_signature)
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
        fill_current = (
            current_values if append_only
            and runtime.get_cache("ir_context_signature") == context_signature else None
        )
        if managed and runtime.get_cache("ir_context_signature") is None:
            fill_current = runtime.get_cache("ir_field_values") or None
        try:
            field_results = await fill_ir_fields(
                inv, user_query=user_query, tool_evidence=tool_evidence,
                # Old extracted values are not evidence when their sources/query may
                # have been removed or rewritten. Start fresh in those cases.
                confirmed_context=confirmed_context,
                current_values=fill_current,
                reuse_field_ids=reuse_ids, trace_dir=trace_dir,
            )
            runtime.set_cache("ir_field_values", field_results)
            runtime.set_cache("ir_fields_signature", input_signature)
            runtime.set_cache("ir_context_signature", context_signature)
            runtime.set_cache("ir_evidence_parts", tool_evidence_parts)
        except ValueError:
            # fill_field_templates validates tuning options before inference;
            # invalid configuration must not be mistaken for a model failure.
            raise
        except Exception as exc:
            logger.warning("[update_ir] field extraction failed: {}", type(exc).__name__)
            _extraction_failed(runtime, input_signature)

    if any(field.get("_extraction_error") for field in field_results):
        _extraction_failed(runtime, input_signature)
    field_results, gate_warnings, gate_completed = await _check_and_repair_ir(
        inv, field_results,
        user_query=user_query,
        tool_evidence=tool_evidence,
        confirmed_context=confirmed_context,
        input_signature=input_signature,
        field_templates=field_templates,
        trace_dir=trace_dir,
    )
    runtime.set_cache("ir_gate_warnings", gate_warnings)
    if not gate_completed or (gate_warnings and ir_consistency_policy(runtime) == "enforce"):
        runtime.set_cache("ir_dirty", True)
        raise IRRefreshError(
            "DataTaskIR 尚未通过本轮检查；不能消费本次或旧版本 IR。" + "；".join(gate_warnings)
        )
    if managed:
        ir_session.commit(runtime, field_results, tool_evidence_parts, input_signature)
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
    """Return consumable IR context; individual gate diagnostics stay in logs/trace."""
    if ir_session.enabled(runtime):
        ir_session.ensure_task_bound(runtime)
    if runtime.get_cache("ir_fallback") and ir_failure_policy(runtime) == "fallback_without_ir":
        return ""
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
