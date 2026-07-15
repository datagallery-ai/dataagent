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
"""Tests for dataagent.core.utils.performance."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from dataagent.core.utils.performance import (
    PerformanceCollector,
    bind_agent_performance,
    bind_current_collector,
    build_state_summary,
    create_collector,
    get_current_collector,
    is_noop,
    make_perf_state_holder,
    performance_enabled_from_env,
    run_in_perf_context,
    submit_in_perf_context,
    summarize_llm_usage,
    update_latest_state_from_stream_item,
)


@pytest.fixture
def perf_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """重定向 DataAgent 运行目录到临时路径，确保 collector 文件落到隔离目录。"""
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path))
    return tmp_path


def _active_collector(
    user_id: str = "u", session_id: str = "s", run_id: str = "r-1", sub_id: int = 0
) -> PerformanceCollector:
    return PerformanceCollector(enabled=True, user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id)


# ---------------------------------------------------------------------------
# disabled / no-op
# ---------------------------------------------------------------------------


def test_create_collector_uses_env_only(perf_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAAGENT_PERFORMANCE_ENABLED", raising=False)
    off = create_collector(user_id="u", session_id="s", run_id="r1")
    assert is_noop(off)
    assert off.flush({}) is None

    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "1")
    on = create_collector(user_id="u", session_id="s", run_id="r2")
    assert not is_noop(on)
    assert on.jsonl_path is not None and on.jsonl_path.suffix == ".jsonl"

    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "false")
    off2 = create_collector(user_id="u", session_id="s", run_id="r3")
    assert is_noop(off2)


def test_performance_enabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAAGENT_PERFORMANCE_ENABLED", raising=False)
    assert not performance_enabled_from_env()
    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "on")
    assert performance_enabled_from_env()
    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "bogus")
    assert not performance_enabled_from_env()


def test_path_is_process_isolated(perf_home: Path) -> None:
    """同 run_id 在不同进程下应天然落到不同文件（路径含 PID）。"""
    collector = _active_collector(run_id="shared", sub_id=7)
    assert collector.jsonl_path is not None
    assert collector.jsonl_path.name == f"Runshared_Sub7.{os.getpid()}.jsonl"
    assert ".performance" in collector.jsonl_path.parts


def test_bind_agent_performance_reads_sub_id_from_state(perf_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent binding must preserve the trajectory sub id in the collector identity."""
    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "1")
    state = {"user_id": "u", "session_id": "s", "run_id": 3, "sub_id": 42}

    with bind_agent_performance(object(), state=state) as collector:
        assert collector.sub_id == "42"
        assert collector.jsonl_path is not None
        assert collector.jsonl_path.name == f"Run3_Sub42.{os.getpid()}.jsonl"


def test_disabled_does_not_write_files() -> None:
    collector = PerformanceCollector(enabled=False)
    with collector.measure("agent", "agent"), collector.measure("tool", "x"):
        pass
    assert collector.events == []
    assert collector.flush({}) is None
    assert collector.jsonl_path is None


# ---------------------------------------------------------------------------
# active collector basics
# ---------------------------------------------------------------------------


def test_records_success_event(perf_home: Path) -> None:
    collector = _active_collector()
    with collector.measure("node", "planner") as handle:
        handle["node_kind"] = "planner"
        handle.update(slot="left")
    events = collector.events
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "node"
    assert ev["name"] == "planner"
    assert ev["success"] is True
    assert ev.get("run_id") == "r-1"
    assert ev.get("sub_id") == "0"
    assert ev.get("pid") == os.getpid()
    assert "error_type" not in ev
    assert ev["elapsed_ms"] >= 0.0
    assert ev["extra"]["node_kind"] == "planner"
    assert ev["extra"]["slot"] == "left"


def test_exception_marks_failure_and_reraises(perf_home: Path) -> None:
    collector = _active_collector()
    with pytest.raises(RuntimeError, match="boom"), collector.measure("tool", "bash"):
        raise RuntimeError("boom")
    ev = collector.events[0]
    assert ev["success"] is False
    assert ev["error_type"] == "RuntimeError"


def test_summary_aggregates(perf_home: Path) -> None:
    collector = _active_collector()
    for _ in range(3):
        with collector.measure("node", "planner"):
            pass
    with collector.measure("node", "executor"):
        pass
    with collector.measure("llm", "qwen3"):
        pass
    with pytest.raises(ValueError), collector.measure("tool", "bash"):
        raise ValueError("bad")
    summary = collector.build_summary({"num_turns": 2})
    assert summary["agent"]["num_turns"] == 2
    assert summary["nodes"]["planner"]["count"] == 3
    assert summary["nodes"]["executor"]["count"] == 1
    assert summary["llms"]["qwen3"]["count"] == 1
    assert summary["tools"]["bash"]["count"] == 1
    assert summary["tools"]["bash"]["error_count"] == 1
    assert "error_type" not in summary["tools"]["bash"]
    assert collector.events[-1]["error_type"] == "ValueError"


# ---------------------------------------------------------------------------
# real-time jsonl + footer line on flush
# ---------------------------------------------------------------------------


def test_jsonl_is_written_in_real_time(perf_home: Path) -> None:
    """Each event must be visible in the jsonl file BEFORE flush() is called."""
    collector = _active_collector()
    assert collector.jsonl_path is not None
    with collector.measure("node", "planner"):
        pass
    with collector.measure("tool", "bash"):
        pass
    lines = collector.jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["kind"] == "node"
    assert parsed[1]["kind"] == "tool"
    assert all(record.get("run_id") == "r-1" for record in parsed)
    assert all(record.get("sub_id") == "0" for record in parsed)
    assert all(record.get("pid") == os.getpid() for record in parsed)


def test_flush_appends_footer_to_jsonl(perf_home: Path) -> None:
    collector = _active_collector()
    with collector.measure("node", "planner"):
        pass
    out = collector.flush({"num_turns": 1})
    assert out is not None and out.exists()
    assert out.suffix == ".jsonl"
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    node_ev = json.loads(lines[0])
    assert node_ev["kind"] == "node"
    footer = json.loads(lines[1])
    assert footer["kind"] == "_flush"
    metadata = footer.get("metadata", {})
    assert metadata.get("run_id") == "r-1"
    assert metadata.get("sub_id") == "0"
    assert metadata.get("pid") == os.getpid()
    assert footer["summary"]["nodes"]["planner"]["count"] == 1


def test_flush_returns_none_when_footer_append_fails(perf_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Footer 写入失败必须吞掉，不影响调用方。"""
    collector = _active_collector()
    with collector.measure("agent", "agent"):
        pass
    assert collector._jsonl_fh is not None

    def _boom(*_a: Any, **_kw: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(collector._jsonl_fh, "write", _boom)
    assert collector.flush({}) is None


def test_jsonl_open_failure_does_not_break_collector(perf_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_open = builtins.open

    def _bad_open(*a: Any, **kw: Any) -> Any:
        if a and isinstance(a[0], Path) and str(a[0]).endswith(".jsonl"):
            raise OSError("read-only fs")
        return real_open(*a, **kw)

    monkeypatch.setattr(builtins, "open", _bad_open)
    collector = _active_collector()
    with collector.measure("node", "planner"):
        pass
    assert len(collector.events) == 1


# ---------------------------------------------------------------------------
# concurrency / isolation
# ---------------------------------------------------------------------------


def test_per_instance_lock_under_concurrent_appends(perf_home: Path) -> None:
    coll_a = _active_collector(run_id="r-a")
    coll_b = _active_collector(run_id="r-b")

    def _worker(coll: PerformanceCollector, name: str, count: int) -> None:
        for _ in range(count):
            with coll.measure("tool", name):
                pass

    threads = [
        threading.Thread(target=_worker, args=(coll_a, "tool_a", 100)),
        threading.Thread(target=_worker, args=(coll_a, "tool_a", 100)),
        threading.Thread(target=_worker, args=(coll_b, "tool_b", 50)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(coll_a.events) == 200
    assert len(coll_b.events) == 50


def test_contextvar_get_returns_noop_by_default() -> None:
    assert is_noop(get_current_collector())


def test_bind_current_collector_token_reset(perf_home: Path) -> None:
    coll = _active_collector()
    with bind_current_collector(coll):
        assert get_current_collector() is coll
    assert is_noop(get_current_collector())


def test_run_in_perf_context_propagates_collector(perf_home: Path) -> None:
    coll = _active_collector()

    def _work() -> None:
        with get_current_collector().measure("llm", "in_thread"):
            pass

    with bind_current_collector(coll):
        run_in_perf_context(_work)
    assert any(ev["kind"] == "llm" and ev["name"] == "in_thread" for ev in coll.events)


def test_submit_in_perf_context_propagates_collector(perf_home: Path) -> None:
    coll = _active_collector()

    def _work() -> None:
        with get_current_collector().measure("llm", "pool_worker"):
            pass

    with bind_current_collector(coll), ThreadPoolExecutor(max_workers=1) as executor:
        fut = submit_in_perf_context(executor, _work)
        fut.result()
    assert any(ev["kind"] == "llm" and ev["name"] == "pool_worker" for ev in coll.events)


# ---------------------------------------------------------------------------
# safe summaries
# ---------------------------------------------------------------------------


def test_summarize_llm_usage_normalizes_ints() -> None:
    usage = summarize_llm_usage({"input_tokens": "10", "output_tokens": None, "total_tokens": 12})
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 0,
        "total_tokens": 12,
        "input_cache_read_tokens": 0,
        "input_cache_creation_tokens": 0,
        "output_reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# legacy state-derived metrics integration
# ---------------------------------------------------------------------------


class _UsageMessage:
    def __init__(self, usage: dict[str, int]) -> None:
        self.usage_metadata = usage


def test_build_state_summary_aggregates_tokens() -> None:
    state = {
        "num_turns": 3,
        "num_valid_tool_calls": 4,
        "num_invalid_tool_calls": 1,
        "messages": [
            _UsageMessage({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            _UsageMessage({"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}),
        ],
    }
    summary = build_state_summary(state)
    assert summary["agent"]["num_turns"] == 3
    assert summary["llms"] == {
        "input_tokens": 30,
        "output_tokens": 9,
        "total_tokens": 39,
        "input_cache_read_tokens": 0,
        "input_cache_creation_tokens": 0,
        "output_reasoning_tokens": 0,
    }
    assert summary["tools"]["num_valid_tool_calls"] == 4
    assert summary["tools"]["num_invalid_tool_calls"] == 1


def test_build_state_summary_tolerates_missing_fields() -> None:
    assert build_state_summary(None)["agent"]["num_turns"] == 0
    assert build_state_summary({})["llms"]["total_tokens"] == 0


def test_make_perf_state_holder_tracks_mutations() -> None:
    latest, flush_provider = make_perf_state_holder({"session_id": "s1"})
    assert flush_provider()["session_id"] == "s1"
    latest["state"] = {"session_id": "s1", "num_turns": 3}
    assert flush_provider()["num_turns"] == 3


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (("values", {"num_turns": 2}), {"num_turns": 2}),
        (("updates", {"num_turns": 9}), None),
        ({"num_turns": 4}, {"num_turns": 4}),
        ({"error": {"message": "boom"}}, None),
    ],
)
def test_update_latest_state_from_stream_item(item: Any, expected: dict[str, Any] | None) -> None:
    latest, flush_provider = make_perf_state_holder({"session_id": "s1"})
    update_latest_state_from_stream_item(item, latest)
    if expected is None:
        assert flush_provider() == {"session_id": "s1"}
    else:
        assert flush_provider() == expected
