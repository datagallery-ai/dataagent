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
"""Trajectory format contract tests — guard the data shape that evo-eval consumes.

These tests verify the **output format** of OTel trajectory files at the schema
level.  They are deliberately decoupled from the recorder's internal behaviour
tests (in ``test_otel_yaml_injection.py``) so that a refactor of the recorder
implementation cannot silently break the consumer contract.

Consumer reference: ``evo_eval.tracing.instrumentors.dataagent.DataAgentInstrumentor``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dataagent.core.utils.otel_event_recorder import OtelEventRecorder

# ═══════════════════════════════════════════════════════════════════════════════
# 1. Group & event schema contract
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrajectorySchemaContract:
    """Verify the exact field names, types, and structure of trajectory JSON output."""

    # ── Group-level schema ─────────────────────────────────────────────────

    def test_main_group_schema(self, tmp_path: Path) -> None:
        """Main-agent groups must have all required top-level fields with correct types."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="schema_main",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="planner:GLM-5.1", messages=[{"role": "user", "content": "hi"}])
        recorder.record_llm_end(usage={"input_tokens": 10, "output_tokens": 5}, finish_reason="stop")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        g = data[0]

        # Required top-level fields
        assert isinstance(g["role"], str) and g["role"] == "main"
        assert isinstance(g["session_id"], str)
        assert isinstance(g["run_id"], int)
        assert isinstance(g["span_id"], str) and len(g["span_id"]) == 16
        assert isinstance(g["parent_span_id"], str)
        assert isinstance(g["trace_id"], str) and len(g["trace_id"]) == 32
        assert isinstance(g["round_index"], int)
        assert isinstance(g["otel_config"], dict)
        assert isinstance(g["events"], list)

    def test_sub_agent_group_schema(self, tmp_path: Path) -> None:
        """Sub-agent groups must have 'role': 'sub-agent' and the same required fields."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="schema_sub",
            otel_config={"enabled": True, "trace_id": "a" * 32, "span_id": "b" * 16},
            role="sub-agent",
            sub_id=3,
            run_id=0,
        )
        recorder.record_llm_start(model="planner:GLM-5.1")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory_3_0.json").read_text(encoding="utf-8"))
        g = data[0]
        assert g["role"] == "sub-agent"
        assert isinstance(g["span_id"], str) and len(g["span_id"]) == 16
        assert isinstance(g["parent_span_id"], str) and len(g["parent_span_id"]) == 16
        assert isinstance(g["trace_id"], str) and len(g["trace_id"]) == 32

    def test_sub_agent_parent_tool_call_id_schema(self, tmp_path: Path) -> None:
        """Sub-agent groups with parent_tool_call_id must have it as a non-empty string."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="schema_ptcid",
            otel_config={
                "enabled": True,
                "trace_id": "a" * 32,
                "span_id": "b" * 16,
                "parent_tool_call_id": "call_abc123",
            },
            role="sub-agent",
            sub_id=3,
            run_id=0,
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory_3_0.json").read_text(encoding="utf-8"))
        g = data[0]
        assert isinstance(g["parent_tool_call_id"], str)
        assert g["parent_tool_call_id"] == "call_abc123"
        # Also inside otel_config
        assert g["otel_config"]["parent_tool_call_id"] == "call_abc123"

    def test_main_agent_no_parent_tool_call_id_when_absent(self, tmp_path: Path) -> None:
        """Main agent groups must not include parent_tool_call_id when not provided."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="schema_no_ptcid",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        g = data[0]
        assert "parent_tool_call_id" not in g
        assert "parent_tool_call_id" not in g["otel_config"]

    # ── Event-level schema ─────────────────────────────────────────────────

    def test_llm_start_event_schema(self, tmp_path: Path) -> None:
        """llm_start must have type, model, timestamp; input when provided."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="evt_llm_start",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(
            model="planner:GLM-5.1",
            messages=[{"role": "system", "content": "hello"}, {"role": "user", "content": "hi"}],
        )
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        evt = data[0]["events"][0]
        assert evt["type"] == "llm_start"
        assert isinstance(evt["model"], str)
        assert isinstance(evt["timestamp"], (int, float))
        assert isinstance(evt["input"], list)
        assert len(evt["input"]) == 2
        assert evt["input"][0]["role"] == "system"
        assert evt["input"][1]["role"] == "user"

    def test_llm_end_event_schema(self, tmp_path: Path) -> None:
        """llm_end must have type, timestamp; usage/content/reasoning_content when provided."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="evt_llm_end",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_llm_end(
            usage={"input_tokens": 100, "output_tokens": 50},
            finish_reason="tool_calls",
            content="I will call bash",
            reasoning_content="thinking...",
        )
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        evt = data[0]["events"][1]
        assert evt["type"] == "llm_end"
        assert isinstance(evt["timestamp"], (int, float))
        assert isinstance(evt["usage"], dict)
        assert "input_tokens" in evt["usage"]
        assert "output_tokens" in evt["usage"]
        assert evt["finish_reason"] == "tool_calls"
        assert evt["content"] == "I will call bash"
        assert evt["reasoning_content"] == "thinking..."

    def test_tool_start_event_schema(self, tmp_path: Path) -> None:
        """tool_start must have type, tool_name, tool_call_id, timestamp; arguments when provided."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="evt_tool_start",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_llm_end(finish_reason="tool_calls")
        recorder.record_tool_start(
            tool_name="bash",
            tool_call_id="call_abc123",
            arguments='{"cmd": "ls"}',
        )
        recorder.record_tool_end(tool_call_id="call_abc123", result="file.txt")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        # Find the tool_start event
        tool_start = next(e for e in data[0]["events"] if e["type"] == "tool_start")
        assert tool_start["type"] == "tool_start"
        assert tool_start["tool_name"] == "bash"
        assert tool_start["tool_call_id"] == "call_abc123"
        assert isinstance(tool_start["timestamp"], (int, float))
        assert tool_start["arguments"] == '{"cmd": "ls"}'

    def test_tool_end_event_schema(self, tmp_path: Path) -> None:
        """tool_end must have type, tool_call_id, timestamp; result and is_error when provided."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="evt_tool_end",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_tool_start(tool_name="bash", tool_call_id="call_err1")
        recorder.record_tool_end(tool_call_id="call_err1", result="command not found", is_error=True)
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        tool_end = next(e for e in data[0]["events"] if e["type"] == "tool_end")
        assert tool_end["type"] == "tool_end"
        assert tool_end["tool_call_id"] == "call_err1"
        assert isinstance(tool_end["timestamp"], (int, float))
        assert tool_end["result"] == "command not found"
        assert tool_end["is_error"] is True

    # ── otel_config inside group ───────────────────────────────────────────

    def test_otel_config_schema(self, tmp_path: Path) -> None:
        """otel_config inside groups must contain trace_id, span_id, parent_span_id, output_dir."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="otel_cfg_schema",
            otel_config={"enabled": True, "provider_name": "dataagent"},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        oc = data[0]["otel_config"]
        # Runtime keys (always present)
        assert isinstance(oc["trace_id"], str) and len(oc["trace_id"]) == 32
        assert isinstance(oc["span_id"], str) and len(oc["span_id"]) == 16
        assert isinstance(oc["parent_span_id"], str)  # may be "" for top-level
        assert isinstance(oc["output_dir"], str)
        # User-provided keys preserved
        assert oc["enabled"] is True
        assert oc["provider_name"] == "dataagent"

    # ── File format ────────────────────────────────────────────────────────

    def test_file_is_valid_json_array(self, tmp_path: Path) -> None:
        """Trajectory file must be a JSON array (not dict, not raw string)."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="fmt_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        raw = (tmp_path / "trajectory.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert isinstance(parsed, list), "Trajectory file must be a JSON array"
        assert len(parsed) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Span hierarchy contract
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpanHierarchyContract:
    """Verify that span_id / parent_span_id / trace_id relationships are
    resolvable across main and sub-agent trajectory files.

    This is the direct regression guard for the bug where sub-agent
    parent_span_id pointed to an untraceable virtual run span.
    """

    def test_sub_parent_span_id_resolvable_in_main(self, tmp_path: Path) -> None:
        """Sub-agent group.parent_span_id must appear as a span_id in the main agent file."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="hier_main",
            otel_config={"enabled": True},
            role="main",
        )
        # Round 0
        main.record_llm_start(model="m")
        main.record_round_end()
        # Round 1 — sub-agent will be launched from here
        round_config = main.round_otel_config
        main.record_llm_start(model="m")
        main.record_round_end()

        # Sub-agent from round 1
        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="hier_sub",
            otel_config=round_config,
            role="sub-agent",
            sub_id=10,
            run_id=0,
        )
        sub.record_llm_start(model="sub-m")
        sub.record_round_end()

        # Read back and verify
        main_data = json.loads((otel_dir / "trajectory.json").read_text(encoding="utf-8"))
        sub_data = json.loads((otel_dir / "trajectory_10_0.json").read_text(encoding="utf-8"))

        main_span_ids = {g["span_id"] for g in main_data}
        sub_parent = sub_data[0]["parent_span_id"]
        assert sub_parent in main_span_ids, (
            f"Sub-agent parent_span_id '{sub_parent}' not found in main agent span_ids: {main_span_ids}"
        )

    def test_main_top_level_parent_span_id_is_empty(self, tmp_path: Path) -> None:
        """Main agent without external parent: parent_span_id must be empty string."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="top_main",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        for g in data:
            assert g["parent_span_id"] == "", "Top-level main agent should have empty parent_span_id"

    def test_main_with_external_parent(self, tmp_path: Path) -> None:
        """Main agent with YAML-injected parent: parent_span_id equals the injected value."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ext_parent_main",
            otel_config={"enabled": True, "parent_span_id": "deadbeef12345678"},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        assert data[0]["parent_span_id"] == "deadbeef12345678"

    def test_sub_agent_shares_trace_id_with_main(self, tmp_path: Path) -> None:
        """Sub-agent must share the same trace_id as the main agent."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="trace_main",
            otel_config={"enabled": True},
            role="main",
        )
        round_config = main.round_otel_config
        main.record_llm_start(model="m")
        main.record_round_end()

        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="trace_sub",
            otel_config=round_config,
            role="sub-agent",
            sub_id=1,
            run_id=0,
        )
        sub.record_llm_start(model="m")
        sub.record_round_end()

        main_data = json.loads((otel_dir / "trajectory.json").read_text(encoding="utf-8"))
        sub_data = json.loads((otel_dir / "trajectory_1_0.json").read_text(encoding="utf-8"))

        main_trace = main_data[0]["trace_id"]
        sub_trace = sub_data[0]["trace_id"]
        assert main_trace == sub_trace, f"Trace ID mismatch: main={main_trace}, sub={sub_trace}"

    def test_nested_sub_agent_parent_resolvable(self, tmp_path: Path) -> None:
        """Nested sub-agents: sub-B (from sub-A) parent_span_id must be resolvable in sub-A."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="nest_main",
            otel_config={"enabled": True},
            role="main",
        )
        main_round_cfg = main.round_otel_config
        main.record_llm_start(model="m")
        main.record_round_end()

        # Sub-agent A from main round 0
        sub_a = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="nest_sub_a",
            otel_config=main_round_cfg,
            role="sub-agent",
            sub_id=100,
            run_id=0,
        )
        # Sub-A round 0
        sub_a_round_cfg = sub_a.round_otel_config
        sub_a.record_llm_start(model="m")
        sub_a.record_round_end()

        # Sub-agent B from sub-A round 0
        sub_b = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="nest_sub_b",
            otel_config=sub_a_round_cfg,
            role="sub-agent",
            sub_id=200,
            run_id=0,
        )
        sub_b.record_llm_start(model="m")
        sub_b.record_round_end()

        # Verify: sub-B's parent_span_id is in sub-A's span_ids
        sub_a_data = json.loads((otel_dir / "trajectory_100_0.json").read_text(encoding="utf-8"))
        sub_b_data = json.loads((otel_dir / "trajectory_200_0.json").read_text(encoding="utf-8"))

        sub_a_spans = {g["span_id"] for g in sub_a_data}
        sub_b_parent = sub_b_data[0]["parent_span_id"]
        assert sub_b_parent in sub_a_spans, (
            f"Nested sub-B parent_span_id '{sub_b_parent}' not found in sub-A spans: {sub_a_spans}"
        )

    def test_sub_agent_parent_tool_call_id_resolvable_in_main(self, tmp_path: Path) -> None:
        """Sub-agent parent_tool_call_id must appear as a tool_call_id in the main agent's tool_start events."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_main",
            otel_config={"enabled": True},
            role="main",
        )
        main.record_llm_start(model="m")
        # Simulate the submit_subagent tool call
        main.record_tool_start(tool_name="submit_subagent", tool_call_id="call_launch_1")
        main.record_tool_end(tool_call_id="call_launch_1", result='{"status": "queued"}')
        main.record_round_end()

        round_config = main.round_otel_config
        round_config["parent_tool_call_id"] = "call_launch_1"

        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_sub",
            otel_config=round_config,
            role="sub-agent",
            sub_id=10,
            run_id=0,
        )
        sub.record_llm_start(model="sub-m")
        sub.record_round_end()

        main_data = json.loads((otel_dir / "trajectory.json").read_text(encoding="utf-8"))
        sub_data = json.loads((otel_dir / "trajectory_10_0.json").read_text(encoding="utf-8"))

        # Collect all tool_call_ids from main agent's tool_start events
        main_tool_call_ids = {
            e["tool_call_id"] for g in main_data for e in g.get("events", []) if e.get("type") == "tool_start"
        }
        sub_ptcid = sub_data[0].get("parent_tool_call_id", "")
        assert sub_ptcid in main_tool_call_ids, (
            f"Sub-agent parent_tool_call_id '{sub_ptcid}' not found in main agent tool_call_ids: {main_tool_call_ids}"
        )

    def test_multiple_sub_agents_same_round_distinguishable_by_tool_call_id(self, tmp_path: Path) -> None:
        """Two sub-agents from the same round must carry different parent_tool_call_ids."""
        otel_dir = tmp_path / "otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="ptcid_multi_main",
            otel_config={"enabled": True},
            role="main",
        )
        main.record_llm_start(model="m")
        main.record_tool_start(tool_name="submit_subagent", tool_call_id="call_sub_a")
        main.record_tool_start(tool_name="submit_subagent", tool_call_id="call_sub_b")
        main.record_tool_end(tool_call_id="call_sub_a", result="ok")
        main.record_tool_end(tool_call_id="call_sub_b", result="ok")
        main.record_round_end()

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

        # Both share the same parent_span_id (same round)
        assert data_a[0]["parent_span_id"] == data_b[0]["parent_span_id"]
        # But different parent_tool_call_ids
        assert data_a[0]["parent_tool_call_id"] == "call_sub_a"
        assert data_b[0]["parent_tool_call_id"] == "call_sub_b"
        assert data_a[0]["parent_tool_call_id"] != data_b[0]["parent_tool_call_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. File layout contract
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileLayoutContract:
    """Verify file naming, directory structure, and the no-merge guarantee."""

    def test_main_file_named_trajectory_json(self, tmp_path: Path) -> None:
        """Main agent must write to trajectory.json."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="name_main",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()
        assert (tmp_path / "trajectory.json").is_file()

    def test_sub_file_named_with_sub_id_and_run_id(self, tmp_path: Path) -> None:
        """Sub-agent must write to trajectory_{sub_id}_{run_id}.json."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="name_sub",
            otel_config={"enabled": True, "trace_id": "a" * 32, "span_id": "b" * 16},
            role="sub-agent",
            sub_id=42,
            run_id=1,
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()
        assert (tmp_path / "trajectory_42_1.json").is_file()

    def test_main_and_sub_in_same_directory(self, tmp_path: Path) -> None:
        """Main and sub-agent files must appear in the same .otel/ directory."""
        otel_dir = tmp_path / "shared_otel"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="dir_main",
            otel_config={"enabled": True},
            role="main",
        )
        main.record_llm_start(model="m")
        main.record_round_end()

        round_cfg = main.round_otel_config
        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="dir_sub",
            otel_config=round_cfg,
            role="sub-agent",
            sub_id=1,
            run_id=0,
        )
        sub.record_llm_start(model="m")
        sub.record_round_end()

        files = {p.name for p in otel_dir.iterdir() if p.is_file()}
        assert "trajectory.json" in files
        assert "trajectory_1_0.json" in files

    def test_no_merge_on_flush(self, tmp_path: Path) -> None:
        """Main agent flush must not delete or merge sub-agent file."""
        otel_dir = tmp_path / "no_merge"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="nm_main",
            otel_config={"enabled": True},
            role="main",
        )
        main.record_llm_start(model="m")
        main.record_round_end()

        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="nm_sub",
            otel_config={"enabled": True, "trace_id": "a" * 32, "span_id": "b" * 16},
            role="sub-agent",
            sub_id=5,
            run_id=0,
        )
        sub.record_llm_start(model="sub-m")
        sub.record_round_end()

        # Flush main again — must not touch sub file
        main.record_llm_start(model="m2")
        main.record_round_end()

        sub_file = otel_dir / "trajectory_5_0.json"
        assert sub_file.is_file(), "Sub-agent file must survive main agent flush"

        sub_data = json.loads(sub_file.read_text(encoding="utf-8"))
        assert sub_data[0]["role"] == "sub-agent"
        # Sub file must still only contain sub-agent events (not merged)
        assert all(g["role"] == "sub-agent" for g in sub_data)

    def test_discover_trajectory_files_classification(self, tmp_path: Path) -> None:
        """Simplified discover logic: main file has role=main, sub files have role!=main."""
        otel_dir = tmp_path / "discover"
        main = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="disc_main",
            otel_config={"enabled": True},
            role="main",
        )
        main.record_llm_start(model="m")
        main.record_round_end()

        sub = OtelEventRecorder(
            output_dir=otel_dir,
            session_id="disc_sub",
            otel_config={"enabled": True, "trace_id": "a" * 32, "span_id": "b" * 16},
            role="sub-agent",
            sub_id=3,
            run_id=0,
        )
        sub.record_llm_start(model="m")
        sub.record_round_end()

        # Simplified discovery logic (mirrors evo-eval's DataAgentInstrumentor.discover_trajectory_files)
        main_file = None
        sub_files: list[Path] = []
        for path in sorted(otel_dir.glob("trajectory*.json")):
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            groups = payload if isinstance(payload, list) else [payload]
            role = groups[0].get("role", "main") if groups else "main"
            if role == "main":
                main_file = path
            else:
                sub_files.append(path)

        assert main_file is not None
        assert main_file.name == "trajectory.json"
        assert len(sub_files) == 1
        assert sub_files[0].name == "trajectory_3_0.json"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Event data integrity
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventDataIntegrity:
    """Verify internal consistency and format of events within trajectory files."""

    def test_event_order_within_round(self, tmp_path: Path) -> None:
        """Events within a round must follow: llm_start → llm_end → tool_start → tool_end."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="order_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_llm_end(finish_reason="tool_calls")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="ok")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        types = [e["type"] for e in data[0]["events"]]
        assert types == ["llm_start", "llm_end", "tool_start", "tool_end"]

    def test_tool_start_end_paired_by_tool_call_id(self, tmp_path: Path) -> None:
        """Every tool_start's tool_call_id must have a matching tool_end in the same round."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="pair_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_start(tool_name="read_file", tool_call_id="c2")
        recorder.record_tool_end(tool_call_id="c1", result="out1")
        recorder.record_tool_end(tool_call_id="c2", result="out2")
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        events = data[0]["events"]
        start_ids = {e["tool_call_id"] for e in events if e["type"] == "tool_start"}
        end_ids = {e["tool_call_id"] for e in events if e["type"] == "tool_end"}
        assert start_ids == end_ids, f"Mismatch: tool_start ids={start_ids}, tool_end ids={end_ids}"

    def test_llm_start_end_paired(self, tmp_path: Path) -> None:
        """Every llm_start must have a corresponding llm_end (except possibly the last round)."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="llm_pair_test",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_llm_end(finish_reason="tool_calls")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="ok")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        events = data[0]["events"]
        starts = sum(1 for e in events if e["type"] == "llm_start")
        ends = sum(1 for e in events if e["type"] == "llm_end")
        assert starts == ends, f"llm_start count ({starts}) != llm_end count ({ends})"

    def test_timestamp_monotonic_within_round(self, tmp_path: Path) -> None:
        """Event timestamps must be monotonically non-decreasing within a round."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="ts_mono",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_tool_start(tool_name="bash", tool_call_id="c1")
        recorder.record_tool_end(tool_call_id="c1", result="ok")
        recorder.record_llm_end(finish_reason="stop")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        timestamps = [e["timestamp"] for e in data[0]["events"]]
        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1], (
                f"Timestamp not monotonic: event {i} ts={timestamps[i]} < event {i - 1} ts={timestamps[i - 1]}"
            )

    def test_span_id_16_hex_chars(self, tmp_path: Path) -> None:
        """All span_id values must be 16-character hex strings (8 bytes)."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="span_fmt",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        for g in data:
            sid = g["span_id"]
            assert len(sid) == 16, f"span_id '{sid}' is not 16 chars"
            assert all(c in "0123456789abcdef" for c in sid), f"span_id '{sid}' is not hex"

    def test_trace_id_32_hex_chars(self, tmp_path: Path) -> None:
        """All trace_id values must be 32-character hex strings (16 bytes)."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="trace_fmt",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        tid = data[0]["trace_id"]
        assert len(tid) == 32, f"trace_id '{tid}' is not 32 chars"
        assert all(c in "0123456789abcdef" for c in tid), f"trace_id '{tid}' is not hex"

    def test_round_index_monotonic(self, tmp_path: Path) -> None:
        """round_index must start at 0 and increment by 1 across groups in a file."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="idx_mono",
            otel_config={"enabled": True},
            role="main",
        )
        for _ in range(4):
            recorder.record_llm_start(model="m")
            recorder.record_llm_end(finish_reason="stop")
            recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        indices = [g["round_index"] for g in data]
        assert indices == [0, 1, 2, 3]

    def test_usage_has_input_and_output_tokens(self, tmp_path: Path) -> None:
        """llm_end.usage dict must contain 'input_tokens' and 'output_tokens' keys."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="usage_keys",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m")
        recorder.record_llm_end(
            usage={"input_tokens": 200, "output_tokens": 100},
            finish_reason="stop",
        )
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        llm_end = next(e for e in data[0]["events"] if e["type"] == "llm_end")
        assert "input_tokens" in llm_end["usage"]
        assert "output_tokens" in llm_end["usage"]


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Incremental flush integrity
# ═══════════════════════════════════════════════════════════════════════════════


class TestIncrementalFlushIntegrity:
    """Verify that incremental per-round writes preserve file structure integrity."""

    def test_file_always_valid_json_after_each_round(self, tmp_path: Path) -> None:
        """After each record_round_end, the file must be valid JSON."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="flush_valid",
            otel_config={"enabled": True},
            role="main",
        )
        for i in range(3):
            recorder.record_llm_start(model=f"m{i}")
            recorder.record_llm_end(finish_reason="stop")
            recorder.record_round_end()
            # Read back and verify valid JSON
            raw = (tmp_path / "trajectory.json").read_text(encoding="utf-8")
            parsed = json.loads(raw)
            assert isinstance(parsed, list)

    def test_no_duplicate_groups_after_append(self, tmp_path: Path) -> None:
        """Multiple round_end calls must not produce duplicate groups."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="no_dup",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m0")
        recorder.record_round_end()
        recorder.record_llm_start(model="m1")
        recorder.record_round_end()

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        assert len(data) == 2
        # Verify the groups are distinct (different round_index, different span_id)
        assert data[0]["round_index"] != data[1]["round_index"]
        assert data[0]["span_id"] != data[1]["span_id"]

    def test_round_end_then_flush_no_duplicate(self, tmp_path: Path) -> None:
        """record_round_end + flush must not write the same events twice."""
        recorder = OtelEventRecorder(
            output_dir=tmp_path,
            session_id="no_dup_flush",
            otel_config={"enabled": True},
            role="main",
        )
        recorder.record_llm_start(model="m0")
        recorder.record_round_end()  # persists round 0
        recorder.flush()  # should be a no-op

        data = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
        assert len(data) == 1, f"Expected 1 group, got {len(data)} — possible duplicate write"
        assert len(data[0]["events"]) == 1  # just the llm_start
