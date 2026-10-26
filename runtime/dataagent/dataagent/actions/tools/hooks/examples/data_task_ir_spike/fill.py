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
"""Concurrent real-LLM field filling for the independent DataTaskIR spike."""

# ruff: noqa: UP045 -- repository convention requires Optional for nullable annotations.

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional
from loguru import logger

import yaml

from dataagent.core.managers.llm_manager.llm_client import LLMClient
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.json_llm import ainvoke_json_object, repair_json_object
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import materialize_writable_template

CONCISE_RATIONALE_INSTRUCTION = (
    "\n输出精简要求：value 必须完整保留已有证据，不要求填满未知槽位，不能省略已知字段、条件、表达式或分支。"
    "rationale 用1至2句简短说明直接依据，不复述整段问题、模板规则或元数据；"
    "unresolved 逐项简述真实缺失/冲突，不重复，question 合并为一句。"
    "精简只作用于说明文字，不能缩减业务内容。"
)
COMPACT_JSON_INSTRUCTION = (
    "\n使用紧凑 JSON 输出：不缩进、不换行，省去 JSON 标点周围的非必要空格。"
    "这只是序列化格式要求，字符串内的空格、全部键和值、数组元素及业务表达式必须完整保留。"
)

TOOL_EVIDENCE_GRADING_NOTE = (
    "（证据分级提示：以下工具证据中，表/列/指标/UDF 的语义描述属映射级，"
    "仅用于把用户概念映射到具体字段名、表名或函数名；对既有实现的结构描述--"
    "源表物理主键、GROUP BY键、去重键、join键、分区键，以及历史SQL/参考SQL 的"
    "源表选择、过滤条件、时间窗口、输出列结构--属参考级，只反映该实现当时怎么写，"
    "不构成输出主键、输出字段结构或业务口径的依据；口径依据只认【用户原始问题】"
    "与【已确认上下文】中的用户表述，详见【模板公共契约】证据分级。）"
)


# Structural integrity only: absent leaves are normalized to unknown locally.
# Unknown values and empty arrays never request business-content completion.
# This is not a checklist of business information the user must provide.
CRITICAL_PATHS = {
    "identity": [
        "components[].field_ref",
        "components[].null_semantics",
    ],
    "sources": [
        "facts[].source_ref",
        "facts[].filters[].field_ref",
        "facts[].filters[].operator",
        "dimensions[].source_ref",
        "dimensions[].join.fact_source_ref",
        "dimensions[].join.join_type",
        "dimensions[].join.conditions",
        "dimensions[].join.cardinality",
    ],
    "time_context": [
        "input_windows[].window_id",
        "input_windows[].time_field",
        "input_windows[].range",
    ],
    "output_features": [
        "layout",
        "axes[].axis_id",
        "axes[].values",
        "measures[].measure_id",
        "measures[].aggregation",
        "features[].field_name",
    ],
    "pipeline": [],
    "sequence": [
        "ordering[].field_ref",
        "ordering[].direction",
    ],
}


def _degraded_field_output(
    field_id: str,
    error: str,
    *,
    previous: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a non-blocking placeholder when one field fails to extract.

    Downstream consumers keep running with remaining fields; this field is marked
    unresolved so it is not treated as confirmed口径.
    """
    err = (error or "unknown error").strip() or "unknown error"
    if isinstance(previous, dict) and isinstance(previous.get("value"), dict):
        output = deepcopy(previous)
        output["field_id"] = field_id
        unresolved = [str(item) for item in (output.get("unresolved") or []) if str(item).strip()]
        note = f"本轮重新提取失败，暂沿用上一轮结果（{err}）"
        if note not in unresolved:
            unresolved.append(note)
        output["unresolved"] = unresolved
        output["question"] = (
            output.get("question")
            if isinstance(output.get("question"), str) and output["question"].strip()
            else f"字段 {field_id} 本轮提取失败，已沿用旧值，请复核后重试 update_ir。"
        )
        output["rationale"] = (
            output.get("rationale")
            if isinstance(output.get("rationale"), str) and output["rationale"].strip()
            else note
        )
        return output
    return {
        "field_id": field_id,
        "value": {},
        "rationale": f"字段提取失败，本轮跳过该字段：{err}",
        "unresolved": [f"字段提取失败：{err}"],
        "question": (
            f"字段 {field_id} 未能生成有效 JSON/结果，本轮不强制该字段口径；"
            "请补充证据后重试 update_ir。"
        ),
    }


def write_field_attempt(
    trace_dir: str | None,
    field_id: str,
    attempt: int,
    messages: list[dict[str, str]],
    raw_response: str,
) -> None:
    """Persist one fill attempt (full prompt + raw response) for offline audit."""
    if not trace_dir:
        return
    try:
        target = Path(trace_dir) / field_id
        target.mkdir(parents=True, exist_ok=True)
        prompt_text = "\n\n".join(
            f"===== {message.get('role', 'unknown')} =====\n{message.get('content', '')}"
            for message in messages
        )
        (target / f"attempt_{attempt}_prompt.txt").write_text(prompt_text, encoding="utf-8")
        (target / f"attempt_{attempt}_raw_response.txt").write_text(raw_response or "", encoding="utf-8")
    except Exception:
        logger.exception("[fill] failed to write field trace, field_id=%s attempt=%s", field_id, attempt)


def write_field_meta(trace_dir: str | None, field_id: str, result: dict[str, Any]) -> None:
    """Persist a small per-field summary (success/attempts/errors/final output)."""
    if not trace_dir:
        return
    try:
        target = Path(trace_dir) / field_id
        target.mkdir(parents=True, exist_ok=True)
        meta = {
            "field_id": field_id,
            "success": result.get("success"),
            "attempts": result.get("attempts"),
            "model_requests": result.get("model_requests", result.get("attempts", 0)),
            "error": result.get("error"),
            "completion_retried": result.get("completion_retried"),
            "missing_critical_paths": result.get("missing_critical_paths"),
            "latency_seconds": result.get("latency_seconds", 0.0),
            "queue_wait_seconds": result.get("queue_wait_seconds", 0.0),
            "model_call_seconds": result.get("model_call_seconds", 0.0),
            "skipped_incremental": result.get("skipped_incremental", False),
            "completion_pending": result.get("completion_pending", False),
            "completion_skipped_reason": result.get("completion_skipped_reason"),
            "completion_batch_size": result.get("completion_batch_size", 1),
            "completion_batch_trace": result.get("completion_batch_trace"),
            "final_output": result.get("output"),
        }
        (target / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, default=str, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("[fill] failed to write field trace meta, field_id=%s", field_id)


def create_llm_client(
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
    num_retries: int = 2,
) -> LLMClient:
    """Create the repository's real OpenAI-compatible LLM client for the spike."""
    return LLMClient(
        model=model,
        api_base=api_base,
        api_key=api_key,
        provider="custom",
        timeout=timeout,
        num_retries=num_retries,
        enable_cache_control=False,
        extra_body={"temperature": 0},
    )


def build_field_messages(
    field_template: dict[str, Any],
    *,
    user_query: str,
    tool_evidence: str,
    confirmed_context: str = "",
    current_value: Optional[dict[str, Any]] = None,
    concise_rationale: bool = False,
    compact_json: bool = False,
) -> list[dict[str, str]]:
    """Compile one independent field unit and its evidence into model messages."""
    system, common, specific = _build_field_parts(
        field_template, user_query=user_query, tool_evidence=tool_evidence,
        confirmed_context=confirmed_context, current_value=current_value,
    )
    if concise_rationale:
        system += CONCISE_RATIONALE_INSTRUCTION
    if compact_json:
        system += COMPACT_JSON_INSTRUCTION
    return [{"role": "system", "content": system},
            {"role": "user", "content": common + "\n\n" + specific}]


def _build_field_parts(
    field_template, *, user_query, tool_evidence, confirmed_context, current_value,
):
    """Build shared and field-specific blocks without parsing untrusted evidence."""
    writable = materialize_writable_template(field_template, current_value)
    system = """你是 DataTaskIR 单字段提取器。你只能根据本次输入中的证据填写一个字段。
只输出一个 JSON 对象，不输出 Markdown 或 JSON 之外的任何文字。
field_id 和 value 必须保留；rationale 必须输出；unresolved 和 question 按输出字段协议决定是否出现。
rationale 是一段纯文本，必须说明你填写 value 的依据与关键判断逻辑；引用用户原话或工具证据中的具体表述，说明为什么这样映射；
即使 value 为空，也要说明输入中没有可填内容的证据或原因。禁止把 rationale 写成与输入无关的套话，禁止编造输入中不存在的依据。
few-shot 只演示协议，不是当前任务证据，禁止复制例子中的业务值。
必须尽量提取用户原始问题、已确认上下文和工具证据中属于当前 Field Unit 的全部确定内容。
即使用户没有使用字段标题，也不能忽略自然语言中已经表达的直接口径。
允许部分填写；没有证据、存在歧义或冲突的子字段保持 null 或空数组，禁止自行裁决。
上述规则适用于整个 Field Unit 及其任意子字段；完整提取已有证据，不等于填满模板。
模板的 required/completion_conditions 描述结构或信息齐备状态，不要求为满足它们而补出未知业务值。
未涉及的操作保持空值，不为填满模板新增操作或要求用户逐项补齐。
空值不等于已确认不执行；只有用户、已确认上下文或工具证据明确说明不执行时，才作此判断。
仅当缺失或冲突影响当前任务的正确实现时输出 unresolved 和 question；unresolved 是纯文本字符串数组。
可选操作未涉及、与当前任务无关的槽位为空，不是待澄清问题。
question 是一个字符串，必须说明用户和证据中是否存在相关信息：
没有时说明需要补充什么；有时指出哪里没说清楚。
unresolved 只能记录当前 Field Unit 的直接问题；其他字段的缺失或冲突必须完全忽略。
冲突涉及的 value 槽位必须保持 null 或空数组；不得填写某个候选值后再备注冲突。
禁止把“通常、一般、默认、建议、prefer、preference、latest、earliest”写成确定口径。
[当前字段值]是历史填充的记录，你需要根据新查询的信息，看是否要在已有基础上添加信息/修正信息/删除信息等，注意严格按照模板规范"""
    common_sections = {
        "模板公共契约": _model_visible_contract(field_template),
        "输出字段协议": _model_visible_output_contract(field_template),
        "用户原始问题": user_query,
        "已确认上下文": confirmed_context or "（无）",
        # Keep the complete common input before field-specific content so providers
        # with prefix caching can share it across fields and completion calls.
        "工具证据": (TOOL_EVIDENCE_GRADING_NOTE + "\n" + tool_evidence) if tool_evidence else "（无）",
    }
    field_sections = {
        "当前字段值": current_value if current_value is not None else "（无）",
        "字段目的": field_template.get("purpose", ""),
        "字段填写说明": field_template.get("field_instructions", {}),
        "字段 Few-shots": _model_visible_few_shots(field_template),
        "可写模板": writable,
    }
    def render_sections(sections):
        chunks = []
        for title, content in sections.items():
            # Large evidence/context strings are already text. YAML scanning/escaping
            # each copy blocks the event loop and does not add information.
            rendered = content if isinstance(content, str) else yaml.safe_dump(
                content, allow_unicode=True, sort_keys=False,
            ).strip()
            chunks.append(f"【{title}】\n{rendered}")
        return "\n\n".join(chunks)

    return system, render_sections(common_sections), render_sections(field_sections)


def parse_model_json(raw_text: str) -> dict[str, Any]:
    """Repair and parse a model response while preserving the public object-returning interface."""
    parsed = repair_json_object(raw_text)
    logger.info(f"[parse_model_json] parsed_response: {parsed}")
    if parsed is None:
        raise ValueError("Model output could not be repaired into a JSON object")
    return parsed


async def fill_field_template(
    field_template: dict[str, Any],
    *,
    user_query: str,
    tool_evidence: str,
    llm: LLMClient,
    confirmed_context: str = "",
    current_value: Optional[dict[str, Any]] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    allow_repair: bool = True,
    trace_dir: Optional[str] = None,
    completion_policy: str = "always",
    concise_rationale: bool = False,
    compact_json: bool = False,
) -> dict[str, Any]:
    """Fill one independent DataTaskIR field template and return its repaired JSON object."""
    detailed = await fill_field_template_detailed(
        field_template,
        user_query=user_query,
        tool_evidence=tool_evidence,
        llm=llm,
        confirmed_context=confirmed_context,
        current_value=current_value,
        semaphore=semaphore,
        allow_repair=allow_repair,
        trace_dir=trace_dir,
        completion_policy=completion_policy,
        concise_rationale=concise_rationale,
        compact_json=compact_json,
    )
    output = detailed.get("output")
    if not isinstance(output, dict) or detailed.get("error"):
        raise ValueError(str(detailed.get("error", "DataTaskIR field extraction failed")))
    return output


async def fill_field_template_detailed(
    field_template: dict[str, Any],
    *,
    user_query: str,
    tool_evidence: str,
    llm: LLMClient,
    confirmed_context: str = "",
    current_value: Optional[dict[str, Any]] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    allow_repair: bool = True,
    trace_dir: Optional[str] = None,
    defer_completion: bool = False,
    completion_policy: str = "always",
    concise_rationale: bool = False,
    compact_json: bool = False,
) -> dict[str, Any]:
    """Fill one field and return output plus attempts, latency, raw responses, and any call or parsing error.

    When ``trace_dir`` is set, every attempt's full prompt and raw response are
    written under ``{trace_dir}/{field_id}/`` for offline audit, together with a
    ``meta.json`` summary written on return.
    """
    if completion_policy not in {"always", "partial_only"}:
        raise ValueError("completion_policy must be always or partial_only")
    messages = build_field_messages(
        field_template,
        user_query=user_query,
        tool_evidence=tool_evidence,
        confirmed_context=confirmed_context,
        current_value=current_value,
        concise_rationale=concise_rationale,
        compact_json=compact_json,
    )
    gate = semaphore or asyncio.Semaphore(1)
    started = time.perf_counter()
    field_id = str(field_template.get("field_id"))

    def finalize(result: dict[str, Any]) -> dict[str, Any]:
        write_field_meta(trace_dir, field_id, result)
        return result
    raw_responses = []
    errors = []
    queue_wait_seconds = 0.0
    model_call_seconds = 0.0
    max_attempts = 2 if allow_repair else 1
    best_output = None
    previous_missing_paths: list[str] = []
    completion_retried = False
    for attempt in range(1, max_attempts + 1):
        call_messages = messages
        if attempt == 2:
            if previous_missing_paths:
                call_messages = _build_completion_messages(messages, raw_responses[-1], previous_missing_paths)
                completion_retried = True
            else:
                call_messages = _build_repair_messages(messages, raw_responses[-1], errors[-1])
        try:
            queue_started = time.perf_counter()
            async with gate:
                call_started = time.perf_counter()
                queue_wait_seconds += call_started - queue_started
                try:
                    output, raw = await ainvoke_json_object(llm, call_messages)
                finally:
                    model_call_seconds += time.perf_counter() - call_started
            raw_responses.append(raw)
            write_field_attempt(trace_dir, field_id, attempt, call_messages, raw)
            if output is None:
                raise ValueError("Model output could not be repaired into a JSON object")
            if not isinstance(output.get("value"), dict):
                raise ValueError("DataTaskIR field value must be an object")
            output["field_id"] = field_template.get("field_id")
            output["value"] = _normalize_field_value(
                output["value"], field_template.get("value_schema", {}),
                field_template.get("writable_template", {}).get("value", {}),
            )
            best_output = deepcopy(output)
            # Repair malformed structure, never ask the model to fill unknown business values.
            # completion_policy is retained for configuration compatibility; both policies
            # now permit unknowns and repair structural errors only.
            missing_paths = _find_missing_critical_paths(field_template, output.get("value", {}))
            if missing_paths and attempt < max_attempts and not defer_completion:
                previous_missing_paths = missing_paths
                errors.append(f"Invalid structural paths: {', '.join(missing_paths)}")
                continue
            output = _add_completion_diagnostics(field_template, output, missing_paths)
            return finalize({
                "field_id": field_template.get("field_id"),
                "success": True,
                "attempts": attempt,
                "latency_seconds": time.perf_counter() - started,
                "queue_wait_seconds": queue_wait_seconds,
                "model_call_seconds": model_call_seconds,
                "output": output,
                "raw_responses": raw_responses,
                "error": None,
                "completion_retried": completion_retried,
                "missing_critical_paths": missing_paths,
                "completion_pending": bool(missing_paths and defer_completion),
                "completion_skipped_reason": "no_structural_errors" if not missing_paths else None,
            })
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if len(raw_responses) < attempt:
                raw_responses.append("")
    if best_output is not None:
        missing_paths = _find_missing_critical_paths(field_template, best_output.get("value", {}))
        output = _add_completion_diagnostics(field_template, best_output, missing_paths)
        return finalize({
            "field_id": field_template.get("field_id"),
            "success": False,
            "attempts": max_attempts,
            "latency_seconds": time.perf_counter() - started,
            "queue_wait_seconds": queue_wait_seconds,
            "model_call_seconds": model_call_seconds,
            "output": output,
            "raw_responses": raw_responses,
            "error": errors[-1] if errors else "Field completion failed",
            "completion_retried": completion_retried,
            "missing_critical_paths": missing_paths,
        })
    return finalize({
        "field_id": field_template.get("field_id"),
        "success": False,
        "attempts": max_attempts,
        "latency_seconds": time.perf_counter() - started,
        "queue_wait_seconds": queue_wait_seconds,
        "model_call_seconds": model_call_seconds,
        "output": None,
        "raw_responses": raw_responses,
        "error": errors[-1] if errors else "Unknown extraction error",
        "completion_retried": completion_retried,
        "missing_critical_paths": [],
    })


async def fill_field_templates(
    field_templates: list[dict[str, Any]],
    *,
    user_query: str,
    tool_evidence: str,
    llm: LLMClient,
    confirmed_context: str = "",
    current_values: Optional[list[dict[str, Any]]] = None,
    max_concurrency: int = 8,
    allow_repair: bool = True,
    trace_dir: Optional[str] = None,
    reuse_field_ids: Optional[set[str]] = None,
    completion_batch_size: int = 1,
    completion_policy: str = "always",
    concise_rationale: bool = False,
    compact_json: bool = False,
    field_order: str = "template",
) -> list[dict[str, Any]]:
    """Fill independent DataTaskIR field units concurrently with one shared concurrency limit."""
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    if (isinstance(completion_batch_size, bool) or not isinstance(completion_batch_size, int)
            or not 1 <= completion_batch_size <= 4):
        raise ValueError("completion_batch_size must be between 1 and 4")
    if completion_policy not in {"always", "partial_only"}:
        raise ValueError("completion_policy must be always or partial_only")
    if field_order not in {"template", "long_first"}:
        raise ValueError("field_order must be template or long_first")
    values = {value["field_id"]: value for value in (current_values or [])}
    reuse_ids = reuse_field_ids or set()
    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = []
    deferred_templates = []
    # Start historically long outputs early, without changing their prompts or
    # public result order. Keep the same semaphore limit and full validation.
    priority = {"output_features": 0, "sources": 1, "time_context": 2}
    scheduled = (
        sorted(field_templates, key=lambda t: priority.get(t["field_id"], 3))
        if field_order == "long_first" else field_templates
    )
    async def _safe_detailed(field_template: dict[str, Any], *, defer_completion: bool) -> dict[str, Any]:
        field_id = str(field_template.get("field_id", ""))
        try:
            return await fill_field_template_detailed(
                field_template, user_query=user_query, tool_evidence=tool_evidence,
                llm=llm, confirmed_context=confirmed_context, current_value=values.get(field_id),
                semaphore=semaphore, allow_repair=allow_repair, trace_dir=trace_dir,
                defer_completion=defer_completion,
                completion_policy=completion_policy, concise_rationale=concise_rationale,
                compact_json=compact_json,
            )
        except Exception as exc:
            logger.warning("[IR] field {} raised {}; degrading", field_id, type(exc).__name__)
            return {
                "field_id": field_id,
                "success": False,
                "attempts": 0,
                "output": None,
                "error": f"{type(exc).__name__}: {exc}",
                "completion_pending": False,
                "raw_responses": [],
                "missing_critical_paths": [],
            }

    for field_template in scheduled:
        field_id = str(field_template.get("field_id", ""))
        file_value = values.get(field_id)
        if field_id in reuse_ids and is_field_confirmed(file_value, field_template):
            reused = deepcopy(file_value)
            write_field_meta(trace_dir, field_id, {
                "success": True, "attempts": 0, "skipped_incremental": True, "output": reused,
            })
            tasks.append(asyncio.sleep(0, result={
                "field_id": field_id, "success": True, "output": reused, "error": None,
                "completion_pending": False,
            }))
            continue
        if completion_batch_size > 1 and allow_repair:
            deferred_templates.append(field_template)
            tasks.append(_safe_detailed(field_template, defer_completion=True))
            continue
        tasks.append(_safe_detailed(field_template, defer_completion=False))
    details_list = await _gather_cancel_on_error(tasks)
    details_by_id = {str(item.get("field_id", "")): item for item in details_list if isinstance(item, dict)}

    def _materialize(field_template: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
        field_id = str(field_template.get("field_id", ""))
        if detail is None:
            logger.warning("[IR] field {} missing from fill results; degrading", field_id)
            return _degraded_field_output(field_id, "missing fill result", previous=values.get(field_id))
        output = detail.get("output")
        if isinstance(output, dict) and isinstance(output.get("value"), dict) and (
            detail.get("success") or detail.get("degraded")
        ):
            return output
        error = str(detail.get("error") or "DataTaskIR field extraction failed")
        logger.warning("[IR] field {} degraded: {}", field_id, error)
        degraded = _degraded_field_output(field_id, error, previous=values.get(field_id))
        detail["output"] = degraded
        detail["degraded"] = True
        detail["completion_pending"] = False
        write_field_meta(trace_dir, field_id, detail)
        return degraded

    if deferred_templates:
        for template in deferred_templates:
            field_id = str(template.get("field_id", ""))
            detail = details_by_id.get(field_id)
            if detail is None or not detail.get("success") or detail.get("error"):
                # Leave a degraded output now; do not send failed fields to completion batch.
                error = (detail or {}).get("error") or "DataTaskIR field extraction failed"
                degraded = _degraded_field_output(field_id, str(error), previous=values.get(field_id))
                details_by_id[field_id] = {
                    "field_id": field_id,
                    "success": False,
                    "degraded": True,
                    "output": degraded,
                    "error": error,
                    "completion_pending": False,
                    "attempts": (detail or {}).get("attempts", 0),
                    "raw_responses": (detail or {}).get("raw_responses") or [],
                    "missing_critical_paths": [],
                }
                write_field_meta(trace_dir, field_id, details_by_id[field_id])
        incomplete = [
            template for template in deferred_templates
            if details_by_id[str(template["field_id"])].get("completion_pending")
        ]
        await _gather_cancel_on_error([
            _complete_field_batch(
                incomplete[start:start + completion_batch_size], details=details_by_id,
                user_query=user_query, tool_evidence=tool_evidence, confirmed_context=confirmed_context,
                current_values=values, llm=llm, semaphore=semaphore, trace_dir=trace_dir,
                concise_rationale=concise_rationale, compact_json=compact_json,
            )
            for start in range(0, len(incomplete), completion_batch_size)
        ])

    return [
        deepcopy(_materialize(template, details_by_id.get(str(template.get("field_id", "")))))
        for template in field_templates
    ]


async def _gather_cancel_on_error(tasks: list) -> list:
    pending = [asyncio.create_task(task) for task in tasks]
    try:
        outputs = await asyncio.gather(*pending)
    except BaseException:
        # Do not leave model calls running after the runtime releases its update lock.
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise
    return outputs


async def _complete_field_batch(
    templates: list[dict[str, Any]], *, details: dict[str, dict[str, Any]],
    user_query: str, tool_evidence: str, confirmed_context: str,
    current_values: dict[str, dict[str, Any]], llm: LLMClient,
    semaphore: asyncio.Semaphore, trace_dir: Optional[str],
    concise_rationale: bool = False, compact_json: bool = False,
) -> None:
    """Share evidence across at most four second opinions; never waive missing checks.

    A malformed/failed batch falls back to the original independent completion
    request. This opt-in path changes model reasoning and needs task-level evals.
    """
    requests = []
    messages = None
    for template in templates:
        field_id = template["field_id"]
        _, common, specific = _build_field_parts(
            template, user_query=user_query, tool_evidence=tool_evidence,
            confirmed_context=confirmed_context, current_value=current_values.get(field_id),
        )
        if messages is None:
            messages = [
                {"role": "system", "content": (
                    "你是 DataTaskIR 字段补全审查员。分别复核本批每个字段，只能依据完整用户问题、"
                    "已确认上下文和工具证据补全。每个字段遵循它自己的模板、说明和示例，"
                    "不能互相代填，历史值和示例都不是新增证据。只修复错误的数据结构，"
                    "空值不是漏填，不要求填满模板，也不得把空值自动解释为不适用。"
                    "缺证据或冲突时保留空值；只对影响当前任务正确实现的问题保留 unresolved/question，"
                    "禁止猜测或丢弃原有已证实内容。逐字段输出 rationale 说明依据。"
                    '仅输出 JSON 对象 {"fields": [各字段的完整输出对象]}，每个请求字段恰好出现一次。'
                    '模板中的单字段输出协议在 fields 数组的每个元素内适用。'
                )},
                {"role": "user", "content": common},
            ]
        # Include contracts per field too: callers may supply different catalogs.
        requests.append({
            "field_id": field_id,
            "template_contract": _model_visible_contract(template),
            "output_contract": _model_visible_output_contract(template),
            "field_request": specific,
            "previous_output": details[field_id]["output"],
            "missing_critical_paths": details[field_id]["missing_critical_paths"],
        })
    messages.append({"role": "user", "content": json.dumps(requests, ensure_ascii=False)})
    started = time.perf_counter()
    raw = ""
    error = None
    if concise_rationale:
        messages[0]["content"] += CONCISE_RATIONALE_INSTRUCTION
    if compact_json:
        messages[0]["content"] += COMPACT_JSON_INSTRUCTION
    candidates = {}
    call_started = started
    try:
        async with semaphore:
            call_started = time.perf_counter()
            parsed, raw = await ainvoke_json_object(llm, messages)
        fields = parsed.get("fields") if isinstance(parsed, dict) else None
        expected = {template["field_id"] for template in templates}
        if (not isinstance(fields, list) or len(fields) != len(expected)
                or any(not isinstance(item, dict) or not isinstance(item.get("field_id"), str)
                       for item in fields)
                or {item["field_id"] for item in fields} != expected):
            raise ValueError("Batch completion returned missing, duplicate or unknown field IDs")
        candidates = {item["field_id"]: item for item in fields}
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("[IR] batch completion failed ({}); retry individual fields", error)
    elapsed = time.perf_counter() - started
    batch_id = "_completion_" + "_".join(template["field_id"] for template in templates)
    write_field_attempt(trace_dir, batch_id, 1, messages, raw)
    write_field_meta(trace_dir, batch_id, {
        "success": error is None, "attempts": 1, "error": error,
        "latency_seconds": elapsed, "queue_wait_seconds": call_started - started,
        "model_call_seconds": elapsed - (call_started - started),
    })

    async def finish(template):
        field_id = template["field_id"]
        detail = details[field_id]
        output = candidates.get(field_id)
        fallback_wait = 0.0
        fallback_seconds = 0.0
        initial_attempts = detail["attempts"]
        try:
            # A parseable object is not sufficient to trust a batched response.
            valid = (isinstance(output, dict) and isinstance(output.get("value"), dict)
                     and isinstance(output.get("rationale"), str) and bool(output["rationale"].strip())
                     and isinstance(output.get("unresolved", []), list)
                     and all(isinstance(item, str) for item in output.get("unresolved", []))
                     and (not output.get("unresolved") or
                          isinstance(output.get("question"), str) and bool(output["question"].strip())))
            if not valid:
                original = build_field_messages(
                    template, user_query=user_query, tool_evidence=tool_evidence,
                    confirmed_context=confirmed_context, current_value=current_values.get(field_id),
                    concise_rationale=concise_rationale, compact_json=compact_json,
                )
                raw_responses = detail.get("raw_responses") or [""]
                fallback = _build_completion_messages(
                    original, raw_responses[-1], detail.get("missing_critical_paths") or [],
                )
                fallback_started = time.perf_counter()
                async with semaphore:
                    fallback_call_started = time.perf_counter()
                    fallback_wait = fallback_call_started - fallback_started
                    try:
                        output, fallback_raw = await ainvoke_json_object(llm, fallback)
                    except Exception as exc:
                        raise ValueError(f"Individual completion failed for {field_id}: {type(exc).__name__}") from exc
                    fallback_seconds = time.perf_counter() - fallback_call_started
                write_field_attempt(trace_dir, field_id, detail["attempts"] + 2, fallback, fallback_raw)
                if not isinstance(output, dict) or not isinstance(output.get("value"), dict):
                    raise ValueError(f"Individual completion failed for {field_id}")
                detail["attempts"] += 1
            output["field_id"] = field_id
            output["value"] = _normalize_field_value(
                output["value"], template.get("value_schema", {}),
                template.get("writable_template", {}).get("value", {}),
            )
            missing = _find_missing_critical_paths(template, output["value"])
            detail.update(
                output=_add_completion_diagnostics(template, output, missing),
                attempts=detail["attempts"] + 1, completion_retried=True, completion_pending=False,
                completion_batch_size=len(templates), missing_critical_paths=missing,
                completion_batch_trace=batch_id,
                model_requests=initial_attempts + (not valid),
                latency_seconds=detail["latency_seconds"] + time.perf_counter() - started,
                # Shared-call time is recorded under batch_id once, not per field.
                queue_wait_seconds=detail["queue_wait_seconds"] + fallback_wait,
                model_call_seconds=detail["model_call_seconds"] + fallback_seconds,
            )
            write_field_meta(trace_dir, field_id, detail)
        except Exception as exc:
            error = str(exc)
            logger.warning("[IR] completion for {} degraded: {}", field_id, error)
            degraded = _degraded_field_output(
                field_id, error, previous=current_values.get(field_id) or detail.get("output"),
            )
            detail.update({
                "success": False,
                "degraded": True,
                "error": error,
                "output": degraded,
                "completion_pending": False,
                "completion_retried": True,
                "completion_batch_trace": batch_id,
                "attempts": detail.get("attempts", 0) + 1,
            })
            write_field_meta(trace_dir, field_id, detail)

    await _gather_cancel_on_error([finish(template) for template in templates])


def _build_repair_messages(
    original_messages: list[dict[str, str]], raw_response: str, error: str
) -> list[dict[str, str]]:
    repair = (
        "上一输出经过 json-repair 后仍不能得到 JSON 对象。"
        "只修复 JSON 表达，不得改变证据含义。\n"
        f"解析错误：{error}\n"
        f"上一输出：\n{raw_response}\n"
        "请重新输出一个完整、合法且仅包含可写模板字段的 JSON 对象。"
    )
    messages = deepcopy(original_messages)
    messages.append({"role": "user", "content": repair})
    return messages


def _build_completion_messages(
    original_messages: list[dict[str, str]], raw_response: str, missing_paths: list[str]
) -> list[dict[str, str]]:
    refill = (
        "上一输出存在数据结构或输出协议问题，请修复格式、类型或非法枚举值。"
        "只使用用户原始问题和工具证据，不得为修复结构而补造业务值。\n"
        f"需检查的结构路径：{', '.join(missing_paths)}\n"
        f"上一输出：\n{raw_response}\n"
        "没有证据的整个字段或子字段保持 null/空数组，不要求填满模板。"
        "只对影响当前任务正确实现的真实缺失或冲突输出 unresolved/question。"
    )
    messages = deepcopy(original_messages)
    messages.append({"role": "user", "content": refill})
    return messages


def _normalize_field_value(value: Any, schema: Any, prototype: Any) -> Any:
    """Restore missing keys as unknown, without copying example/history business values.

    Descriptive schemas constrain known values; even a non-nullable business slot
    can be unknown during extraction. Missing containers become null (not []/{}),
    so normalization cannot invent a negative decision or a global grouping.
    """
    schema = schema if isinstance(schema, dict) else {}
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, dict):
        prototype = prototype if isinstance(prototype, dict) else {}
        properties = schema.get("properties", {})
        result = deepcopy(value)
        for key in list(prototype) + schema.get("required", []):
            result.setdefault(key, None)
        return {
            key: _normalize_field_value(child, properties.get(key, {}), prototype.get(key))
            for key, child in result.items()
        }
    if isinstance(value, list):
        item_prototype = prototype[0] if isinstance(prototype, list) and prototype else None
        return [_normalize_field_value(child, schema.get("items", {}), item_prototype) for child in value]
    return value


def _find_missing_critical_paths(field_template: dict[str, Any], value: Any) -> list[str]:
    """Return structural errors only; business completeness is evidence-dependent."""
    field_id = str(field_template.get("field_id", ""))
    paths = CRITICAL_PATHS.get(field_id, [])
    mapping = value if isinstance(value, dict) else {}
    schema = field_template.get("value_schema", {})
    return [path for path in paths if not _has_structural_path(mapping, schema, path.split("."))]


def _has_structural_path(value: Any, schema: Any, parts: list[str]) -> bool:
    if value is None:
        return True  # Unknown, including an entire intermediate object/container.
    schema = schema if isinstance(schema, dict) else {}
    if not parts:
        if "enum" in schema and value not in schema["enum"]:
            return False
        type_ = schema.get("type")
        types = type_ if isinstance(type_, list) else [type_]
        type_checks = {
            "string": isinstance(value, str), "array": isinstance(value, list),
            "object": isinstance(value, dict), "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }
        return type_ is None or any(type_checks.get(name, False) for name in types)
    if not isinstance(value, dict):
        return False
    part = parts[0]
    key = part[:-2] if part.endswith("[]") else part
    if key not in value:
        return False  # The fill pipeline normalizes absent keys before this check.
    child = value[key]
    child_schema = schema.get("properties", {}).get(key, {})
    if part.endswith("[]"):
        if child is None:
            return True
        if not isinstance(child, list):
            return False
        return all(_has_structural_path(item, child_schema.get("items", {}), parts[1:]) for item in child)
    return _has_structural_path(child, child_schema, parts[1:])


def _has_meaningful_path(value: Any, parts: list[str]) -> bool:
    if not parts:
        return _is_meaningful_value(value)
    if not isinstance(value, dict):
        return False
    part = parts[0]
    if part.endswith("[]"):
        child = value.get(part[:-2])
        if not isinstance(child, list):
            return False
        # Empty list is intentional absence (宁缺毋滥), not a missing leaf.
        if not child:
            return True
        return all(_has_meaningful_path(item, parts[1:]) for item in child)
    child = value.get(part)
    return _has_meaningful_path(child, parts[1:])


def _is_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _has_meaningful_leaf(value: Any) -> bool:
    """Return whether a nested value contains any non-empty leaf."""
    if isinstance(value, dict):
        return any(_has_meaningful_leaf(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_meaningful_leaf(child) for child in value)
    return _is_meaningful_value(value)


def is_field_confirmed(current_value: Any, field_template: dict[str, Any]) -> bool:
    """Keep reuse stricter than extraction: unknown critical slots must be rechecked."""
    if not isinstance(current_value, dict):
        return False
    value = current_value.get("value")
    return (
        isinstance(value, dict)
        and _has_meaningful_leaf(value)
        and not current_value.get("unresolved")
        and not _find_missing_critical_paths(field_template, value)
        and all(_has_meaningful_path(value, path.split("."))
                for path in CRITICAL_PATHS.get(str(field_template.get("field_id", "")), []))
    )


async def detect_field_changes(
    *,
    new_evidence: str,
    confirmed_fields: list[dict[str, Any]],
    llm: LLMClient,
    trace_dir: Optional[str] = None,
    max_input_chars: int = 24000,
) -> set[str]:
    """Return affected IDs, conservatively refilling all on overflow or failure."""
    field_ids = {field["field_id"] for field in confirmed_fields}
    if not field_ids or not new_evidence.strip():
        return set()
    summary = json.dumps(
        [{"field_id": field["field_id"], "value": field["value"]} for field in confirmed_fields],
        ensure_ascii=False,
    )
    # Never classify unseen evidence as irrelevant. Large deltas use the full fill path.
    if len(summary) + len(new_evidence) > max_input_chars:
        logger.info("[IR] change detection budget exceeded; refill {} fields", len(field_ids))
        write_field_meta(trace_dir, "_change_detection", {
            "success": False, "attempts": 0, "error": "input_budget_exceeded",
            "output": {"reestimate": sorted(field_ids)},
        })
        return field_ids
    messages = [
        {"role": "system", "content": (
            "你是 DataTaskIR 字段变更检测器。判断完整新增证据是否补充、修正、撤销或冲突于已确认字段，"
            "包括字段之间的依赖影响；拿不准的字段必须重估。仅输出 JSON："
            '{"reestimate": ["field_id"], "reason": "简短依据"}。'
            "field_id 只能来自输入；无字段受影响时返回空数组。"
        )},
        {"role": "user", "content": f"已确认字段：\n{summary}\n\n新增证据：\n{new_evidence}"},
    ]
    started = time.perf_counter()
    raw = ""
    error = None
    try:
        parsed, raw = await ainvoke_json_object(llm, messages)
        detected = parsed.get("reestimate") if isinstance(parsed, dict) else None
        if not isinstance(detected, list) or any(
            not isinstance(item, str) or item not in field_ids for item in detected
        ):
            raise ValueError("Change detector returned invalid field IDs or shape")
        result = set(detected)
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("[IR] change detection failed ({}); refill all confirmed fields", error)
        result = field_ids
    write_field_attempt(trace_dir, "_change_detection", 1, messages, raw)
    write_field_meta(trace_dir, "_change_detection", {
        "success": error is None, "attempts": 1, "error": error,
        "latency_seconds": time.perf_counter() - started,
        "model_call_seconds": time.perf_counter() - started,
        "output": {"reestimate": sorted(result)},
    })
    return result


def _add_completion_diagnostics(
    field_template: dict[str, Any], output: dict[str, Any], missing_paths: list[str]
) -> dict[str, Any]:
    finalized = deepcopy(output)
    if not missing_paths:
        return finalized
    field_title = str(field_template.get("title", field_template.get("field_id", "当前字段")))
    message = f"{field_title}存在数据结构异常（非业务信息不足）：{', '.join(missing_paths)}"
    raw_unresolved = finalized.get("unresolved", [])
    unresolved = [str(item) for item in raw_unresolved] if isinstance(raw_unresolved, list) else []
    if message not in unresolved:
        unresolved.append(message)
    finalized["unresolved"] = unresolved
    question = finalized.get("question")
    if not isinstance(question, str) or not question.strip():
        finalized["question"] = f"请重新提取并修复{field_title}的数据结构；缺证据的业务值继续留空，无需用户补齐模板。"
    return finalized


def _model_visible_contract(field_template: dict[str, Any]) -> list[Any]:
    contract = field_template.get("template_contract", [])
    if not isinstance(contract, list):
        return []
    hidden_terms = ("optional", "not_applicable_value", "用户问题不涉及")
    return [item for item in contract if not any(term in str(item) for term in hidden_terms)]


def _model_visible_output_contract(field_template: dict[str, Any]) -> dict[str, Any]:
    contract = deepcopy(field_template.get("output_contract", {}))
    if not isinstance(contract, dict):
        return {}
    value_rule = contract.get("value")
    if isinstance(value_rule, str) and "optional" in value_rule:
        contract["value"] = value_rule.split("optional", maxsplit=1)[0].rstrip("，；。 ") + "。"
    return contract


def _model_visible_few_shots(field_template: dict[str, Any]) -> list[Any]:
    few_shots = field_template.get("few_shots", [])
    if not isinstance(few_shots, list):
        return []
    not_applicable_value = field_template.get("not_applicable_value")
    visible = []
    for few_shot in few_shots:
        output = few_shot.get("output", {}) if isinstance(few_shot, dict) else {}
        if not_applicable_value is not None and output.get("value") == not_applicable_value:
            continue
        visible.append(few_shot)
    return visible
