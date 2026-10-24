"""Unfinished tool calls remain observable without flushing a completed round."""

import json
from concurrent.futures import ThreadPoolExecutor

from dataagent.core.utils.otel_event_recorder import OtelEventRecorder


def test_live_journal_records_pending_call_and_omits_payloads(tmp_path):
    recorder = OtelEventRecorder(output_dir=tmp_path, session_id="live")
    recorder.record_llm_start("model", messages=[{"content": "payload-prompt"}])
    recorder.record_llm_end(content="payload-answer")
    recorder.record_tool_start("schema", "call-1", arguments="payload-args")
    path = tmp_path / "trajectory.progress.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[-1]["type"] == "tool_start"
    assert events[-1]["tool_name"] == "schema"
    assert "payload-" not in path.read_text()
    assert not (tmp_path / "trajectory.json").exists()
    assert recorder.unflushed_event_count == 3
    recorder.record_tool_end("call-1", result="payload-result", is_error=True)
    assert json.loads(path.read_text().splitlines()[-1])["is_error"]
    recorder.record_round_end()
    assert len(json.loads((tmp_path / "trajectory.json").read_text())) == 1
    assert recorder.unflushed_event_count == 0


def test_concurrent_tools_and_subagents_keep_separate_complete_events(tmp_path):
    recorder = OtelEventRecorder(output_dir=tmp_path)

    def record(index):
        recorder.record_tool_start("tool", str(index))
        recorder.record_tool_end(str(index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(20)))
    events = [json.loads(line) for line in (tmp_path / "trajectory.progress.jsonl").read_text().splitlines()]
    for index in range(20):
        assert [event["type"] for event in events if event["tool_call_id"] == str(index)] == ["tool_start", "tool_end"]
    sub = OtelEventRecorder(output_dir=tmp_path, sub_id=2, run_id=1)
    sub.record_tool_start("subtool", "sub-call")
    assert (tmp_path / "trajectory_2_1.progress.jsonl").exists()
    assert len(events) == 40
