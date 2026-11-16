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
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from loguru import logger

from dataagent import AgentContext

from . import session as ir_session
from .fill import (
    COMPACT_JSON_INSTRUCTION,
    detect_field_changes,
    field_structural_errors,
    fill_field_templates,
    is_field_confirmed,
    write_field_attempt,
    write_field_meta,
)
from .inputs import collect_evidence, input_state
from .json_llm import ainvoke_json_object
from .render import render_constraint_context, render_context
from .runtime import ir_enabled, ir_options, ir_state
from .template import (
    list_field_templates,
    load_template_catalog,
)


class IRRefreshError(RuntimeError):
    """Extraction/validation failure eligible for IR-only fallback.

    Configuration, task selection, cancellation and storage errors must propagate.
    """


def ir_failure_policy(context: AgentContext) -> str:
    policy = _ir_option(context, "failure_policy", "fallback_without_ir")
    if policy not in {"fallback_without_ir", "strict"}:
        raise ValueError("DataTaskIR 配置错误：failure_policy 必须为 fallback_without_ir 或 strict")
    return policy


def ir_consistency_policy(context: AgentContext) -> str:
    policy = _ir_option(context, "consistency_policy", "warn")
    if policy not in {"warn", "enforce"}:
        raise ValueError("DataTaskIR 配置错误：consistency_policy 必须为 warn 或 enforce")
    return policy


def _block_ir_refresh(context: AgentContext, signature: str, reasons: list[str]) -> None:
    cache = ir_state(context)
    message = (
        "DataTaskIR 尚未通过本轮检查；当前输入的自动检查/修复已结束。不能使用本次或旧版本 IR 作为已确认约束；请核验以下诊断，必要时补充证据或澄清。\n"
        + "\n".join(reasons)
    )
    cache["ir_blocked_input"] = {"signature": signature, "message": message}
    cache["ir_gate_warnings"] = reasons
    cache["ir_dirty"] = True
    cache["ir_refresh_failed"] = True
    raise IRRefreshError(message)


def _structural_issues(templates: list[dict], fields: list[dict]) -> list[str]:
    by_id = {t["field_id"]: t for t in templates}
    return [
        f"{field['field_id']}：数据结构异常 {', '.join(errors)}（不是要求补齐未知业务值）"
        for field in fields
        if field.get("field_id") in by_id
        if (errors := field_structural_errors(by_id[field["field_id"]], field))
    ]


def _extraction_failed(context: AgentContext, signature: str) -> None:
    cache = ir_state(context)
    failures = cache.get("ir_extraction_failures") or {"signature": signature, "count": 0}
    if failures["signature"] != signature:
        failures = {"signature": signature, "count": 0}
    failures["count"] += 1
    cache["ir_extraction_failures"] = failures
    cache["ir_fields_signature"] = None
    cache["ir_dirty"] = True
    if failures["count"] >= 2:
        _block_ir_refresh(context, signature, ["字段提取连续失败；请检查模型服务或补充证据。"])
    raise IRRefreshError("本轮 DataTaskIR 提取失败；旧快照不能作为当前 SQL 约束。")


async def _check_and_repair_ir(
    context,
    fields,
    *,
    user_query,
    tool_evidence,
    confirmed_context,
    input_signature,
    field_templates,
    trace_dir,
):
    """Check structure, then observe warnings or enforce one bounded semantic repair."""
    cache = ir_state(context)
    if issues := _structural_issues(field_templates, fields):
        _block_ir_refresh(context, input_signature, issues)
    (warnings, completed) = await _ir_consistency_gate(
        context,
        user_query=user_query,
        tool_evidence=tool_evidence,
        field_results=fields,
        confirmed_context=confirmed_context,
        trace_dir=trace_dir,
    )
    if completed and warnings and (ir_consistency_policy(context) == "warn"):
        logger.warning(
            "[DataTaskIR consistency_warning] IR 存在一致性问题，按 warn 策略继续注入；不触发定向修复或降级。signature={} trace={}\n{}",
            input_signature,
            trace_dir,
            "\n".join(warnings),
        )
        return (fields, warnings, completed)
    if completed and warnings:
        affected = {
            t["field_id"]
            for t in field_templates
            if re.search("\\b" + re.escape(t["field_id"]) + "\\b", "\n".join(warnings))
        }
        if affected and cache.get("ir_repaired_input") != input_signature:
            cache["ir_repaired_input"] = input_signature
            repair_dir = _resolve_ir_trace_dir(context)
            logger.info(
                "[update_ir] one targeted repair: fields={} trace={}", sorted(affected), repair_dir
            )
            try:
                repaired = await fill_ir_fields(
                    context,
                    user_query=user_query,
                    tool_evidence=tool_evidence,
                    confirmed_context=confirmed_context
                    + "\n\n待核验的审查意见（不是新增业务证据）：\n"
                    + "\n".join(warnings)
                    + "\n仅依据原问题和工具证据修正涉及字段；审查可能误判，不照抄建议、不补猜测值。",
                    current_values=fields,
                    field_ids=affected,
                    trace_dir=repair_dir,
                )
            except ValueError:
                raise
            except Exception as exc:
                _block_ir_refresh(
                    context,
                    input_signature,
                    [f"定向字段修复失败（{type(exc).__name__}）。", *warnings],
                )
            replacements = {f["field_id"]: f for f in repaired if f.get("field_id") in affected}
            if set(replacements) != affected or any(
                (f.get("_extraction_error") for f in replacements.values())
            ):
                _block_ir_refresh(context, input_signature, ["定向字段修复未完成。", *warnings])
            updated = [replacements.get(f["field_id"], f) for f in fields]

            def gate_values(values):
                return [
                    {
                        "field_id": f.get("field_id"),
                        "value": f.get("value"),
                        "unresolved": f.get("unresolved", []),
                    }
                    for f in values
                ]

            if _input_digest(gate_values(updated)) == _input_digest(gate_values(fields)):
                _block_ir_refresh(
                    context, input_signature, ["定向修复没有改变待检查的字段内容。", *warnings]
                )
            fields = updated
            cache["ir_field_values"] = fields
            if issues := _structural_issues(field_templates, fields):
                _block_ir_refresh(context, input_signature, issues)
            (warnings, completed) = await _ir_consistency_gate(
                context,
                user_query=user_query,
                tool_evidence=tool_evidence,
                field_results=fields,
                confirmed_context=confirmed_context,
                trace_dir=repair_dir,
            )
        if completed and warnings:
            _block_ir_refresh(context, input_signature, warnings)
    if not completed:
        failures = cache.get("ir_gate_failures") or {"signature": input_signature, "count": 0}
        if failures["signature"] != input_signature:
            failures = {"signature": input_signature, "count": 0}
        failures["count"] += 1
        cache["ir_gate_failures"] = failures
        if failures["count"] >= 2:
            _block_ir_refresh(context, input_signature, warnings)
    return (fields, warnings, completed)


async def create_ir(context: AgentContext) -> None:
    """Initialize templates and empty fields after a successful plan write."""
    cache = ir_state(context)
    if cache.get("ir_rendered_context"):
        return
    try:
        field_templates = list_field_templates(load_template_catalog())
    except Exception as exc:
        raise RuntimeError("读取TEMPLATE IR失败，检查IR格式") from exc
    if not cache.get("ir_field_values"):
        cache["ir_field_values"] = []
    cache["ir_initialized"] = True
    cache["ir_rendered_context"] = "IR init!"
    cache["ir_field_templates"] = field_templates


def _resolve_ir_trace_dir(context: AgentContext) -> str | None:
    """Resolve the offline fill-trace root under the workspace.

    Each update_ir call gets its own ``run_N`` directory so successive updates
    do not overwrite earlier traces. Returns None when no workspace is bound.
    """
    cache = ir_state(context)
    managed = ir_session.current(context)
    if managed:
        run_index = cache.get("ir_trace_run", 0)
        cache["ir_trace_run"] = run_index + 1
        trace_root = managed["store"].root / "ir_trace" / managed["turn_id"] / f"run_{run_index}"
        trace_root.mkdir(parents=True, exist_ok=True)
        return str(trace_root)
    workspace = context.session.outputs
    if workspace is None:
        return None
    if "ir_trace_run" not in cache:
        trace_root = Path(workspace) / ".memory" / "ir_trace"
        cache["ir_trace_run"] = 1 + max(
            (int(path.name[4:]) for path in trace_root.glob("run_*") if path.name[4:].isdigit()),
            default=-1,
        )
    run_index = cache.get("ir_trace_run", 0)
    cache["ir_trace_run"] = run_index + 1
    trace_root = Path(workspace) / ".memory" / "ir_trace" / f"run_{run_index}"
    trace_root.mkdir(parents=True, exist_ok=True)
    return str(trace_root)


async def fill_ir_fields(
    context: AgentContext,
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
        context: Native model, configuration and private plugin state access.
        user_query: The user's original query.
        tool_evidence: Evidence from tool calls.
        confirmed_context: Additional confirmed context.
        current_values: Optional current values for each field_id.
        max_concurrency: Maximum concurrent field fill calls.

    Returns:
        List of field outputs from fill_field_templates.
    """
    cache = ir_state(context)
    field_templates = cache.get("ir_field_templates")
    if not field_templates:
        # 兜底：如果 cache 没有则重新加载
        catalog = load_template_catalog()
        field_templates = list_field_templates(catalog)

    if field_ids is not None:
        field_templates = [t for t in field_templates if t["field_id"] in field_ids]
    llm = context.get_model(ir_options(context).get("model"))
    trace_dir = trace_dir or _resolve_ir_trace_dir(context)
    logger.info(f"[fill_ir_fields] trace_dir={trace_dir}")

    field_results = await fill_field_templates(
        field_templates,
        user_query=user_query,
        tool_evidence=tool_evidence,
        llm=llm,
        confirmed_context=confirmed_context,
        current_values=current_values,
        max_concurrency=_ir_option(context, "max_concurrency", max_concurrency),
        completion_batch_size=_ir_option(context, "completion_batch_size", 1),
        completion_policy=_ir_option(context, "completion_policy", "always"),
        concise_rationale=_ir_option(context, "concise_rationale", False),
        compact_json=_ir_option(context, "compact_json", False),
        field_order=_ir_option(context, "field_order", "template"),
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
    context: AgentContext,
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
                    "\n先输出 consistent 布尔判定，再输出 warnings；例如发现问题时："
                    '{"consistent": false, "warnings": [{"field": "字段名", "issue": "问题描述", "suggestion": "修正建议"}]}。'
                    '无问题则输出 {"consistent": true, "warnings": []}。所有有依据的问题仍须完整列出。'
                ),
            },
        ]
        if _ir_option(context, "compact_json", False):
            messages[0]["content"] += COMPACT_JSON_INSTRUCTION
        parsed, raw = await ainvoke_json_object(
            context.get_model(ir_options(context).get("model")), messages
        )
        if not isinstance(parsed, dict) or not isinstance(parsed.get("warnings"), list):
            raise ValueError("Consistency gate returned invalid warnings")
        if not isinstance(parsed.get("consistent"), bool):
            raise ValueError("Consistency gate omitted its consistency verdict")
        for item in parsed["warnings"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("issue"), str)
                or not item["issue"].strip()
            ):
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
    write_field_meta(
        trace_dir,
        "_consistency_gate",
        {
            "success": error is None,
            "attempts": 1,
            "error": error,
            "latency_seconds": time.perf_counter() - started,
            "model_call_seconds": time.perf_counter() - started,
            "output": {"warnings": warnings},
        },
    )
    return warnings, error is None


async def _refresh_with_policy(context, state):
    cache = ir_state(context)
    policy = ir_failure_policy(context)
    ir_consistency_policy(context)
    try:
        await _update_ir_locked(context, state)
    except IRRefreshError as exc:
        if policy == "strict":
            raise
        fallback = {"signature": cache.get("ir_attempt_signature"), "reason": str(exc)}
        if cache.get("ir_fallback") != fallback:
            ir_session.record_fallback(context, fallback)
            logger.warning("[DataTaskIR fallback_without_ir] {}", fallback)
        cache.update(
            ir_fallback=fallback,
            ir_blocked_input={"signature": fallback["signature"], "message": str(exc)},
            ir_input_signature=None,
            ir_rendered_context="",
            ir_dirty=True,
            ir_refresh_failed=True,
        )
    # Called under the update lock. Capture this call before another consumer
    # changes the shared IR state, including an explicit None after recovery.
    return deepcopy(cache.get("ir_fallback"))


async def update_ir(context: AgentContext, state: dict) -> dict | None:
    """Refresh IR after a successful todo completion, or defer until consumption."""
    cache = ir_state(context)
    await ir_session.ensure_task_bound(context, state)
    if cache.get("ir_turn_suppressed"):
        return deepcopy(cache.get("ir_fallback"))
    policy = _ir_option(context, "refresh_policy", "eager")
    if policy not in {"eager", "on_consume"}:
        raise ValueError("DATA_TASK_IR.refresh_policy must be eager or on_consume")
    if policy == "on_consume":
        async with _update_lock(context):
            cache["ir_initialized"] = True
            cache["ir_dirty"] = True
        logger.info("[update_ir] deferred until IR consumer; no model calls")
        return None
    async with _update_lock(context):
        return await _refresh_with_policy(context, state)


def _update_lock(context: AgentContext) -> asyncio.Lock:
    cache = ir_state(context)
    lock = cache.get("ir_update_lock")
    if lock is None:
        lock = asyncio.Lock()
        cache["ir_update_lock"] = lock
    return lock


async def ensure_ir_ready(context: AgentContext, state: dict) -> dict | None:
    """Refresh from current state/evidence before a configured IR consumer runs.

    Both refresh policies check inputs even if no todo completed. The plugin's
    enabled switch, rather than plan creation, controls activation.
    """
    cache = ir_state(context)
    if not ir_enabled(context):
        return
    await ir_session.ensure_task_bound(context, state)
    if cache.get("ir_turn_suppressed"):
        return deepcopy(cache.get("ir_fallback"))
    cache["ir_initialized"] = True
    if state is None:
        raise RuntimeError("DataTaskIR consumer pre-hook requires current workflow state")
    async with _update_lock(context):
        fallback = await _refresh_with_policy(context, state)
        if cache.get("ir_input_signature") is None and not cache.get("ir_fallback"):
            cache["ir_dirty"] = True
            raise RuntimeError(
                "DataTaskIR consistency check incomplete; retry consumer before using IR"
            )
        return fallback


def _input_digest(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _ir_option(context: AgentContext, name: str, default):
    """Keep tuning scoped to the agent configuration, including child agents."""
    return ir_options(context).get(name, default)


def validate_ir_config(context):
    """Validate all policies before inference or durable state mutation."""
    ir_failure_policy(context)
    ir_consistency_policy(context)
    if _ir_option(context, "refresh_policy", "eager") not in {"eager", "on_consume"}:
        raise ValueError("DataTaskIR refresh_policy must be eager or on_consume")


async def _update_ir_locked(context: AgentContext, state: dict) -> None:
    cache = ir_state(context)
    await ir_session.ensure_task_bound(context, state)
    if cache.get("ir_turn_suppressed"):
        return
    managed = ir_session.current(context)
    evidence = collect_evidence(context, state or {})
    tool_evidence_parts = list(managed["inherited_evidence"]) if managed else []
    tool_evidence_parts.extend(
        json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) for record in evidence
    )
    latest = {}
    for part in tool_evidence_parts:
        item = json.loads(part)
        latest[_input_digest([item["tool"], item["params"]])] = part
    tool_evidence_parts = list(latest.values())
    if managed:
        ir_session.record_evidence(context, tool_evidence_parts)
    tool_evidence = "\n\n---\n\n".join(tool_evidence_parts)
    inputs = input_state(state or {})
    if not managed and cache.get("turn"):
        inputs = {"user_query": cache["turn"]["query"], "confirmed_context": ""}
    user_query = ir_session.query_context(context) if managed else inputs["user_query"]
    confirmed_context = inputs["confirmed_context"] if not managed else ir_session.INHERITANCE_RULES
    if not managed:
        feedback = [
            text
            for message in (state or {}).get("messages", [])
            if (text := ir_session.feedback_content(message))
            and getattr(message, "tool_call_id", None)
            not in cache.get("turn", {}).get("old_feedback_ids", set())
        ]
        confirmed_context += "\n" + "\n".join(feedback)
    confirmed_context += "\n本轮日期（运行环境本地时区）：" + datetime.now().date().isoformat()
    cache["input_state"] = {"user_query": user_query, "confirmed_context": confirmed_context}
    started = time.perf_counter()

    field_templates = cache.get("ir_field_templates")
    if not field_templates:
        field_templates = list_field_templates(load_template_catalog())
        cache["ir_field_templates"] = field_templates
    semantic_reuse = _ir_option(context, "semantic_field_reuse", False) is True
    context_signature = _input_digest(
        [
            user_query,
            confirmed_context,
            field_templates,
            {
                "completion_batch_size": _ir_option(context, "completion_batch_size", 1),
                "completion_policy": _ir_option(context, "completion_policy", "always"),
                "concise_rationale": _ir_option(context, "concise_rationale", False),
                "compact_json": _ir_option(context, "compact_json", False),
                "field_order": _ir_option(context, "field_order", "template"),
                "semantic_field_reuse": semantic_reuse,
                "consistency_policy": ir_consistency_policy(context),
            },
            datetime.now().date().isoformat(),
        ]
    )
    input_signature = _input_digest([context_signature, tool_evidence_parts])
    cache["ir_attempt_signature"] = input_signature
    blocked = cache.get("ir_blocked_input")
    if blocked and blocked["signature"] == input_signature:
        raise IRRefreshError(blocked["message"])
    cache["ir_fallback"] = None
    if cache.get("ir_input_signature") == input_signature:
        if managed and managed["snapshot"]["status"] != "committed":
            ir_session.commit(
                context, cache.get("ir_field_values") or [], tool_evidence_parts, input_signature
            )
        cache["ir_dirty"] = False
        logger.info("[update_ir] unchanged inputs; skip fill and gate")
        return

    # Publish no old validated snapshot while this refresh is awaiting model calls.
    cache["ir_input_signature"] = None
    cache["ir_refresh_failed"] = True
    cache["ir_initialized"] = True
    current_values = cache.get("ir_field_values") or []
    trace_dir = _resolve_ir_trace_dir(context)
    reuse_ids: set[str] = set()
    if cache.get("ir_fields_signature") == input_signature:
        # The previous fill completed, but its gate failed. Retry only the gate.
        field_results = current_values
    else:
        previous_parts = cache.get("ir_evidence_parts")
        append_only = (
            isinstance(previous_parts, list)
            and tool_evidence_parts[: len(previous_parts)] == previous_parts
            and len(tool_evidence_parts) > len(previous_parts)
        )
        if (
            semantic_reuse
            and cache.get("ir_context_signature") == context_signature
            and append_only
            and not cache.get("ir_gate_warnings")
        ):
            templates_by_id = {template["field_id"]: template for template in field_templates}
            confirmed = [
                field
                for field in current_values
                if field.get("field_id") in templates_by_id
                and is_field_confirmed(field, templates_by_id[field["field_id"]])
            ]
            if confirmed:
                changed_ids = await detect_field_changes(
                    new_evidence="\n\n---\n\n".join(tool_evidence_parts[len(previous_parts) :]),
                    confirmed_fields=confirmed,
                    llm=context.get_model(ir_options(context).get("model")),
                    trace_dir=trace_dir,
                )
                reuse_ids = {field["field_id"] for field in confirmed} - changed_ids
        fill_current = (
            current_values
            if append_only and cache.get("ir_context_signature") == context_signature
            else None
        )
        if managed and cache.get("ir_context_signature") is None:
            fill_current = current_values or None
        try:
            field_results = await fill_ir_fields(
                context,
                user_query=user_query,
                tool_evidence=tool_evidence,
                confirmed_context=confirmed_context,
                current_values=fill_current,
                reuse_field_ids=reuse_ids,
                trace_dir=trace_dir,
            )
            cache["ir_field_values"] = field_results
            cache["ir_fields_signature"] = input_signature
            cache["ir_context_signature"] = context_signature
            cache["ir_evidence_parts"] = tool_evidence_parts
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("[update_ir] field extraction failed: {}", type(exc).__name__)
            _extraction_failed(context, input_signature)

    if any(field.get("_extraction_error") for field in field_results):
        _extraction_failed(context, input_signature)
    field_results, gate_warnings, gate_completed = await _check_and_repair_ir(
        context,
        field_results,
        user_query=user_query,
        tool_evidence=tool_evidence,
        confirmed_context=confirmed_context,
        input_signature=input_signature,
        field_templates=field_templates,
        trace_dir=trace_dir,
    )
    cache["ir_gate_warnings"] = gate_warnings
    if not gate_completed or (gate_warnings and ir_consistency_policy(context) == "enforce"):
        cache["ir_dirty"] = True
        raise IRRefreshError(
            "DataTaskIR 尚未通过本轮检查；不能消费本次或旧版本 IR。" + "；".join(gate_warnings)
        )
    ir_session.commit(context, field_results, tool_evidence_parts, input_signature)
    rendered = render_constraint_context(field_results, warnings=gate_warnings)
    logger.info(f"[update_ir] ir_rendered_context: \n{rendered}")
    cache["ir_rendered_context"] = rendered
    cache["ir_refresh_failed"] = False
    cache["ir_dirty"] = False
    if gate_completed:
        cache["ir_input_signature"] = input_signature
    logger.info(
        "[update_ir] completed: fields={} reused={} gate_completed={} elapsed={:.3f}s trace={}",
        len(field_results),
        len(reuse_ids),
        gate_completed,
        time.perf_counter() - started,
        trace_dir,
    )


def get_ir_context(context: AgentContext) -> str:
    """Return consumable constraints; keep gate diagnostics in trace and snapshots."""
    cache = ir_state(context)
    if cache.get("ir_turn_suppressed") or (
        cache.get("ir_fallback") and ir_failure_policy(context) == "fallback_without_ir"
    ):
        return ""
    if cache.get("ir_dirty") or cache.get("ir_refresh_failed"):
        raise RuntimeError(
            "DataTaskIR refresh failed or is in progress; retry update_ir before consuming IR"
        )
    fields = cache.get("ir_field_values")
    if not fields:
        return ""
    rendered = cache.get("ir_rendered_context")
    if not rendered or rendered == "IR init!":
        rendered = render_constraint_context(fields, warnings=cache.get("ir_gate_warnings") or [])
    return rendered


def read_ir(context: AgentContext) -> str:
    """Read the current IR without refreshing it, as in the legacy implementation."""
    if not ir_state(context).get("ir_rendered_context"):
        logger.error("[read_ir] task ir not created, use create_ir to init IR.")
        return ""
    return get_ir_context(context)
