# Licensed under the Apache License, Version 2.0 (the "License");
"""Token usage and LLM throughput analysis for performance artifacts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.analyzer.base import AnalyzerSpec, BaseAnalyzer
from scripts.analyzer.performance import PerformanceDataset, natural_key, percentile

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _run_id_key(event: dict[str, Any]) -> str:
    return str(event.get("_run_id"))


def _model_key(event: dict[str, Any]) -> str:
    return str(event.get("name") or "unknown")


def _call_mode_key(event: dict[str, Any]) -> str:
    return str(event.get("extra", {}).get("call_mode") or "unknown")


def _run_row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return natural_key(row.get("run_id"))


def _token_row_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    label = row.get("model") or row.get("caller") or row.get("call_mode") or ""
    return -int(row.get("total_tokens", 0)), str(label)


class TokenAnalyzer(BaseAnalyzer):
    name = "token"
    description = "LLM token usage, throughput, context growth, and anomaly analysis"
    spec = AnalyzerSpec(
        name=name,
        title="Tokens",
        order=30,
        description=description,
        data_sources=(".performance/*.jsonl",),
        depends_on=("time",),
        empty_message="This session was recorded without performance token data.",
        template="token",
    )

    # ── regular public methods ────────

    @staticmethod
    def _caller_key(event: dict[str, Any]) -> str:
        """Extract ``caller_kind:caller_name`` key from an LLM event."""
        extra = event.get("extra", {})
        kind = extra.get("caller_kind") or "unknown"
        name = extra.get("caller_name") or "unknown"
        return f"{kind}:{name}"

    @classmethod
    def _token(cls, event: dict[str, Any], key: str) -> int:
        try:
            return int(event.get("extra", {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _aggregate_llms(cls, events: list[dict[str, Any]]) -> dict[str, Any]:
        elapsed_ms = sum(max(float(event.get("elapsed_ms") or 0.0), 0.0) for event in events)
        totals: dict[str, int] = {}
        for key in _TOKEN_KEYS:
            totals[key] = sum(cls._token(event, key) for event in events)
        tps_values: list[float] = []
        for event in events:
            tokens_per_sec = event.get("extra", {}).get("tokens_per_sec")
            if tokens_per_sec is not None:
                tps_values.append(float(tokens_per_sec))
        failures = sum(event.get("success") is False for event in events)
        tool_calls = sum(int(event.get("extra", {}).get("tool_call_count") or 0) for event in events)
        invalid_tool_calls = sum(int(event.get("extra", {}).get("invalid_tool_call_count") or 0) for event in events)
        return {
            "calls": len(events),
            **totals,
            "tokens_per_call": round(totals.get("total_tokens", 0) / len(events), 2) if events else 0.0,
            "input_output_ratio": (
                round(totals.get("input_tokens", 0) / totals.get("output_tokens", 1), 2)
                if totals.get("output_tokens", 0)
                else None
            ),
            "weighted_tps": round(totals.get("output_tokens", 0) / (elapsed_ms / 1000.0), 2) if elapsed_ms else 0.0,
            "median_tps": round(percentile(tps_values, 0.50), 2),
            "p95_tps": round(percentile(tps_values, 0.95), 2),
            "tool_calls": tool_calls,
            "invalid_tool_calls": invalid_tool_calls,
            "failures": failures,
            "error_rate": round(failures / len(events) * 100.0, 2) if events else 0.0,
        }

    @classmethod
    def _group_rows(
        cls,
        events: list[dict[str, Any]],
        key_fn: Callable[[dict[str, Any]], str],
        key_name: str,
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            group_key = key_fn(event)
            grouped_events = groups.get(group_key)
            if grouped_events is None:
                grouped_events = []
                groups[group_key] = grouped_events
            grouped_events.append(event)
        rows: list[dict[str, Any]] = []
        for key, items in groups.items():
            rows.append({key_name: key, **cls._aggregate_llms(items)})
        if key_name == "run_id":
            rows.sort(key=_run_row_sort_key)
        else:
            rows.sort(key=_token_row_sort_key)
        return rows

    @classmethod
    def _turn_rows(cls, llms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counters: dict[str, int] = defaultdict(int)
        cumulative = 0
        previous_input: dict[str, int] = defaultdict(int)
        rows = []
        for event in llms:
            run_id = str(event.get("_run_id"))
            turn = counters.get(run_id, 0) + 1
            counters[run_id] = turn
            extra = event.get("extra", {})
            input_tokens = cls._token(event, "input_tokens")
            output_tokens = cls._token(event, "output_tokens")
            total_tokens = cls._token(event, "total_tokens")
            cumulative += total_tokens
            rows.append(
                {
                    "run_id": run_id,
                    "turn": turn,
                    "model": event.get("name"),
                    "caller": extra.get("caller_name") or "",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "input_delta": input_tokens - previous_input.get(run_id, 0) if turn > 1 else input_tokens,
                    "cumulative_tokens": cumulative,
                    "elapsed_ms": float(event.get("elapsed_ms") or 0.0),
                    "tokens_per_sec": float(extra.get("tokens_per_sec") or 0.0),
                    "tool_call_count": int(extra.get("tool_call_count") or 0),
                    "invalid_tool_call_count": int(extra.get("invalid_tool_call_count") or 0),
                    "content_len": int(extra.get("content_len") or 0),
                    "reasoning_len": int(extra.get("reasoning_len") or 0),
                    "chunk_count": int(extra.get("chunk_count") or 0),
                    "call_mode": extra.get("call_mode") or "",
                }
            )
            previous_input[run_id] = input_tokens
        return rows

    @classmethod
    def _anomalies(cls, llms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        input_values: list[int] = []
        tps_values: list[float] = []
        for event in llms:
            input_values.append(cls._token(event, "input_tokens"))
            tokens_per_sec = float(event.get("extra", {}).get("tokens_per_sec") or 0.0)
            if tokens_per_sec > 0:
                tps_values.append(tokens_per_sec)
        high_input = percentile(input_values, 0.90)
        low_tps = percentile(tps_values, 0.10)
        rows = []
        counters: dict[str, int] = defaultdict(int)
        for event in llms:
            run_id = str(event.get("_run_id"))
            turn = counters.get(run_id, 0) + 1
            counters[run_id] = turn
            extra = event.get("extra", {})
            input_tokens = cls._token(event, "input_tokens")
            output_tokens = cls._token(event, "output_tokens")
            total_tokens = cls._token(event, "total_tokens")
            tps = float(extra.get("tokens_per_sec") or 0.0)
            findings = []
            if not any((input_tokens, output_tokens, total_tokens)):
                findings.append(("missing_usage", "Token usage is missing"))
            if total_tokens and total_tokens != input_tokens + output_tokens:
                findings.append(("token_mismatch", "total_tokens differs from input + output"))
            if input_tokens >= high_input and high_input > 0 and output_tokens < max(50, input_tokens * 0.01):
                findings.append(("high_input_low_output", "High input context with very small output"))
            if tps and low_tps and tps <= low_tps:
                msg = f"Generation throughput is in the slowest decile ({tps:.2f} tok/s)"
                findings.append(("slow_generation", msg))
            invalid = int(extra.get("invalid_tool_call_count") or 0)
            if invalid:
                findings.append(("invalid_tool_call", f"{invalid} invalid tool call(s)"))
            for code, message in findings:
                rows.append(
                    {
                        "run_id": run_id,
                        "turn": turn,
                        "model": event.get("name"),
                        "code": code,
                        "message": message,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "tokens_per_sec": tps,
                    }
                )
        return rows

    @classmethod
    def _reconcile(cls, dataset: PerformanceDataset) -> list[dict[str, Any]]:
        complete_files = {footer.get("_file") for footer in dataset.flushes}
        all_llms: list[dict[str, Any]] = []
        complete_llms: list[dict[str, Any]] = []
        for event in dataset.events:
            if event.get("kind") != "llm":
                continue
            all_llms.append(event)
            if event.get("_file") in complete_files:
                complete_llms.append(event)
        all_totals: dict[str, int] = {}
        complete_totals: dict[str, int] = {}
        for key in _TOKEN_KEYS:
            all_totals[key] = sum(cls._token(event, key) for event in all_llms)
            complete_totals[key] = sum(cls._token(event, key) for event in complete_llms)
        footer_totals = dict.fromkeys(_TOKEN_KEYS, 0)
        state_totals = dict.fromkeys(_TOKEN_KEYS, 0)
        for footer in dataset.flushes:
            llm_summary = footer.get("summary", {}).get("llms", {})
            if not isinstance(llm_summary, dict):
                continue
            for key in _TOKEN_KEYS:
                footer_totals[key] = int(footer_totals.get(key, 0)) + int(llm_summary.get(key) or 0)
                state_totals[key] = int(state_totals.get(key, 0)) + int(
                    llm_summary.get("state_messages", {}).get(key) or 0
                )
        rows = []
        for key in _TOKEN_KEYS:
            all_value = int(all_totals.get(key, 0))
            complete_value = int(complete_totals.get(key, 0))
            footer_value = int(footer_totals.get(key, 0))
            state_value = int(state_totals.get(key, 0))
            rows.append(
                {
                    "metric": key,
                    "all_events": all_value,
                    "complete_events": complete_value,
                    "footer": footer_value,
                    "state_messages": state_value,
                    "footer_match": complete_value == footer_value,
                    "state_match": complete_value == state_value,
                }
            )
        return rows

    def analyze(self, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Aggregate LLM token usage for one physical or scoped logical session."""
        dataset = kwargs.get("performance_dataset") or PerformanceDataset.load(session_root)
        if dataset.scope_error:
            return {"error": dataset.scope_error, "quality": dataset.quality()}
        if dataset.is_empty:
            return {"error": "This session was recorded without performance data (记录时未开启 performance 采集)."}

        llms: list[dict[str, Any]] = []
        for event in dataset.events:
            if event.get("kind") == "llm":
                llms.append(event)
        if not llms:
            return {
                "error": "Performance files were found, but they contain no LLM events",
                "quality": dataset.quality(),
            }

        overview = self._aggregate_llms(llms)
        overview.update(
            {
                "run_count": len({event.get("_run_id") for event in llms}),
                "model_count": len({event.get("name") for event in llms}),
                "missing_usage_calls": sum(not any(self._token(event, key) for key in _TOKEN_KEYS) for event in llms),
            }
        )

        runs = self._group_rows(llms, _run_id_key, "run_id")
        models = self._group_rows(llms, _model_key, "model")
        callers = self._group_rows(llms, self._caller_key, "caller")
        modes = self._group_rows(llms, _call_mode_key, "call_mode")
        turns = self._turn_rows(llms)
        anomalies = self._anomalies(llms)
        reconciliation = self._reconcile(dataset)
        source_files: list[str] = []
        for source_file in dataset.files:
            source_files.append(source_file.name)

        return {
            "overview": overview,
            "runs": runs,
            "models": models,
            "callers": callers,
            "call_modes": modes,
            "turns": turns,
            "anomalies": anomalies,
            "reconciliation": reconciliation,
            "quality": dataset.quality(),
            "source_files": source_files,
        }
