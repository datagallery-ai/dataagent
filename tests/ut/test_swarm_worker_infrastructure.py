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
import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.context.message_history import (
    read_messages_file,
    sanitize_messages,
    serialize_message,
    write_messages_file,
)
from dataagent.core.swarm.worker_lock import acquire_worker_lock, release_worker_lock
from dataagent.core.swarm.worker_memory import load_worker_messages, persist_worker_messages
from dataagent.core.swarm.worker_metadata import (
    build_worker_metadata_context,
    list_worker_metadata,
    load_worker_metadata,
    upsert_worker_metadata,
)
from dataagent.core.swarm.worker_result import synthesize_worker_result
from dataagent.utils.constants import MAX_WORKER_METADATA_ARTIFACTS
from dataagent.utils.runtime_paths import resolve_worker_memory_dir, resolve_worker_root


def test_runtime_paths_resolve_worker_memory_under_parent_session(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    root = resolve_worker_root(user_id="u", parent_session_id="parent-s", sub_id=123456)
    mem_dir = resolve_worker_memory_dir(user_id="u", parent_session_id="parent-s", sub_id=123456)

    assert root == (tmp_path / "dataagent-home" / "u" / "parent-s" / "workers" / "123456").resolve()
    assert mem_dir == root / ".memory"
    assert mem_dir.is_dir()


def test_history_writer_public_helpers_round_trip_messages(tmp_path):
    path = tmp_path / "messages.json"
    messages = [HumanMessage(content="hello"), AIMessage(content="world")]

    assert serialize_message(messages[0])["type"] == "HumanMessage"
    write_messages_file(path, messages)

    loaded = read_messages_file(path)
    assert [type(msg).__name__ for msg in loaded] == ["HumanMessage", "AIMessage"]
    assert [msg.content for msg in loaded] == ["hello", "world"]
    assert sanitize_messages(loaded) == loaded


def test_worker_memory_uses_history_writer_public_format(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    persist_worker_messages(
        user_id="u",
        parent_session_id="parent-s",
        sub_id=123456,
        messages=[HumanMessage(content="hello"), AIMessage(content="world")],
    )

    loaded = load_worker_messages(user_id="u", parent_session_id="parent-s", sub_id=123456)
    assert loaded is not None
    assert [msg.content for msg in loaded] == ["hello", "world"]


def test_worker_metadata_uses_custom_workers_dir_with_parent_workspace(tmp_path):
    parent_workspace = tmp_path / "parent-ws"
    parent_workspace.mkdir()
    config = {"WORKSPACE_POLICY": {"layout": {"workers_dir": "swarm"}}}

    upsert_worker_metadata(
        user_id="u",
        parent_session_id="parent-s",
        worker_session_id="subagent_parent-s_123456",
        sub_id=123456,
        config_path="/tmp/sub.yaml",
        query="first",
        worker_result={
            "status": "success",
            "final_answer": "first summary",
            "artifacts": ["/tmp/a.csv"],
            "error": None,
        },
        status="success",
        last_run_id_executed=0,
        parent_workspace=parent_workspace,
        config=config,
    )

    meta_path = parent_workspace / "swarm" / "123456" / ".memory" / "metadata.json"
    assert meta_path.is_file()
    listed = list_worker_metadata(
        user_id="u",
        parent_session_id="parent-s",
        parent_workspace=parent_workspace,
        config=config,
    )
    assert len(listed) == 1


def test_worker_metadata_upsert_and_context_scan(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    first = upsert_worker_metadata(
        user_id="u",
        parent_session_id="parent-s",
        worker_session_id="subagent_parent-s_123456",
        sub_id=123456,
        config_path="/tmp/sub.yaml",
        query="first",
        worker_result={
            "status": "success",
            "final_answer": "first summary",
            "artifacts": ["/tmp/a.csv"],
            "error": None,
        },
        status="success",
        last_run_id_executed=0,
    )
    second = upsert_worker_metadata(
        user_id="u",
        parent_session_id="parent-s",
        worker_session_id="subagent_parent-s_123457",
        sub_id=123457,
        config_path="/tmp/sub.yaml",
        query="second",
        worker_result={
            "status": "timeout",
            "final_answer": "",
            "artifacts": [],
            "error": "timeout",
        },
        status="timeout",
        error="timeout",
        last_run_id_executed=0,
    )

    assert first.last_run_id == 0
    assert second.status == "timeout"
    listed = list_worker_metadata(user_id="u", parent_session_id="parent-s")
    assert {item.sub_id for item in listed} == {123456, 123457}
    context = build_worker_metadata_context(user_id="u", parent_session_id="parent-s", limit=1)
    assert len(context) == 1
    assert set(context[0]) == {"sub_id", "last_query", "last_answer", "artifacts", "error"}


def test_worker_metadata_merges_artifacts_and_caps(monkeypatch, tmp_path):
    """Repeated upserts for the same worker should accumulate artifact paths with a cap."""
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    upsert_worker_metadata(
        user_id="u",
        parent_session_id="parent-s",
        worker_session_id="subagent_parent-s_123456",
        sub_id=123456,
        config_path="/tmp/sub.yaml",
        query="first",
        worker_result={
            "status": "success",
            "final_answer": "s1",
            "artifacts": ["/tmp/a.csv", "/tmp/b.csv"],
            "error": None,
        },
        status="success",
        last_run_id_executed=0,
    )
    upsert_worker_metadata(
        user_id="u",
        parent_session_id="parent-s",
        worker_session_id="subagent_parent-s_123456",
        sub_id=123456,
        config_path="/tmp/sub.yaml",
        query="second",
        worker_result={
            "status": "success",
            "final_answer": "s2",
            "artifacts": ["/tmp/b.csv", "/tmp/c.csv"],
            "error": None,
        },
        status="success",
        last_run_id_executed=1,
    )

    meta_path = resolve_worker_memory_dir(user_id="u", parent_session_id="parent-s", sub_id=123456) / "metadata.json"
    artifacts = json.loads(meta_path.read_text(encoding="utf-8"))["artifacts"]
    assert artifacts == ["/tmp/a.csv", "/tmp/b.csv", "/tmp/c.csv"]

    for idx in range(60):
        upsert_worker_metadata(
            user_id="u",
            parent_session_id="parent-s",
            worker_session_id="subagent_parent-s_123456",
            sub_id=123456,
            config_path="/tmp/sub.yaml",
            query=f"q-{idx}",
            worker_result={
                "status": "success",
                "final_answer": "s",
                "artifacts": [f"/extra/{idx}.txt"],
                "error": None,
            },
            status="success",
            last_run_id_executed=idx + 2,
        )

    capped = json.loads(meta_path.read_text(encoding="utf-8"))["artifacts"]
    assert len(capped) == MAX_WORKER_METADATA_ARTIFACTS


def test_worker_messages_overwrite_on_each_persist(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    persist_worker_messages(
        user_id="u",
        parent_session_id="parent-s",
        sub_id=123456,
        messages=[HumanMessage(content="one"), AIMessage(content="two")],
    )

    persist_worker_messages(
        user_id="u",
        parent_session_id="parent-s",
        sub_id=123456,
        messages=[
            HumanMessage(content="one"),
            AIMessage(content="two"),
            HumanMessage(content="three"),
            AIMessage(content="four"),
        ],
    )

    loaded = load_worker_messages(user_id="u", parent_session_id="parent-s", sub_id=123456)
    assert loaded is not None
    contents = [m.content for m in loaded]
    assert contents == ["one", "two", "three", "four"]


def test_worker_messages_second_persist_replaces_full_transcript(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    persist_worker_messages(
        user_id="u",
        parent_session_id="parent-s",
        sub_id=123456,
        messages=[HumanMessage(content="a"), AIMessage(content="b")],
    )

    persist_worker_messages(
        user_id="u",
        parent_session_id="parent-s",
        sub_id=123456,
        messages=[AIMessage(content="b"), HumanMessage(content="c"), AIMessage(content="d")],
    )

    loaded = load_worker_messages(user_id="u", parent_session_id="parent-s", sub_id=123456)
    assert loaded is not None
    contents = [m.content for m in loaded]
    assert contents == ["b", "c", "d"]


def test_worker_lock_busy_and_release(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    lock = acquire_worker_lock(
        user_id="u",
        parent_session_id="parent-s",
        sub_id=123456,
        query="running",
        ttl_seconds=60,
    )
    assert lock is not None
    lock_file = Path(lock.lock_dir) / "lock.json"
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    assert payload["token"] == lock.token

    assert (
        acquire_worker_lock(
            user_id="u",
            parent_session_id="parent-s",
            sub_id=123456,
            query="second",
            ttl_seconds=60,
        )
        is None
    )

    release_worker_lock(lock)
    assert not Path(lock.lock_dir).exists()


def test_synthesize_worker_result_prefers_explicit_final_answer():
    """Explicit ``final_answer`` in state must beat assistant message extraction."""
    state = {
        "messages": [AIMessage(content="from assistant")],
        "final_answer": "explicit",
    }
    got = synthesize_worker_result(final_state=state, sub_id=1, parent_session_id="parent-s")
    assert got.final_answer == "explicit"


def test_synthesize_worker_result_uses_last_visible_assistant_after_tool_round():
    """Skip tool-only AIMessage rounds and use the last assistant text body."""
    tc = [{"name": "multiply", "args": {"a": 1, "b": 2}, "id": "call_x", "type": "tool_call"}]
    state = {
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=tc),
            ToolMessage(content="2", tool_call_id="call_x", name="multiply"),
            AIMessage(content="answer is 2"),
        ],
    }
    got = synthesize_worker_result(final_state=state, sub_id=1, parent_session_id="parent-s")
    assert got.final_answer == "answer is 2"


def test_synthesize_worker_result_reads_dict_ai_messages():
    """Accept serialized ``{"type": "AIMessage", ...}`` entries when present."""
    state = {
        "messages": [
            {"type": "HumanMessage", "content": "q"},
            {"type": "AIMessage", "content": "done", "tool_calls": []},
        ],
    }
    got = synthesize_worker_result(final_state=state, sub_id=1, parent_session_id="parent-s")
    assert got.final_answer == "done"


def test_synthesize_worker_result_state_summary_fills_final_answer():
    """Graph state may expose ``summary`` text as the sole answer when defined."""
    state = {"summary": "short meta"}
    got = synthesize_worker_result(final_state=state, sub_id=1, parent_session_id="parent-s")
    assert got.final_answer == "short meta"


def test_synthesize_worker_result_iteration_count_prefers_curr_iter_over_run_id():
    """Planner steps come from Flex ``curr_iter``; swarm ``run_id`` is not planner depth."""
    state = {"curr_iter": 2, "run_id": 9, "final_answer": "ok"}
    got = synthesize_worker_result(final_state=state, sub_id=1, parent_session_id="parent-s")
    assert got.iteration_count == 2


def test_synthesize_worker_result_iteration_count_ignores_run_id_without_curr_iter():
    """Without ``curr_iter``, ``run_id`` must not fill ``iteration_count``."""
    state = {"run_id": 9, "final_answer": "ok"}
    got = synthesize_worker_result(final_state=state, sub_id=1, parent_session_id="parent-s")
    assert got.iteration_count == 0


def test_synthesize_worker_result_iteration_count_fallback_explicit_counter():
    """Non-Flex graphs may expose only ``iteration_count`` / ``iterations``."""
    got_explicit = synthesize_worker_result(
        final_state={"iteration_count": 4, "final_answer": "ok"},
        sub_id=1,
        parent_session_id="parent-s",
    )
    assert got_explicit.iteration_count == 4
    got_iter = synthesize_worker_result(
        final_state={"iterations": 3, "final_answer": "ok"},
        sub_id=1,
        parent_session_id="parent-s",
    )
    assert got_iter.iteration_count == 3


def test_worker_metadata_reads_legacy_last_summary_key(monkeypatch, tmp_path):
    """Older ``metadata.json`` files used ``last_summary``; loading must still work."""
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    mem = resolve_worker_memory_dir(user_id="u", parent_session_id="parent-s", sub_id=999999)
    legacy = {
        "sub_id": 999999,
        "user_id": "u",
        "parent_session_id": "parent-s",
        "worker_session_id": "subagent_parent-s_999999",
        "config_path": "/tmp/x.yaml",
        "agent_name": "x",
        "status": "success",
        "created_at": "t0",
        "last_invoked_at": "t1",
        "last_run_id": 0,
        "last_query": "q",
        "last_summary": "legacy text",
        "artifacts": [],
        "error": None,
    }
    (mem / "metadata.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = load_worker_metadata(user_id="u", parent_session_id="parent-s", sub_id=999999)
    assert loaded is not None
    assert loaded.last_answer == "legacy text"
