# Licensed under the Apache License, Version 2.0 (the "License");
"""Discover subagent sessions associated with a parent session."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from scripts.analyzer.base import AnalyzerSpec, BaseAnalyzer
from scripts.analyzer.performance import format_duration, natural_key, parse_performance_filename
from scripts.analyzer.scope import AnalysisScope

_PARENT_SESSION_RE = re.compile(r"^(?P<stamp>\d{8}_\d{6})_(?P<token>.+)$")
_SUBAGENT_SESSION_RE = re.compile(r"^subagent_(?P<session_id>.+)_(?P<sub_id>[^_]+)$")
_RUN_CONTEXT_RE = re.compile(r"^Run(?P<run_id>\d+)_Sub(?P<sub_id>\d+)\.json$")
_PERFORMANCE_LOG_RE = re.compile(r"\[perf\] enabled, jsonl=(?P<path>.+?\.jsonl)(?:\s|$)")
_LOG_TIMESTAMP_RE = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
_LOADED_CONFIG_RE = re.compile(r"Loaded configuration file:\s*(?P<path>\S+\.ya?ml)")
_PATH_EVIDENCE_RE = re.compile(
    r"(?P<path>(?:/|~/?)[^\s'\"<>|]+?\."
    r"(?:json|jsonl|csv|tsv|parquet|txt|md|html|png|jpg|jpeg|db|sqlite))"
)


def _path_natural_key(path: Path) -> tuple[Any, ...]:
    return natural_key(path.name)


def _path_string_key(path: Path) -> str:
    return str(path)


class SubagentAnalyzer(BaseAnalyzer):
    """Discover physical and inline subagent session roots and report links."""

    name = "subagents"
    description = "Find subagent session roots linked to the current session."
    spec = AnalyzerSpec(
        name="subagents",
        title="Subagents",
        order=35,
        description="Discovers subagent workspaces and inline subagent trajectories.",
        data_sources=("subagents/", ".context/", ".performance/", "logs/"),
        empty_message="No subagent sessions were discovered for this session.",
    )

    @staticmethod
    def anchor_id(session_id: str) -> str:
        """Return the stable DOM anchor for a subagent session row."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", session_id)
        return f"subagent-row-{safe}"

    @staticmethod
    def enrich_item_from_report(item: dict[str, Any]) -> None:
        """Read a generated report-data.json and attach display metrics to an item."""
        report_path_value = item.get("report_path")
        session_id = str(item.get("session_id", ""))
        if not report_path_value:
            item.update(SubagentAnalyzer._default_metrics(session_id))
            return
        report_path = Path(str(report_path_value))
        item.update(SubagentAnalyzer._extract_report_metrics(report_path.with_name("report-data.json"), session_id))

    @staticmethod
    def summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Summarize discovered subagents for the overview cards."""
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in items)
        durations: list[float] = []
        for item in items:
            duration = item.get("duration_seconds")
            if duration is not None:
                durations.append(float(duration))
        return {
            "discovered": len(items),
            "with_context": sum(1 for item in items if item.get("context_count", 0) > 0),
            "with_logs": sum(1 for item in items if item.get("log_count", 0) > 0),
            "with_report": sum(1 for item in items if item.get("report_exists")),
            "total_turns": sum(int(item.get("turn_count") or 0) for item in items),
            "total_tokens": total_tokens,
            "total_tokens_display": f"{total_tokens:,}" if total_tokens else "—",
            "known_durations": len(durations),
            "total_duration": format_duration(sum(durations) * 1000.0) if durations else "—",
        }

    @staticmethod
    def sort_items(items: list[dict[str, Any]]) -> None:
        """Sort subagent rows by discovered or inferred start time."""
        items.sort(key=SubagentAnalyzer._sort_key)

    @staticmethod
    def _performance_files(session_root: Path) -> list[Path]:
        return sorted(
            (path.resolve() for path in (session_root / ".performance").glob("*.jsonl")),
            key=_path_natural_key,
        )

    @staticmethod
    def _performance_sub_ids(session_root: Path) -> set[str]:
        sub_ids: set[str] = set()
        for performance_file in SubagentAnalyzer._performance_files(session_root):
            _, sub_id, _ = parse_performance_filename(performance_file.name)
            if sub_id is not None:
                sub_ids.add(sub_id)
        return sub_ids

    @staticmethod
    def _context_files(session_root: Path) -> list[Path]:
        context_dir = session_root / ".context"
        return sorted(
            (path.resolve() for path in context_dir.glob("Run*.json") if not path.name.endswith(".meta.json")),
            key=_path_natural_key,
        )

    @staticmethod
    def _sub_ids(context_files: list[Path]) -> set[str]:
        values: set[str] = set()
        for context_file in context_files:
            match = _RUN_CONTEXT_RE.match(context_file.name)
            if match is not None and match.group("sub_id") != "0":
                values.add(match.group("sub_id"))
        return values

    @staticmethod
    def _run_ids(context_files: list[Path]) -> set[str]:
        values: set[str] = set()
        for context_file in context_files:
            match = _RUN_CONTEXT_RE.match(context_file.name)
            if match is not None:
                values.add(match.group("run_id"))
        return values

    @staticmethod
    def _candidate_log_roots(parent_root: Path, session_root: Path) -> list[Path]:
        candidates = [
            session_root,
            session_root / "logs",
            parent_root,
            parent_root / "logs",
            parent_root.parent / "logs",
        ]
        for ancestor in (parent_root, *parent_root.parents):
            if ancestor.parent.name in {".dataagent", ".ferry"}:
                candidates.append(ancestor / "logs")
                break
        roots: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if candidate.is_dir() and resolved not in seen:
                roots.append(resolved)
                seen.add(resolved)
        return roots

    @staticmethod
    def _read_contains(path: Path, needles: set[str]) -> bool:
        if not needles:
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return any(needle and needle in text for needle in needles)

    @staticmethod
    def _performance_paths_from_logs(log_files: list[Path]) -> list[Path]:
        paths: set[Path] = set()
        for log_file in log_files:
            try:
                with log_file.open(encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        match = _PERFORMANCE_LOG_RE.search(line)
                        if match is None:
                            continue
                        path = Path(match.group("path").strip()).expanduser()
                        if path.is_file():
                            paths.add(path.resolve())
            except OSError:
                continue
        return sorted(paths, key=_path_string_key)

    @staticmethod
    def _performance_files_have_multiple_flushes(performance_files: list[Path]) -> bool:
        flush_count = 0
        for performance_file in performance_files:
            try:
                with performance_file.open(encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        if '"kind": "_flush"' in line or '"kind":"_flush"' in line:
                            flush_count += 1
                            if flush_count > 1:
                                return True
            except OSError:
                continue
        return False

    @staticmethod
    def _log_time_window(log_files: list[Path]) -> Optional[tuple[float, float]]:
        timestamps: list[float] = []
        for log_file in log_files:
            try:
                with log_file.open(encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        match = _LOG_TIMESTAMP_RE.match(line)
                        if match is None:
                            continue
                        try:
                            timestamps.append(
                                datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                            )
                        except ValueError:
                            continue
            except OSError:
                continue
        if not timestamps:
            return None
        return min(timestamps) - 1.0, max(timestamps) + 1.0

    @staticmethod
    def _display_info(
        session_root: Path,
        sub_id: str,
        log_files: list[Path],
        context_files: list[Path],
    ) -> dict[str, Any]:
        config_path = SubagentAnalyzer._extract_config_path_from_logs(log_files)
        config_name = Path(config_path).stem if config_path else ""
        query = SubagentAnalyzer._extract_first_query(session_root, context_files)
        return {
            "display_name": config_name or f"Sub {sub_id}",
            "config_name": config_name,
            "config_path": config_path,
            "last_query": query,
        }

    @staticmethod
    def _extract_config_path_from_logs(log_files: list[Path]) -> str:
        for log_file in log_files:
            try:
                with log_file.open(encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        match = _LOADED_CONFIG_RE.search(line)
                        if match is None:
                            continue
                        path = match.group("path")
                        if Path(path).name != "flex_default_configs.yaml":
                            return path
            except OSError:
                continue
        return ""

    @staticmethod
    def _extract_first_query(session_root: Path, context_files: Optional[list[Path]] = None) -> str:
        files = context_files if context_files is not None else SubagentAnalyzer._context_files(session_root)
        for context_file in files:
            try:
                payload = json.loads(context_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            nodes = payload.get("nodes", [])
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, dict) or node.get("node_type") != "Query":
                    continue
                query = str(node.get("query") or "").strip()
                if query:
                    return query
        return ""

    @staticmethod
    def _extract_evidence_paths_from_logs(log_files: list[Path], limit: int = 30) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for log_file in log_files:
            try:
                with log_file.open(encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        for match in _PATH_EVIDENCE_RE.finditer(line):
                            value = match.group("path").rstrip(".,);]")
                            if value in seen:
                                continue
                            paths.append(value)
                            seen.add(value)
                            if len(paths) >= limit:
                                return paths
            except OSError:
                continue
        return paths

    @staticmethod
    def _href_for(path: Path, parent_report_dir: Optional[Path]) -> str:
        resolved = path.resolve()
        if parent_report_dir is None:
            return resolved.as_uri()
        try:
            return resolved.relative_to(parent_report_dir).as_posix()
        except ValueError:
            return resolved.as_uri()

    @staticmethod
    def _default_metrics(session_id: str) -> dict[str, Any]:
        start_ts = SubagentAnalyzer._start_ts_from_session_id(session_id)
        return {
            "start_ts": start_ts,
            "start_sort_ts": start_ts,
            "start_time": SubagentAnalyzer._format_timestamp(start_ts),
            "end_ts": None,
            "end_time": "—",
            "duration_seconds": None,
            "duration": "—",
            "turn_count": 0,
            "total_rounds": 0,
            "total_tokens": None,
            "total_tokens_display": "—",
            "token_status": "not recorded",
        }

    @staticmethod
    def _extract_report_metrics(report_data_path: Path, session_id: str) -> dict[str, Any]:
        metrics = SubagentAnalyzer._default_metrics(session_id)
        if not report_data_path.is_file():
            return metrics
        try:
            payload = json.loads(report_data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return metrics

        results = payload.get("results", {})
        results = results if isinstance(results, dict) else {}
        trajectory = results.get("trajectory", {})
        trajectory = trajectory if isinstance(trajectory, dict) else {}
        logs = results.get("logs", {})
        logs = logs if isinstance(logs, dict) else {}
        token = results.get("token", {})
        token = token if isinstance(token, dict) else {}
        start_ts = logs.get("session_start_ts") or metrics.get("start_ts")
        end_ts = logs.get("session_end_ts")
        duration_seconds = logs.get("session_duration_seconds")
        if duration_seconds is None and start_ts is not None and end_ts is not None:
            duration_seconds = max(float(end_ts) - float(start_ts), 0.0)
        overview = token.get("overview", {})
        overview = overview if isinstance(overview, dict) else {}
        total_tokens = overview.get("total_tokens")
        metrics.update(
            {
                "start_ts": start_ts,
                "start_sort_ts": start_ts,
                "start_time": SubagentAnalyzer._format_timestamp(start_ts),
                "end_ts": end_ts,
                "end_time": SubagentAnalyzer._format_timestamp(end_ts),
                "duration_seconds": duration_seconds,
                "duration": format_duration(float(duration_seconds) * 1000.0) if duration_seconds is not None else "—",
                "turn_count": int(trajectory.get("turn_count") or 0),
                "total_rounds": int(trajectory.get("total_rounds") or 0),
                "total_tokens": int(total_tokens) if total_tokens is not None else None,
                "total_tokens_display": f"{int(total_tokens):,}" if total_tokens is not None else "—",
                "token_status": token.get("error") if total_tokens is None else "recorded",
            }
        )
        return metrics

    @staticmethod
    def _format_timestamp(timestamp: Optional[float]) -> str:
        if timestamp is None:
            return "—"
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> tuple[bool, float, str]:
        start_ts = item.get("start_sort_ts")
        return (start_ts is None, float(start_ts or 0.0), str(item.get("session_id", "")))

    @staticmethod
    def _start_ts_from_session_id(session_id: str) -> Optional[float]:
        subagent_match = _SUBAGENT_SESSION_RE.match(session_id)
        if subagent_match:
            session_id = subagent_match.group("session_id")
        match = _PARENT_SESSION_RE.match(session_id)
        if match is None:
            return None
        try:
            return datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            return None

    def analyze(self, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Discover subagent sessions associated with *session_root*."""
        report_name = str(kwargs.get("report_name", "report.html"))
        report_root_value = kwargs.get("report_root")
        parent_report_dir_value = kwargs.get("parent_report_dir")
        report_root = Path(report_root_value).expanduser().resolve() if report_root_value else None
        parent_report_dir = Path(parent_report_dir_value).expanduser().resolve() if parent_report_dir_value else None
        candidates = self.discover(session_root, report_name, report_root, parent_report_dir)
        self.sort_items(candidates)
        return {
            "subagent_count": len(candidates),
            "summary": self.summarize_items(candidates),
            "subagents": candidates,
            "discovery_rules": [
                "A nonzero Sub id in parent .performance selects shared-workspace subagent analysis.",
                "Otherwise, prefer <session-root>/subagents/<subagent-session>/ as independent workspaces.",
                "Inline performance uses filename sub_id, then the subagent log's jsonl path, "
                "then an exclusive run id.",
                "Legacy sibling subagent_<parent-session>_<sub-id> directories are a final compatibility fallback.",
            ],
        }

    def discover(
        self,
        session_root: Path,
        report_name: str = "report.html",
        report_root: Optional[Path] = None,
        parent_report_dir: Optional[Path] = None,
    ) -> list[dict[str, Any]]:
        """Return report-ready rows for all subagents belonging to a parent session."""
        parent_root = session_root.expanduser().resolve()
        if parent_root.parent.name == "subagents" or parent_root.name.startswith("subagent_"):
            return []

        if self._uses_inline_workspace(parent_root):
            inline_rows = self._discover_inline_sessions(parent_root, report_name, report_root, parent_report_dir)
            if inline_rows:
                return inline_rows

        subagents_root = parent_root / "subagents"
        if subagents_root.is_dir():
            return self._discover_workspace_sessions(
                parent_root, subagents_root, report_name, report_root, parent_report_dir
            )

        inline_rows = self._discover_inline_sessions(parent_root, report_name, report_root, parent_report_dir)
        if inline_rows:
            return inline_rows
        return self._discover_legacy_sibling_sessions(parent_root, report_name, report_root, parent_report_dir)

    def discover_job_specs(self, session_root: Path) -> list[dict[str, Any]]:
        """Discover child report jobs using directory and context metadata only."""
        parent_root = session_root.expanduser().resolve()
        if parent_root.parent.name == "subagents" or parent_root.name.startswith("subagent_"):
            return []

        specs: list[dict[str, Any]] = []
        grouped: dict[str, list[Path]] = {}
        if self._uses_inline_workspace(parent_root):
            for context_file in self._context_files(parent_root):
                match = _RUN_CONTEXT_RE.match(context_file.name)
                if match is None or match.group("sub_id") == "0":
                    continue
                grouped.setdefault(match.group("sub_id"), []).append(context_file)
        if grouped:
            for sub_id in sorted(grouped, key=natural_key):
                context_files = sorted(grouped.get(sub_id, []), key=_path_natural_key)
                virtual_session_id = f"subagent_{parent_root.name}_{sub_id}"
                scope = AnalysisScope(
                    kind="inline_shared_workspace",
                    session_id=virtual_session_id,
                    parent_session_id=parent_root.name,
                    sub_id=sub_id,
                    context_files=tuple(str(path) for path in context_files),
                )
                specs.append(
                    {"session_path": parent_root, "report_key": f"sub-{sub_id}", "analysis_scope": scope.to_dict()}
                )
            return specs

        subagents_root = parent_root / "subagents"
        if subagents_root.is_dir():
            for candidate in sorted(subagents_root.iterdir(), key=_path_natural_key):
                if not candidate.is_dir() or not (candidate / ".context").is_dir():
                    continue
                context_files = self._context_files(candidate)
                sub_ids = self._sub_ids(context_files)
                sub_id = next(iter(sub_ids)) if len(sub_ids) == 1 else candidate.name
                scope = AnalysisScope(
                    kind="workspace_subagent",
                    session_id=candidate.name,
                    parent_session_id=parent_root.name,
                    sub_id=sub_id,
                )
                specs.append(
                    {"session_path": candidate, "report_key": candidate.name, "analysis_scope": scope.to_dict()}
                )
            return specs

        if not parent_root.parent.is_dir():
            return []
        for candidate in sorted(parent_root.parent.iterdir(), key=_path_natural_key):
            match = _SUBAGENT_SESSION_RE.match(candidate.name)
            if not candidate.is_dir() or match is None or match.group("session_id") != parent_root.name:
                continue
            if not (candidate / ".context").is_dir():
                continue
            scope = AnalysisScope(
                kind="legacy_sibling",
                session_id=candidate.name,
                parent_session_id=parent_root.name,
                sub_id=match.group("sub_id"),
            )
            specs.append({"session_path": candidate, "report_key": candidate.name, "analysis_scope": scope.to_dict()})
        return specs

    def resolve_scope(self, session_root: Path, value: Any) -> Optional[AnalysisScope]:
        """Resolve log and shared-performance evidence for a lightweight child job scope."""
        scope = AnalysisScope.from_value(value)
        if scope is None or not scope.sub_id:
            return scope
        if scope.log_files and (not scope.is_inline or scope.performance_files or scope.performance_error):
            return scope

        resolved_root = session_root.expanduser().resolve()
        if scope.is_inline:
            parent_root = resolved_root
        elif resolved_root.parent.name == "subagents":
            parent_root = resolved_root.parent.parent
        else:
            parent_root = resolved_root.parent / scope.parent_session_id
        log_files = self._find_subagent_logs(parent_root, resolved_root, scope.sub_id, strict=scope.is_inline)
        if not scope.is_inline:
            return replace(scope, log_files=tuple(str(path) for path in log_files))

        context_files = scope.resolve_context_files(resolved_root)
        all_context_files = self._context_files(resolved_root)
        performance_files, performance_mode = self._match_inline_performance(
            parent_root, scope.sub_id, context_files, all_context_files, log_files
        )
        performance_error = ""
        if not performance_files and any((resolved_root / ".performance").glob("*.jsonl")):
            performance_error = (
                "Shared performance data exists, but this subagent cannot be attributed reliably "
                "(共享 performance 未记录 sub_id，且日志/独占 run_id 均无法完成归属)."
            )
        return replace(
            scope,
            log_files=tuple(str(path) for path in log_files),
            performance_files=tuple(str(path) for path in performance_files),
            performance_time_window=(
                self._log_time_window(log_files)
                if self._performance_files_have_multiple_flushes(performance_files)
                else None
            ),
            performance_match_mode=performance_mode,
            performance_error=performance_error,
        )

    def resolve_main_scope(self, session_root: Path) -> Optional[AnalysisScope]:
        """Build a conservative parent scope when inline subagents share the workspace."""
        parent_root = session_root.expanduser().resolve()
        performance_sub_ids = self._performance_sub_ids(parent_root)
        has_inline_performance = any(sub_id != "0" for sub_id in performance_sub_ids)
        if (parent_root / "subagents").is_dir() and not has_inline_performance:
            return None
        context_files = self._context_files(parent_root)
        main_context_files: list[Path] = []
        inline_groups: dict[str, list[Path]] = {}
        for context_file in context_files:
            match = _RUN_CONTEXT_RE.match(context_file.name)
            if match is None:
                continue
            sub_id = match.group("sub_id")
            if sub_id == "0":
                main_context_files.append(context_file)
            else:
                inline_groups.setdefault(sub_id, []).append(context_file)
        if not inline_groups and not has_inline_performance:
            return None

        main_runs = self._run_ids(main_context_files)
        claimed_files: set[Path] = set()
        ambiguous_runs: set[str] = set()
        for sub_id, sub_context_files in inline_groups.items():
            log_files = self._find_subagent_logs(parent_root, parent_root, sub_id, strict=True)
            performance_files, _ = self._match_inline_performance(
                parent_root, sub_id, sub_context_files, context_files, log_files
            )
            if performance_files:
                claimed_files.update(performance_files)
                if self._performance_files_have_multiple_flushes(performance_files):
                    ambiguous_runs.update(self._run_ids(sub_context_files) & main_runs)
            else:
                ambiguous_runs.update(self._run_ids(sub_context_files) & main_runs)

        selected_files: list[Path] = []
        for performance_file in self._performance_files(parent_root):
            resolved = performance_file.resolve()
            run_id, file_sub_id, _ = parse_performance_filename(performance_file.name)
            if file_sub_id is not None:
                if file_sub_id == "0":
                    selected_files.append(resolved)
                continue
            if resolved not in claimed_files and run_id not in ambiguous_runs:
                selected_files.append(resolved)
        performance_error = ""
        if ambiguous_runs:
            runs = ", ".join(sorted(ambiguous_runs, key=natural_key))
            performance_error = (
                f"Main and inline subagent performance overlap in run(s) {runs}, but the writer did not record sub_id. "
                "The analyzer withheld ambiguous timing/token data instead of mixing ownership."
            )
        return AnalysisScope(
            kind="main_shared_workspace",
            session_id=parent_root.name,
            parent_session_id=parent_root.name,
            context_files=tuple(str(path) for path in main_context_files),
            performance_files=tuple(str(path) for path in selected_files),
            performance_match_mode=(
                "filename_sub_id_main" if has_inline_performance else "exclude_inline_subagent_files"
            ),
            performance_error=performance_error,
        )

    def _discover_workspace_sessions(
        self,
        parent_root: Path,
        subagents_root: Path,
        report_name: str,
        report_root: Optional[Path],
        parent_report_dir: Optional[Path],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for candidate in sorted(subagents_root.iterdir(), key=_path_natural_key):
            if not candidate.is_dir() or not (candidate / ".context").is_dir():
                continue
            context_files = self._context_files(candidate)
            sub_ids = self._sub_ids(context_files)
            sub_id = next(iter(sub_ids)) if len(sub_ids) == 1 else candidate.name
            log_files = self._find_subagent_logs(parent_root, candidate, sub_id)
            scope = AnalysisScope(
                kind="workspace_subagent",
                session_id=candidate.name,
                parent_session_id=parent_root.name,
                sub_id=sub_id,
                log_files=tuple(str(path) for path in log_files),
            )
            rows.append(
                self._build_row(
                    parent_root=parent_root,
                    session_root=candidate,
                    session_id=candidate.name,
                    report_key=candidate.name,
                    sub_id=sub_id,
                    context_files=context_files,
                    log_files=log_files,
                    scope=scope,
                    match_mode="workspace_subagents_dir",
                    evidence=f"workspace directory {candidate.relative_to(parent_root)}",
                    report_name=report_name,
                    report_root=report_root,
                    parent_report_dir=parent_report_dir,
                )
            )
        return rows

    def _discover_inline_sessions(
        self,
        parent_root: Path,
        report_name: str,
        report_root: Optional[Path],
        parent_report_dir: Optional[Path],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[Path]] = {}
        all_context_files = self._context_files(parent_root)
        for context_file in all_context_files:
            match = _RUN_CONTEXT_RE.match(context_file.name)
            if match is None or match.group("sub_id") == "0":
                continue
            grouped.setdefault(match.group("sub_id"), []).append(context_file)

        rows: list[dict[str, Any]] = []
        for sub_id in sorted(grouped, key=natural_key):
            context_files = sorted(grouped.get(sub_id, []), key=_path_natural_key)
            log_files = self._find_subagent_logs(parent_root, parent_root, sub_id, strict=True)
            performance_files, performance_mode = self._match_inline_performance(
                parent_root, sub_id, context_files, all_context_files, log_files
            )
            time_window = (
                self._log_time_window(log_files)
                if self._performance_files_have_multiple_flushes(performance_files)
                else None
            )
            performance_error = ""
            if not performance_files and any((parent_root / ".performance").glob("*.jsonl")):
                performance_error = (
                    "Shared performance data exists, but this subagent cannot be attributed reliably "
                    "(共享 performance 未记录 sub_id，且日志/独占 run_id 均无法完成归属)."
                )
            virtual_session_id = f"subagent_{parent_root.name}_{sub_id}"
            scope = AnalysisScope(
                kind="inline_shared_workspace",
                session_id=virtual_session_id,
                parent_session_id=parent_root.name,
                sub_id=sub_id,
                context_files=tuple(str(path) for path in context_files),
                performance_files=tuple(str(path) for path in performance_files),
                log_files=tuple(str(path) for path in log_files),
                performance_time_window=time_window,
                performance_match_mode=performance_mode,
                performance_error=performance_error,
            )
            rows.append(
                self._build_row(
                    parent_root=parent_root,
                    session_root=parent_root,
                    session_id=virtual_session_id,
                    report_key=f"sub-{sub_id}",
                    sub_id=sub_id,
                    context_files=context_files,
                    log_files=log_files,
                    scope=scope,
                    match_mode="inline_context",
                    evidence=f"{len(context_files)} context file(s) with Sub{sub_id}",
                    report_name=report_name,
                    report_root=report_root,
                    parent_report_dir=parent_report_dir,
                )
            )
        return rows

    def _discover_legacy_sibling_sessions(
        self,
        parent_root: Path,
        report_name: str,
        report_root: Optional[Path],
        parent_report_dir: Optional[Path],
    ) -> list[dict[str, Any]]:
        if not parent_root.parent.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for candidate in sorted(parent_root.parent.iterdir(), key=_path_natural_key):
            if not candidate.is_dir() or candidate == parent_root:
                continue
            match = _SUBAGENT_SESSION_RE.match(candidate.name)
            if match is None or match.group("session_id") != parent_root.name:
                continue
            if not (candidate / ".context").is_dir():
                continue
            sub_id = match.group("sub_id")
            context_files = self._context_files(candidate)
            log_files = self._find_subagent_logs(parent_root, candidate, sub_id)
            scope = AnalysisScope(
                kind="legacy_sibling",
                session_id=candidate.name,
                parent_session_id=parent_root.name,
                sub_id=sub_id,
                log_files=tuple(str(path) for path in log_files),
            )
            rows.append(
                self._build_row(
                    parent_root=parent_root,
                    session_root=candidate,
                    session_id=candidate.name,
                    report_key=candidate.name,
                    sub_id=sub_id,
                    context_files=context_files,
                    log_files=log_files,
                    scope=scope,
                    match_mode="sibling_session_id",
                    evidence=f"legacy sibling directory with sub id {sub_id}",
                    report_name=report_name,
                    report_root=report_root,
                    parent_report_dir=parent_report_dir,
                )
            )
        return rows

    def _build_row(
        self,
        *,
        parent_root: Path,
        session_root: Path,
        session_id: str,
        report_key: str,
        sub_id: str,
        context_files: list[Path],
        log_files: list[Path],
        scope: AnalysisScope,
        match_mode: str,
        evidence: str,
        report_name: str,
        report_root: Optional[Path],
        parent_report_dir: Optional[Path],
    ) -> dict[str, Any]:
        report_path = (report_root / report_key / report_name) if report_root else (session_root / report_name)
        context_file_names: list[str] = []
        for context_file in context_files:
            context_file_names.append(context_file.name)
        log_file_names: list[str] = []
        log_hrefs: list[str] = []
        for log_file in log_files:
            log_file_names.append(str(log_file))
            log_hrefs.append(log_file.as_uri())
        item = {
            "sub_id": sub_id,
            **self._display_info(session_root, sub_id, log_files, context_files),
            "session_id": session_id,
            "anchor_id": self.anchor_id(session_id),
            "path": str(session_root),
            "path_href": session_root.as_uri(),
            "report_path": str(report_path),
            "report_href": self._href_for(report_path, parent_report_dir),
            "report_exists": report_path.is_file(),
            "context_count": len(context_files),
            "context_files": context_file_names,
            "log_count": len(log_files),
            "log_files": log_file_names,
            "log_hrefs": log_hrefs,
            "evidence_paths": self._extract_evidence_paths_from_logs(log_files),
            "match_mode": match_mode,
            "performance_match_mode": scope.performance_match_mode,
            "evidence": evidence,
            "analysis_scope": scope.to_dict(),
            "parent_session_id": parent_root.name,
        }
        item.update(self._default_metrics(session_id))
        self.enrich_item_from_report(item)
        return item

    def _find_subagent_logs(
        self,
        parent_root: Path,
        session_root: Path,
        sub_id: str,
        *,
        strict: bool = False,
    ) -> list[Path]:
        synthetic_id = f"subagent_{parent_root.name}_{sub_id}"
        matched: list[Path] = []
        for log_root in self._candidate_log_roots(parent_root, session_root):
            for log_file in sorted(log_root.glob("*.log")):
                if synthetic_id in log_file.name or session_root.name in log_file.name:
                    matched.append(log_file.resolve())
                    continue
                needles = {synthetic_id}
                if not strict:
                    needles.update({session_root.name, str(session_root)})
                if self._read_contains(log_file, needles):
                    matched.append(log_file.resolve())
        return sorted(set(matched), key=_path_string_key)

    def _match_inline_performance(
        self,
        parent_root: Path,
        sub_id: str,
        context_files: list[Path],
        all_context_files: list[Path],
        log_files: list[Path],
    ) -> tuple[list[Path], str]:
        own_runs = self._run_ids(context_files)
        named_files: list[Path] = []
        for performance_file in self._performance_files(parent_root):
            _, file_sub_id, _ = parse_performance_filename(performance_file.name)
            if file_sub_id == sub_id:
                named_files.append(performance_file.resolve())
        if named_files:
            return named_files, "filename_sub_id"

        from_logs = self._performance_paths_from_logs(log_files)
        if from_logs:
            return from_logs, "subagent_log_jsonl_path"

        occupied_runs: set[str] = set()
        for context_file in all_context_files:
            match = _RUN_CONTEXT_RE.match(context_file.name)
            if match is None or match.group("sub_id") == sub_id:
                continue
            occupied_runs.add(match.group("run_id"))
        exclusive_runs = own_runs - occupied_runs
        if not exclusive_runs:
            return [], "ambiguous_shared_performance"

        matched: list[Path] = []
        for performance_file in self._performance_files(parent_root):
            run_id, file_sub_id, _ = parse_performance_filename(performance_file.name)
            if run_id in exclusive_runs and file_sub_id in (None, sub_id):
                matched.append(performance_file.resolve())
        return matched, "exclusive_context_run_id" if matched else "performance_not_recorded"

    def _uses_inline_workspace(self, parent_root: Path) -> bool:
        performance_sub_ids = self._performance_sub_ids(parent_root)
        if any(sub_id != "0" for sub_id in performance_sub_ids):
            return True
        if (parent_root / "subagents").is_dir():
            return False
        return any(sub_id != "0" for sub_id in self._sub_ids(self._context_files(parent_root)))
