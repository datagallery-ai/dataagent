# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for OtelEventRecorder YAML injection via FlexAgent._inject_otel_config_from_yaml,
incremental flush via record_round_end(), and the no-merge architecture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dataagent.core.flex.agent import FlexAgent
from dataagent.core.utils.otel_event_recorder import OtelEventRecorder

# ── _inject_otel_config_from_yaml ──────────────────────────────────────────


class TestInjectOtelConfigFromYaml:
    """Verify that YAML OTEL_CONFIG is merged into initial_state correctly."""

    def test_inject_from_yaml_passthrough(self) -> None:
        """YAML keys are passed through verbatim; enabled is NOT auto-set."""
        state: dict[str, Any] = {}
        config = {"OTEL_CONFIG": {"output_dir": "/tmp/otel"}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        # enabled should NOT be auto-injected
        assert state["__otel_config"] == {"output_dir": "/tmp/otel"}
        assert "enabled" not in state["__otel_config"]

    def test_inject_preserves_explicit_enabled_true(self) -> None:
        state: dict[str, Any] = {}
        config = {"OTEL_CONFIG": {"enabled": True, "output_dir": "/tmp/otel"}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        assert state["__otel_config"]["enabled"] is True

    def test_inject_preserves_explicit_enabled_false(self) -> None:
        state: dict[str, Any] = {}
        config = {"OTEL_CONFIG": {"enabled": False, "output_dir": "/tmp/otel"}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        assert state["__otel_config"]["enabled"] is False

    def test_inject_does_not_overwrite_existing(self) -> None:
        """Programmatic __otel_config always wins over YAML."""
        existing = {"enabled": True, "output_dir": "/custom", "parent_trace_id": "abc"}
        state: dict[str, Any] = {"__otel_config": existing}
        config = {"OTEL_CONFIG": {"enabled": True, "output_dir": "/from_yaml"}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        assert state["__otel_config"]["output_dir"] == "/custom"

    def test_inject_noop_when_no_yaml_config(self) -> None:
        state: dict[str, Any] = {}
        FlexAgent._inject_otel_config_from_yaml(state, None)
        assert "__otel_config" not in state

    def test_inject_noop_when_empty_yaml_config(self) -> None:
        state: dict[str, Any] = {}
        FlexAgent._inject_otel_config_from_yaml(state, {"OTEL_CONFIG": {}})
        assert "__otel_config" not in state

    def test_inject_with_parent_trace_and_span(self) -> None:
        state: dict[str, Any] = {}
        config = {
            "OTEL_CONFIG": {
                "enabled": True,
                "output_dir": "/tmp/otel",
                "parent_trace_id": "0123abcd" * 4,
                "parent_span_id": "4567efgh",
            }
        }
        FlexAgent._inject_otel_config_from_yaml(state, config)
        assert state["__otel_config"]["parent_trace_id"] == "0123abcd" * 4
        assert state["__otel_config"]["parent_span_id"] == "4567efgh"

    def test_inject_noop_when_state_is_not_dict(self) -> None:
        FlexAgent._inject_otel_config_from_yaml("not a dict", {"OTEL_CONFIG": {"enabled": True}})  # type: ignore[arg-type]


# ── from_state: enabled defaults to False ───────────────────────────────────


class TestOtelEnabledDefaultFalse:
    """Verify that tracing is disabled unless enabled=True is explicit."""

    def test_no_enabled_means_disabled(self, tmp_path: Path) -> None:
        """OTEL_CONFIG without enabled=True should NOT create a recorder."""
        state: dict[str, Any] = {
            "session_id": "test",
            "workspace": str(tmp_path),
            "__otel_config": {"output_dir": str(tmp_path / "otel")},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is None

    def test_enabled_false_means_disabled(self, tmp_path: Path) -> None:
        state: dict[str, Any] = {
            "session_id": "test",
            "__otel_config": {"enabled": False, "output_dir": str(tmp_path)},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is None

    def test_enabled_true_activates(self, tmp_path: Path) -> None:
        state: dict[str, Any] = {
            "session_id": "test",
            "__otel_config": {"enabled": True, "output_dir": str(tmp_path)},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None

    def test_yaml_without_enabled_disabled(self, tmp_path: Path) -> None:
        """YAML OTEL_CONFIG without enabled: true → no recorder."""
        state: dict[str, Any] = {"session_id": "test", "workspace": str(tmp_path)}
        config = {"OTEL_CONFIG": {"output_dir": str(tmp_path)}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is None

    def test_yaml_with_enabled_true_activates(self, tmp_path: Path) -> None:
        state: dict[str, Any] = {"session_id": "test", "workspace": str(tmp_path)}
        config = {"OTEL_CONFIG": {"enabled": True, "output_dir": str(tmp_path)}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None


# ── from_state: output_dir defaults to workspace/.otel ──────────────────────


class TestOtelOutputDirDefault:
    """Verify that output_dir falls back to <workspace>/.otel when not set."""

    def test_output_dir_defaults_to_workspace_otel(self, tmp_path: Path) -> None:
        workspace = str(tmp_path / "my_workspace")
        state: dict[str, Any] = {
            "session_id": "test",
            "workspace": workspace,
            "__otel_config": {"enabled": True},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None
        assert str(recorder.output_dir) == str(Path(workspace) / ".otel")

    def test_output_dir_defaults_to_workspace_otel_from_yaml(self, tmp_path: Path) -> None:
        workspace = str(tmp_path / "ws")
        state: dict[str, Any] = {"session_id": "test", "workspace": workspace}
        config = {"OTEL_CONFIG": {"enabled": True}}
        FlexAgent._inject_otel_config_from_yaml(state, config)
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None
        assert str(recorder.output_dir) == str(Path(workspace) / ".otel")

    def test_explicit_output_dir_overrides_default(self, tmp_path: Path) -> None:
        explicit_dir = str(tmp_path / "custom_otel")
        workspace = str(tmp_path / "ws")
        state: dict[str, Any] = {
            "session_id": "test",
            "workspace": workspace,
            "__otel_config": {"enabled": True, "output_dir": explicit_dir},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None
        assert str(recorder.output_dir) == explicit_dir

    def test_no_output_dir_no_workspace_means_disabled(self) -> None:
        state: dict[str, Any] = {
            "session_id": "test",
            "__otel_config": {"enabled": True},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is None

    def test_workspace_as_path_object(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws_pathobj"
        state: dict[str, Any] = {
            "session_id": "test",
            "workspace": workspace,
            "__otel_config": {"enabled": True},
        }
        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None
        assert str(recorder.output_dir) == str(workspace / ".otel")


# ── End-to-end: YAML → state → recorder → record → flush ───────────────────


class TestOtelRecorderEndToEnd:
    """Full pipeline: YAML inject → recorder → events → flush."""

    def test_record_and_flush_with_default_output_dir(self, tmp_path: Path) -> None:
        workspace = tmp_path / "e2e_ws"
        state: dict[str, Any] = {"session_id": "e2e_test", "workspace": str(workspace)}
        config = {"OTEL_CONFIG": {"enabled": True}}
        FlexAgent._inject_otel_config_from_yaml(state, config)

        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None

        recorder.record_llm_start(model="test-model")
        recorder.record_llm_end(usage={"input_tokens": 10, "output_tokens": 5}, finish_reason="stop")
        recorder.record_tool_start(tool_name="bash", tool_call_id="call_001", arguments='{"cmd": "ls"}')
        recorder.record_tool_end(tool_call_id="call_001", result="file1.txt\nfile2.txt")
        recorder.flush()

        trajectory = Path(workspace) / ".otel" / "trajectory.json"
        assert trajectory.is_file()
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["role"] == "main"
        assert len(data[0]["events"]) == 4
        assert data[0]["round_index"] == 0

    def test_record_and_flush_with_custom_output_dir(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "custom_output"
        workspace = tmp_path / "e2e_ws2"
        state: dict[str, Any] = {"session_id": "e2e_test2", "workspace": str(workspace)}
        config = {"OTEL_CONFIG": {"enabled": True, "output_dir": str(output_dir)}}
        FlexAgent._inject_otel_config_from_yaml(state, config)

        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None
        recorder.record_llm_start(model="test-model")
        recorder.flush()

        trajectory = output_dir / "trajectory.json"
        assert trajectory.is_file()

    def test_sub_agent_inference(self, tmp_path: Path) -> None:
        workspace = tmp_path / "sub_ws"
        state: dict[str, Any] = {"session_id": "sub_test", "sub_id": 3, "run_id": 0, "workspace": str(workspace)}
        config = {"OTEL_CONFIG": {"enabled": True}}
        FlexAgent._inject_otel_config_from_yaml(state, config)

        recorder = OtelEventRecorder.from_state(state)
        assert recorder is not None
        recorder.record_llm_start(model="sub-model")
        recorder.flush()

        sub_file = Path(workspace) / ".otel" / "trajectory_3_0.json"
        assert sub_file.is_file()
        data = json.loads(sub_file.read_text(encoding="utf-8"))
        assert data[0]["role"] == "sub-agent"

    def test_no_merge_main_and_sub_independent(self, tmp_path: Path) -> None:
        """Main agent and sub-agent write separate files; no merging occurs."""
        workspace = tmp_path / "no_merge_ws"
        otel_dir = workspace / ".otel"

        # Main agent
        main_state: dict[str, Any] = {
            "session_id": "main_test",
            "workspace": str(workspace),
            "__otel_config": {"enabled": True, "output_dir": str(otel_dir)},
        }
        main_recorder = OtelEventRecorder.from_state(main_state)
        assert main_recorder is not None
        main_recorder.record_llm_start(model="main-model")
        main_recorder.flush()

        # Sub-agent
        sub_state: dict[str, Any] = {
            "session_id": "sub_test",
            "sub_id": 5,
            "run_id": 0,
            "workspace": str(workspace),
            "__otel_config": {"enabled": True, "output_dir": str(otel_dir)},
        }
        sub_recorder = OtelEventRecorder.from_state(sub_state)
        assert sub_recorder is not None
        sub_recorder.record_llm_start(model="sub-model")
        sub_recorder.flush()

        # Verify: main agent's flush did NOT merge sub-agent file
        main_file = otel_dir / "trajectory.json"
        sub_file = otel_dir / "trajectory_5_0.json"
        assert main_file.is_file()
        assert sub_file.is_file()  # sub file still exists (not deleted)

        main_data = json.loads(main_file.read_text(encoding="utf-8"))
        sub_data = json.loads(sub_file.read_text(encoding="utf-8"))

        # Main file contains only main agent events
        assert len(main_data) == 1
        assert main_data[0]["role"] == "main"

        # Sub file contains only sub-agent events
        assert len(sub_data) == 1
        assert sub_data[0]["role"] == "sub-agent"


# ── Incremental flush via record_round_end() ────────────────────────────────


class TestIncrementalFlush:
    """Verify that record_round_end() persists events per-round to disk."""

    def test_record_round_end_writes_first_round(self, tmp_path: Path) -> None:
        """First record_round_end() creates the file with one group."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="round_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m1")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="ok")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        assert trajectory.is_file()
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["round_index"] == 0
        assert len(data[0]["events"]) == 3

    def test_record_round_end_appends_second_round(self, tmp_path: Path) -> None:
        """Subsequent record_round_end() appends a new group to the same file."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="round_test",
            otel_config={"enabled": True},
            role="main",
        )
        # Round 0
        recorder.record_llm_start(model="m1")
        recorder.record_llm_end(finish_reason="tool_calls")
        recorder.record_round_end()

        # Round 1
        recorder.record_llm_start(model="m1")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="ok")
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["round_index"] == 0
        assert len(data[0]["events"]) == 2
        assert data[1]["round_index"] == 1
        assert len(data[1]["events"]) == 4

    def test_flush_after_round_end_is_noop(self, tmp_path: Path) -> None:
        """flush() after all rounds already persisted writes nothing new."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="round_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()
        recorder.flush()  # should be a no-op

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 1  # still only one group

    def test_flush_captures_remaining_events(self, tmp_path: Path) -> None:
        """flush() writes events that weren't captured by a record_round_end()."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="round_test",
            otel_config={"enabled": True},
            role="main",
        )
        # Round 0: persisted
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        # Partial round (e.g. agent crashed mid-round)
        recorder.record_llm_start(model="m1")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        # No record_round_end() for this round — flush should catch it
        recorder.flush()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["round_index"] == 0
        assert data[1]["round_index"] == 1
        assert len(data[1]["events"]) == 2

    def test_unflushed_event_count(self, tmp_path: Path) -> None:
        """unflushed_event_count tracks events not yet written to disk."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="count_test",
            otel_config={"enabled": True},
            role="main",
        )
        assert recorder.unflushed_event_count == 0

        recorder.record_llm_start(model="m1")
        assert recorder.unflushed_event_count == 1

        recorder.record_llm_end()
        assert recorder.unflushed_event_count == 2

        recorder.record_round_end()
        assert recorder.unflushed_event_count == 0

    def test_record_round_end_noop_when_no_events(self, tmp_path: Path) -> None:
        """record_round_end() with no new events does not write to disk."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="noop_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_round_end()  # no events at all
        trajectory = tmp_path / "trajectory.json"
        assert not trajectory.is_file()

    def test_sub_agent_incremental_flush(self, tmp_path: Path) -> None:
        """Sub-agent record_round_end() writes to its own file."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="sub_round_test",
            otel_config={"enabled": True},
            role="sub-agent",
            sub_id=7,
            run_id=0,
        )
        recorder.record_llm_start(model="sub-model")
        recorder.record_tool_start(tool_name="python", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="42")
        recorder.record_round_end()

        sub_file = tmp_path / "trajectory_7_0.json"
        assert sub_file.is_file()
        data = json.loads(sub_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["role"] == "sub-agent"
        assert data[0]["round_index"] == 0
        assert len(data[0]["events"]) == 3

    def test_timeout_survival_simulation(self, tmp_path: Path) -> None:
        """Simulate a process killed after round 1: round 0 data survives."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="timeout_sim",
            otel_config={"enabled": True},
            role="sub-agent",
            sub_id=2,
            run_id=0,
        )

        # Round 0: complete, persisted
        recorder.record_llm_start(model="m1")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="ok")
        recorder.record_llm_end(finish_reason="tool_calls")
        recorder.record_round_end()

        # Round 1: LLM call completes, but then process is "killed"
        # before record_round_end() — these events are lost (in memory only)
        recorder.record_llm_start(model="m1")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c2")
        # "killed" here — flush() never called

        # Verify: only round 0 data is on disk
        sub_file = tmp_path / "trajectory_2_0.json"
        assert sub_file.is_file()
        data = json.loads(sub_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["round_index"] == 0
        assert len(data[0]["events"]) == 4
        # Round 1 events (llm_start + tool_start) are NOT in the file
        total_events_on_disk = sum(len(g["events"]) for g in data)
        assert total_events_on_disk == 4

    def test_three_rounds_append(self, tmp_path: Path) -> None:
        """Three rounds are correctly appended as three groups."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="three_rounds",
            otel_config={"enabled": True},
            role="main",
        )
        for i in range(3):
            recorder.record_llm_start(model=f"model-{i}")
            recorder.record_llm_end(finish_reason="stop")
            recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 3
        for i, group in enumerate(data):
            assert group["round_index"] == i
            assert len(group["events"]) == 2

    def test_round_index_increments_across_flush(self, tmp_path: Path) -> None:
        """round_index continues incrementing across record_round_end + flush."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="idx_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()  # round_index = 0

        recorder.record_llm_start(model="m1")
        recorder.flush()  # round_index = 1 (flush picks up remaining)

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["round_index"] == 0
        assert data[1]["round_index"] == 1


# ── llm_start input field ──────────────────────────────────────────────────


class TestLlmStartInput:
    """Verify that llm_start records the input messages."""

    def test_llm_start_with_input(self, tmp_path: Path) -> None:
        """record_llm_start with input stores it in the event."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="input_test",
            otel_config={"enabled": True},
            role="main",
        )
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        recorder.record_llm_start(model="test-model", messages=messages)
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 1
        event = data[0]["events"][0]
        assert event["type"] == "llm_start"
        assert event["input"] == messages

    def test_llm_start_without_input(self, tmp_path: Path) -> None:
        """record_llm_start without input does not add the field."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="no_input_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="test-model")
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        event = data[0]["events"][0]
        assert "input" not in event

    def test_llm_start_input_none(self, tmp_path: Path) -> None:
        """record_llm_start with messages=None does not add the field."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="none_input_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="test-model", messages=None)
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        event = data[0]["events"][0]
        assert "input" not in event

    def test_llm_start_input_with_tool_calls(self, tmp_path: Path) -> None:
        """Input with tool_calls in assistant messages is stored verbatim."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="tool_input_test",
            otel_config={"enabled": True},
            role="main",
        )
        messages = [
            {"role": "user", "content": "Run bash"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
                ],
            },
            {"role": "tool", "content": "file1.txt", "tool_call_id": "c1"},
        ]
        recorder.record_llm_start(model="test-model", messages=messages)
        recorder.record_llm_end(finish_reason="tool_calls")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        event = data[0]["events"][0]
        assert event["input"] == messages
        assert event["input"][1]["tool_calls"] is not None

    def test_input_survives_incremental_flush(self, tmp_path: Path) -> None:
        """Input is preserved across incremental round writes."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="input_flush_test",
            otel_config={"enabled": True},
            role="main",
        )
        # Round 0
        recorder.record_llm_start(model="m1", messages=[{"role": "user", "content": "Hello"}])
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        # Round 1
        recorder.record_llm_start(model="m1", messages=[{"role": "user", "content": "World"}])
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["events"][0]["input"][0]["content"] == "Hello"
        assert data[1]["events"][0]["input"][0]["content"] == "World"


# ── Per-round span_id and round_otel_config ────────────────────────────────


class TestRoundSpanId:
    """Verify that each round has an independent span_id and round_otel_config works."""

    def test_each_round_has_unique_span_id(self, tmp_path: Path) -> None:
        """Each round's group has a different span_id."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="span_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["span_id"] != data[1]["span_id"]

    def test_round_parent_span_id_is_agent_run_span(self, tmp_path: Path) -> None:
        """Main agent without external parent: round parent_span_id is empty (top-level)."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="parent_test",
            otel_config={"enabled": True},
            role="main",
        )

        recorder.record_llm_start(model="m1")
        recorder.record_round_end()
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        for group in data:
            # No external parent → parent_span_id is empty for a top-level main agent
            assert group["parent_span_id"] == ""

    def test_round_parent_span_id_with_external_parent(self, tmp_path: Path) -> None:
        """Main agent with external parent: round parent_span_id equals the external parent."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="parent_test_ext",
            otel_config={"enabled": True, "parent_span_id": "ext_parent_123"},
            role="main",
        )
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert data[0]["parent_span_id"] == "ext_parent_123"

    def test_round_otel_config_contains_round_span_id(self, tmp_path: Path) -> None:
        """round_otel_config.span_id is the current round's span_id, not the run-level one."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="round_config_test",
            otel_config={"enabled": True},
            role="main",
        )
        # Before any round: round_otel_config.span_id should be the initial round span
        round0_span = recorder.round_otel_config["span_id"]
        assert round0_span != recorder._span_id  # different from run-level span
        assert round0_span == recorder._current_round_span_id

    def test_round_otel_config_parent_is_run_span(self, tmp_path: Path) -> None:
        """round_otel_config.parent_span_id: empty for top-level main agent."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="round_parent_test",
            otel_config={"enabled": True},
            role="main",
        )
        config = recorder.round_otel_config
        # No external parent → parent_span_id is empty
        assert config["parent_span_id"] == ""

    def test_round_otel_config_updates_after_round_end(self, tmp_path: Path) -> None:
        """After record_round_end, round_otel_config returns the next round's span_id."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="update_test",
            otel_config={"enabled": True},
            role="main",
        )
        round0_span = recorder.round_otel_config["span_id"]
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        round1_span = recorder.round_otel_config["span_id"]
        assert round1_span != round0_span  # new span for next round

    def test_sub_agent_parent_points_to_round(self, tmp_path: Path) -> None:
        """Sub-agent's _parent_span_id points to the round that invoked it."""
        # Simulate main agent
        main_recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="main_test",
            otel_config={"enabled": True},
            role="main",
        )
        # Main agent Round 0: read round_otel_config (this is what executor does)
        round_config = main_recorder.round_otel_config
        round0_span_id = round_config["span_id"]

        # Sub-agent receives round_config as its otel_config
        sub_recorder = OtelEventRecorder(
            output_dir=tmp_path / "sub",
            session_id="sub_test",
            otel_config=round_config,
            role="sub-agent",
            sub_id=5,
            run_id=0,
        )
        # Sub-agent's run-level parent_span_id should point to the invoking round
        assert sub_recorder._parent_span_id == round0_span_id

        # Persist and verify: sub-agent round's parent_span_id points to sub's own run-level span
        main_recorder.record_llm_start(model="main-model")
        main_recorder.record_round_end()

        sub_recorder.record_llm_start(model="sub-model")
        sub_recorder.flush()

        sub_file = tmp_path / "sub" / "trajectory_5_0.json"
        sub_data = json.loads(sub_file.read_text(encoding="utf-8"))
        # Sub-agent round's parent_span_id points to the main agent's round that invoked it
        # (not the sub-agent's own virtual run span, which is untraceable from the main file).
        assert sub_data[0]["parent_span_id"] == round0_span_id
        # sub-agent's _parent_span_id also points back to the main round that invoked it
        assert sub_recorder._parent_span_id == round0_span_id

    def test_init_generates_first_round_span(self, tmp_path: Path) -> None:
        """_current_round_span_id is generated at init, not deferred."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="init_test",
            otel_config={"enabled": True},
            role="main",
        )
        assert recorder._current_round_span_id  # not empty
        assert len(recorder._current_round_span_id) == 16  # 8 bytes = 16 hex chars

    def test_flush_uses_current_round_span_id(self, tmp_path: Path) -> None:
        """flush() writes the current round's span_id into the group."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="flush_span_test",
            otel_config={"enabled": True},
            role="main",
        )
        expected_span = recorder._current_round_span_id
        recorder.record_llm_start(model="m1")
        recorder.flush()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        assert data[0]["span_id"] == expected_span

    def test_sub_agent_inherits_main_output_dir(self, tmp_path: Path) -> None:
        """Sub-agent created from round_otel_config writes to the same directory as main agent."""
        main_output = tmp_path / "shared_otel"
        main_recorder = OtelEventRecorder(
            output_dir=main_output,
            session_id="main_test",
            otel_config={"enabled": True},
            role="main",
        )
        round_config = main_recorder.round_otel_config

        # Sub-agent uses round_config as its otel_config
        sub_recorder = OtelEventRecorder.from_state(
            {
                "session_id": "sub_test",
                "sub_id": 3,
                "workspace": str(tmp_path / "sub_ws"),  # different workspace
                "__otel_config": round_config,
            }
        )
        assert sub_recorder is not None
        # Sub-agent's output_dir should be the main agent's, not derived from sub workspace
        assert str(sub_recorder.output_dir) == str(main_output)

    def test_otel_config_includes_output_dir(self, tmp_path: Path) -> None:
        """otel_config and round_otel_config both include output_dir."""
        main_output = tmp_path / "otel_dir"
        recorder = OtelEventRecorder(
            output_dir=main_output,
            session_id="dir_test",
            otel_config={"enabled": True},
            role="main",
        )
        assert recorder.otel_config["output_dir"] == str(main_output)
        assert recorder.round_otel_config["output_dir"] == str(main_output)

    def test_group_otel_config_matches_outer_fields(self, tmp_path: Path) -> None:
        """The otel_config inside each group matches the outer span_id/parent_span_id/trace_id."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="consistency_test",
            otel_config={"enabled": True, "provider_name": "dataagent"},
            role="main",
        )
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        trajectory = tmp_path / "trajectory.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        g = data[0]
        # Outer fields
        assert g["span_id"]
        # Top-level main agent: parent_span_id is empty (no external parent)
        assert g["parent_span_id"] == ""
        assert g["trace_id"]
        # otel_config inside should match
        assert g["otel_config"]["span_id"] == g["span_id"]
        assert g["otel_config"]["parent_span_id"] == g["parent_span_id"]
        assert g["otel_config"]["trace_id"] == g["trace_id"]
        assert g["otel_config"]["output_dir"] == str(tmp_path)
        # User-provided keys should also be preserved
        assert g["otel_config"]["provider_name"] == "dataagent"
        assert g["otel_config"]["enabled"] is True


# ── parent_tool_call_id ──────────────────────────────────────────────────────


class TestParentToolCallId:
    """Verify that parent_tool_call_id is propagated and persisted for sub-agents."""

    def test_main_agent_parent_tool_call_id_is_empty(self, tmp_path: Path) -> None:
        """Main agent should have empty parent_tool_call_id."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ptcid_main",
            otel_config={"enabled": True},
            role="main",
        )
        assert recorder.parent_tool_call_id == ""

    def test_sub_agent_receives_parent_tool_call_id(self, tmp_path: Path) -> None:
        """Sub-agent created from round_otel_config with parent_tool_call_id inherits it."""
        main_recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ptcid_main",
            otel_config={"enabled": True},
            role="main",
        )
        # Simulate executor injecting parent_tool_call_id into round_otel_config
        round_config = main_recorder.round_otel_config
        round_config["parent_tool_call_id"] = "call_abc123"

        sub_recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ptcid_sub",
            otel_config=round_config,
            role="sub-agent",
            sub_id=1,
            run_id=0,
        )
        assert sub_recorder.parent_tool_call_id == "call_abc123"

    def test_sub_agent_group_contains_parent_tool_call_id(self, tmp_path: Path) -> None:
        """Sub-agent trajectory group must include parent_tool_call_id at top level and in otel_config."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_grp_main",
            otel_config={"enabled": True},
            role="main",
        )
        round_config = main.round_otel_config
        round_config["parent_tool_call_id"] = "call_xyz789"

        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_grp_sub",
            otel_config=round_config,
            role="sub-agent",
            sub_id=2,
            run_id=0,
        )
        sub.record_llm_start(model="m")
        sub.record_round_end()

        sub_data = json.loads((otel_dir / "trajectory_2_0.json").read_text(encoding="utf-8"))
        g = sub_data[0]
        # Group top-level field
        assert g["parent_tool_call_id"] == "call_xyz789"
        # Also inside otel_config
        assert g["otel_config"]["parent_tool_call_id"] == "call_xyz789"

    def test_main_agent_group_omits_parent_tool_call_id_when_empty(self, tmp_path: Path) -> None:
        """Main agent groups should not include parent_tool_call_id when it's empty."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ptcid_omit",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        g = data[0]
        assert "parent_tool_call_id" not in g
        assert "parent_tool_call_id" not in g["otel_config"]

    def test_round_otel_config_propagates_parent_tool_call_id(self, tmp_path: Path) -> None:
        """round_otel_config from a sub-agent should include its parent_tool_call_id."""
        main = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ptcid_round_main",
            otel_config={"enabled": True},
            role="main",
        )
        round_config = main.round_otel_config
        round_config["parent_tool_call_id"] = "call_prop1"

        sub = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ptcid_round_sub",
            otel_config=round_config,
            role="sub-agent",
            sub_id=3,
            run_id=0,
        )
        # Sub-agent's round_otel_config should carry parent_tool_call_id
        # so nested sub-agents can inherit it
        sub_round_cfg = sub.round_otel_config
        assert sub_round_cfg["parent_tool_call_id"] == "call_prop1"

    def test_multiple_sub_agents_same_round_different_tool_call_ids(self, tmp_path: Path) -> None:
        """Two sub-agents launched in the same round carry different parent_tool_call_ids."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_multi_main",
            otel_config={"enabled": True},
            role="main",
        )
        main.record_llm_start(model="m")
        main.record_round_end()

        # Simulate two submit_subagent calls in round 1
        round_config = main.round_otel_config
        round_cfg_a = dict(round_config)
        round_cfg_a["parent_tool_call_id"] = "call_sub_a"
        round_cfg_b = dict(round_config)
        round_cfg_b["parent_tool_call_id"] = "call_sub_b"

        sub_a = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_multi_a",
            otel_config=round_cfg_a,
            role="sub-agent",
            sub_id=10,
            run_id=0,
        )
        sub_b = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_multi_b",
            otel_config=round_cfg_b,
            role="sub-agent",
            sub_id=20,
            run_id=0,
        )
        sub_a.record_llm_start(model="m")
        sub_a.record_round_end()
        sub_b.record_llm_start(model="m")
        sub_b.record_round_end()

        data_a = json.loads((otel_dir / "trajectory_10_0.json").read_text(encoding="utf-8"))
        data_b = json.loads((otel_dir / "trajectory_20_0.json").read_text(encoding="utf-8"))
        assert data_a[0]["parent_tool_call_id"] == "call_sub_a"
        assert data_b[0]["parent_tool_call_id"] == "call_sub_b"
