# Licensed under the Apache License, Version 2.0 (the "License");
"""Wall-clock and latency analysis for DataAgent performance artifacts."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from scripts.analyzer.base import AnalyzerSpec, BaseAnalyzer
from scripts.analyzer.performance import (
    PerformanceDataset,
    aggregate_events,
    format_duration,
    format_timestamp,
    interval_metrics,
)


def _component_sort_key(item: dict[str, Any]) -> tuple[float, str, str]:
    return -float(item.get("total_ms", 0)), str(item.get("kind", "")), str(item.get("name", ""))


def _breakdown_sort_key(item: dict[str, Any]) -> tuple[float, str]:
    return -float(item.get("total_ms", 0)), str(item.get("name", ""))


def _elapsed_sort_key(event: dict[str, Any]) -> float:
    return float(event.get("elapsed_ms") or 0.0)


def _tool_end_sort_key(event: dict[str, Any]) -> float:
    return float(event.get("_ended_ts", 0))


class TimeAnalyzer(BaseAnalyzer):
    name = "time"
    description = "Wall-clock, event latency, concurrency, and component timing analysis"
    spec = AnalyzerSpec(
        name=name,
        title="Time",
        order=20,
        description=description,
        data_sources=(".performance/*.jsonl",),
        depends_on=("trajectory",),
        empty_message="This session was recorded without performance timing data.",
        template="time",
    )

    # ── regular public methods ────────

    @staticmethod
    def _session_bounds(dataset: PerformanceDataset) -> tuple[Optional[float], Optional[float]]:
        from scripts.analyzer.performance import parse_timestamp

        starts: list[Optional[float]] = []
        ends: list[Optional[float]] = []
        for footer in dataset.flushes:
            metadata = footer.get("metadata", {})
            starts.append(parse_timestamp(metadata.get("started_at")))
            ends.append(parse_timestamp(metadata.get("ended_at")))
        starts.extend(event.get("_started_ts") for event in dataset.events)
        ends.extend(event.get("_ended_ts") for event in dataset.events)
        valid_starts: list[float] = []
        valid_ends: list[float] = []
        for value in starts:
            if value is not None:
                valid_starts.append(value)
        for value in ends:
            if value is not None:
                valid_ends.append(value)
        return (
            min(valid_starts) if valid_starts else None,
            max(valid_ends) if valid_ends else None,
        )

    @staticmethod
    def _event_row(event: dict[str, Any]) -> dict[str, Any]:
        extra = event.get("extra", {})
        tool_call_id = extra.get("tool_call_id") or ""
        return {
            "run_id": event.get("_run_id"),
            "pid": event.get("_pid"),
            "kind": event.get("kind"),
            "name": event.get("name"),
            "start_time": format_timestamp(event.get("_started_ts")),
            "elapsed_ms": float(event.get("elapsed_ms") or 0.0),
            "duration": format_duration(event.get("elapsed_ms")),
            "success": event.get("success") is not False,
            "error_type": event.get("error_type") or extra.get("error_type") or "",
            "caller": extra.get("caller_name") or "",
            "tool_call_id": tool_call_id,
            "target_anchor": (
                f"timeline-action-{event.get('_run_id')}-{tool_call_id}"
                if event.get("kind") == "tool" and tool_call_id
                else ""
            ),
        }

    @staticmethod
    def _timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        starts: list[float] = []
        for event in events:
            started = event.get("_started_ts")
            if started is not None:
                starts.append(float(started))
        if not starts:
            return []
        origin = min(starts)
        rows = []
        for event in events:
            kind = event.get("kind")
            elapsed_ms = float(event.get("elapsed_ms") or 0.0)
            visible = kind in {"llm", "tool"} or (
                kind == "hook" and event.get("name") == "pruner" and elapsed_ms >= 1000.0
            )
            if not visible or event.get("_started_ts") is None:
                continue
            rows.append(
                {
                    "run_id": event.get("_run_id"),
                    "kind": event.get("kind"),
                    "name": event.get("name"),
                    "start_ms": round((event.get("_started_ts", origin) - origin) * 1000.0, 4),
                    "elapsed_ms": max(elapsed_ms, 0.05),
                    "start_time": format_timestamp(event.get("_started_ts")),
                    "duration": format_duration(event.get("elapsed_ms")),
                    "success": event.get("success") is not False,
                }
            )
        return rows

    @staticmethod
    def _add_duration_labels(item: dict[str, Any]) -> None:
        for key in ("wall_ms", "active_ms", "work_ms", "idle_ms"):
            item[key.replace("_ms", "_duration")] = format_duration(item.get(key))

    @staticmethod
    def _add_aggregate_labels(item: dict[str, Any]) -> None:
        for key in ("total_ms", "avg_ms", "p50_ms", "p95_ms", "max_ms"):
            item[key.replace("_ms", "_duration")] = format_duration(item.get(key))

    @staticmethod
    def _is_tool_in_turn(event: dict[str, Any], llm_end: Optional[float], next_start: Optional[float]) -> bool:
        """Return True if a tool event falls within the current LLM turn's time window."""
        if event.get("kind") != "tool":
            return False
        started = event.get("_started_ts")
        if llm_end is None or started is None:
            return False
        if started < llm_end:
            return False
        return next_start is None or started < next_start

    def analyze(self, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Aggregate timing events for one physical or scoped logical session."""
        dataset = kwargs.get("performance_dataset") or PerformanceDataset.load(session_root)
        if dataset.scope_error:
            return {"error": dataset.scope_error, "quality": dataset.quality()}
        if dataset.is_empty:
            return {"error": "This session was recorded without performance data (记录时未开启 performance 采集)."}

        scoped_events: list[dict[str, Any]] = []
        for event in dataset.events:
            if event.get("kind") != "agent":
                scoped_events.append(event)
        overview = interval_metrics(scoped_events)
        session_start, session_end = self._session_bounds(dataset)
        if session_start is not None and session_end is not None:
            overview["wall_ms"] = round(max((session_end - session_start) * 1000.0, 0.0), 4)
            idle = round(max(overview.get("wall_ms", 0) - overview.get("active_ms", 0), 0.0), 4)
            overview["idle_ms"] = idle
        overview.update(
            {
                "start_time": format_timestamp(session_start),
                "end_time": format_timestamp(session_end),
                "event_count": len(dataset.events),
                "failed_count": sum(event.get("success") is False for event in dataset.events),
                "run_count": len(dataset.by_run()),
                "process_count": len({(event.get("_run_id"), event.get("_pid")) for event in dataset.events}),
            }
        )
        self._add_duration_labels(overview)

        components, breakdowns = self._build_component_breakdowns(dataset)
        runs: list[dict[str, Any]] = []
        timeline_by_run: dict[str, list[dict[str, Any]]] = {}
        for run_id, events in dataset.by_run().items():
            runs.append(self._summarize_run(run_id, events, dataset))
            timeline_by_run[run_id] = self._timeline(events)
        turns = self._derive_turns(dataset)
        slow_events = self._get_top_slow_events(dataset)
        failed_events: list[dict[str, Any]] = []
        for event in dataset.events:
            if event.get("success") is False:
                failed_events.append(self._event_row(event))
        turns_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for turn in turns:
            run_id = str(turn.get("run_id"))
            run_turns = turns_by_run.get(run_id)
            if run_turns is None:
                run_turns = []
                turns_by_run[run_id] = run_turns
            run_turns.append(turn)
        timeline_runs: list[dict[str, Any]] = []
        for run_id, events in timeline_by_run.items():
            timeline_runs.append({"run_id": run_id, "event_count": len(events), "height": max(360, len(events) * 20)})
        source_files: list[str] = []
        for source_file in dataset.files:
            source_files.append(source_file.name)

        return {
            "overview": overview,
            "breakdowns": breakdowns,
            "components": components,
            "runs": runs,
            "turns": turns,
            "turns_by_run": dict(turns_by_run),
            "slow_events": slow_events,
            "failed_events": failed_events,
            "timeline_by_run": timeline_by_run,
            "timeline_runs": timeline_runs,
            "quality": dataset.quality(),
            "source_files": source_files,
            "scope_note": (
                "Agent contains Nodes; Nodes contain Hooks, LLM calls, and Tools. "
                "The four charts therefore show composition within each kind and must not be added together."
            ),
        }

    def _build_component_breakdowns(
        self, dataset: PerformanceDataset
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """Build per-component aggregate stats and per-kind breakdown lists."""
        kind_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        component_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for event in dataset.events:
            kind = str(event.get("kind") or "unknown")
            name = str(event.get("name") or "unknown")
            kind_events = kind_groups.get(kind)
            if kind_events is None:
                kind_events = []
                kind_groups[kind] = kind_events
            kind_events.append(event)
            component_key = (kind, name)
            component_events = component_groups.get(component_key)
            if component_events is None:
                component_events = []
                component_groups[component_key] = component_events
            component_events.append(event)

        kind_totals: dict[str, float] = {}
        for kind, events in kind_groups.items():
            kind_totals[kind] = sum(float(event.get("elapsed_ms") or 0.0) for event in events)
        components = []
        for (kind, name), events in component_groups.items():
            item = {"kind": kind, "name": name, **aggregate_events(events)}
            item["kind_share"] = (
                round(item.get("total_ms", 0) / kind_totals.get(kind, 0) * 100.0, 2) if kind_totals.get(kind) else 0.0
            )
            self._add_aggregate_labels(item)
            components.append(item)
        components.sort(key=_component_sort_key)

        breakdowns: dict[str, list[dict[str, Any]]] = {}
        for kind in ("hook", "llm", "node", "tool"):
            breakdowns[kind] = []
        for (kind, name), events in component_groups.items():
            if kind not in breakdowns:
                continue
            total_ms = sum(float(event.get("elapsed_ms") or 0.0) for event in events)
            breakdowns.get(kind, []).append({"name": name, "total_ms": round(total_ms, 4), "count": len(events)})
        for values in breakdowns.values():
            values.sort(key=_breakdown_sort_key)
        return components, breakdowns

    def _get_top_slow_events(self, dataset: PerformanceDataset, limit: int = 25) -> list[dict[str, Any]]:
        """Return the top N slowest hook/llm/tool events ordered by elapsed time."""
        relevant: list[dict[str, Any]] = []
        for event in dataset.events:
            if event.get("kind") in {"hook", "llm", "tool"}:
                relevant.append(event)
        relevant.sort(key=_elapsed_sort_key, reverse=True)
        rows: list[dict[str, Any]] = []
        for event in relevant[:limit]:
            rows.append(self._event_row(event))
        return rows

    def _summarize_run(self, run_id: str, events: list[dict[str, Any]], dataset: PerformanceDataset) -> dict[str, Any]:
        scoped: list[dict[str, Any]] = []
        starts: list[float] = []
        ends: list[float] = []
        for event in events:
            if event.get("kind") != "agent":
                scoped.append(event)
            started = event.get("_started_ts")
            ended = event.get("_ended_ts")
            if started is not None:
                starts.append(float(started))
            if ended is not None:
                ends.append(float(ended))
        metrics = interval_metrics(scoped)
        footer = dataset.footer_for(run_id)
        metadata = footer.get("metadata", {}) if footer else {}
        if metadata.get("e2e_ms") is not None:
            metrics["wall_ms"] = round(float(metadata.get("e2e_ms", 0)), 4)
            idle = round(max(metrics.get("wall_ms", 0) - metrics.get("active_ms", 0), 0.0), 4)
            metrics["idle_ms"] = idle
        result = {
            "run_id": run_id,
            **metrics,
            "start_time": format_timestamp(min(starts) if starts else None),
            "end_time": format_timestamp(max(ends) if ends else None),
            "event_count": len(events),
            "failed_count": sum(e.get("success") is False for e in events),
            "turn_count": sum(e.get("kind") == "llm" for e in events),
            "process_count": len({e.get("_pid") for e in events}),
            "pids": ", ".join(str(pid) for pid in sorted({e.get("_pid") for e in events}, key=str)),
            "complete": footer is not None,
        }
        self._add_duration_labels(result)
        return result

    def _derive_turns(self, dataset: PerformanceDataset) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        for run_id, events in dataset.by_run().items():
            llms: list[dict[str, Any]] = []
            for event in events:
                if event.get("kind") == "llm":
                    llms.append(event)
            run_end = max(
                (e.get("_ended_ts") for e in events if e.get("_ended_ts") is not None),
                default=None,
            )
            for index, llm in enumerate(llms, 1):
                start = llm.get("_started_ts")
                llm_end = llm.get("_ended_ts")
                next_start = llms[index].get("_started_ts") if index < len(llms) else run_end
                tools: list[dict[str, Any]] = []
                for event in events:
                    if self._is_tool_in_turn(event, llm_end, next_start):
                        tools.append(event)
                tool_metrics = interval_metrics(tools)
                timed_tools: list[dict[str, Any]] = []
                for tool in tools:
                    if tool.get("_started_ts") is not None and tool.get("_ended_ts") is not None:
                        timed_tools.append(tool)
                blocking_tool = max(timed_tools, key=_tool_end_sort_key, default=None)
                tool_batch_ms = (
                    (blocking_tool.get("_ended_ts", 0) - llm_end) * 1000.0
                    if blocking_tool is not None and llm_end is not None
                    else 0.0
                )
                turn_wall_ms = (
                    max((next_start - start) * 1000.0, 0.0) if start is not None and next_start is not None else 0.0
                )
                extra = llm.get("extra", {})
                critical_path_ms = round(float(llm.get("elapsed_ms") or 0.0) + max(tool_batch_ms, 0.0), 3)
                blocking_call_id = blocking_tool.get("extra", {}).get("tool_call_id") if blocking_tool else ""
                turns.append(
                    {
                        "run_id": run_id,
                        "turn": index,
                        "start_time": format_timestamp(start),
                        "llm": llm.get("name"),
                        "caller": extra.get("caller_name") or "",
                        "llm_ms": float(llm.get("elapsed_ms") or 0.0),
                        "llm_duration": format_duration(llm.get("elapsed_ms")),
                        "tool_count": len(tools),
                        "tool_wall_ms": tool_metrics.get("active_ms", 0),
                        "tool_wall_duration": format_duration(tool_metrics.get("active_ms")),
                        "tool_work_ms": tool_metrics.get("work_ms", 0),
                        "turn_wall_ms": round(turn_wall_ms, 4),
                        "turn_duration": format_duration(turn_wall_ms),
                        "critical_path_ms": round(critical_path_ms, 4),
                        "critical_path_duration": format_duration(critical_path_ms),
                        "orchestration_overhead_ms": round(max(turn_wall_ms - critical_path_ms, 0.0), 4),
                        "orchestration_overhead_duration": format_duration(max(turn_wall_ms - critical_path_ms, 0.0)),
                        "blocking_tool": blocking_tool.get("name") if blocking_tool else "",
                        "blocking_tool_call_id": blocking_call_id,
                        "target_anchor": (f"timeline-action-{run_id}-{blocking_call_id}" if blocking_call_id else ""),
                    }
                )
        return turns
