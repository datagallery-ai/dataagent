from __future__ import annotations

import json
from datetime import datetime

from scripts.analyzer.base import AnalyzerRegistry
from scripts.analyzer.logs import LogAnalyzer
from scripts.analyzer.report import HTMLReportGenerator, _truncate_expandable_filter
from scripts.analyzer.trajectory import TrajectoryAnalyzer


def _timestamp(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def test_expandable_text_keeps_quotes_and_newlines_out_of_attributes() -> None:
    value = '"bash -c \\"echo hello\\"\\nline two"'

    rendered = str(_truncate_expandable_filter(None, value, length=10))

    assert "data-full=" not in rendered
    assert '<span class="tr-expand">' in rendered
    assert 'class="tr-toggle"' in rendered
    assert 'class="tr-full" hidden' in rendered
    assert "toggleTruncated" not in rendered
    assert "onclick=" not in rendered
    assert "line two" in rendered
    assert rendered.count("<button") == 1


def test_report_renders_sidebar_index_for_both_tabs() -> None:
    results = {
        "trajectory": {
            "turn_count": 1,
            "total_rounds": 1,
            "total_actions": 0,
            "failed_actions": [],
            "node_type_counts": {"Query": 1},
            "tool_stats": [],
            "queries": [{"run_id": 0, "query": "hello", "id": "Query(q0)"}],
            "timeline": [
                {
                    "node_type": "Query",
                    "run_id": 0,
                    "id": "Query(q0)",
                    "query": "hello",
                    "anchor_id": "timeline-query-0",
                }
            ],
            "runs": [],
            "messages_context": None,
        },
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": ["session.log"],
            "session_id_in_logs": True,
        },
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="session")

    assert 'class="report-index"' in html
    assert 'href="#section-timeline"' in html
    assert 'href="#section-logs"' in html
    assert 'href="#timeline-query-0"' in html
    assert 'id="section-timeline"' in html
    assert 'id="timeline-query-0"' in html
    assert 'id="section-logs"' in html
    assert 'data-tab="time"' in html
    assert 'data-tab="token"' in html
    assert "记录时未开启 performance 采集" not in html
    assert "No performance timing data" in html
    assert "No performance token data" in html


def test_report_renders_trajectory_search_for_collapsed_timeline_content() -> None:
    results = {
        "trajectory": {
            "turn_count": 1,
            "total_rounds": 1,
            "total_actions": 1,
            "failed_actions": [],
            "node_type_counts": {"Action": 1},
            "tool_stats": [],
            "queries": [],
            "timeline": [
                {
                    "node_type": "Action",
                    "id": "Action(call_00_secret)",
                    "run_id": 0,
                    "action": "bash",
                    "success": True,
                    "params": {"command": "echo hidden-needle"},
                    "output": "hidden-output-needle",
                    "data_nodes": [],
                    "anchor_id": "timeline-action-0-call_00_secret",
                }
            ],
            "runs": [],
            "messages_context": None,
        },
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": [],
            "session_id_in_logs": True,
        },
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="session")

    assert 'id="trajectorySearch"' in html
    assert "searchTrajectory()" in html
    assert "jumpTrajectoryMatch(1)" in html
    assert "hidden-output-needle" in html
    assert "including collapsed Action details" in html


def test_report_renders_failed_actions_without_subagent_item_scope() -> None:
    results = {
        "trajectory": {
            "turn_count": 1,
            "total_rounds": 1,
            "total_actions": 1,
            "failed_actions": [
                {
                    "tool": "broken_tool",
                    "run_id": 0,
                    "anchor_id": "timeline-action-0-broken",
                    "param_lines": [{"key": "query", "value": '"hello"'}],
                    "output": "error happened",
                }
            ],
            "node_type_counts": {"Action": 1},
            "tool_stats": [],
            "queries": [],
            "timeline": [],
            "runs": [],
            "messages_context": None,
        },
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": [],
            "session_id_in_logs": True,
        },
        "subagents": {"subagent_count": 0, "summary": {}, "subagents": []},
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="session")

    assert "broken_tool" in html
    assert 'href="#timeline-action-0-broken"' in html
    assert "item.anchor_id" not in html


def test_report_renders_run_tabs_and_percentage_controls() -> None:
    results = {
        "trajectory": {"error": "none"},
        "logs": {"error": "none"},
        "time": {
            "overview": {
                "wall_duration": "1 s",
                "active_duration": "1 s",
                "start_time": "00:00:00.000",
                "end_time": "00:00:01.000",
                "run_count": 1,
                "process_count": 1,
                "event_count": 1,
                "failed_count": 0,
            },
            "scope_note": "nested",
            "breakdowns": {"hook": [], "llm": [], "node": [], "tool": []},
            "quality": {
                "file_count": 1,
                "flush_count": 1,
                "malformed_count": 0,
                "missing_timestamp_count": 0,
                "incomplete_files": [],
            },
            "timeline_runs": [{"run_id": "0", "event_count": 1, "height": 360}],
            "timeline_by_run": {
                "0": [
                    {
                        "kind": "llm",
                        "name": "model",
                        "start_ms": 0,
                        "elapsed_ms": 1000,
                        "start_time": "00:00:00.000",
                        "duration": "1 s",
                        "success": True,
                    }
                ]
            },
            "runs": [
                {
                    "run_id": "0",
                    "pids": "1",
                    "complete": True,
                    "wall_duration": "1 s",
                    "active_duration": "1 s",
                    "turn_count": 1,
                    "event_count": 1,
                    "failed_count": 0,
                }
            ],
            "components": [
                {
                    "kind": "llm",
                    "name": "model",
                    "count": 1,
                    "total_duration": "1 s",
                    "kind_share": 100,
                    "avg_duration": "1 s",
                    "p50_duration": "1 s",
                    "p95_duration": "1 s",
                    "max_duration": "1 s",
                    "failures": 0,
                }
            ],
            "turns_by_run": {
                "0": [
                    {
                        "turn": 1,
                        "start_time": "00:00:00.000",
                        "llm": "model",
                        "llm_duration": "1 s",
                        "tool_count": 0,
                        "tool_wall_duration": "0 ms",
                        "tool_work_ms": 0,
                        "turn_duration": "1 s",
                    }
                ]
            },
            "slow_events": [],
        },
        "token": {"error": "none"},
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="session")

    assert 'class="run-panel timeline-run-panel active"' in html
    assert 'class="run-panel turns-run-panel active"' in html
    assert 'data-run="0"' in html
    assert "Share in Kind" in html
    assert "100.0%" in html
    assert "percentageLabelsPlugin" in html
    assert "options.display !== true" in html
    assert "options.position === 'segmentCenter'" in html
    assert html.count("percentageLabels:{display:true") == 1


def test_timeline_assigns_query_anchor_ids_in_numeric_run_order(tmp_path) -> None:
    context_dir = tmp_path / ".context"
    context_dir.mkdir()
    for run_id in (10, 2, 0, 1):
        (context_dir / f"Run{run_id}_Sub0.json").write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": f"Query(q{run_id})",
                            "node_type": "Query",
                            "run_id": run_id,
                            "query": f"query {run_id}",
                        }
                    ],
                    "edges": [],
                }
            ),
            encoding="utf-8",
        )

    result = TrajectoryAnalyzer().analyze(tmp_path)
    queries = [entry for entry in result["timeline"] if entry["node_type"] == "Query"]

    assert [entry["run_id"] for entry in queries] == [0, 1, 2, 10]
    assert [entry["anchor_id"] for entry in queries] == [
        "timeline-query-0",
        "timeline-query-1",
        "timeline-query-2",
        "timeline-query-3",
    ]


def test_action_params_accept_scalar_and_json_string() -> None:
    timeline, _ = TrajectoryAnalyzer._build_timeline(
        [
            {
                "node_type": "Action",
                "id": "Action(a0)",
                "run_id": 0,
                "action": "bash",
                "params": '{"command":"pwd"}',
            },
            {
                "node_type": "Action",
                "id": "Action(a1)",
                "run_id": 0,
                "action": "bash",
                "params": "plain text",
                "success": False,
            },
        ]
    )
    failed = TrajectoryAnalyzer._collect_failed_actions([{"action": "bash", "params": "plain text", "success": False}])

    assert timeline[0]["params"] == {"command": "pwd"}
    assert timeline[1]["params"] == {"value": "plain text"}
    assert failed[0]["param_lines"] == [{"key": "value", "value": '"plain text"'}]


def test_actions_and_failures_share_stable_deep_link_anchor() -> None:
    node = {
        "node_type": "Action",
        "id": "Action(call_00_exact)",
        "run_id": 2,
        "action": "bash",
        "success": False,
    }

    timeline, _ = TrajectoryAnalyzer._build_timeline([node])
    failed = TrajectoryAnalyzer._collect_failed_actions([node])

    assert timeline[0]["tool_call_id"] == "call_00_exact"
    assert timeline[0]["anchor_id"] == "timeline-action-2-call_00_exact"
    assert failed[0]["anchor_id"] == timeline[0]["anchor_id"]


def test_generate_file_writes_machine_readable_report_data(tmp_path) -> None:
    output = tmp_path / "report.html"
    results = {
        "trajectory": {"error": "none"},
        "time": {"error": "none"},
        "token": {"error": "none"},
        "logs": {"error": "none"},
        "_manifest": AnalyzerRegistry.manifest(),
    }

    HTMLReportGenerator().generate_file(results, user_id="anonymous", session_id="session", output=output)

    payload = json.loads((tmp_path / "report-data.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert payload["session_id"] == "session"
    assert "_manifest" not in payload["results"]
    assert [item["name"] for item in payload["analyzers"]] == [
        "trajectory",
        "time",
        "token",
        "subagents",
        "logs",
    ]


def test_state_node_maps_legacy_state_to_content() -> None:
    timeline, _ = TrajectoryAnalyzer._build_timeline(
        [
            {
                "node_type": "State",
                "id": "State(old)",
                "run_id": 0,
                "state": "**legacy markdown**",
            }
        ]
    )

    assert timeline[0]["content"] == "**legacy markdown**"
    assert timeline[0]["reasoning_content"] == ""
    assert timeline[0]["state_details"] == {}
    assert "state" not in timeline[0]


def test_state_node_exposes_all_non_empty_fields_and_future_fields() -> None:
    timeline, _ = TrajectoryAnalyzer._build_timeline(
        [
            {
                "node_type": "State",
                "id": "State(new)",
                "run_id": 0,
                "content": "main content",
                "reasoning_content": "reasoning",
                "goal": "finish analysis",
                "belief": "",
                "available_actions": ["search", "write"],
                "future_field": {"enabled": True},
                "feedback": None,
            }
        ]
    )

    state = timeline[0]
    details = state["state_details"]
    assert state["reasoning_content"] == "reasoning"
    assert state["content"] == "main content"
    assert list(details) == ["goal", "available_actions", "future_field"]
    assert details["available_actions"] == ["search", "write"]
    assert details["future_field"] == {"enabled": True}
    assert "belief" not in details
    assert "feedback" not in details


def test_state_node_prefers_new_content_when_both_schemas_exist() -> None:
    state = TrajectoryAnalyzer._state_entry({"state": "legacy", "content": "new content", "goal": "goal"})

    assert state["content"] == "new content"


def test_report_renders_state_content_and_non_empty_fields() -> None:
    results = {
        "trajectory": {
            "turn_count": 1,
            "total_rounds": 1,
            "total_actions": 0,
            "failed_actions": [],
            "node_type_counts": {"State": 1},
            "tool_stats": [],
            "queries": [],
            "timeline": [
                {
                    "node_type": "State",
                    "id": "State(new)",
                    "run_id": 0,
                    "reasoning_content": "step by step",
                    "content": "# Current state",
                    "state_details": {"goal": "finish"},
                }
            ],
            "runs": [],
            "messages_context": None,
        },
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": [],
            "session_id_in_logs": True,
        },
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="session")

    reasoning_pos = html.index("Reasoning Content")
    content_pos = html.index('<div class="tl-state-field-label">Content</div>')
    details_pos = html.index("State Fields")
    assert reasoning_pos < content_pos < details_pos
    assert '<div class="tl-state-content state-md"># Current state</div>' in html
    assert '"goal": "finish"' in html


def test_parallel_action_latency_uses_parent_state() -> None:
    state_ts = _timestamp("2026-06-10 16:01:32.000")
    action_one_ts = _timestamp("2026-06-10 16:01:34.000")
    action_two_ts = _timestamp("2026-06-10 16:01:35.000")
    trajectory = {
        "timeline": [
            {"node_type": "State", "id": "State(state00000)"},
            {
                "node_type": "Action",
                "id": "Action(call_00_first)",
                "parent_state_id": "State(state00000)",
            },
            {
                "node_type": "Action",
                "id": "Action(call_01_second)",
                "parent_state_id": "State(state00000)",
            },
        ]
    }
    logs = {
        "node_updates": {
            "State(state00000)": {"timestamp": state_ts},
            "Action(call_00_first)": {"timestamp": action_one_ts},
            "Action(call_01_second)": {"timestamp": action_two_ts},
        },
    }

    HTMLReportGenerator._enrich_timeline_with_timestamps(trajectory, logs)

    first_action = trajectory["timeline"][1]
    second_action = trajectory["timeline"][2]
    assert first_action["_latency_str"] == "2.0s"
    assert second_action["_latency_str"] == "3.0s"
    assert trajectory["timeline"][0]["_time_source"] == "State modify_node time (fallback)"


def test_state_uses_llm_completion_before_later_modify_time() -> None:
    emitted_ts = _timestamp("2026-06-10 16:01:32.000")
    modified_ts = _timestamp("2026-06-10 16:01:35.000")
    action_ts = _timestamp("2026-06-10 16:01:32.250")
    trajectory = {
        "timeline": [
            {"node_type": "State", "id": "State(state00000)", "run_id": 0},
            {
                "node_type": "Action",
                "id": "Action(call_00_first)",
                "run_id": 0,
                "parent_state_id": "State(state00000)",
            },
        ]
    }
    logs = {
        "llm_turns": [{"timestamp": emitted_ts}],
        "node_updates": {
            "State(state00000)": {"timestamp": modified_ts},
            "Action(call_00_first)": {"timestamp": action_ts},
        },
    }

    HTMLReportGenerator._enrich_timeline_with_timestamps(trajectory, logs)

    state, action = trajectory["timeline"]
    assert state["_time_str"] == "16:01:32.000"
    assert state["_modify_time_str"] == "16:01:35.000"
    assert action["_latency_str"] == "250ms"


def test_timeline_never_interpolates_missing_timestamps() -> None:
    trajectory = {
        "timeline": [
            {"node_type": "State", "id": "State(state00000)", "run_id": 0},
            {"node_type": "State", "id": "State(state00001)", "run_id": 0},
            {
                "node_type": "Action",
                "id": "Action(call_00_missing)",
                "run_id": 0,
                "parent_state_id": "State(state00001)",
            },
        ]
    }
    logs = {
        "session_start_ts": _timestamp("2026-06-10 16:00:00.000"),
        "session_end_ts": _timestamp("2026-06-10 16:03:00.000"),
        # Count mismatch means this record cannot be mapped safely.
        "llm_turns": [{"timestamp": _timestamp("2026-06-10 16:01:00.000")}],
        "node_updates": {},
    }

    HTMLReportGenerator._enrich_timeline_with_timestamps(trajectory, logs)

    assert all(not entry["_time_str"] for entry in trajectory["timeline"])
    assert all(not entry["_latency_str"] for entry in trajectory["timeline"])
    assert trajectory["_timing_coverage"] == {
        "timed": 0,
        "total": 3,
        "states": 0,
        "state_total": 2,
        "actions": 0,
        "action_total": 1,
        "llm_count_matched": False,
    }


def test_report_links_whitelisted_action_to_subagent_using_time() -> None:
    state_ts = _timestamp("2026-06-04 12:13:21.000")
    action_ts = _timestamp("2026-06-04 12:13:53.000")
    trajectory = {
        "turn_count": 1,
        "total_rounds": 1,
        "total_actions": 1,
        "failed_actions": [],
        "node_type_counts": {"State": 1, "Action": 1},
        "tool_stats": [],
        "queries": [],
        "timeline": [
            {"node_type": "State", "id": "State(state00000)", "run_id": 0, "content": "ready"},
            {
                "node_type": "Action",
                "id": "Action(call_00_recall)",
                "run_id": 0,
                "parent_state_id": "State(state00000)",
                "action": "document_recall_tool",
                "success": True,
                "params": {"query": "如何连接企业语义引擎"},
                "output": "/tmp/session/recall_result.json",
                "data_nodes": [],
                "anchor_id": "timeline-action-0-call_00_recall",
            },
        ],
        "runs": [],
        "messages_context": None,
    }
    results = {
        "trajectory": trajectory,
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": [],
            "session_id_in_logs": True,
            "node_updates": {
                "State(state00000)": {"timestamp": state_ts},
                "Action(call_00_recall)": {"timestamp": action_ts},
            },
        },
        "subagents": {
            "subagent_count": 1,
            "summary": {},
            "subagents": [
                {
                    "session_id": "subagent_parent_396212",
                    "sub_id": "396212",
                    "display_name": "document_recall_sub_agent",
                    "anchor_id": "subagent-row-subagent_parent_396212",
                    "report_href": "subagent_reports/subagent_parent_396212/index.html",
                    "report_exists": True,
                    "last_query": "如何连接企业语义引擎",
                    "evidence_paths": ["/tmp/session/recall_result.json"],
                    "start_ts": _timestamp("2026-06-04 12:13:24.000"),
                    "end_ts": _timestamp("2026-06-04 12:13:52.000"),
                    "start_time": "2026-06-04 12:13:24",
                    "end_time": "2026-06-04 12:13:52",
                    "duration": "28s",
                    "turn_count": 1,
                    "total_tokens_display": "100",
                }
            ],
        },
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="parent")
    action = trajectory.get("timeline", [])[1]

    assert action.get("subagent_match", {}).get("sub_id") == "396212"
    assert action.get("subagent_match", {}).get("display_name") == "document_recall_sub_agent"
    assert "confidence" not in action.get("subagent_match", {})
    assert "decision_reason" not in action.get("subagent_match", {})
    assert 'href="#subagent-row-subagent_parent_396212"' in html
    assert 'id="subagent-row-subagent_parent_396212"' in html
    assert "Subagent Match" in html


def test_subagent_match_links_metadata_recall_but_rejects_read_file() -> None:
    state_ts = _timestamp("2026-06-04 12:13:21.000")
    tool_ts = _timestamp("2026-06-04 12:13:53.000")
    read_ts = _timestamp("2026-06-04 12:13:54.000")
    trajectory = {
        "turn_count": 1,
        "total_rounds": 1,
        "total_actions": 2,
        "failed_actions": [],
        "node_type_counts": {"State": 1, "Action": 2},
        "tool_stats": [],
        "queries": [],
        "timeline": [
            {"node_type": "State", "id": "State(state00000)", "run_id": 0, "content": "ready"},
            {
                "node_type": "Action",
                "id": "Action(call_00_metadata)",
                "run_id": 0,
                "parent_state_id": "State(state00000)",
                "action": "metadata_recall",
                "success": True,
                "params": {"query": "如何连接企业语义引擎"},
                "output": "/tmp/session/recall_result.json",
                "data_nodes": [],
            },
            {
                "node_type": "Action",
                "id": "Action(call_01_read)",
                "run_id": 0,
                "parent_state_id": "State(state00000)",
                "action": "read_file",
                "success": True,
                "params": {"path": "/tmp/session/recall_result.json"},
                "output": "如何连接企业语义引擎",
                "data_nodes": [],
            },
        ],
        "runs": [],
        "messages_context": None,
    }
    results = {
        "trajectory": trajectory,
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": [],
            "session_id_in_logs": True,
            "node_updates": {
                "State(state00000)": {"timestamp": state_ts},
                "Action(call_00_metadata)": {"timestamp": tool_ts},
                "Action(call_01_read)": {"timestamp": read_ts},
            },
        },
        "subagents": {
            "subagent_count": 1,
            "summary": {},
            "subagents": [
                {
                    "session_id": "subagent_parent_396212",
                    "sub_id": "396212",
                    "display_name": "custom_sub_agent",
                    "anchor_id": "subagent-row-subagent_parent_396212",
                    "report_href": "subagent_reports/subagent_parent_396212/index.html",
                    "last_query": "如何连接企业语义引擎",
                    "evidence_paths": ["/tmp/session/recall_result.json"],
                    "start_ts": _timestamp("2026-06-04 12:13:24.000"),
                    "end_ts": _timestamp("2026-06-04 12:13:52.000"),
                }
            ],
        },
    }

    HTMLReportGenerator().generate(results, user_id="anonymous", session_id="parent")
    metadata_action = trajectory.get("timeline", [])[1]
    read_action = trajectory.get("timeline", [])[2]

    assert metadata_action.get("subagent_match", {}).get("sub_id") == "396212"
    assert "subagent_match" not in read_action
    assert "subagent_candidates" not in read_action
    assert "subagent_match_debug" not in read_action


def test_timeline_uses_graph_edges_for_action_and_data_parents() -> None:
    nodes = [
        {"node_type": "State", "id": "State(s0)", "run_id": 0, "state": "ready"},
        {
            "node_type": "Action",
            "id": "Action(a0)",
            "run_id": 0,
            "action": "one",
        },
        {
            "node_type": "Action",
            "id": "Action(a1)",
            "run_id": 0,
            "action": "two",
        },
        {"node_type": "File", "id": "File(f0)", "run_id": 0, "path": "one.txt"},
        {"node_type": "File", "id": "File(f1)", "run_id": 0, "path": "two.txt"},
    ]
    links = [
        {"source": "State(s0)", "target": "Action(a0)", "_run_id": 0},
        {"source": "State(s0)", "target": "Action(a1)", "_run_id": 0},
        {"source": "Action(a0)", "target": "File(f0)", "_run_id": 0},
        {"source": "Action(a1)", "target": "File(f1)", "_run_id": 0},
    ]

    timeline, _ = TrajectoryAnalyzer._build_timeline(nodes, links)
    actions = [entry for entry in timeline if entry["node_type"] == "Action"]

    assert [entry["parent_state_id"] for entry in actions] == [
        "State(s0)",
        "State(s0)",
    ]
    assert actions[0]["data_nodes"][0]["path"] == "one.txt"
    assert actions[1]["data_nodes"][0]["path"] == "two.txt"


def test_parse_run_file_accepts_networkx_edges_key(tmp_path) -> None:
    context_file = tmp_path / "Run0_Sub0.json"
    context_file.write_text(
        '{"nodes":[{"id":"State(s0)","node_type":"State","run_id":0}],'
        '"edges":[{"source":"State(s0)","target":"Action(a0)"}]}',
        encoding="utf-8",
    )

    parsed = TrajectoryAnalyzer()._parse_run_file(context_file)

    assert parsed["links"] == [{"source": "State(s0)", "target": "Action(a0)"}]


def test_trajectory_sorts_runs_numerically(tmp_path) -> None:
    context_dir = tmp_path / ".context"
    context_dir.mkdir()
    for run_id in (0, 10, 2, 1):
        (context_dir / f"Run{run_id}_Sub0.json").write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": f"Query(q{run_id})",
                            "node_type": "Query",
                            "run_id": str(run_id),
                            "query": f"query {run_id}",
                        }
                    ],
                    "edges": [],
                }
            ),
            encoding="utf-8",
        )

    result = TrajectoryAnalyzer().analyze(tmp_path)

    assert [run["run_id"] for run in result["runs"]] == ["0", "1", "2", "10"]
    assert [query["run_id"] for query in result["queries"]] == ["0", "1", "2", "10"]
    assert [entry["run_id"] for entry in result["timeline"]] == ["0", "1", "2", "10"]


def test_session_local_logs_override_explicit_and_global_sources(tmp_path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    local_log = session / "session.log"
    local_log.write_text(
        "2020-01-01 00:00:00.000 | WARNING  | local:test:1 | local warning\n",
        encoding="utf-8",
    )
    external = tmp_path / "external"
    external.mkdir()
    (external / "external.log").write_text(
        "2026-06-13 00:00:00.000 | ERROR    | external:test:1 | external error\n",
        encoding="utf-8",
    )

    result = LogAnalyzer().analyze(session, log_dir=external)

    assert result["match_mode"] == "session_local"
    assert result["matched_log_files"] == ["session.log"]
    assert result["warning_count"] == 1
    assert result["error_count"] == 0


def test_log_analyzer_reads_user_level_logs_with_process_column(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    session = user_root / "20260604_121316_test-user-level-logs-session-000001"
    context = session / ".context"
    context.mkdir(parents=True)
    (context / "Run0_Sub0.json").write_text('{"nodes":[]}', encoding="utf-8")
    log_root = user_root / "logs"
    log_root.mkdir()
    log_file = log_root / f"{session.name}.log"
    log_file.write_text(
        f"2026-06-04 12:13:16.100 | INFO     | main | module:start:1 | session_id={session.name}\n"
        "2026-06-04 12:13:18.200 | WARNING  | main | module:warn:2 | process-column warning\n",
        encoding="utf-8",
    )

    result = LogAnalyzer().analyze(session)

    assert result["match_mode"] == "intersection"
    assert result["matched_log_files"] == [log_file.name]
    assert result["warning_count"] == 1
    assert result["warnings"][0]["message"] == "process-column warning"
    assert result["session_duration_seconds"] == 2.1


def test_log_analyzer_uses_batch_log_file_index(tmp_path) -> None:
    session = tmp_path / "session_a"
    context = session / ".context"
    context.mkdir(parents=True)
    (context / "Run0_Sub0.json").write_text('{"nodes":[]}', encoding="utf-8")
    log_root = tmp_path / "logs"
    log_root.mkdir()
    indexed_log = log_root / "indexed.log"
    indexed_log.write_text(
        "2026-06-13 00:00:00.000 | ERROR    | indexed:test:1 | indexed error\n",
        encoding="utf-8",
    )
    ignored_log = log_root / "ignored.log"
    ignored_log.write_text(
        "2026-06-13 00:00:01.000 | ERROR    | ignored:test:1 | ignored error\n",
        encoding="utf-8",
    )

    result = LogAnalyzer().analyze(session, log_dir=log_root, log_file_index={session.name: [indexed_log]})

    assert result["match_mode"] == "intersection"
    assert result["matched_log_files"] == ["indexed.log"]
    assert result["error_count"] == 1
    assert "indexed.log" in result["by_file"]
    assert "ignored.log" not in result["by_file"]


def test_subagent_log_analyzer_excludes_parent_log_from_index(tmp_path) -> None:
    parent_id = "20260604_121316_e93a436f-6169-487b-acb1-30749d03edc5"
    subagent_id = f"subagent_{parent_id}_396212"
    session = tmp_path / "anonymous" / subagent_id
    context = session / ".context"
    context.mkdir(parents=True)
    (context / "Run0_Sub0.json").write_text('{"nodes":[]}', encoding="utf-8")
    log_root = tmp_path / "anonymous" / "logs"
    log_root.mkdir()
    parent_log = log_root / f"{parent_id}.log"
    subagent_log = log_root / f"{subagent_id}_396212.log"
    parent_log.write_text(
        f"2026-06-04 12:13:20.000 | ERROR    | parent:test:1 | parent mentions {subagent_id}\n",
        encoding="utf-8",
    )
    subagent_log.write_text(
        f"2026-06-04 12:13:21.000 | WARNING  | subagent | child:test:1 | child owns {subagent_id}\n",
        encoding="utf-8",
    )

    result = LogAnalyzer().analyze(session, log_dir=log_root, log_file_index={subagent_id: [parent_log, subagent_log]})

    assert result["match_mode"] == "intersection"
    assert result["matched_log_files"] == [subagent_log.name]
    assert result["warning_count"] == 1
    assert result["error_count"] == 0
    assert parent_log.name not in result["by_file"]


def test_action_update_timestamp_is_keyed_by_full_action_id(tmp_path) -> None:
    log_file = tmp_path / "session.log"
    log_file.write_text(
        "2026-06-10 16:01:34.123 | DEBUG    | "
        "dataagent.core.context.context_trajectory:modify_node:530 | "
        "Context: Modifying node=Action(call_00_exact) with "
        "changes={'output': 'done', 'success': True}\n",
        encoding="utf-8",
    )

    updates = LogAnalyzer._extract_action_update_timestamps([log_file])

    assert set(updates) == {"Action(call_00_exact)"}
    assert updates["Action(call_00_exact)"]["timestamp_str"] == "2026-06-10 16:01:34.123"


def test_node_update_timestamp_maps_state_and_action_ids(tmp_path) -> None:
    log_file = tmp_path / "session.log"
    log_file.write_text(
        "2026-06-10 16:01:32.803 | DEBUG    | module:modify_node:522 | "
        "Context: Modifying node=State(state00000) with changes={'state': 'ready'}\n"
        "2026-06-10 16:01:45.804 | DEBUG    | module:modify_node:522 | "
        "Context: Modifying node=Action(call_00_exact) with "
        "changes={'output': 'done', 'success': True}\n",
        encoding="utf-8",
    )

    updates = LogAnalyzer._extract_node_update_timestamps([log_file], state_values={"State(state00000)": "ready"})

    assert updates["State(state00000)"]["timestamp_str"] == "2026-06-10 16:01:32.803"
    assert updates["Action(call_00_exact)"]["timestamp_str"] == "2026-06-10 16:01:45.804"


def test_state_update_rejects_same_id_with_different_content(tmp_path) -> None:
    log_file = tmp_path / "concurrent.log"
    log_file.write_text(
        "2026-06-10 16:01:31.000 | DEBUG    | module:modify_node:522 | "
        "Context: Modifying node=State(state00000) with changes={'state': 'other session'}\n"
        "2026-06-10 16:01:32.000 | DEBUG    | module:modify_node:522 | "
        "Context: Modifying node=State(state00000) with changes={'state': 'this session'}\n",
        encoding="utf-8",
    )

    updates = LogAnalyzer._extract_node_update_timestamps(
        [log_file], state_values={"State(state00000)": "this session"}
    )

    assert updates["State(state00000)"]["timestamp_str"] == "2026-06-10 16:01:32.000"


def test_time_window_uses_dated_parent_directory_in_local_time(tmp_path) -> None:
    session = tmp_path / "bench_20260516_161339_n4r3" / "bench_n4_r3_q27"

    window = LogAnalyzer._infer_time_window_from_dirname(session)

    assert window is not None
    expected = datetime.strptime("20260516_161339", "%Y%m%d_%H%M%S").timestamp()
    assert window == (expected - 60, expected + 21600)
