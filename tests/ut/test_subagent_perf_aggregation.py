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
"""Unit tests for subagent token/cache aggregation: merge_subagent_llm_usage,
build_summary three views (main_agent/subagents/overall), WorkerResult.perf_summary,
and sub_agent_entry perf_summary construction."""

from __future__ import annotations

from typing import Any


from dataagent.core.swarm.worker_result import (
    synthesize_worker_result,
    worker_result_from_payload,
)
from dataagent.core.utils.performance import PerformanceCollector


def _perf_summary(
    *,
    sub_id: int = 1,
    worker_session_id: str = "subagent_s1_1",
    worker_run_id: int = 0,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    input_cache_read: int = 800,
    input_cache_creation: int = 100,
    call_count: int = 4,
    status: str = "success",
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "source": "subagent",
        "agent_type": "nl2sql",
        "sub_id": sub_id,
        "parent_session_id": "s1",
        "worker_session_id": worker_session_id,
        "worker_run_id": worker_run_id,
        "tool_call_id": "call_x",
        "provider": "bailian",
        "model": "qwen3.7-plus",
        "cache_control_mode": "explicit",
        "status": status,
        "llms": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_cache_read_tokens": input_cache_read,
            "input_cache_creation_tokens": input_cache_creation,
            "output_reasoning_tokens": 0,
            "call_count": call_count,
        },
    }


def _identity(
    *,
    sub_id: str = "1",
    tool_call_id: str = "call_x",
    worker_session_id: str = "subagent_s1_1",
    worker_run_id: str = "0",
) -> dict[str, Any]:
    return {
        "parent_session_id": "s1",
        "parent_run_id": "0",
        "tool_call_id": tool_call_id,
        "sub_id": sub_id,
        "worker_session_id": worker_session_id,
        "worker_run_id": worker_run_id,
        "query": "q",
    }


class TestMergeSubagentLlmUsage:
    def test_normal_aggregate(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        ok = coll.merge_subagent_llm_usage(_perf_summary(), _identity())
        assert ok is True
        summary = coll.build_summary(None)
        sub = summary["llms"]["subagents"]
        assert sub["input_tokens"] == 1000
        assert sub["output_tokens"] == 200
        assert sub["input_cache_read_tokens"] == 800
        assert sub["call_count"] == 4
        # by_agent drill-down
        key = "nl2sql:1:subagent_s1_1"
        assert key in sub["by_agent"]
        assert sub["by_agent"][key]["input_tokens"] == 1000

    def test_duplicate_identity_skipped(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        coll.merge_subagent_llm_usage(_perf_summary(), _identity())
        ok = coll.merge_subagent_llm_usage(_perf_summary(input_tokens=9999), _identity())
        assert ok is False
        summary = coll.build_summary(None)
        # not double-counted
        assert summary["llms"]["subagents"]["input_tokens"] == 1000

    def test_negative_token_rejected(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        bad = _perf_summary()
        bad["llms"]["input_tokens"] = -5
        ok = coll.merge_subagent_llm_usage(bad, _identity())
        assert ok is False
        summary = coll.build_summary(None)
        assert summary["llms"]["subagents"]["input_tokens"] == 0

    def test_bad_schema_rejected(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        bad = _perf_summary()
        bad["schema_version"] = 0
        assert coll.merge_subagent_llm_usage(bad, _identity()) is False
        bad2 = _perf_summary()
        bad2.pop("llms")
        assert coll.merge_subagent_llm_usage(bad2, _identity()) is False

    def test_missing_tool_call_id_uses_hash_fallback(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        ident = _identity(tool_call_id="")
        # First call aggregates; same identity again dedupes via hash fallback.
        assert coll.merge_subagent_llm_usage(_perf_summary(), ident) is True
        assert coll.merge_subagent_llm_usage(_perf_summary(), ident) is False


class TestBuildSummaryThreeViews:
    def test_three_views_structure(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        with coll.measure(
            "llm", "qwen3", input_tokens=100, output_tokens=20, total_tokens=120, input_cache_read_tokens=40
        ):
            pass
        coll.merge_subagent_llm_usage(
            _perf_summary(input_tokens=500, output_tokens=50, input_cache_read=300), _identity()
        )
        summary = coll.build_summary(None)
        llms = summary["llms"]
        for view in ("main_agent", "subagents", "overall"):
            assert view in llms
        assert llms["main_agent"]["input_tokens"] == 100
        assert llms["subagents"]["input_tokens"] == 500
        # overall == main + subagents per field
        for field in ("input_tokens", "output_tokens", "total_tokens", "input_cache_read_tokens"):
            assert llms["overall"][field] == llms["main_agent"][field] + llms["subagents"][field]
        # top-level compat fields == overall
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            assert llms[field] == llms["overall"][field]

    def test_cache_hit_rate_decimal(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        coll.merge_subagent_llm_usage(_perf_summary(input_tokens=1000, input_cache_read=800), _identity())
        summary = coll.build_summary(None)
        sub = summary["llms"]["subagents"]
        assert sub["cache_hit_rate"] == 0.8

    def test_no_subagents_overall_equals_main(self):
        coll = PerformanceCollector(enabled=True, user_id="u", session_id="s", run_id="r")
        with coll.measure("llm", "qwen3", input_tokens=50, output_tokens=10, total_tokens=60):
            pass
        summary = coll.build_summary(None)
        llms = summary["llms"]
        assert llms["overall"]["input_tokens"] == 50
        assert llms["subagents"]["input_tokens"] == 0
        assert llms["input_tokens"] == 50  # top-level == overall


class TestWorkerResultPerfSummary:
    def test_synthesize_passes_perf_summary(self):
        wr = synthesize_worker_result(
            final_state={"final_answer": "done"},
            sub_id=1,
            parent_session_id="s1",
            perf_summary=_perf_summary(),
        )
        assert isinstance(wr.perf_summary, dict)
        assert wr.perf_summary["schema_version"] == 1
        assert wr.perf_summary["llms"]["input_tokens"] == 1000

    def test_synthesize_default_none(self):
        wr = synthesize_worker_result(final_state={"final_answer": "done"}, sub_id=1, parent_session_id="s1")
        assert wr.perf_summary is None

    def test_from_payload_preserves_dict(self):
        payload = {
            "sub_id": 1,
            "parent_session_id": "s1",
            "worker_session_id": "subagent_s1_1",
            "status": "success",
            "final_answer": "done",
            "artifacts": [],
            "tool_calls_count": 0,
            "iteration_count": 0,
            "error": None,
            "resumed": False,
            "perf_summary": _perf_summary(),
        }
        wr = worker_result_from_payload(payload)
        assert isinstance(wr.perf_summary, dict)
        assert wr.perf_summary["llms"]["call_count"] == 4

    def test_from_payload_legacy_none(self):
        # Old payload without perf_summary still parses business fields.
        payload = {
            "sub_id": 1,
            "parent_session_id": "s1",
            "worker_session_id": "subagent_s1_1",
            "status": "success",
            "final_answer": "done",
            "artifacts": [],
            "tool_calls_count": 0,
            "iteration_count": 0,
            "error": None,
            "resumed": False,
        }
        wr = worker_result_from_payload(payload)
        assert wr.perf_summary is None
        assert wr.final_answer == "done"

    def test_to_dict_roundtrip(self):
        wr = synthesize_worker_result(
            final_state={"final_answer": "done"},
            sub_id=1,
            parent_session_id="s1",
            perf_summary=_perf_summary(),
        )
        d = wr.to_dict()
        assert isinstance(d["perf_summary"], dict)
        wr2 = worker_result_from_payload(d)
        assert wr2.perf_summary == wr.perf_summary


class TestSubAgentEntryPerfSummary:
    def test_build_perf_summary_returns_none_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATAAGENT_PERFORMANCE_ENABLED", raising=False)
        from dataagent.actions.tools.local_tool.sub_agent_entry import _build_perf_summary

        # Even if a summary dict is provided, perf disabled → None.
        summary = {"llms": {"overall": {"input_tokens": 100, "call_count": 1}}}
        result = _build_perf_summary(
            summary,
            result={"run_id": 0},
            query="q",
            config_path=str(tmp_path / "cfg.yaml"),
            parent_session_id="s1",
            worker_session_id="subagent_s1_1",
            sub_id=1,
        )
        assert result is None

    def test_build_perf_summary_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "1")
        from dataagent.actions.tools.local_tool.sub_agent_entry import _build_perf_summary

        summary = {
            "llms": {
                "overall": {
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "total_tokens": 1500,
                    "input_cache_read_tokens": 800,
                    "input_cache_creation_tokens": 100,
                    "output_reasoning_tokens": 0,
                    "call_count": 4,
                    "cache_control_mode": "explicit",
                }
            }
        }
        # Minimal config YAML for identity extraction.
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "AGENT:\n  type: nl2sql\n"
            "MODEL:\n  chat_model:\n    provider: bailian\n"
            "    params:\n      model: qwen3.7-plus\n",
            encoding="utf-8",
        )
        result = _build_perf_summary(
            summary,
            result={"run_id": 2},
            query="q",
            config_path=str(cfg),
            parent_session_id="s1",
            worker_session_id="subagent_s1_1",
            sub_id=1,
        )
        assert result is not None
        assert result["schema_version"] == 1
        assert result["source"] == "subagent"
        assert result["agent_type"] == "nl2sql"
        assert result["provider"] == "bailian"
        assert result["model"] == "qwen3.7-plus"
        assert result["worker_run_id"] == 2
        assert result["cache_control_mode"] == "explicit"
        assert result["llms"]["input_tokens"] == 1200
        assert result["llms"]["call_count"] == 4

    def test_build_perf_summary_none_summary(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "1")
        from dataagent.actions.tools.local_tool.sub_agent_entry import _build_perf_summary

        result = _build_perf_summary(
            None,
            result={},
            query="q",
            config_path=str(tmp_path / "cfg.yaml"),
            parent_session_id="s1",
            worker_session_id="subagent_s1_1",
            sub_id=1,
        )
        assert result is None


class TestStripPerfSummaryForOriginalMsg:
    def test_strips_perf_summary_adds_perf_ref(self):
        from dataagent.actions.tools.local_tool.tools import _strip_perf_summary_for_original_msg

        wr_dict = {
            "sub_id": 1,
            "parent_session_id": "s1",
            "worker_session_id": "subagent_s1_1",
            "status": "success",
            "final_answer": "done",
            "artifacts": [],
            "tool_calls_count": 0,
            "iteration_count": 0,
            "error": None,
            "resumed": False,
            "perf_summary": _perf_summary(),
        }
        stripped = _strip_perf_summary_for_original_msg(wr_dict)
        assert "perf_summary" not in stripped
        assert stripped["perf_ref"]["source"] == "subagent"
        assert stripped["perf_ref"]["schema_version"] == 1
        assert stripped["perf_ref"]["worker_session_id"] == "subagent_s1_1"
        assert stripped["final_answer"] == "done"

    def test_no_perf_summary_passthrough(self):
        from dataagent.actions.tools.local_tool.tools import _strip_perf_summary_for_original_msg

        wr_dict = {"sub_id": 1, "final_answer": "done"}
        assert _strip_perf_summary_for_original_msg(wr_dict) is wr_dict
