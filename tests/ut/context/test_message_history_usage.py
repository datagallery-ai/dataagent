import json
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.context.message_history import (
    _compute_round_summaries,
    _deserialize,
    _read_raw,
    _serialize,
    _write_raw,
)


class TestSerializeUsageMetadata:
    def test_ai_message_with_full_usage(self):
        msg = AIMessage(
            content="hello",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "input_cache_read_tokens": 30,
                "input_cache_creation_tokens": 5,
                "output_reasoning_tokens": 10,
            },
        )
        ser = _serialize(msg)
        um = ser["usage_metadata"]
        assert um["input_tokens"] == 100
        assert um["output_tokens"] == 50
        assert um["total_tokens"] == 150
        assert um["input_cache_read_tokens"] == 30
        assert um["input_cache_creation_tokens"] == 5
        assert um["output_reasoning_tokens"] == 10

    def test_ai_message_with_partial_usage(self):
        msg = AIMessage(content="hello", usage_metadata={"input_tokens": 80, "output_tokens": 40, "total_tokens": 120})
        ser = _serialize(msg)
        um = ser["usage_metadata"]
        assert um["input_tokens"] == 80
        assert um["output_tokens"] == 40
        assert um["input_cache_read_tokens"] == 0
        assert um["input_cache_creation_tokens"] == 0
        assert um["output_reasoning_tokens"] == 0

    def test_ai_message_no_usage(self):
        msg = AIMessage(content="hello")
        ser = _serialize(msg)
        assert "usage_metadata" not in ser

    def test_human_message_no_usage(self):
        msg = HumanMessage(content="query")
        ser = _serialize(msg)
        assert "usage_metadata" not in ser

    def test_tool_message_no_usage(self):
        msg = ToolMessage(content="result", tool_call_id="tc_1")
        ser = _serialize(msg)
        assert "usage_metadata" not in ser


class TestDeserializeUsageMetadata:
    def test_full_usage_roundtrip(self):
        msg = AIMessage(
            content="hello",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "input_cache_read_tokens": 30,
                "input_cache_creation_tokens": 5,
                "output_reasoning_tokens": 10,
            },
        )
        ser = _serialize(msg)
        deser = _deserialize(ser)
        assert isinstance(deser, AIMessage)
        um = deser.usage_metadata
        assert um["input_tokens"] == 100
        assert um["output_tokens"] == 50
        assert um["total_tokens"] == 150
        assert um["input_cache_read_tokens"] == 30
        assert um["input_cache_creation_tokens"] == 5
        assert um["output_reasoning_tokens"] == 10

    def test_missing_usage_defaults_to_zero(self):
        payload = {"type": "AIMessage", "content": "hello", "additional_kwargs": {}, "response_metadata": {}}
        deser = _deserialize(payload)
        um = deser.usage_metadata
        assert um["input_tokens"] == 0
        assert um["output_tokens"] == 0
        assert um["total_tokens"] == 0
        assert um["input_cache_read_tokens"] == 0
        assert um["input_cache_creation_tokens"] == 0
        assert um["output_reasoning_tokens"] == 0

    def test_partial_usage_fields_default(self):
        payload = {
            "type": "AIMessage",
            "content": "hello",
            "usage_metadata": {"input_tokens": 100, "output_tokens": 50},
            "additional_kwargs": {},
            "response_metadata": {},
        }
        deser = _deserialize(payload)
        um = deser.usage_metadata
        assert um["input_tokens"] == 100
        assert um["output_tokens"] == 50
        assert um["total_tokens"] == 0
        assert um["input_cache_read_tokens"] == 0


class TestComputeRoundSummaries:
    def test_single_round(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}},
        ]
        summaries = _compute_round_summaries(records)
        assert len(summaries) == 1
        assert summaries[0]["round"] == 0
        assert summaries[0]["input_tokens"] == 100

    def test_two_rounds(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}},
            {"type": "HumanMessage", "content": "q2"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280}},
        ]
        summaries = _compute_round_summaries(records)
        assert len(summaries) == 2
        assert summaries[0]["round"] == 0
        assert summaries[0]["input_tokens"] == 100
        assert summaries[1]["round"] == 1
        assert summaries[1]["input_tokens"] == 200

    def test_cache_and_reasoning_tokens(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {
                "type": "AIMessage",
                "usage_metadata": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "input_cache_read_tokens": 30,
                    "input_cache_creation_tokens": 5,
                    "output_reasoning_tokens": 10,
                },
            },
        ]
        summaries = _compute_round_summaries(records)
        assert summaries[0]["input_cache_read_tokens"] == 30
        assert summaries[0]["input_cache_creation_tokens"] == 5
        assert summaries[0]["output_reasoning_tokens"] == 10

    def test_no_human_message_start(self):
        records = [
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 100}},
        ]
        summaries = _compute_round_summaries(records)
        assert len(summaries) == 0

    def test_multiple_ai_in_one_round(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 100, "output_tokens": 30}},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 50, "output_tokens": 20}},
        ]
        summaries = _compute_round_summaries(records)
        assert len(summaries) == 1
        assert summaries[0]["input_tokens"] == 150
        assert summaries[0]["output_tokens"] == 50

    def test_empty_records(self):
        summaries = _compute_round_summaries([])
        assert summaries == []

    def test_cache_hit_rate_computed(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {
                "type": "AIMessage",
                "usage_metadata": {
                    "input_tokens": 200,
                    "output_tokens": 50,
                    "input_cache_read_tokens": 150,
                    "input_cache_creation_tokens": 10,
                },
            },
        ]
        summaries = _compute_round_summaries(records)
        assert summaries[0]["cache_hit_rate"] == 75.0  # 150 / 200 * 100

    def test_cache_hit_rate_zero_when_no_input(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 0}},
        ]
        summaries = _compute_round_summaries(records)
        assert summaries[0]["cache_hit_rate"] == 0.0

    def test_elapsed_sec_from_timestamps(self):
        records = [
            {"type": "HumanMessage", "content": "q1", "additional_kwargs": {"_ts": 1000.0}},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 10}, "additional_kwargs": {"_ts": 1005.0}},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 20}, "additional_kwargs": {"_ts": 1012.5}},
        ]
        summaries = _compute_round_summaries(records)
        assert summaries[0]["elapsed_sec"] == 12.5  # 1012.5 - 1000.0

    def test_elapsed_sec_zero_without_timestamps(self):
        """Old records without _ts should default elapsed_sec to 0.0."""
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 10}},
        ]
        summaries = _compute_round_summaries(records)
        assert summaries[0]["elapsed_sec"] == 0.0

    def test_elapsed_sec_across_two_rounds(self):
        records = [
            {"type": "HumanMessage", "content": "q1", "additional_kwargs": {"_ts": 100.0}},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 10}, "additional_kwargs": {"_ts": 103.0}},
            {"type": "HumanMessage", "content": "q2", "additional_kwargs": {"_ts": 110.0}},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 20}, "additional_kwargs": {"_ts": 115.0}},
        ]
        summaries = _compute_round_summaries(records)
        assert summaries[0]["elapsed_sec"] == 3.0   # 103 - 100
        assert summaries[1]["elapsed_sec"] == 5.0   # 115 - 110


class TestSerializeTimestampStamping:
    def test_serialize_stamps_ts_on_message(self):
        msg = HumanMessage(content="q1")
        assert "_ts" not in (msg.additional_kwargs or {})
        _serialize(msg)
        assert "_ts" in msg.additional_kwargs
        ts1 = msg.additional_kwargs["_ts"]
        # Second serialization keeps the same timestamp (idempotent)
        import time as _time

        _time.sleep(0.01)
        _serialize(msg)
        assert msg.additional_kwargs["_ts"] == ts1

    def test_serialize_preserves_existing_ts(self):
        msg = HumanMessage(content="q1", additional_kwargs={"_ts": 42.0, "custom": "x"})
        _serialize(msg)
        assert msg.additional_kwargs["_ts"] == 42.0
        assert msg.additional_kwargs["custom"] == "x"

    def test_ts_survives_roundtrip(self):
        msg = AIMessage(
            content="hello",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )
        ser = _serialize(msg)
        assert "_ts" in ser["additional_kwargs"]
        deser = _deserialize(ser)
        # Deserialized message keeps _ts, so re-serialization won't re-stamp
        ts_before = deser.additional_kwargs["_ts"]
        ser2 = _serialize(deser)
        assert ser2["additional_kwargs"]["_ts"] == ts_before


class TestWriteRawWithRoundSummaries:
    def test_write_and_read_round_summaries(self):
        records = [
            {"type": "HumanMessage", "content": "q1"},
            {"type": "AIMessage", "usage_metadata": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "messages.json"
            _write_raw(path, records)
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert "messages" in payload
            assert "round_summaries" in payload
            assert len(payload["round_summaries"]) == 1
            assert payload["round_summaries"][0]["input_tokens"] == 100

    def test_read_raw_preserves_old_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "messages.json"
            old_payload = {"messages": [{"type": "HumanMessage", "content": "q1"}]}
            path.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")
            records = _read_raw(path)
            assert len(records) == 1
