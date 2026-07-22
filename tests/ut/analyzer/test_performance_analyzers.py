from __future__ import annotations

import json

from scripts.analyzer.performance import PerformanceDataset, interval_metrics
from scripts.analyzer.time import TimeAnalyzer
from scripts.analyzer.token import TokenAnalyzer


def _write_sample(tmp_path):
    performance_dir = tmp_path / ".performance"
    performance_dir.mkdir()
    records = [
        {
            "kind": "llm",
            "name": "planner:model-a",
            "elapsed_ms": 1000.0,
            "success": True,
            "started_at": "2026-06-13 10:00:00.000",
            "ended_at": "2026-06-13 10:00:01.000",
            "extra": {
                "caller_kind": "node",
                "caller_name": "planner",
                "call_mode": "astream",
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "tokens_per_sec": 20.0,
                "tool_call_count": 2,
                "invalid_tool_call_count": 0,
            },
        },
        {
            "kind": "tool",
            "name": "bash",
            "elapsed_ms": 500.0,
            "success": True,
            "started_at": "2026-06-13 10:00:01.100",
            "ended_at": "2026-06-13 10:00:01.600",
            "extra": {"tool_call_id": "call-a"},
        },
        {
            "kind": "tool",
            "name": "read_file",
            "elapsed_ms": 300.0,
            "success": False,
            "error_type": "ToolError",
            "started_at": "2026-06-13 10:00:01.200",
            "ended_at": "2026-06-13 10:00:01.500",
            "extra": {"tool_call_id": "call-b"},
        },
        {
            "kind": "llm",
            "name": "planner:model-a",
            "elapsed_ms": 2000.0,
            "success": True,
            "started_at": "2026-06-13 10:00:02.000",
            "ended_at": "2026-06-13 10:00:04.000",
            "extra": {
                "caller_kind": "node",
                "caller_name": "planner",
                "input_tokens": 180,
                "output_tokens": 40,
                "total_tokens": 220,
                "tokens_per_sec": 20.0,
                "tool_call_count": 0,
                "invalid_tool_call_count": 0,
            },
        },
        {
            "kind": "agent",
            "name": "FlexAgent",
            "elapsed_ms": 4500.0,
            "success": True,
            "started_at": "2026-06-13 10:00:00.000",
            "ended_at": "2026-06-13 10:00:04.500",
            "extra": {},
        },
        {
            "kind": "_flush",
            "metadata": {
                "run_id": "0",
                "pid": 42,
                "started_at": "2026-06-13 10:00:00.000",
                "ended_at": "2026-06-13 10:00:04.500",
                "e2e_ms": 4500.0,
            },
            "summary": {
                "llms": {
                    "input_tokens": 280,
                    "output_tokens": 60,
                    "total_tokens": 340,
                    "state_messages": {
                        "input_tokens": 280,
                        "output_tokens": 60,
                        "total_tokens": 340,
                    },
                }
            },
        },
    ]
    path = performance_dir / "0.42.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def test_interval_metrics_uses_union_for_parallel_events() -> None:
    events = [
        {"elapsed_ms": 1000, "_started_ts": 0.0, "_ended_ts": 1.0},
        {"elapsed_ms": 1000, "_started_ts": 0.0, "_ended_ts": 1.0},
    ]

    result = interval_metrics(events)

    assert result["active_ms"] == 1000.0
    assert result["work_ms"] == 2000.0
    assert result["parallelism"] == 2.0
    assert result["peak_concurrency"] == 2


def test_time_analyzer_separates_wall_active_and_work(tmp_path) -> None:
    _write_sample(tmp_path)

    result = TimeAnalyzer().analyze(tmp_path)

    assert result["overview"]["wall_ms"] == 4500.0
    assert result["overview"]["work_ms"] == 3800.0
    assert result["overview"]["failed_count"] == 1
    assert result["runs"][0]["turn_count"] == 2
    assert result["turns"][0]["tool_count"] == 2
    assert result["turns"][0]["tool_wall_ms"] == 500.0
    assert result["turns"][0]["tool_work_ms"] == 800.0
    assert result["turns"][0]["blocking_tool"] == "bash"
    assert result["turns"][0]["blocking_tool_call_id"] == "call-a"
    assert result["turns"][0]["critical_path_ms"] == 1600.0
    assert result["turns"][0]["target_anchor"] == "timeline-action-0-call-a"
    assert "tool_parallelism" not in result["turns"][0]
    assert {event["kind"] for event in result["timeline_by_run"]["0"]} == {"llm", "tool"}
    assert all(event["kind"] not in {"agent", "node"} for event in result["slow_events"])
    assert result["runs"][0]["pids"] == "42"
    assert result["timeline_runs"] == [{"run_id": "0", "event_count": 4, "height": 360}]
    assert len(result["turns_by_run"]["0"]) == 2
    assert result["timeline_by_run"]["0"][0]["start_ms"] == 0.0
    tool_components = {row["name"]: row for row in result["components"] if row["kind"] == "tool"}
    assert tool_components["bash"]["kind_share"] == 62.5
    assert tool_components["read_file"]["kind_share"] == 37.5


def test_token_analyzer_aggregates_and_reconciles(tmp_path) -> None:
    _write_sample(tmp_path)

    result = TokenAnalyzer().analyze(tmp_path)

    assert result["overview"]["input_tokens"] == 280
    assert result["overview"]["output_tokens"] == 60
    assert result["overview"]["total_tokens"] == 340
    assert result["overview"]["weighted_tps"] == 20.0
    assert result["turns"][1]["input_delta"] == 80
    assert all(row["footer_match"] for row in result["reconciliation"])
    assert all(row["all_events"] == row["complete_events"] for row in result["reconciliation"])


def test_token_cumulative_is_session_wide_across_runs() -> None:
    llms = [
        {
            "_run_id": "0",
            "name": "model",
            "elapsed_ms": 1,
            "extra": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        },
        {
            "_run_id": "1",
            "name": "model",
            "elapsed_ms": 1,
            "extra": {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
        },
    ]

    rows = TokenAnalyzer._turn_rows(llms)

    assert [row["turn"] for row in rows] == [1, 1]
    assert [row["cumulative_tokens"] for row in rows] == [12, 35]


def test_dataset_reports_incomplete_and_malformed_files(tmp_path) -> None:
    performance_dir = tmp_path / ".performance"
    performance_dir.mkdir()
    (performance_dir / "2.9.jsonl").write_text(
        '{"kind":"tool","name":"x","elapsed_ms":1,"success":true,'
        '"started_at":"2026-06-13 10:00:00.000","ended_at":"2026-06-13 10:00:00.001"}\n'
        "not-json\n",
        encoding="utf-8",
    )

    dataset = PerformanceDataset.load(tmp_path)

    assert dataset.quality()["malformed_count"] == 1
    assert dataset.quality()["incomplete_files"] == ["2.9.jsonl"]


def test_dataset_parses_new_run_sub_filename_and_event_identity(tmp_path) -> None:
    performance_dir = tmp_path / ".performance"
    performance_dir.mkdir()
    (performance_dir / "Run3_Sub42.99.jsonl").write_text(
        json.dumps(
            {
                "kind": "tool",
                "name": "bash",
                "run_id": "3",
                "sub_id": "42",
                "pid": 99,
                "elapsed_ms": 1,
                "success": True,
                "started_at": "2026-06-13 10:00:00.000",
                "ended_at": "2026-06-13 10:00:00.001",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    dataset = PerformanceDataset.load(tmp_path)

    event = dataset.events[0]
    assert event.get("_run_id") == "3"
    assert event.get("_sub_id") == "42"
    assert event.get("_pid") == 99


def test_missing_performance_data_returns_recording_hint(tmp_path) -> None:
    time_result = TimeAnalyzer().analyze(tmp_path)
    token_result = TokenAnalyzer().analyze(tmp_path)

    assert "记录时未开启 performance 采集" in time_result["error"]
    assert "记录时未开启 performance 采集" in token_result["error"]
