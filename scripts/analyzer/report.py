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
"""HTML report generator — renders analysis results via Jinja2 + Chart.js."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, pass_eval_context
from markupsafe import Markup


@pass_eval_context
def _truncate_filter(eval_ctx, s, length=200, end="..."):
    """Truncate a string to *length*, appending *end* if truncated."""
    if not isinstance(s, str):
        s = str(s)
    if len(s) <= length:
        return s
    return s[:length] + end


@pass_eval_context
def _truncate_expandable_filter(eval_ctx, s, length=200):
    """Truncate a string, rendering a delegated click-to-expand control."""
    # Markup is a str subclass. Converting it to a plain string before escaping
    # is important: Markup.escape(Markup(...)) trusts the existing value, which
    # can break the surrounding HTML when JSON contains quotes or newlines.
    s = str(s)
    escaped = Markup.escape(s)
    if len(s) <= length:
        return escaped
    short = Markup.escape(s[:length])
    return Markup(
        '<span class="tr-expand">'
        '<button type="button" class="tr-toggle" aria-expanded="false">'
        f'<span class="tr-short">{short}<span class="tr-dots">…</span></span>'
        '<span class="tr-label tr-label-open">展开</span>'
        '<span class="tr-label tr-label-close">收起</span>'
        "</button>"
        f'<span class="tr-full" hidden>{escaped}</span>'
        "</span>"
    )


def _tojson_filter(value, indent=None):
    """JSON encode a value for embedding in HTML/JS."""
    return Markup(json.dumps(value, ensure_ascii=False, default=str, indent=indent))


def _timed_subagent_sort_key(pair: tuple[float, dict[str, Any]]) -> tuple[float, str]:
    return pair[0], str(pair[1].get("session_id", ""))


class HTMLReportGenerator:
    """Generate an interactive HTML report from analyzer results."""

    _template_dir = Path(__file__).resolve().parent / "templates"

    # ── magic methods ────────────────

    def __init__(self) -> None:
        self._env = Environment(loader=FileSystemLoader(str(self._template_dir)), autoescape=True)
        self._env.filters["truncate"] = _truncate_filter
        self._env.filters["truncate_exp"] = _truncate_expandable_filter
        self._env.filters["tojson"] = _tojson_filter

    @staticmethod
    def _is_subagent_launcher_action(entry: dict[str, Any]) -> bool:
        """Return whether an action is allowed to link to a subagent."""
        action_name = str(entry.get("action", "")).lower()
        return (
            action_name
            in {
                "document_recall_tool",
                "nl2sql_sub_agent_tool",
                "metadata_recall",
            }
            or "subagent" in action_name
            or "sub_agent" in action_name
        )

    @staticmethod
    def _subagent_time_distance(
        entry: dict[str, Any], item: dict[str, Any], state_ts: Optional[float]
    ) -> Optional[float]:
        """Return a time distance for assigning a subagent to an action."""
        action_end = entry.get("_ts")
        sub_start = item.get("start_ts")
        sub_end = item.get("end_ts")
        if action_end is None or sub_start is None:
            return None
        try:
            action_end_f = float(action_end)
            sub_start_f = float(sub_start)
            sub_end_f = float(sub_end) if sub_end is not None else None
            state_ts_f = float(state_ts) if state_ts is not None else None
        except (TypeError, ValueError):
            return None

        if (
            state_ts_f is not None
            and sub_end_f is not None
            and state_ts_f - 2 <= sub_start_f <= action_end_f + 2
            and sub_end_f <= action_end_f + 5
        ):
            return abs(action_end_f - sub_end_f)
        if sub_start_f <= action_end_f + 5 and (sub_end_f is None or sub_end_f >= action_end_f - 5):
            reference = sub_end_f if sub_end_f is not None else sub_start_f
            return abs(action_end_f - reference) + 60.0
        if abs(sub_start_f - action_end_f) <= 30:
            return abs(sub_start_f - action_end_f) + 120.0
        return None

    @staticmethod
    def _select_subagent_for_action(
        entry: dict[str, Any], items: list[dict[str, Any]], state_ts: Optional[float]
    ) -> Optional[dict[str, Any]]:
        """Select a subagent for a whitelisted action using time when available."""
        timed: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            distance = HTMLReportGenerator._subagent_time_distance(entry, item, state_ts)
            if distance is not None:
                timed.append((distance, item))
        if timed:
            timed.sort(key=_timed_subagent_sort_key)
            return timed[0][1]
        if len(items) == 1:
            return items[0]
        return None

    @staticmethod
    def _enrich_timeline_with_subagent_matches(trajectory: dict[str, Any], subagents: dict[str, Any]) -> None:
        """Attach likely subagent links to parent timeline actions."""
        timeline = trajectory.get("timeline")
        items = subagents.get("subagents", []) if isinstance(subagents, dict) else []
        if not isinstance(timeline, list) or not isinstance(items, list) or not items:
            return

        state_timestamps = {
            (entry.get("run_id"), entry.get("id")): entry.get("_ts")
            for entry in timeline
            if isinstance(entry, dict) and entry.get("node_type") == "State" and entry.get("_ts") is not None
        }
        linked_count = 0
        for entry in timeline:
            if not isinstance(entry, dict) or entry.get("node_type") != "Action":
                continue
            if not HTMLReportGenerator._is_subagent_launcher_action(entry):
                continue
            parent_state_ts = state_timestamps.get((entry.get("run_id"), entry.get("parent_state_id", "")))
            selected = HTMLReportGenerator._select_subagent_for_action(entry, items, parent_state_ts)
            if selected is not None:
                entry["subagent_match"] = HTMLReportGenerator._subagent_match_payload(selected)
                linked_count += 1
        subagents["linked_action_count"] = linked_count

    @staticmethod
    def _subagent_match_payload(item: dict[str, Any]) -> dict[str, Any]:
        """Return the HTML/API payload for one action-to-subagent match."""
        return {
            "session_id": item.get("session_id", ""),
            "sub_id": item.get("sub_id", ""),
            "display_name": item.get("display_name", "") or f"Sub {item.get('sub_id', '')}",
            "report_href": item.get("report_href", ""),
            "anchor_id": item.get("anchor_id", ""),
        }

    @staticmethod
    def _enrich_timeline_with_timestamps(trajectory: dict[str, Any], logs: dict[str, Any]) -> None:
        """Add log-based timestamps to timeline entries.

        - State entries use ordered ``LLM stream finished`` records only when
          their count exactly matches the trajectory State count.
        - Otherwise State/Action entries require an exact node id match in
          ``modify_node`` logs.
        - Missing timing data stays blank; timestamps are never interpolated.
        - Latency for State = time since previous State.
        - Latency for Action = time since its parent State. Parallel Actions
          therefore share the same latency origin rather than chaining.
        """
        timeline = trajectory.get("timeline")
        if not timeline:
            return

        start_ts = logs.get("session_start_ts")
        end_ts = logs.get("session_end_ts")
        if start_ts and end_ts:
            logs["_start_str"] = datetime.fromtimestamp(start_ts).strftime("%H:%M:%S")
            logs["_end_str"] = datetime.fromtimestamp(end_ts).strftime("%H:%M:%S")

        llm_turns: list[dict] = logs.get("llm_turns", [])
        node_updates: dict[str, dict] = logs.get("node_updates", {})
        if not node_updates:
            node_updates = logs.get("action_updates", {})
        state_count = sum(1 for entry in timeline if entry.get("node_type") == "State")
        action_count = sum(1 for entry in timeline if entry.get("node_type") == "Action")
        use_llm_turns = state_count > 0 and len(llm_turns) == state_count
        turn_idx = 0
        prev_state_ts: Optional[float] = None
        state_timestamps: dict[tuple[Any, str], float] = {}

        for entry in timeline:
            nt = entry.get("node_type", "")
            entry.setdefault("_time_str", "")
            entry.setdefault("_latency_str", "")
            entry.setdefault("_time_source", "")

            if nt != "State":
                continue

            update = node_updates.get(entry.get("id", ""))
            if use_llm_turns:
                ts = llm_turns[turn_idx].get("timestamp")
                turn_idx += 1
                entry["_time_source"] = "State emitted at LLM completion"
                if update:
                    entry["_modify_time_str"] = datetime.fromtimestamp(update.get("timestamp", 0)).strftime(
                        "%H:%M:%S.%f"
                    )[:-3]
                    modify_time = entry.get("_modify_time_str", "")
                    entry["_time_source"] += f"; persisted by modify_node at {modify_time}"
            elif update:
                ts = update.get("timestamp")
                entry["_time_source"] = "State modify_node time (fallback)"
            else:
                continue
            if ts is None:
                continue

            entry["_time_str"] = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
            entry["_ts"] = ts
            state_timestamps[(entry.get("run_id"), entry.get("id", ""))] = ts

            if prev_state_ts is not None:
                latency_ms = int((ts - prev_state_ts) * 1000)
                if latency_ms >= 0:
                    entry["_latency_ms"] = latency_ms
                    entry["_latency_str"] = _format_latency(latency_ms)
            prev_state_ts = ts

        for entry in timeline:
            if entry.get("node_type") != "Action":
                continue
            update = node_updates.get(entry.get("id", ""))
            if update:
                action_ts = update.get("timestamp")
                if action_ts is None:
                    continue
                entry["_time_str"] = datetime.fromtimestamp(action_ts).strftime("%H:%M:%S.%f")[:-3]
                entry["_ts"] = action_ts
                entry["_time_source"] = "Action modify_node time"
                parent_key = (
                    entry.get("run_id"),
                    entry.get("parent_state_id", ""),
                )
                parent_ts = state_timestamps.get(parent_key)
                if parent_ts is not None:
                    latency_ms = int((action_ts - parent_ts) * 1000)
                    if latency_ms >= 0:
                        entry["_latency_ms"] = latency_ms
                        entry["_latency_str"] = _format_latency(latency_ms)

        timed_states = sum(bool(entry.get("_time_str")) for entry in timeline if entry.get("node_type") == "State")
        timed_actions = sum(bool(entry.get("_time_str")) for entry in timeline if entry.get("node_type") == "Action")
        trajectory["_timing_coverage"] = {
            "timed": timed_states + timed_actions,
            "total": state_count + action_count,
            "states": timed_states,
            "state_total": state_count,
            "actions": timed_actions,
            "action_total": action_count,
            "llm_count_matched": use_llm_turns,
        }

    def generate(self, results: dict[str, Any], *, user_id: str, session_id: str) -> str:
        """Render the full HTML report as a string."""
        trajectory = results.get("trajectory", _error_dict("No trajectory data"))
        time_analysis = results.get("time", _error_dict("No performance timing data"))
        token_analysis = results.get("token", _error_dict("No performance token data"))
        subagents = results.get("subagents", _error_dict("No subagent data"))
        logs = results.get("logs", _error_dict("No log data"))
        self._enrich_timeline_with_timestamps(trajectory, logs)
        self._enrich_timeline_with_subagent_matches(trajectory, subagents)
        template = self._env.get_template("report.html")
        return template.render(
            trajectory=trajectory,
            time_analysis=time_analysis,
            token_analysis=token_analysis,
            subagents=subagents,
            logs=logs,
            user_id=user_id,
            session_id=session_id,
            generated_at=datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            analyzer_manifest=results.get("_manifest", []),
        )

    def generate_file(self, results: dict[str, Any], *, user_id: str, session_id: str, output: Path) -> Path:
        """Render report and write to *output*."""
        html = self.generate(results, user_id=user_id, session_id=session_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")
        public_results: dict[str, Any] = {}
        for key, value in results.items():
            if not key.startswith("_"):
                public_results[key] = value
        payload = {
            "schema_version": "1",
            "user_id": user_id,
            "session_id": session_id,
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "analyzers": results.get("_manifest", []),
            "results": public_results,
        }
        output.with_name("report-data.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return output


def _error_dict(msg: str) -> dict[str, Any]:
    return {"error": msg}


def _format_latency(ms: int) -> str:
    """Format latency in a human-readable way."""
    if ms < 1000:
        return f"{ms}ms"
    elif ms < 60000:
        return f"{ms / 1000:.1f}s"
    else:
        return f"{ms / 60000:.1f}m"
