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
"""Trajectory analyzer — parses NetworkX node-link JSON to produce session stats."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from scripts.analyzer.base import AnalyzerSpec, BaseAnalyzer
from scripts.analyzer.scope import AnalysisScope

# Node types that are DataNodes (output artifacts) rather than logic nodes
_DATA_NODE_TYPES = frozenset({"File", "Table", "Column", "Script", "Skill", "Knowledge", "Tool"})
_STATE_DETAIL_FIELD_ORDER = (
    "goal",
    "belief",
    "action_history",
    "current_status",
    "available_actions",
    "feedback",
    "uncertainty",
    "uncentainty",
    "description",
)
_STATE_INTERNAL_FIELDS = frozenset({"id", "node_type", "run_id", "state", "content", "reasoning_content"})
_RUN_FILE_RE = re.compile(r"^Run(\d+)(?:_Sub(\d+))?")


def _run_file_sort_key(path: Path) -> tuple[int, int, str]:
    """Sort Run2 before Run10 while keeping unexpected names deterministic."""
    match = _RUN_FILE_RE.match(path.stem)
    if not match:
        return (2**31 - 1, 2**31 - 1, path.name)
    return (int(match.group(1)), int(match.group(2) or 0), path.name)


def _run_id_sort_key(value: Any) -> tuple[int, int | str]:
    """Sort numeric run ids numerically and place non-numeric ids afterwards."""
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, "" if value is None else str(value))


def _tool_stat_sort_key(item: tuple[str, dict[str, int]]) -> int:
    return -int(item[1].get("calls", 0))


def _query_run_sort_key(node: dict[str, Any]) -> tuple[int, int | str]:
    return _run_id_sort_key(node.get("run_id"))


class TrajectoryAnalyzer(BaseAnalyzer):
    """Parse session trajectory JSON files and compute tool call statistics."""

    name = "trajectory"
    description = "Trajectory summary: rounds, tool calls, failures"
    spec = AnalyzerSpec(
        name=name,
        title="Trajectory",
        order=10,
        description=description,
        data_sources=(".context/Run*.json", ".memory/messages.json"),
        template="trajectory",
    )

    # ── regular public methods ────────

    @staticmethod
    def _group_by_type(nodes: list[dict]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for node in nodes:
            node_type = node.get("node_type", "Unknown")
            grouped_nodes = grouped.get(node_type)
            if grouped_nodes is None:
                grouped_nodes = []
                grouped[node_type] = grouped_nodes
            grouped_nodes.append(node)
        return dict(grouped)

    @staticmethod
    def _compute_tool_stats(actions: list[dict]) -> list[dict]:
        counters: dict[str, dict[str, int]] = {}
        for action in actions:
            name = action.get("action", "unknown")
            counter = counters.get(name, {"calls": 0, "failures": 0})
            counter["calls"] = counter.get("calls", 0) + 1
            if not action.get("success", True):
                counter["failures"] = counter.get("failures", 0) + 1
            counters[name] = counter

        stats: list[dict[str, Any]] = []
        for name, counter in sorted(counters.items(), key=_tool_stat_sort_key):
            calls = counter.get("calls", 0)
            failures = counter.get("failures", 0)
            success_rate = round((1 - failures / calls) * 100, 1) if calls else 100
            stats.append(
                {
                    "tool": name,
                    "calls": calls,
                    "failures": failures,
                    "success_rate": success_rate,
                }
            )
        return stats

    @staticmethod
    def _normalize_params(params: Any) -> dict[str, Any]:
        """Normalize legacy/scalar action params for row-based rendering."""
        if isinstance(params, dict):
            return params
        if isinstance(params, str):
            try:
                parsed = json.loads(params)
            except json.JSONDecodeError:
                parsed = params
            if isinstance(parsed, dict):
                return parsed
            params = parsed
        return {"value": params}

    @staticmethod
    def _tool_call_id(node_id: Any) -> str:
        value = str(node_id or "")
        return value[7:-1] if value.startswith("Action(") and value.endswith(")") else value

    @staticmethod
    def _collect_failed_actions(actions: list[dict]) -> list[dict]:
        failed = []
        for a in actions:
            if not a.get("success", True):
                output = a.get("output", "")
                params = TrajectoryAnalyzer._normalize_params(a.get("params", {}))
                param_lines: list[dict] = []
                for k, v in params.items():
                    param_lines.append({"key": k, "value": json.dumps(v, ensure_ascii=False)})
                failed.append(
                    {
                        "tool": a.get("action", "unknown"),
                        "params": params,
                        "param_lines": param_lines,
                        "output": str(output)[:1000],
                        "output_lines": str(output).split("\n"),
                        "run_id": a.get("run_id"),
                        "label": a.get("id", a.get("label", "")),
                        "description": a.get("description", ""),
                        "anchor_id": TrajectoryAnalyzer._action_anchor(a.get("run_id"), a.get("id", "")),
                    }
                )
        return failed

    @staticmethod
    def _data_node_entry(node: dict) -> dict:
        nt = node.get("node_type", "Unknown")
        entry: dict = {
            "node_type": nt,
            "id": node.get("id", ""),
            "label": node.get("label", ""),
        }
        if nt == "File":
            entry["path"] = node.get("path", "")
            entry["source"] = node.get("source", "")
        elif nt == "Table":
            entry["path"] = node.get("path", "")
        elif nt == "Script":
            entry["script_type"] = node.get("script_type", "")
            entry["path"] = node.get("path", "")
        elif nt == "Knowledge":
            entry["knowledge_type"] = node.get("knowledge_type", "")
        return entry

    @staticmethod
    def _has_display_value(value: Any) -> bool:
        """Return whether a StateNode field carries visible information."""
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return True

    @staticmethod
    def _read_messages_context(session_root: Path) -> Optional[list[dict]]:
        """Read messages.json and build a git-commit-style round-by-round diff."""
        msg_file = session_root / ".memory" / "messages.json"
        if not msg_file.is_file():
            return None

        try:
            data = json.loads(msg_file.read_text("utf-8"))
            messages: list[dict] = data.get("messages", [])
        except (OSError, json.JSONDecodeError):
            return None

        if not messages:
            return None

        rounds: list[dict] = []
        current_round: Optional[dict] = None
        round_idx = 0

        for msg in messages:
            mtype = msg.get("type", "")
            if mtype == "HumanMessage":
                rounds.append(
                    {
                        "round": round_idx,
                        "type": "query",
                        "content": msg.get("content", "")[:600],
                        "tool_calls": [],
                        "tool_results": [],
                        "added_count": 1,
                    }
                )
                round_idx += 1
            elif mtype == "AIMessage":
                tool_calls: list[dict[str, Any]] = []
                for tool_call in msg.get("tool_calls", []):
                    tool_calls.append({"name": tool_call.get("name", "?"), "args": tool_call.get("args", {})})
                current_round = {
                    "round": round_idx,
                    "type": "llm_response",
                    "content": msg.get("content", "")[:600],
                    "tool_calls": tool_calls,
                    "tool_results": [],
                    "added_count": 1 + len(msg.get("tool_calls", [])),
                }
                rounds.append(current_round)
                round_idx += 1
            elif mtype == "ToolMessage" and current_round is not None:
                tool_results = current_round.get("tool_results", [])
                tool_results.append(
                    {
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "name": msg.get("name", ""),
                        "content": str(msg.get("content", ""))[:400],
                    }
                )
                current_round["tool_results"] = tool_results
                current_round["added_count"] = current_round.get("added_count", 0) + 1

        cumulative = 0
        for r in rounds:
            cumulative += r.get("added_count", 1)
            r["cumulative"] = cumulative

        return rounds

    @classmethod
    def _action_anchor(cls, run_id: Any, node_id: Any) -> str:
        safe_run = re.sub(r"[^A-Za-z0-9_-]+", "-", str(run_id))
        safe_call = re.sub(r"[^A-Za-z0-9_-]+", "-", cls._tool_call_id(node_id))
        return f"timeline-action-{safe_run}-{safe_call}"

    @classmethod
    def _state_entry(cls, node: dict[str, Any]) -> dict[str, Any]:
        """Normalize legacy ``state`` and expose all non-empty State fields."""
        normalized = dict(node)
        if "state" in normalized:
            legacy_content = normalized.pop("state")
            if not cls._has_display_value(normalized.get("content")):
                normalized["content"] = legacy_content

        ordered_keys: list[str] = []
        for key in _STATE_DETAIL_FIELD_ORDER:
            if key in normalized and key not in _STATE_INTERNAL_FIELDS:
                ordered_keys.append(key)
        ordered_keys.extend(
            sorted(key for key in normalized if key not in _STATE_INTERNAL_FIELDS and key not in ordered_keys)
        )
        details: dict[str, Any] = {}
        for key in ordered_keys:
            value = normalized.get(key)
            if cls._has_display_value(value):
                details[key] = value
        return {
            "reasoning_content": normalized.get("reasoning_content", "") or "",
            "content": normalized.get("content", "") or "",
            "details": details,
        }

    @classmethod
    def _build_timeline(cls, nodes: list[dict], links: Optional[list[dict]] = None) -> tuple[list[dict], int]:
        """Build timeline with DataNodes nested under their parent ActionNodes.

        Returns (timeline_entries, turn_count).
        A turn is one State node + the Action/DataNode chain that follows it.
        """
        timeline: list[dict] = []
        turn_count = 0
        query_index = 0
        links = links or []
        action_parent: dict[tuple[Any, str], str] = {}
        data_parent: dict[tuple[Any, str], str] = {}
        for link in links:
            run_id = link.get("_run_id")
            source = link.get("source", "")
            target = link.get("target", "")
            if source.startswith("State(") and target.startswith("Action("):
                action_parent[(run_id, target)] = source
            elif source.startswith("Action("):
                data_parent[(run_id, target)] = source

        action_entries: dict[tuple[Any, str], dict] = {}

        for n in nodes:
            nt = n.get("node_type", "Unknown")
            run_id = n.get("run_id")
            node_id = n.get("id", "")

            if nt in _DATA_NODE_TYPES:
                parent_id = data_parent.get((run_id, node_id))
                parent = action_entries.get((run_id, parent_id or ""))
                if parent is not None:
                    parent.setdefault("data_nodes", []).append(cls._data_node_entry(n))
                continue

            entry: dict = {
                "node_type": nt,
                "id": node_id,
                "run_id": run_id,
                "description": n.get("description", ""),
            }

            if nt == "Action":
                entry["action"] = n.get("action", "")
                entry["success"] = n.get("success", True)
                entry["params"] = cls._normalize_params(n.get("params", {}))
                entry["output"] = str(n.get("output", ""))
                entry["data_nodes"] = []
                entry["parent_state_id"] = action_parent.get((run_id, node_id), "")
                entry["tool_call_id"] = cls._tool_call_id(node_id)
                entry["anchor_id"] = cls._action_anchor(run_id, node_id)
                action_entries[(run_id, node_id)] = entry
            elif nt == "State":
                state_entry = cls._state_entry(n)
                entry["reasoning_content"] = state_entry.get("reasoning_content", "")
                entry["content"] = state_entry.get("content", "")
                entry["state_details"] = state_entry.get("details", {})
                turn_count += 1
            elif nt == "Query":
                entry["query"] = (n.get("query", "") or "")[:500]
                entry["anchor_id"] = f"timeline-query-{query_index}"
                query_index += 1

            timeline.append(entry)

        if turn_count > 0 and timeline:
            first_state = next((t for t in timeline if t.get("node_type") == "State"), None)
            if first_state and not str(first_state.get("content", "")).strip():
                turn_count -= 1

        return timeline, max(turn_count, 0)

    def analyze(self, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Parse trajectory JSON files and compute tool-call statistics, timeline, and failures."""
        context_dir = session_root / ".context"
        if not context_dir.is_dir():
            return {"error": f"Context directory not found: {context_dir}"}

        scope = AnalysisScope.from_value(kwargs.get("analysis_scope"))
        run_files = self._select_run_files(session_root, scope)
        if not run_files:
            return {"error": f"No Run*.json files found in {context_dir}"}

        all_nodes: list[dict] = []
        all_links: list[dict] = []
        runs: list[dict] = []
        for run_file in run_files:
            run_data = self._parse_run_file(run_file)
            all_nodes.extend(run_data.get("nodes", []))
            all_links.extend({**link, "_run_id": run_data.get("run_id")} for link in run_data.get("links", []))
            runs.append(
                {
                    "file": run_file.name,
                    "run_id": run_data.get("run_id"),
                    "node_count": len(run_data.get("nodes", [])),
                    "link_count": len(run_data.get("links", [])),
                }
            )

        nodes_by_type = self._group_by_type(all_nodes)
        tool_stats = self._compute_tool_stats(nodes_by_type.get("Action", []))
        failed_actions = self._collect_failed_actions(nodes_by_type.get("Action", []))
        timeline, turn_count = self._build_timeline(all_nodes, all_links)
        query_nodes = sorted(nodes_by_type.get("Query", []), key=_query_run_sort_key)
        messages_context = None if scope and scope.is_inline else self._read_messages_context(session_root)
        unique_run_ids = {node.get("run_id") for node in all_nodes}
        queries: list[dict[str, Any]] = []
        for node in query_nodes:
            queries.append(
                {
                    "run_id": node.get("run_id"),
                    "query": node.get("query", ""),
                    "id": node.get("id", ""),
                }
            )

        return {
            "total_runs": len(run_files),
            "total_rounds": len(unique_run_ids),
            "total_nodes": len(all_nodes),
            "total_actions": len(nodes_by_type.get("Action", [])),
            "turn_count": turn_count,
            "node_type_counts": {node_type: len(nodes) for node_type, nodes in nodes_by_type.items()},
            "tool_stats": tool_stats,
            "failed_actions": failed_actions,
            "queries": queries,
            "timeline": timeline,
            "runs": runs,
            "messages_context": messages_context,
        }

    def _select_run_files(self, session_root: Path, scope: Optional[AnalysisScope]) -> list[Path]:
        if scope is not None:
            return sorted(scope.resolve_context_files(session_root), key=_run_file_sort_key)

        run_files = sorted(
            (path for path in (session_root / ".context").glob("Run*.json") if not path.name.endswith(".meta.json")),
            key=_run_file_sort_key,
        )
        has_main_context = False
        has_inline_context = False
        for path in run_files:
            match = _RUN_FILE_RE.match(path.stem)
            if match is None:
                continue
            if match.group(2) in (None, "0"):
                has_main_context = True
            else:
                has_inline_context = True
        if has_main_context and has_inline_context:
            main_files = []
            for path in run_files:
                match = _RUN_FILE_RE.match(path.stem)
                if match is not None and match.group(2) in (None, "0"):
                    main_files.append(path)
            return main_files
        return run_files

    def _parse_run_file(self, filepath: Path) -> dict[str, Any]:
        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)

        nodes = data.get("nodes", [])
        run_id: Optional[int] = None
        for node in nodes:
            rid = node.get("run_id")
            if rid is not None:
                run_id = rid
                break

        # NetworkX 3.4+ node-link data defaults to ``edges``; older dumps
        # use ``links``. Accept both so graph relationships are not lost.
        links = data.get("links")
        if links is None:
            links = data.get("edges", [])
        return {"nodes": nodes, "links": links, "run_id": run_id}
