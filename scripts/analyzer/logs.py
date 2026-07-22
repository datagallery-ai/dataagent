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
"""Log analyzer — parses Loguru-formatted log files for errors and warnings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from scripts.analyzer.base import AnalyzerSpec, BaseAnalyzer
from scripts.analyzer.scope import AnalysisScope
from scripts.analyzer.trajectory import _run_file_sort_key

# Loguru format variants:
#   2026-06-01 17:45:41.187 | LEVEL    | name:function:line | message
#   2026-06-01 17:45:41.187 | LEVEL    | process | name:function:line | message
_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"\| (?P<level>DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL|TRACE)\s*"
    r"\| (?:(?P<process>[^|]+?)\s*\| )?"
    r"(?P<name>.+?):(?P<function>.+?):(?P<line_no>\d+) "
    r"\| (?P<message>.+)$"
)

TARGET_LEVELS = {"ERROR", "WARNING"}

# Matches: "LLM stream finished rid=xxx: reasoning_len=N content_len=N tool_calls=N ..."
_LLM_DONE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"\| \w+\s+"
    r"\| (?:[^|]+ \| )?.+ "
    r"\| LLM stream finished rid=\S+: reasoning_len=(\d+) content_len=(\d+) tool_calls=(\d+)"
)

_NODE_UPDATE_RE = re.compile(r"Context: Modifying node=((?:Action|State)\([^)]+\)) with changes=")


@dataclass(frozen=True)
class _ResolvedLogFiles:
    log_dirs: list[Path]
    all_files: list[Path]
    scope_files: list[Path]
    local_locked: bool


@dataclass(frozen=True)
class _LogSelection:
    matched_files: list[Path]
    source_files: list[Path]
    mode: str
    filter_by_time: bool


@dataclass(frozen=True)
class _LogTiming:
    start_ts: Optional[float]
    end_ts: Optional[float]
    llm_turns: list[dict[str, Any]]
    node_updates: dict[str, dict[str, Any]]


def _entry_timestamp_key(entry: dict[str, Any]) -> float:
    return float(entry.get("timestamp", 0))


class LogAnalyzer(BaseAnalyzer):
    """Parse Loguru log files and extract ERROR/WARNING entries."""

    name = "logs"
    description = "Log analysis: error and warning extraction"
    spec = AnalyzerSpec(
        name=name,
        title="Logs",
        order=40,
        description=description,
        data_sources=("session/*.log", "logs/*.log"),
        depends_on=("trajectory",),
        template="logs",
    )

    # ── @staticmethod ────────────────

    @staticmethod
    def resolve_log_dirs(session_root: Path, explicit: Optional[str | Path]) -> list[Path]:
        """Resolve likely log roots, preferring the data home containing the session."""
        if explicit:
            path = Path(explicit).expanduser()
            return [path] if path.is_dir() else []

        candidates: list[Path] = []
        candidates.append(session_root.parent / "logs")
        for ancestor in (session_root, *session_root.parents):
            if ancestor.parent.name in {".dataagent", ".ferry"}:
                candidates.append(ancestor / "logs")
                break
        return list(dict.fromkeys(path for path in candidates if path.is_dir()))

    @staticmethod
    def _session_log_files(session_root: Path) -> list[Path]:
        """Return logs stored with the session, which are authoritative if present."""
        files = set(session_root.glob("*.log"))
        local_log_dir = session_root / "logs"
        if local_log_dir.is_dir():
            files.update(local_log_dir.glob("*.log"))
        return sorted(files)

    @staticmethod
    def _read_timeline_nodes(
        session_root: Path, context_files: Optional[list[Path]] = None
    ) -> tuple[set[str], dict[str, str]]:
        """Read unique Action ids and final State values from trajectory files."""
        action_ids: set[str] = set()
        state_values: dict[str, str] = {}
        context_dir = session_root / ".context"
        files = context_files if context_files is not None else list(context_dir.glob("Run*.json"))
        for context_file in sorted(files, key=_run_file_sort_key):
            if context_file.name.endswith(".meta.json"):
                continue
            try:
                data = json.loads(context_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for node in data.get("nodes", []):
                node_id = node.get("id", "")
                if node_id.startswith("Action("):
                    action_ids.add(node_id)
                elif node_id.startswith("State("):
                    state_values[node_id] = str(node.get("state", ""))
        return action_ids, state_values

    @staticmethod
    def _refine_time_range(
        log_files: list[Path], fallback: Optional[tuple[float, float]]
    ) -> tuple[Optional[float], Optional[float]]:
        """Scan all log entries to find actual min/max timestamps.

        Returns (start_ts, end_ts), falling back to *fallback* if no timestamps found.
        """
        min_ts = float("inf")
        max_ts = 0.0
        _TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ")
        for lf in log_files:
            try:
                with open(lf, encoding="utf-8") as fh:
                    for line in fh:
                        m = _TS_RE.match(line)
                        if not m:
                            continue
                        try:
                            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                            min_ts = min(min_ts, ts)
                            max_ts = max(max_ts, ts)
                        except ValueError:
                            continue
            except OSError:
                pass

        if min_ts != float("inf"):
            return (min_ts, max_ts)
        if fallback:
            return fallback
        return (None, None)

    @staticmethod
    def _extract_llm_turn_timestamps(log_files: list[Path]) -> list[dict]:
        """Extract LLM turn-completion timestamps from matched log files.

        Each ``LLM stream finished`` log line marks the end of one LLM call,
        which corresponds to one State entry in the trajectory timeline.
        Returns a list of {timestamp, timestamp_str, tool_calls, reasoning_len, content_len}.
        """
        turns: list[dict] = []
        for lf in log_files:
            try:
                with open(lf, encoding="utf-8") as fh:
                    for line in fh:
                        m = _LLM_DONE_RE.match(line)
                        if not m:
                            continue
                        try:
                            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                        except ValueError:
                            continue
                        turns.append(
                            {
                                "timestamp": ts,
                                "timestamp_str": m.group(1),
                                "reasoning_len": int(m.group(2)),
                                "content_len": int(m.group(3)),
                                "tool_calls": int(m.group(4)),
                            }
                        )
            except OSError:
                pass
        turns.sort(key=_entry_timestamp_key)
        return turns

    @staticmethod
    def _extract_node_update_timestamps(
        log_files: list[Path], *, state_values: Optional[dict[str, str]] = None
    ) -> dict[str, dict]:
        """Return the final ``modify_node`` timestamp for each State/Action node id."""
        updates: dict[str, dict] = {}
        for lf in log_files:
            try:
                with open(lf, encoding="utf-8") as fh:
                    for line in fh:
                        parsed = _LOG_LINE_RE.match(line.rstrip("\n"))
                        if not parsed:
                            continue
                        message = parsed.group("message")
                        match = _NODE_UPDATE_RE.search(message)
                        if not match:
                            continue
                        timestamp_str = parsed.group("timestamp")
                        try:
                            timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S.%f").timestamp()
                        except ValueError:
                            continue
                        node_id = match.group(1)
                        if node_id.startswith("State(") and state_values is not None:
                            expected_state = state_values.get(node_id)
                            if expected_state is None:
                                continue
                            # State ids are reused across concurrent sessions. Matching
                            # the final persisted value prevents cross-session timing.
                            if f"'state': {expected_state!r}" not in message:
                                continue
                        current = updates.get(node_id)
                        if current is None or timestamp >= current.get("timestamp", 0):
                            updates[node_id] = {
                                "timestamp": timestamp,
                                "timestamp_str": timestamp_str,
                                "file": lf.name,
                            }
            except OSError:
                continue
        return updates

    @staticmethod
    def _extract_action_update_timestamps(log_files: list[Path]) -> dict[str, dict]:
        """Backward-compatible Action-only view of node update timestamps."""
        base = LogAnalyzer._extract_node_update_timestamps(log_files)
        return {node_id: update for node_id, update in base.items() if node_id.startswith("Action(")}

    @staticmethod
    def _infer_time_window_from_dirname(session_root: Path) -> Optional[tuple[float, float]]:
        """Fallback: infer session time from directory name (format: YYYYMMDD_HHMMSS_...)."""
        date_re = re.compile(r"(?<!\d)(\d{8}_\d{6})(?!\d)")
        for path in (session_root, *session_root.parents):
            match = date_re.search(path.name)
            if not match:
                continue
            try:
                ts = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").timestamp()
            except ValueError:
                continue
            return (ts - 60, ts + 21600)  # 6-hour window from session start
        return None

    @staticmethod
    def _filter_files_by_session_id(log_files: list[Path], session_id: str) -> list[Path]:
        """Return log files whose content contains *session_id* anywhere."""
        if not session_id or len(session_id) <= 20:
            return []
        matched: list[Path] = []
        for lf in log_files:
            try:
                content = lf.read_text(encoding="utf-8", errors="ignore")
                if session_id in content:
                    matched.append(lf)
            except OSError:
                pass
        return matched

    @staticmethod
    def _filter_files_for_session(log_files: list[Path], *, session_id: str, action_ids: set[str]) -> list[Path]:
        """Return whole files containing the session id or an exact trajectory node id."""
        identifiers = set(action_ids)
        if session_id and len(session_id) > 20:
            identifiers.add(session_id)
        if not identifiers:
            return []

        matched: list[Path] = []
        for log_file in log_files:
            try:
                content = log_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(identifier in content for identifier in identifiers):
                matched.append(log_file)
        return matched

    @staticmethod
    def _indexed_log_files(session_root: Path, index: Any) -> list[Path]:
        """Return pre-indexed log files for a session, if available."""
        if not isinstance(index, dict):
            return []
        values = index.get(session_root.name, [])
        return sorted(Path(value) for value in values if Path(value).is_file())

    @staticmethod
    def _scope_log_files_to_session(session_root: Path, log_files: list[Path]) -> list[Path]:
        """Keep parent and subagent log files in separate ownership scopes."""
        session_id = session_root.name
        if session_id.startswith("subagent_"):
            return sorted(path for path in log_files if session_id in path.name)
        subagent_prefix = f"subagent_{session_id}_"
        return sorted(path for path in log_files if not path.name.startswith(subagent_prefix))

    def analyze(self, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Extract ERROR/WARNING entries from Loguru logs matching the session time window."""
        scope = AnalysisScope.from_value(kwargs.get("analysis_scope"))
        scope_log_files = scope.resolve_log_files() if scope else []
        if scope is not None and scope.sub_id and not scope_log_files:
            return {
                "error": "No log file could be attributed to this subagent without including parent logs.",
                "match_mode": "subagent_log_not_attributed",
            }
        resolved = self._resolve_log_files(session_root, scope_log_files, kwargs)
        if not resolved.all_files:
            searched = ", ".join(str(path) for path in resolved.log_dirs)
            return {"error": f"No .log files found in: {searched}"}

        context_files = scope.resolve_context_files(session_root) if scope else None
        time_window = self._infer_time_window(session_root, context_files)
        session_id = scope.session_id if scope and scope.session_id else session_root.name
        action_ids, state_values = self._read_timeline_nodes(session_root, context_files)
        selection = self._select_log_files(session_root, resolved, session_id, action_ids, kwargs)
        entries = self._collect_entries(selection, time_window)
        timing = self._collect_timing(selection.matched_files, time_window, state_values)
        return self._build_result(entries, selection, timing)

    def _resolve_log_files(
        self, session_root: Path, scope_files: list[Path], kwargs: dict[str, Any]
    ) -> _ResolvedLogFiles:
        local_files = self._session_log_files(session_root)
        local_locked = bool(local_files) or bool(scope_files)
        if scope_files:
            return _ResolvedLogFiles(sorted({path.parent for path in scope_files}), scope_files, scope_files, True)
        if local_locked:
            return _ResolvedLogFiles([session_root], local_files, [], True)

        log_dirs = self.resolve_log_dirs(session_root, kwargs.get("log_dir"))
        indexed_files = self._indexed_log_files(session_root, kwargs.get("log_file_index"))
        all_files = indexed_files
        if not all_files:
            discovered_files: set[Path] = set()
            for log_dir in log_dirs:
                discovered_files.update(log_dir.glob("*.log"))
            all_files = sorted(discovered_files)
        all_files = self._scope_log_files_to_session(session_root, all_files)
        return _ResolvedLogFiles(log_dirs, all_files, [], False)

    def _select_log_files(
        self,
        session_root: Path,
        resolved: _ResolvedLogFiles,
        session_id: str,
        action_ids: set[str],
        kwargs: dict[str, Any],
    ) -> _LogSelection:
        if resolved.scope_files:
            return _LogSelection(resolved.scope_files, resolved.scope_files, "subagent_scope", False)
        if resolved.local_locked:
            return _LogSelection(resolved.all_files, resolved.all_files, "session_local", False)

        indexed_files = self._indexed_log_files(session_root, kwargs.get("log_file_index"))
        indexed_files = self._scope_log_files_to_session(session_root, indexed_files)
        matched_files = indexed_files
        if not matched_files:
            matched_files = self._filter_files_for_session(
                resolved.all_files, session_id=session_id, action_ids=action_ids
            )
        if matched_files:
            return _LogSelection(matched_files, matched_files, "intersection", True)
        return _LogSelection([], resolved.all_files, "time_window_only", True)

    def _collect_entries(
        self, selection: _LogSelection, time_window: Optional[tuple[float, float]]
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for log_file in selection.source_files:
            parsed_entries = self._parse_log_file(
                log_file,
                time_window=time_window,
                filter_by_time=selection.filter_by_time,
            )
            for entry in parsed_entries:
                key = (entry.get("timestamp_str", ""), entry.get("level", ""), entry.get("message", "")[:80])
                if key not in seen:
                    seen.add(key)
                    entry["match_type"] = selection.mode
                    entries.append(entry)
        entries.sort(key=_entry_timestamp_key)
        return entries

    def _collect_timing(
        self,
        matched_files: list[Path],
        time_window: Optional[tuple[float, float]],
        state_values: dict[str, str],
    ) -> _LogTiming:
        if not matched_files:
            return _LogTiming(None, None, [], {})
        start_ts, end_ts = self._refine_time_range(matched_files, time_window)
        llm_turns = self._extract_llm_turn_timestamps(matched_files)
        node_updates = self._extract_node_update_timestamps(matched_files, state_values=state_values)
        return _LogTiming(start_ts, end_ts, llm_turns, node_updates)

    def _build_result(
        self, entries: list[dict[str, Any]], selection: _LogSelection, timing: _LogTiming
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        by_file: dict[str, int] = {}
        for entry in entries:
            level = entry.get("level", "")
            if level == "ERROR":
                errors.append(entry)
            if level == "WARNING":
                warnings.append(entry)
            file_name = entry.get("file", "")
            by_file[file_name] = by_file.get(file_name, 0) + 1

        action_updates: dict[str, dict[str, Any]] = {}
        for node_id, update in timing.node_updates.items():
            if node_id.startswith("Action("):
                action_updates[node_id] = update
        duration = None
        if timing.start_ts is not None and timing.end_ts is not None:
            duration = round(timing.end_ts - timing.start_ts, 1)
        matched_log_files: list[str] = []
        for path in selection.matched_files:
            matched_log_files.append(path.name)
        return {
            "error_count": len(errors),
            "warning_count": len(warnings),
            "total_entries": len(entries),
            "errors": errors,
            "warnings": warnings,
            "by_file": by_file,
            "by_level": {"ERROR": len(errors), "WARNING": len(warnings)},
            "match_mode": selection.mode,
            "session_id_in_logs": bool(selection.matched_files),
            "matched_log_files": matched_log_files,
            "session_start_ts": timing.start_ts,
            "session_end_ts": timing.end_ts,
            "session_duration_seconds": duration,
            "llm_turns": timing.llm_turns,
            "node_updates": timing.node_updates,
            "action_updates": action_updates,
        }

    def _infer_time_window(
        self, session_root: Path, context_files: Optional[list[Path]] = None
    ) -> Optional[tuple[float, float]]:
        """Infer session time range from context JSON files."""
        context_dir = session_root / ".context"
        if not context_dir.is_dir():
            return None

        earliest = float("inf")
        latest = 0.0
        files = context_files if context_files is not None else list(context_dir.glob("Run*.json"))
        for jf in sorted(files):
            if jf.name.endswith(".meta.json"):
                continue
            try:
                data = json.loads(jf.read_text("utf-8"))
                for node in data.get("nodes", []):
                    created = node.get("created_at")
                    if created:
                        try:
                            ts = datetime.fromisoformat(created).timestamp()
                            earliest = min(earliest, ts)
                            latest = max(latest, ts)
                        except (ValueError, OSError):
                            pass
            except (OSError, ValueError):
                continue

        if earliest == float("inf"):
            return self._infer_time_window_from_dirname(session_root)
        return (earliest - 60, latest + 60)

    def _parse_log_file(
        self,
        filepath: Path,
        *,
        time_window: Optional[tuple[float, float]] = None,
        filter_by_time: bool = True,
    ) -> list[dict]:
        """Parse a log file, returning ERROR/WARNING entries matching the time window."""
        entries: list[dict] = []
        try:
            with open(filepath, encoding="utf-8") as fh:
                for line in fh:
                    entry = self._parse_line(line, filepath.name)
                    if entry is None:
                        continue
                    if filter_by_time and time_window:
                        ts = entry.get("timestamp", 0)
                        if ts < time_window[0] or ts > time_window[1]:
                            continue
                    entries.append(entry)
        except OSError:
            pass
        return entries

    def _parse_line(self, line: str, filename: str) -> Optional[dict]:
        m = _LOG_LINE_RE.match(line.rstrip("\n"))
        if not m:
            return None
        level = m.group("level").strip()
        if level not in TARGET_LEVELS:
            return None

        ts_str = m.group("timestamp")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f").timestamp()
        except ValueError:
            return None

        return {
            "timestamp": ts,
            "timestamp_str": ts_str,
            "level": level,
            "name": m.group("name"),
            "function": m.group("function"),
            "line_no": int(m.group("line_no")),
            "message": m.group("message").strip(),
            "file": filename,
        }
