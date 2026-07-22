# Licensed under the Apache License, Version 2.0 (the "License");
"""Shared reader and aggregation helpers for performance JSONL artifacts."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from scripts.analyzer.scope import AnalysisScope

_PERF_FILE_RE = re.compile(r"^Run(?P<run_id>.+)_Sub(?P<sub_id>[^.]+)\.(?P<pid>\d+)\.jsonl$")
_LEGACY_PERF_FILE_RE = re.compile(r"^(?P<run_id>.+)\.(?P<pid>\d+)\.jsonl$")
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _point_sort_key(item: tuple[float, int]) -> tuple[float, int]:
    return item[0], item[1]


def _performance_path_sort_key(path: Path) -> tuple[Any, ...]:
    return natural_key(path.name)


def _run_group_sort_key(item: tuple[str, list[dict[str, Any]]]) -> tuple[Any, ...]:
    return natural_key(item[0])


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse the collector's UTC timestamp into epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def natural_key(value: Any) -> tuple[Any, ...]:
    """Sort mixed textual identifiers in human order (0, 1, 2, 10)."""
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value)))


def parse_performance_filename(value: str | Path) -> tuple[str, Optional[str], Optional[int]]:
    """Return run id, optional sub id, and optional PID from new or legacy filenames."""
    name = Path(value).name
    match = _PERF_FILE_RE.match(name)
    if match is not None:
        return match.group("run_id"), match.group("sub_id"), int(match.group("pid"))
    legacy_match = _LEGACY_PERF_FILE_RE.match(name)
    if legacy_match is not None:
        return legacy_match.group("run_id"), None, int(legacy_match.group("pid"))
    return Path(name).stem, None, None


def percentile(values: Iterable[float], quantile: float) -> float:
    """Return a linearly interpolated percentile."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def interval_metrics(events: Iterable[dict[str, Any]]) -> dict[str, float | int]:
    """Compute wall, active, work, and concurrency metrics for event intervals."""
    intervals: list[tuple[float, float]] = []
    work_ms = 0.0
    points: list[tuple[float, int]] = []
    for event in events:
        start = event.get("_started_ts")
        end = event.get("_ended_ts")
        elapsed = max(float(event.get("elapsed_ms") or 0.0), 0.0)
        work_ms += elapsed
        if start is None or end is None or end < start:
            continue
        intervals.append((start, end))
        # End points sort before starts at the same instant.
        points.extend(((start, 1), (end, -1)))

    if not intervals:
        return {
            "wall_ms": 0.0,
            "active_ms": 0.0,
            "work_ms": round(work_ms, 4),
            "idle_ms": 0.0,
            "parallelism": 0.0,
            "peak_concurrency": 0,
        }

    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    active_ms = sum((end - start) * 1000.0 for start, end in merged)
    wall_ms = (max(end for _, end in intervals) - min(start for start, _ in intervals)) * 1000.0

    concurrency = peak = 0
    for _, delta in sorted(points, key=_point_sort_key):
        concurrency += delta
        peak = max(peak, concurrency)

    return {
        "wall_ms": round(max(wall_ms, 0.0), 4),
        "active_ms": round(active_ms, 4),
        "work_ms": round(work_ms, 4),
        "idle_ms": round(max(wall_ms - active_ms, 0.0), 4),
        "parallelism": round(work_ms / active_ms, 3) if active_ms else 0.0,
        "peak_concurrency": peak,
    }


def aggregate_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a homogeneous event collection."""
    items = list(events)
    elapsed: list[float] = []
    for item in items:
        elapsed.append(max(float(item.get("elapsed_ms") or 0.0), 0.0))
    failures = sum(item.get("success") is False for item in items)
    return {
        "count": len(items),
        "total_ms": round(sum(elapsed), 4),
        "avg_ms": round(sum(elapsed) / len(elapsed), 4) if elapsed else 0.0,
        "p50_ms": round(percentile(elapsed, 0.50), 4),
        "p90_ms": round(percentile(elapsed, 0.90), 4),
        "p95_ms": round(percentile(elapsed, 0.95), 4),
        "p99_ms": round(percentile(elapsed, 0.99), 4),
        "min_ms": round(min(elapsed), 4) if elapsed else 0.0,
        "max_ms": round(max(elapsed), 4) if elapsed else 0.0,
        "failures": failures,
        "error_rate": round(failures / len(items) * 100.0, 2) if items else 0.0,
    }


def format_duration(milliseconds: Optional[float | int]) -> str:
    """Format milliseconds without hiding the underlying unit."""
    value = max(float(milliseconds or 0.0), 0.0)
    if value < 1:
        return f"{value:.3f} ms"
    if value < 1000:
        return f"{value:.1f} ms"
    if value < 60_000:
        return f"{value / 1000:.2f} s"
    return f"{value / 60_000:.2f} min"


def format_timestamp(timestamp: Optional[float]) -> str:
    """Format an epoch timestamp as a UTC time-of-day string."""
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%H:%M:%S.%f")[:-3]


def _event_sort_key(event: dict[str, Any]) -> tuple[bool, float, tuple[Any, ...]]:
    """Sort key: None timestamps last, then by timestamp, then by run id."""
    return (
        event.get("_started_ts") is None,
        event.get("_started_ts") or 0.0,
        natural_key(event.get("_run_id")),
    )


def _record_in_time_window(
    record: dict[str, Any],
    time_window: Optional[tuple[float, float]],
    *,
    is_footer: bool = False,
) -> bool:
    if time_window is None:
        return True
    source = record.get("metadata", {}) if is_footer else record
    source = source if isinstance(source, dict) else {}
    started = parse_timestamp(source.get("started_at"))
    ended = parse_timestamp(source.get("ended_at"))
    if started is None and ended is None:
        return False
    lower, upper = time_window
    interval_start = started if started is not None else ended
    interval_end = ended if ended is not None else started
    return bool(
        interval_start is not None and interval_end is not None and interval_end >= lower and interval_start <= upper
    )


class PerformanceDataset:
    """Normalized view of all ``.performance/*.jsonl`` files in one session."""

    def __init__(
        self,
        *,
        files: list[Path],
        events: list[dict[str, Any]],
        flushes: list[dict[str, Any]],
        malformed_lines: list[dict[str, Any]],
        scope_error: str = "",
        scope_match_mode: str = "",
    ) -> None:
        """Create a normalized dataset from parsed performance records."""
        self.files = files
        self.events = events
        self.flushes = flushes
        self.malformed_lines = malformed_lines
        self.scope_error = scope_error
        self.scope_match_mode = scope_match_mode

    @property
    def is_empty(self) -> bool:
        """Return whether the session has no performance files."""
        return not self.files

    @classmethod
    def load(cls, session_root: Path, analysis_scope: Any = None) -> PerformanceDataset:
        """Load performance JSONL files selected for one logical session."""
        scope = AnalysisScope.from_value(analysis_scope)
        performance_dir = Path(session_root) / ".performance"
        files = scope.resolve_performance_files(session_root) if scope else list(performance_dir.glob("*.jsonl"))
        files = sorted(files, key=_performance_path_sort_key)
        events: list[dict[str, Any]] = []
        flushes: list[dict[str, Any]] = []
        malformed: list[dict[str, Any]] = []
        time_window = scope.performance_time_window if scope else None

        for path in files:
            file_run_id, file_sub_id, file_pid = parse_performance_filename(path.name)
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except (TypeError, json.JSONDecodeError) as exc:
                        malformed.append({"file": path.name, "line": line_no, "error": str(exc)})
                        continue
                    if not isinstance(record, dict):
                        malformed.append({"file": path.name, "line": line_no, "error": "record is not an object"})
                        continue

                    record["_file"] = path.name
                    metadata = record.get("metadata") if record.get("kind") == "_flush" else {}
                    metadata = metadata if isinstance(metadata, dict) else {}
                    identity = metadata if metadata else record
                    record["_run_id"] = str(identity.get("run_id", file_run_id))
                    sub_id = identity.get("sub_id", file_sub_id)
                    record["_sub_id"] = str(sub_id) if sub_id is not None else None
                    record["_pid"] = identity.get("pid", file_pid)
                    if record.get("kind") == "_flush":
                        if _record_in_time_window(record, time_window, is_footer=True):
                            flushes.append(record)
                        continue

                    record["_started_ts"] = parse_timestamp(record.get("started_at"))
                    record["_ended_ts"] = parse_timestamp(record.get("ended_at"))
                    record["extra"] = record.get("extra") if isinstance(record.get("extra"), dict) else {}
                    if _record_in_time_window(record, time_window):
                        events.append(record)

        events.sort(key=_event_sort_key)
        return cls(
            files=files,
            events=events,
            flushes=flushes,
            malformed_lines=malformed,
            scope_error=scope.performance_error if scope else "",
            scope_match_mode=scope.performance_match_mode if scope else "",
        )

    def by_run(self) -> dict[str, list[dict[str, Any]]]:
        """Group performance events by natural-sorted run id."""
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in self.events:
            run_id = str(event.get("_run_id"))
            run_events = grouped.get(run_id)
            if run_events is None:
                run_events = []
                grouped[run_id] = run_events
            run_events.append(event)
        return dict(sorted(grouped.items(), key=_run_group_sort_key))

    def footer_for(self, run_id: str, pid: Any = None) -> Optional[dict[str, Any]]:
        """Return the final flush footer for a run and optional process id."""
        run_id_str = str(run_id)
        result = None
        for footer in self.flushes:
            if str(footer.get("_run_id")) == run_id_str and (pid is None or footer.get("_pid") == pid):
                result = footer
        return result

    def quality(self) -> dict[str, Any]:
        """Return data-quality counters for performance collection artifacts."""
        timestamp_missing = sum(
            event.get("_started_ts") is None or event.get("_ended_ts") is None for event in self.events
        )
        negative_intervals = sum(
            event.get("_started_ts") is not None
            and event.get("_ended_ts") is not None
            and event.get("_ended_ts", 0) < event.get("_started_ts", 0)
            for event in self.events
        )
        complete_files = {footer.get("_file") for footer in self.flushes}
        incomplete_files: list[str] = []
        for path in self.files:
            if path.name not in complete_files:
                incomplete_files.append(path.name)
        return {
            "file_count": len(self.files),
            "event_count": len(self.events),
            "flush_count": len(self.flushes),
            "malformed_count": len(self.malformed_lines),
            "missing_timestamp_count": timestamp_missing,
            "negative_interval_count": negative_intervals,
            "incomplete_files": incomplete_files,
            "scope_match_mode": self.scope_match_mode,
        }
