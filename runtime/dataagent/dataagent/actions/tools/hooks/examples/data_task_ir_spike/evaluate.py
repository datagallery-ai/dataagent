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
"""CLI and deterministic scoring for the real-LLM DataTaskIR feasibility benchmark."""

# ruff: noqa: UP045 -- repository convention requires Optional for nullable annotations.

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from dataagent.actions.tools.hooks.examples.data_task_ir_spike.cases import generate_benchmark_cases
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.fill import (
    create_llm_client, fill_field_template_detailed
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import render_context
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import (
    list_field_templates, load_template_catalog
)


async def run_benchmark(
    cases: list[dict[str, Any]],
    field_templates: list[dict[str, Any]],
    *,
    api_base: str,
    api_key: str,
    model: str,
    max_concurrency: int,
    case_concurrency: int,
    output_path: Path,
    completed_case_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Run independent field calls with bounded field and case concurrency, checkpointing each case."""
    llm = create_llm_client(api_base=api_base, api_key=api_key, model=model)
    field_gate = asyncio.Semaphore(max_concurrency)
    case_gate = asyncio.Semaphore(case_concurrency)
    completed = completed_case_ids or set()
    pending_cases = [case for case in cases if case.get("case_id") not in completed]
    tasks = [
        _run_case(case, field_templates, llm=llm, field_gate=field_gate, case_gate=case_gate) for case in pending_cases
    ]
    new_results = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for future in asyncio.as_completed(tasks):
        result = await future
        new_results.append(result)
        with output_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        _ = sum(1 for field in result.get("field_results", []) if field.get("success"))
    return new_results


def load_results(path: Path) -> list[dict[str, Any]]:
    """Load checkpointed JSONL benchmark results."""
    if not path.exists():
        return []
    results = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError(f"Invalid result object on line {line_number}")
        results.append(parsed)
    return results


def summarize_results(results: list[dict[str, Any]], model: str, api_base: str) -> dict[str, Any]:
    """Calculate protocol, semantic, hallucination, issue-detection, and latency metrics."""
    field_stats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    mode_stats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    call_latencies = []
    model_call_latencies = []
    queue_wait_latencies = []
    case_latencies = []
    total_calls = 0
    protocol_successes = 0
    repaired_successes = 0
    completion_retries = 0
    remaining_incomplete = 0
    exact_value_matches = 0
    rendered_cases = 0
    rendered_fields = 0
    for case in results:
        mode = str(case.get("mode", "unknown"))
        case_latencies.append(float(case.get("latency_seconds", 0.0)))
        case_rendered_fields = int(case.get("rendered_field_count", 0))
        rendered_fields += case_rendered_fields
        if case_rendered_fields > 0:
            rendered_cases += 1
        gold_values = case.get("gold_values", {})
        expected_unresolved = set(case.get("expect_unresolved", []))
        expected_not_applicable = set(case.get("expect_not_applicable", []))
        for field in case.get("field_results", []):
            total_calls += 1
            field_id = str(field.get("field_id", ""))
            stats = field_stats.setdefault(field_id, defaultdict(float))
            mode_metric = mode_stats.setdefault(mode, defaultdict(float))
            stats["calls"] = stats.get("calls", 0) + 1
            mode_metric["calls"] = mode_metric.get("calls", 0) + 1
            expected_unresolved_present = field_id in expected_unresolved
            issue_target = expected_unresolved_present
            target_metric = "issue_target_calls" if issue_target else "unaffected_calls"
            mode_metric[target_metric] = mode_metric.get(target_metric, 0) + 1
            latency = float(field.get("latency_seconds", 0.0))
            call_latencies.append(latency)
            model_call_latencies.append(float(field.get("model_call_seconds", latency)))
            queue_wait_latencies.append(float(field.get("queue_wait_seconds", 0.0)))
            stats["latency_sum"] = stats.get("latency_sum", 0) + latency
            if not field.get("success"):
                stats["protocol_failures"] = stats.get("protocol_failures", 0) + 1
                continue
            protocol_successes += 1
            stats["protocol_successes"] = stats.get("protocol_successes", 0) + 1
            mode_metric["protocol_successes"] = mode_metric.get("protocol_successes", 0) + 1
            completion_retried = bool(field.get("completion_retried", False))
            if completion_retried:
                completion_retries += 1
                stats["completion_retries"] = stats.get("completion_retries", 0) + 1
            elif int(field.get("attempts", 1)) > 1:
                repaired_successes += 1
                stats["repaired_successes"] = stats.get("repaired_successes", 0) + 1
            missing_paths = field.get("missing_critical_paths", [])
            if isinstance(missing_paths, list) and missing_paths:
                remaining_incomplete += 1
                stats["remaining_incomplete"] = stats.get("remaining_incomplete", 0) + 1
            output = field.get("output", {})
            expected_value = gold_values.get(field_id)
            actual_value = output.get("value")
            if actual_value == expected_value:
                exact_value_matches += 1
                stats["exact_value_matches"] = stats.get("exact_value_matches", 0) + 1
                mode_metric["exact_value_matches"] = mode_metric.get("exact_value_matches", 0) + 1
            exact_metric = "issue_target_exact" if issue_target else "unaffected_exact"
            mode_metric[exact_metric] = mode_metric.get(exact_metric, 0) + int(actual_value == expected_value)
            if field_id in expected_not_applicable:
                mode_metric["optional_target_calls"] = mode_metric.get("optional_target_calls", 0) + 1
                mode_metric["optional_target_exact"] = mode_metric.get("optional_target_exact", 0) + int(
                    actual_value == expected_value
                )
            leaf_metrics = _compare_leaves(expected_value, actual_value)
            for name, count in leaf_metrics.items():
                stats[name] = stats.get(name, 0) + count
            unresolved_present = bool(output.get("unresolved", []))
            question = output.get("question")
            question_present = isinstance(question, str) and bool(question.strip())
            stats["unresolved_correct"] = stats.get("unresolved_correct", 0) + int(
                unresolved_present == expected_unresolved_present
            )
            stats["question_correct"] = stats.get("question_correct", 0) + int(
                question_present == expected_unresolved_present
            )
            mode_metric["unresolved_correct"] = mode_metric.get("unresolved_correct", 0) + int(
                unresolved_present == expected_unresolved_present
            )
            mode_metric["question_correct"] = mode_metric.get("question_correct", 0) + int(
                question_present == expected_unresolved_present
            )
            if expected_unresolved_present:
                mode_metric["unresolved_expected"] = mode_metric.get("unresolved_expected", 0) + 1
                mode_metric["unresolved_detected"] = mode_metric.get("unresolved_detected", 0) + int(unresolved_present)
                mode_metric["question_detected"] = mode_metric.get("question_detected", 0) + int(question_present)
    summary = {
        "model": model,
        "api_base": api_base,
        "case_count": len(results),
        "total_calls": total_calls,
        "protocol_success_rate": _ratio(protocol_successes, total_calls),
        "protocol_error_rate": _ratio(total_calls - protocol_successes, total_calls),
        "repair_success_rate_per_call": _ratio(repaired_successes, total_calls),
        "completion_retry_rate_per_call": _ratio(completion_retries, total_calls),
        "remaining_incomplete_rate_per_call": _ratio(remaining_incomplete, total_calls),
        "exact_value_match_rate": _ratio(exact_value_matches, total_calls),
        "rendered_case_rate": _ratio(rendered_cases, len(results)),
        "rendered_field_rate": _ratio(rendered_fields, total_calls),
        "call_latency_seconds": _latency_summary(call_latencies),
        "model_call_seconds": _latency_summary(model_call_latencies),
        "queue_wait_seconds": _latency_summary(queue_wait_latencies),
        "case_latency_seconds": _latency_summary(case_latencies),
        "field_metrics": _summarize_fields(field_stats),
        "mode_metrics": _summarize_modes(mode_stats),
    }
    return summary


def write_report(summary: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    """Write machine-readable JSON and a concise Markdown benchmark report."""
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path = output_dir / "report.md"
    latency = summary.get("call_latency_seconds", {})
    model_latency = summary.get("model_call_seconds", {})
    queue_latency = summary.get("queue_wait_seconds", {})
    lines = [
        "# DataTaskIR 独立模型调用可行性实验",
        "",
        f"- 模型：{summary.get('model')}",
        f"- Base URL：{summary.get('api_base')}",
        f"- 样本组数：{summary.get('case_count')}",
        f"- 字段调用数：{summary.get('total_calls')}",
        f"- 配置并发：字段={summary.get('configured_field_concurrency')}，"
        f"样本组={summary.get('configured_case_concurrency')}",
        f"- 本轮墙钟时间：{summary.get('benchmark_wall_time_seconds', 0):.3f}s；"
        f"吞吐={summary.get('completed_calls_per_second', 0):.3f} 字段调用/秒",
        f"- 协议成功率：{_percent(summary.get('protocol_success_rate'))}",
        f"- 协议错误率：{_percent(summary.get('protocol_error_rate'))}",
        f"- 二次修复后成功占比：{_percent(summary.get('repair_success_rate_per_call'))}",
        f"- 关键字段定向重填占比：{_percent(summary.get('completion_retry_rate_per_call'))}",
        f"- 重填后仍缺关键字段占比：{_percent(summary.get('remaining_incomplete_rate_per_call'))}",
        f"- value 完全匹配率：{_percent(summary.get('exact_value_match_rate'))}",
        f"- 可完成确定性渲染的样本率：{_percent(summary.get('rendered_case_rate'))}",
        f"- 进入 Agent 上下文的字段占比：{_percent(summary.get('rendered_field_rate'))}",
        f"- 单字段延迟：p50={latency.get('p50', 0):.3f}s，p95={latency.get('p95', 0):.3f}s，"
        f"max={latency.get('max', 0):.3f}s",
        f"- 真实模型调用：p50={model_latency.get('p50', 0):.3f}s，p95={model_latency.get('p95', 0):.3f}s",
        f"- 并发排队等待：p50={queue_latency.get('p50', 0):.3f}s，p95={queue_latency.get('p95', 0):.3f}s",
        "",
        "## 分字段结果",
        "",
        "| 字段 | 协议成功 | 完全匹配 | 已知叶子正确 | 臆造率 | 漏填率 | 定向重填 | 仍缺关键字段 | "
        "未决识别 | 补问识别 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for field_id, metrics in summary.get("field_metrics", {}).items():
        lines.append(
            f"| {field_id} | {_percent(metrics.get('protocol_success_rate'))} | "
            f"{_percent(metrics.get('exact_value_match_rate'))} | "
            f"{_percent(metrics.get('known_leaf_accuracy'))} | "
            f"{_percent(metrics.get('hallucination_rate'))} | "
            f"{_percent(metrics.get('omission_rate'))} | "
            f"{_percent(metrics.get('completion_retry_rate'))} | "
            f"{_percent(metrics.get('remaining_incomplete_rate'))} | "
            f"{_percent(metrics.get('unresolved_detection_accuracy'))} | "
            f"{_percent(metrics.get('question_detection_accuracy'))} |"
        )
    lines.extend(
        [
            "",
            "## 分样本类型结果",
            "",
            "| 类型 | 调用数 | 协议成功 | 全字段完全匹配 | 受影响字段正确 | 可选空操作正确 | "
            "未受影响字段稳定 | 未决召回 | 补问召回 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for mode, metrics in summary.get("mode_metrics", {}).items():
        lines.append(
            f"| {mode} | {metrics.get('calls')} | {_percent(metrics.get('protocol_success_rate'))} | "
            f"{_percent(metrics.get('exact_value_match_rate'))} | "
            f"{_percent(metrics.get('issue_target_value_accuracy'))} | "
            f"{_percent(metrics.get('optional_target_value_accuracy'))} | "
            f"{_percent(metrics.get('unaffected_value_accuracy'))} | "
            f"{_percent(metrics.get('unresolved_recall'))} | "
            f"{_percent(metrics.get('question_recall'))} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, markdown_path


async def _run_case(
    case: dict[str, Any],
    field_templates: list[dict[str, Any]],
    *,
    llm: Any,
    field_gate: asyncio.Semaphore,
    case_gate: asyncio.Semaphore,
) -> dict[str, Any]:
    async with case_gate:
        started = time.perf_counter()
        tasks = []
        evidence_by_field = case.get("evidence_by_field", {})
        if not isinstance(evidence_by_field, dict):
            evidence_by_field = {}
        for field_template in field_templates:
            field_id = str(field_template.get("field_id", ""))
            tasks.append(
                fill_field_template_detailed(
                    field_template,
                    user_query=str(case.get("user_query", "")),
                    tool_evidence=str(evidence_by_field.get(field_id, case.get("tool_evidence", ""))),
                    confirmed_context=str(case.get("confirmed_context", "")),
                    llm=llm,
                    semaphore=field_gate,
                )
            )
        field_results = await asyncio.gather(*tasks)
        outputs = [field.get("output") for field in field_results if isinstance(field.get("output"), dict)]
        rendered = render_context(outputs) if len(outputs) == len(field_templates) else None
        rendered_field_count = sum(1 for line in (rendered or "").splitlines() if line.startswith("- "))
        return {
            "case_id": case.get("case_id"),
            "mode": case.get("mode"),
            "latency_seconds": time.perf_counter() - started,
            "user_query": case.get("user_query"),
            "tool_evidence": case.get("tool_evidence"),
            "evidence_by_field": case.get("evidence_by_field"),
            "gold_values": case.get("gold_values"),
            "expect_unresolved": case.get("expect_unresolved", []),
            "expect_not_applicable": case.get("expect_not_applicable", []),
            "field_results": field_results,
            "rendered_context": rendered,
            "rendered_field_count": rendered_field_count,
        }


def _compare_leaves(expected: Any, actual: Any) -> dict[str, int]:
    expected_leaves = _flatten(expected)
    actual_leaves = _flatten(actual)
    paths = set(expected_leaves) | set(actual_leaves)
    metrics = {"known_total": 0, "known_correct": 0, "unknown_total": 0, "hallucinations": 0, "omissions": 0}
    missing = object()
    for path in paths:
        expected_value = expected_leaves.get(path, missing)
        actual_value = actual_leaves.get(path, missing)
        if expected_value is None or expected_value is missing:
            metrics["unknown_total"] = metrics.get("unknown_total", 0) + 1
            if actual_value is not None and actual_value is not missing:
                metrics["hallucinations"] = metrics.get("hallucinations", 0) + 1
            continue
        metrics["known_total"] = metrics.get("known_total", 0) + 1
        if actual_value == expected_value:
            metrics["known_correct"] = metrics.get("known_correct", 0) + 1
        if actual_value is None or actual_value is missing:
            metrics["omissions"] = metrics.get("omissions", 0) + 1
    return metrics


def _flatten(value: Any, path: str = "$") -> dict[str, Any]:
    if isinstance(value, dict):
        if not value:
            return {path: {}}
        flattened = {}
        for key, child in value.items():
            flattened.update(_flatten(child, f"{path}.{key}"))
        return flattened
    if isinstance(value, list):
        if not value:
            return {path: []}
        flattened = {}
        for index, child in enumerate(value):
            flattened.update(_flatten(child, f"{path}[{index}]"))
        return flattened
    return {path: value}


def _summarize_fields(field_stats: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    summarized = {}
    for field_id, stats in sorted(field_stats.items()):
        calls = stats.get("calls", 0)
        successful = stats.get("protocol_successes", 0)
        summarized[field_id] = {
            "calls": int(calls),
            "protocol_success_rate": _ratio(successful, calls),
            "exact_value_match_rate": _ratio(stats.get("exact_value_matches", 0), calls),
            "known_leaf_accuracy": _ratio(stats.get("known_correct", 0), stats.get("known_total", 0)),
            "hallucination_rate": _ratio(stats.get("hallucinations", 0), stats.get("unknown_total", 0)),
            "omission_rate": _ratio(stats.get("omissions", 0), stats.get("known_total", 0)),
            "unresolved_detection_accuracy": _ratio(stats.get("unresolved_correct", 0), successful),
            "question_detection_accuracy": _ratio(stats.get("question_correct", 0), successful),
            "completion_retry_rate": _ratio(stats.get("completion_retries", 0), successful),
            "remaining_incomplete_rate": _ratio(stats.get("remaining_incomplete", 0), successful),
            "mean_latency_seconds": _ratio(stats.get("latency_sum", 0), calls),
        }
    return summarized


def _summarize_modes(mode_stats: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    summarized = {}
    for mode, stats in sorted(mode_stats.items()):
        calls = stats.get("calls", 0)
        successful = stats.get("protocol_successes", 0)
        summarized[mode] = {
            "calls": int(calls),
            "protocol_success_rate": _ratio(successful, calls),
            "exact_value_match_rate": _ratio(stats.get("exact_value_matches", 0), calls),
            "unresolved_detection_accuracy": _ratio(stats.get("unresolved_correct", 0), successful),
            "unresolved_recall": _ratio(stats.get("unresolved_detected", 0), stats.get("unresolved_expected", 0)),
            "question_detection_accuracy": _ratio(stats.get("question_correct", 0), successful),
            "question_recall": _ratio(stats.get("question_detected", 0), stats.get("unresolved_expected", 0)),
            "issue_target_value_accuracy": _ratio(
                stats.get("issue_target_exact", 0), stats.get("issue_target_calls", 0)
            ),
            "unaffected_value_accuracy": _ratio(stats.get("unaffected_exact", 0), stats.get("unaffected_calls", 0)),
            "optional_target_value_accuracy": _ratio(
                stats.get("optional_target_exact", 0), stats.get("optional_target_calls", 0)
            ),
        }
    return summarized


def _latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {
        "mean": statistics.fmean(ordered),
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max": max(ordered),
    }


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _percent(value: Any) -> str:
    return f"{float(value or 0) * 100:.2f}%"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the independent DataTaskIR real-LLM benchmark")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--start-index", type=int, default=1, help="1-based first benchmark case")
    parser.add_argument("--max-concurrency", type=int, default=20)
    parser.add_argument("--case-concurrency", type=int, default=10)
    parser.add_argument("--api-base", default=os.getenv("DATA_TASK_IR_API_BASE", "http://113.46.219.251:8080"))
    parser.add_argument("--api-key", default=os.getenv("DATA_TASK_IR_API_KEY"))
    parser.add_argument("--model", default=os.getenv("DATA_TASK_IR_MODEL", "GLM-5.2"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Run the benchmark CLI without ever persisting or printing the API key."""
    args = _parse_args()
    if not args.api_key:
        raise ValueError("DATA_TASK_IR_API_KEY or --api-key is required")
    output_dir = args.output_dir.resolve()
    result_path = output_dir / "results.jsonl"
    existing_results = load_results(result_path)
    if existing_results and not args.resume:
        raise FileExistsError(f"{result_path} already exists; pass --resume or choose another output directory")
    completed_ids = {str(result.get("case_id")) for result in existing_results}
    catalog = load_template_catalog()
    field_templates = list_field_templates(catalog)
    if args.start_index < 1 or args.start_index + args.count - 1 > 100:
        raise ValueError("--start-index and --count must select cases within 1..100")
    all_cases = generate_benchmark_cases(100)
    cases = all_cases[args.start_index - 1 : args.start_index - 1 + args.count]
    benchmark_started = time.perf_counter()
    new_results = asyncio.run(
        run_benchmark(
            cases,
            field_templates,
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
            max_concurrency=args.max_concurrency,
            case_concurrency=args.case_concurrency,
            output_path=result_path,
            completed_case_ids=completed_ids,
        )
    )
    benchmark_wall_time = time.perf_counter() - benchmark_started
    all_results = existing_results + new_results
    summary = summarize_results(all_results, args.model, args.api_base)
    summary["configured_field_concurrency"] = args.max_concurrency
    summary["configured_case_concurrency"] = args.case_concurrency
    summary["benchmark_wall_time_seconds"] = benchmark_wall_time
    completed_calls = len(new_results) * len(field_templates)
    summary["completed_calls_per_second"] = _ratio(completed_calls, benchmark_wall_time)
    _, _ = write_report(summary, output_dir)


if __name__ == "__main__":
    main()
