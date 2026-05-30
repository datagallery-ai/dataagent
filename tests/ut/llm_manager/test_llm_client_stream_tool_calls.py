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
from typing import Any, cast

# pylint: disable=protected-access
from dataagent.core.managers.llm_manager import llm_client as llm_client_module
from dataagent.core.managers.llm_manager.llm_client import LLMClient


def _feed_stream_tool_call_deltas(
    client: LLMClient,
    chunk: dict[str, Any],
    by_index: dict[int, dict[str, str]],
) -> str:
    feed = client._feed_stream_tool_call_deltas
    return str(feed(chunk, by_index))


def _finalize_stream_tool_calls(
    by_index: dict[int, dict[str, str]],
    finish_reason: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    finalize = LLMClient._finalize_stream_tool_calls_for_lc
    return cast(tuple[list[dict[str, Any]], list[dict[str, Any]]], finalize(by_index, finish_reason))


def test_finalize_stream_tool_calls_marks_incomplete_arguments_as_invalid():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_1",
                "name": "write_file",
                "arguments": '{"path": "/tmp/demo.txt"',
            }
        }
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_1",
            "name": "write_file",
            "args": '{"path": "/tmp/demo.txt"',
            "error": "Incomplete streamed tool arguments JSON",
        }
    ]


def test_finalize_stream_tool_calls_marks_truncated_json_as_invalid():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_1",
                "name": "write_file",
                "arguments": '{"path": "/tmp/demo.txt"',
            }
        }
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_1",
            "name": "write_file",
            "args": '{"path": "/tmp/demo.txt"',
            "error": "Incomplete streamed tool arguments JSON",
        }
    ]


def test_finalize_stream_tool_calls_uses_length_finish_reason_for_truncation():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_1",
                "name": "write_file",
                "arguments": '{"path": "/tmp/demo.txt"',
            }
        },
        "length",
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_1",
            "name": "write_file",
            "args": '{"path": "/tmp/demo.txt"',
            "error": "Streamed tool arguments were truncated by the model output limit",
        }
    ]


def test_finalize_stream_tool_calls_uses_content_filter_finish_reason():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_1",
                "name": "write_file",
                "arguments": '{"path": "/tmp/demo.txt"',
            }
        },
        "content_filter",
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_1",
            "name": "write_file",
            "args": '{"path": "/tmp/demo.txt"',
            "error": "Streamed tool arguments were blocked or truncated by the content filter",
        }
    ]


def test_feed_stream_tool_call_deltas_records_finish_reason_by_index(monkeypatch):
    records: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(llm_client_module.logger, "debug", lambda message, *args: records.append((message, args)))

    client = LLMClient(model="m", api_base="b", api_key="k")
    by_index: dict[int, dict[str, str]] = {0: {"id": "call_1", "name": "write_file", "arguments": "{}"}}

    finish_reason = _feed_stream_tool_call_deltas(
        client,
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        by_index,
    )

    assert finish_reason == "length"
    assert any(
        message == "stream.finish_reason index={} reason={}" and args == (0, "length") for message, args in records
    )


def test_feed_stream_tool_call_deltas_content_only_no_finish_no_tool_state():
    client = LLMClient(model="m", api_base="b", api_key="k")
    by_index: dict[int, dict[str, str]] = {}

    finish = _feed_stream_tool_call_deltas(
        client,
        {"choices": [{"index": 0, "delta": {"content": "hello", "reasoning_content": "why"}}]},
        by_index,
    )

    assert finish == ""
    assert by_index == {}


def test_feed_stream_tool_call_deltas_usage_only_chunk_returns_empty():
    client = LLMClient(model="m", api_base="b", api_key="k")
    by_index: dict[int, dict[str, str]] = {}

    finish = _feed_stream_tool_call_deltas(
        client,
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        by_index,
    )

    assert finish == ""
    assert by_index == {}


def test_finalize_stream_tool_calls_marks_missing_name_as_invalid():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_missing_name",
                "name": "",
                "arguments": '{"path": "/tmp/demo.txt"}',
            }
        }
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_missing_name",
            "name": "unknown",
            "args": '{"path": "/tmp/demo.txt"}',
            "error": "Streamed tool call is missing a function name",
        }
    ]


def test_finalize_stream_tool_calls_marks_complete_bad_json_as_invalid_json():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_bad_json",
                "name": "write_file",
                "arguments": '{"path": /tmp/demo.txt}',
            }
        }
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_bad_json",
            "name": "write_file",
            "args": '{"path": /tmp/demo.txt}',
            "error": "Invalid streamed tool arguments JSON",
        }
    ]


def test_finalize_stream_tool_calls_keeps_valid_tool_call():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_2",
                "name": "write_file",
                "arguments": '{"path": "/tmp/demo.txt", "content": "hello"}',
            }
        }
    )

    assert tool_calls == [
        {
            "id": "call_2",
            "name": "write_file",
            "args": {"path": "/tmp/demo.txt", "content": "hello"},
            "type": "tool_call",
        }
    ]
    assert invalid_tool_calls == []


def test_finalize_stream_tool_calls_marks_non_dict_args_as_invalid():
    tool_calls, invalid_tool_calls = _finalize_stream_tool_calls(
        {
            0: {
                "id": "call_3",
                "name": "write_file",
                "arguments": "[1, 2, 3]",
            }
        }
    )

    assert tool_calls == []
    assert invalid_tool_calls == [
        {
            "id": "call_3",
            "name": "write_file",
            "args": [1, 2, 3],
            "error": "Streamed tool arguments must decode to object",
        }
    ]
