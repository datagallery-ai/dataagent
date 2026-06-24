import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dataagent.agents.nl2sql.utils.trajectory_recorder import _NullRecorder, _PROMPT_SUMMARY_MAX, _RESULT_SUMMARY_MAX, _TZ_CN, NL2SQLTrajectoryRecorder, _ts_cn
from dataagent.core.context.message_history import _compute_round_summaries, _read_raw


def test_timestamp_in_tool_call_record():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_tool_call(tool_name="t1", args={}, purpose="p1")
    rec = recorder.records[0]
    ts = rec["response_metadata"]["timestamp"]
    assert ts is not None and len(ts) > 0
    parsed = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
    now_cn = datetime.now(tz=_TZ_CN).replace(microsecond=0)
    diff = abs((parsed - now_cn.replace(tzinfo=None)).total_seconds())
    assert diff < 5


def test_timestamp_in_tool_result_record():
    recorder = NL2SQLTrajectoryRecorder()
    tid = recorder.record_tool_call(tool_name="t1", args={}, purpose="p1")
    recorder.record_tool_result(content="result", tool_call_id=tid)
    rec = recorder.records[1]
    assert rec["response_metadata"]["timestamp"] is not None


def test_timestamp_in_llm_call_record():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_llm_call(node_name="test", action="", purpose="p", prompt_summary="p", result_summary="r")
    ai_rec = recorder.records[0]
    tool_rec = recorder.records[1]
    assert ai_rec["response_metadata"]["timestamp"] is not None
    assert tool_rec["response_metadata"]["timestamp"] is not None


def test_timestamp_in_node_start_record():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_node_start(node_name="test", purpose="p")
    rec = recorder.records[0]
    assert rec["response_metadata"]["timestamp"] is not None


def test_record_tool_call_basic():
    recorder = NL2SQLTrajectoryRecorder()
    tid = recorder.record_tool_call(
        tool_name="metavisor_get_table_list",
        args={"databaseName": "test_db", "limit": 1000},
        purpose="Retrieve table list from MetaVisor",
    )
    assert tid.startswith("call_nl2sql_")
    assert len(recorder.records) == 1
    rec = recorder.records[0]
    assert rec["type"] == "AIMessage"
    assert rec["additional_kwargs"]["reasoning_content"] == "Retrieve table list from MetaVisor"
    assert len(rec["tool_calls"]) == 1
    assert rec["tool_calls"][0]["name"] == "metavisor_get_table_list"
    assert rec["tool_calls"][0]["args"]["databaseName"] == "test_db"


def test_record_tool_result():
    recorder = NL2SQLTrajectoryRecorder()
    tid = recorder.record_tool_call(
        tool_name="test_tool", args={"key": "value"}, purpose="test",
    )
    recorder.record_tool_result(content="result data", tool_call_id=tid)
    assert len(recorder.records) == 2
    assert recorder.records[1]["type"] == "ToolMessage"
    assert recorder.records[1]["content"] == "result data"
    assert recorder.records[1]["tool_call_id"] == tid


def test_record_llm_call():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_llm_call(
        node_name="coordinator",
        action="",
        purpose="Parse question",
        prompt_summary="[system] prompt\n[user] prompt",
        result_summary="semantic question and keywords",
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                        "input_cache_read_tokens": 10, "input_cache_creation_tokens": 0,
                        "output_reasoning_tokens": 5},
    )
    assert len(recorder.records) == 2
    ai_rec = recorder.records[0]
    assert ai_rec["type"] == "AIMessage"
    assert ai_rec["additional_kwargs"]["reasoning_content"] == "coordinator: Parse question"
    assert ai_rec["tool_calls"][0]["name"] == "llm_invoke_coordinator"
    assert ai_rec["usage_metadata"]["input_tokens"] == 100
    tool_rec = recorder.records[1]
    assert tool_rec["type"] == "ToolMessage"
    assert tool_rec["content"] == "semantic question and keywords"


def test_record_llm_call_with_action():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_llm_call(
        node_name="validator",
        action="validate_semantic_",
        purpose="Validate SQL semantics",
        prompt_summary="prompt",
        result_summary="result",
    )
    assert recorder.records[0]["tool_calls"][0]["name"] == "llm_invoke_validator_validate_semantic_"


def test_record_node_start():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_node_start(node_name="perceptor", purpose="Build schema")
    assert len(recorder.records) == 1
    assert recorder.records[0]["type"] == "HumanMessage"
    assert "perceptor" in recorder.records[0]["content"]
    assert "Build schema" in recorder.records[0]["content"]


def test_write_trajectory():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_node_start(node_name="entry", purpose="query")
    recorder.record_tool_call(tool_name="tool1", args={}, purpose="step1")
    recorder.record_tool_result(content="result1", tool_call_id="call_nl2sql_1")
    recorder.record_llm_call(
        node_name="gen", action="", purpose="gen sql",
        prompt_summary="p", result_summary="r",
        usage_metadata={"input_tokens": 200, "output_tokens": 80, "total_tokens": 280,
                        "input_cache_read_tokens": 0, "input_cache_creation_tokens": 0,
                        "output_reasoning_tokens": 0},
    )

    tmpdir = tempfile.mkdtemp()
    path = Path(tmpdir) / "test_trajectory.json"
    recorder.write_trajectory(path)

    data = _read_raw(path)
    assert len(data) == 5
    assert data[0]["type"] == "HumanMessage"
    assert data[1]["type"] == "AIMessage"
    assert data[2]["type"] == "ToolMessage"
    assert data[3]["type"] == "AIMessage"
    assert data[4]["type"] == "ToolMessage"

    summaries = _compute_round_summaries(data)
    assert len(summaries) >= 1
    assert summaries[0]["round"] == 0


def test_sequential_tool_call_ids():
    recorder = NL2SQLTrajectoryRecorder()
    tid1 = recorder.record_tool_call(tool_name="t1", args={}, purpose="p1")
    tid2 = recorder.record_tool_call(tool_name="t2", args={}, purpose="p2")
    assert tid1 != tid2
    assert int(tid1.split("_")[-1]) < int(tid2.split("_")[-1])


def test_custom_tool_call_id():
    recorder = NL2SQLTrajectoryRecorder()
    custom_id = "call_custom_xyz"
    tid = recorder.record_tool_call(tool_name="t1", args={}, purpose="p1", tool_call_id=custom_id)
    assert tid == custom_id


def test_write_trajectory_creates_parent_dirs():
    recorder = NL2SQLTrajectoryRecorder()
    recorder.record_node_start(node_name="test", purpose="test")
    tmpdir = tempfile.mkdtemp()
    deep_path = Path(tmpdir) / "sub" / "dir" / "trajectory.json"
    recorder.write_trajectory(deep_path)
    assert deep_path.exists()


def test_prompt_summary_truncation():
    recorder = NL2SQLTrajectoryRecorder()
    long_prompt = "x" * (_PROMPT_SUMMARY_MAX + 1000)
    recorder.record_llm_call(
        node_name="test", action="", purpose="test",
        prompt_summary=long_prompt, result_summary="result",
    )
    args = recorder.records[0]["tool_calls"][0]["args"]
    assert len(args["prompt_summary"]) == _PROMPT_SUMMARY_MAX


def test_result_summary_truncation():
    recorder = NL2SQLTrajectoryRecorder()
    long_result = "y" * (_RESULT_SUMMARY_MAX + 1000)
    recorder.record_llm_call(
        node_name="test", action="", purpose="test",
        prompt_summary="p", result_summary=long_result,
    )
    tool_content = recorder.records[1]["content"]
    assert len(tool_content) == _RESULT_SUMMARY_MAX
