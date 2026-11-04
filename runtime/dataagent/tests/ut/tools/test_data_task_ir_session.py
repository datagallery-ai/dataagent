"""Multi-turn IR behavior, including real files and process boundaries (no LLM)."""

import json
import subprocess
import sys
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolHookRunner
from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config
from dataagent.actions.tools.hooks.examples import ir_hooks
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import session
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.session_store import IRSessionStore, SnapshotConflict
from dataagent.config.config_manager import ConfigManager
from dataagent.core.suite.builtin_suites.de_agent.hooks.ir_config import validate_data_task_ir_config
from dataagent.utils.runtime_paths import dataagent_package_path

PREFIX = "dataagent.actions.tools.hooks.examples.ir_hooks."
FIELD = {"field_id": "time_context", "value": {"window": {"days": 14}, "timezone": "Asia/Shanghai"}, "unresolved": []}


class Runtime:
    def __init__(self, workspace, policy="on_consume", user="u", sid="s"):
        self.workspace_dir = Path(workspace)
        self.user_id, self.session_id, self.run_id, self.sub_id = user, sid, 0, 0
        self.instructions = "Do not guess."
        self.cache = {}
        self.relation_model = Mock()
        self.relation_model.invoke.return_value = SimpleNamespace(content='{"relation":"continue","reason":"related revision"}')
        self.config = {"DATA_TASK_IR": {"refresh_policy": policy}, "TOOLS": {"builtin": [
            {"module": "plan", "function": "create_plan", "hooks": {"post": [PREFIX + "create_ir"]}},
            {"module": "plan", "function": "complete_current_todo", "hooks": {"post": [PREFIX + "update_ir"]}},
        ]}}

    def llm(self, name):
        return self.relation_model

    def get_cache(self, key, default=None):
        return self.cache.get(key, default)

    def set_cache(self, key, value):
        self.cache[key] = value

    def get_all_config(self):
        return deepcopy(self.config)

    def get_config(self, key, default=None):
        current = self.config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def prepare(runtime, query):
    state = {"user_query": query, "user_id": runtime.user_id, "session_id": runtime.session_id, "messages": []}
    validate_data_task_ir_config(state, runtime)
    return SimpleNamespace(runtime=runtime, state=state, tool_name="create_plan", tool_call_id="call",
                           execution=SimpleNamespace(success=True))


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fill = AsyncMock(return_value=[deepcopy(FIELD)])
        self.gate = AsyncMock(return_value=([], True))
        self.nodes = []
        for name, replacement in (("fill_ir_fields", self.fill), ("_ir_consistency_gate", self.gate),
                                  ("get_action_nodes", lambda runtime: self.nodes)):
            patcher = patch.object(ir_hooks, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def runtime(self, **kwargs):
        runtime = Runtime(self.tmp.name, **kwargs)
        runtime.config["DATA_TASK_IR"]["failure_policy"] = "strict"
        runtime.config["DATA_TASK_IR"]["consistency_policy"] = "enforce"
        return runtime

    async def first_turn(self, **kwargs):
        runtime = self.runtime(**kwargs)
        inv = prepare(runtime, "应用14天内下载成功次数")
        await ir_hooks.create_ir(inv)
        await ir_hooks.ensure_ir_ready(inv)
        return runtime

    async def test_no_snapshot_omits_hint_and_does_not_create_task_for_greeting(self):
        runtime = self.runtime()
        user_message = HumanMessage(content="你好")
        retained = AIMessage(id="ordinary-answer", content="旧回答")
        state = {"user_query": "你好", "user_id": "u", "session_id": "s",
                 "messages": [AIMessage(id=session.HINT_ID, content="过时提示"), retained, user_message]}
        validate_data_task_ir_config(state, runtime)
        self.assertEqual(state["messages"], [retained, user_message])
        self.assertTrue(session.enabled(runtime))
        self.assertIsNone(runtime.get_cache("ir_session")["snapshot"])
        self.assertIsNone(runtime.get_cache("ir_session")["relation"])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.fill.assert_not_awaited()
        self.gate.assert_not_awaited()

    async def test_first_task_without_hint_auto_binds_at_plan_or_sql_entry(self):
        for create_plan in (False, True):
            with self.subTest(create_plan=create_plan):
                runtime = self.runtime(sid=f"first-{create_plan}", policy="eager")
                inv = prepare(runtime, "应用14天内下载成功次数")
                self.assertEqual(inv.state["messages"], [])
                self.assertIsNone(runtime.get_cache("ir_session")["relation"])
                if create_plan:
                    await ir_hooks.create_ir(inv)
                    self.assertEqual(runtime.get_cache("ir_session")["relation"], "new")
                await ir_hooks.ensure_ir_ready(inv)
                current = runtime.get_cache("ir_session")
                self.assertEqual(current["relation"], "new")
                self.assertEqual(current["snapshot"]["queries"], ["应用14天内下载成功次数"])
                self.assertEqual(current["snapshot"]["status"], "committed")
                self.assertEqual(inv.state["messages"], [])

    async def test_existing_snapshot_supplies_business_context_without_selection_tool(self):
        first = await self.first_turn()
        previous = first.get_cache("ir_session")["snapshot"]
        runtime = self.runtime()
        inv = prepare(runtime, "改为30天")
        self.assertEqual(inv.state["messages"], [])
        session.prepare_task_context(inv.state, runtime)
        session.prepare_task_context(inv.state, runtime)
        summary = inv.state["messages"][0]
        self.assertEqual(summary.type, "system")
        from dataagent.utils.messages_utils import build_messages
        self.assertIn(summary, build_messages(inv.state["messages"]))
        self.assertIn("应用14天", summary.content)
        self.assertIn("改为30天", summary.content)
        for key in ("select_data_task_ir", "revision", "task_id", "DataTaskIR"):
            self.assertNotIn(key, summary.content)
        runtime.relation_model.invoke.assert_called_once()
        self.assertEqual(runtime.get_cache("ir_session")["store"].load()["revision"], previous["revision"])
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(runtime.get_cache("ir_session")["relation"], "continue")
        self.assertEqual(self.fill.await_args.kwargs["current_values"], [FIELD])

    async def test_existing_empty_draft_resolves_internally(self):
        first = self.runtime()
        await ir_hooks.create_ir(prepare(first, "统计曝光次数"))
        runtime = self.runtime()
        second = prepare(runtime, "改为下载次数")
        session.prepare_task_context(second.state, runtime)
        self.assertIn("统计曝光次数", second.state["messages"][0].content)
        await ir_hooks.ensure_ir_ready(second)
        self.assertEqual(runtime.get_cache("ir_session")["relation"], "continue")

    async def test_continue_reuses_baseline_and_changed_null_is_not_merged_back(self):
        first = await self.first_turn()
        second = self.runtime()
        inv = prepare(second, "把14天改成30天，取消时区限定")
        self.assertIsNone(second.get_cache("ir_field_values"))
        await ir_hooks.create_ir(inv)
        self.assertEqual(second.get_cache("ir_field_values"), [FIELD])
        updated = deepcopy(FIELD)
        updated["value"] = {"window": {"days": 30}, "timezone": None}
        self.fill.return_value = [updated]
        await ir_hooks.ensure_ir_ready(inv)
        call = self.fill.await_args.kwargs
        self.assertEqual(call["current_values"], [FIELD])
        self.assertIn("应用14天", call["user_query"])
        self.assertIn("改成30天", call["user_query"])
        saved = second.get_cache("ir_session")["store"].load()
        self.assertEqual(saved["fields"], [updated])
        self.assertEqual(saved["task_id"], first.get_cache("ir_session")["snapshot"]["task_id"])
        self.assertEqual(saved["status"], "committed")
        self.assertGreaterEqual(len(list(second.get_cache("ir_session")["store"].root.glob("versions/*.json"))), 4)

    async def test_independent_task_excludes_old_context_and_survives_failed_new_turn(self):
        self.nodes = [SimpleNamespace(action="metadata_recall", params={"query": "old"}, output="old-schema",
                                      success=True, created_at=datetime.now(UTC) + timedelta(seconds=1))]
        first = await self.first_turn()
        old_task = first.get_cache("ir_session")["snapshot"]["task_id"]
        # ContextFactory may restore old nodes; their creation time remains historical.
        self.nodes[0].created_at = datetime.now(UTC) - timedelta(days=1)
        second = self.runtime()
        inv = prepare(second, "查询用户留存率")
        session.select_task(second, "new")
        self.fill.side_effect = TimeoutError
        with self.assertRaisesRegex(RuntimeError, "提取失败"):
            await ir_hooks.ensure_ir_ready(inv)
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(second)
        self.assertIsNone(self.fill.await_args.kwargs["current_values"])
        self.assertNotIn("old-schema", self.fill.await_args.kwargs["tool_evidence"])
        third = self.runtime()
        prepare(third, "继续")
        restored = third.get_cache("ir_session")["snapshot"]
        self.assertEqual(restored["status"], "draft")
        self.assertNotEqual(restored["task_id"], old_task)
        self.assertEqual(restored["fields"], [])

    async def test_changed_query_failure_never_consumes_old_snapshot(self):
        await self.first_turn()
        runtime = self.runtime()
        inv = prepare(runtime, "改成30天")
        session.select_task(runtime, "continue")
        self.fill.side_effect = TimeoutError
        with self.assertRaises(RuntimeError):
            await ir_hooks.ensure_ir_ready(inv)
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(runtime)
        self.assertEqual(runtime.get_cache("ir_session")["store"].load()["status"], "draft")

    async def test_gate_failure_and_warning_do_not_commit_and_retry_does_not_refill(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用14天下载次数")
        self.gate.side_effect = [(["check failed"], False), (["粒度冲突"], True), ([], True)]
        for _ in range(4):
            with self.assertRaisesRegex(RuntimeError, "尚未通过"):
                await ir_hooks.ensure_ir_ready(inv)
            self.assertEqual(runtime.get_cache("ir_session")["store"].load()["status"], "draft")
        self.assertEqual(self.fill.await_count, 1)
        self.assertEqual(self.gate.await_count, 2)
        # Only genuinely changed evidence reopens the blocked signature.
        self.nodes.append(SimpleNamespace(action="read_file", params={"path": "new.md"},
                          output="new schema", success=True,
                          created_at=datetime.now(UTC) + timedelta(seconds=1)))
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.gate.await_count, 3)
        self.assertEqual(self.fill.await_count, 2)

    async def test_semantic_repair_targets_only_affected_field_once(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用14天下载次数")
        other = {"field_id": "pipeline", "value": None}
        fixed = {**FIELD, "value": {"input_windows": []}}
        self.fill.side_effect = [[FIELD, other], [fixed]]
        self.gate.side_effect = [(["time_context：窗口方向不符"], True), ([], True)]
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_args.kwargs["field_ids"], {"time_context"})
        self.assertIn("不是新增业务证据", self.fill.await_args.kwargs["confirmed_context"])
        self.assertEqual(runtime.get_cache("ir_field_values"), [fixed, other])
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_count, 2)
        self.assertEqual(self.gate.await_count, 2)

    async def test_semantic_repair_failure_blocks_unchanged_input(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用14天下载次数")
        self.gate.return_value = (["time_context：窗口方向不符"], True)
        for _ in range(4):
            with self.assertRaisesRegex(RuntimeError, "自动检查/修复已结束"):
                await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_count, 2)
        self.assertEqual(self.gate.await_count, 1)  # unchanged repair must not reroll gate
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(runtime)

    async def test_gate_protocol_failures_have_bounded_retries(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用下载次数")
        self.gate.return_value = (["invalid protocol"], False)
        for _ in range(4):
            with self.assertRaises(RuntimeError):
                await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.gate.await_count, 2)
        self.assertEqual(self.fill.await_count, 1)

    async def test_invalid_structure_never_reaches_gate_or_commits(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用ID不能含点号")
        self.fill.return_value = [{"field_id": "sources", "value": {"facts": [{
            "source_ref": "events", "filters": [{"field_ref": "app_id", "operator": "invented"}]
        }]}}]
        for _ in range(3):
            with self.assertRaisesRegex(RuntimeError, "数据结构异常"):
                await ir_hooks.ensure_ir_ready(inv)
        self.gate.assert_not_awaited()
        self.assertEqual(self.fill.await_count, 1)
        self.assertEqual(runtime.get_cache("ir_session")["store"].load()["status"], "draft")

    async def test_unknown_business_values_and_substring_operator_can_commit(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用ID不能含点号")
        self.fill.return_value = [{"field_id": "sources", "value": {"facts": [{
            "source_ref": None, "filters": [{"field_ref": "app_id", "operator": "not_contains",
                                             "operands": ["."]}], "dedup": None
        }]}, "unresolved": ["源表未知"]}]
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(runtime.get_cache("ir_session")["store"].load()["status"], "committed")

    async def test_extraction_transport_failure_has_bounded_retries(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用下载次数")
        self.fill.side_effect = TimeoutError
        for _ in range(4):
            with self.assertRaises(RuntimeError):
                await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_count, 2)
        self.gate.assert_not_awaited()

    async def test_enum_artifact_read_is_evidence_but_arbitrary_text_is_not(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用下载次数")
        for path, output in [("get_table_schema_enum_values_app_0123abcd.txt", "frame_type=0 means 双框架"),
                             ("notes.txt", "untrusted note"),
                             ("../get_table_schema_enum_values_app_abcdef12.txt", "outside workspace"),
                             ("calibration_tables.md", "frozen plan"),
                             ("insert.sql", "generated SQL")]:
            self.nodes.append(SimpleNamespace(action="read_file", params={"path": path}, output=output,
                              success=True, created_at=datetime.now(UTC) + timedelta(seconds=1)))
        await ir_hooks.ensure_ir_ready(inv)
        evidence = self.fill.await_args.kwargs["tool_evidence"]
        self.assertIn("frame_type=0", evidence)
        for text in ["untrusted note", "outside workspace", "frozen plan", "generated SQL"]:
            self.assertNotIn(text, evidence)

    async def test_degraded_fields_cannot_be_committed_and_are_refilled(self):
        runtime = self.runtime()
        inv = prepare(runtime, "应用14天下载次数")
        self.fill.return_value = [{**FIELD, "_extraction_error": "timeout"}]
        with self.assertRaises(RuntimeError):
            await ir_hooks.ensure_ir_ready(inv)
        self.fill.return_value = [FIELD]
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_count, 2)

    async def test_eager_consumer_refreshes_even_without_complete_todo(self):
        runtime = await self.first_turn(policy="eager")
        self.assertEqual(runtime.get_cache("ir_session")["snapshot"]["status"], "committed")
        second = self.runtime(policy="eager")
        inv = prepare(second, "改成30天")
        session.select_task(second, "continue")
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_count, 2)

    async def test_registered_sql_entries_refresh_without_plan_or_todo(self):
        config_path = Path(self.tmp.name) / "suite.yaml"
        config_path.write_text(yaml.safe_dump({"SUITE": {"include": ["data_engineering"]}}))
        manager = ConfigManager()
        manager.reload(str(config_path), str(dataagent_package_path("core", "flex", "flex_default_configs.yaml")))
        entries = {e.get("function"): e for e in manager.get("TOOLS.local_functions")}
        for policy in ("eager", "on_consume"):
            for tool in ("wrapped_nl2sql_sub_agent_tool", "check_and_repair_sql"):
                with self.subTest(policy=policy, tool=tool):
                    sid = policy + tool
                    await self.first_turn(policy=policy, sid=sid)
                    second = self.runtime(policy=policy, sid=sid)
                    state = prepare(second, "改成30天").state
                    hooks = load_tool_hooks_from_config(entries[tool]["hooks"])
                    inv = ToolHookInvocation(tool_name=tool, tool_call_id="consumer", tool_args={},
                                             runtime=second, metadata={}, state=state)
                    # No create_plan/complete_current_todo calls in this turn.
                    count = self.fill.await_count
                    await ToolHookRunner.run_pre_hooks(hooks.pre, inv)
                    self.assertEqual(self.fill.await_count, count + 1)
                    self.assertIn("改成30天", self.fill.await_args.kwargs["user_query"])
                    self.assertFalse(second.get_cache("ir_dirty"))
                    self.assertEqual(second.get_cache("ir_session")["snapshot"]["status"], "committed")

    async def test_exact_repeat_reuses_committed_inputs_without_model_calls(self):
        await self.first_turn()
        runtime = self.runtime()
        inv = prepare(runtime, "应用14天内下载成功次数")
        session.select_task(runtime, "continue")
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(runtime)
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.fill.await_count, 1)
        self.assertEqual(self.gate.await_count, 1)
        self.assertEqual(runtime.get_cache("ir_session")["snapshot"]["status"], "committed")

    async def test_disabled_switch_never_reads_snapshot_or_creates_files(self):
        runtime = self.runtime()
        runtime.config["TOOLS"]["builtin"] = []
        prepare(runtime, "你好")
        self.assertFalse(session.enabled(runtime))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    async def test_greeting_inspect_do_not_write_or_call_models(self):
        runtime = await self.first_turn()
        store = runtime.get_cache("ir_session")["store"]
        previous = (store.root / "current.json").read_bytes()
        second = self.runtime()
        prepare(second, "解释一下刚才的SQL")
        result = session.select_task(second, "inspect")
        self.assertEqual(result["fields"], [FIELD])
        self.assertEqual((store.root / "current.json").read_bytes(), previous)
        self.assertEqual(self.fill.await_count, 1)

    async def test_clarification_and_restart_preserve_original_question(self):
        await self.first_turn()
        second = self.runtime()
        inv = prepare(second, "调整一下")
        result = session.select_task(second, "clarify")
        self.assertTrue(result["needs_human_feedback"])
        with self.assertRaises(RuntimeError):
            await ir_hooks.ensure_ir_ready(inv)
        third = self.runtime()
        inv = prepare(third, "是上一份下载任务，改成30天")
        session.select_task(third, "continue", resolve_pending=True)
        inv.state["messages"].append(ToolMessage(content="使用北京时间", name="request_human_feedback", tool_call_id="hitl"))
        await ir_hooks.ensure_ir_ready(inv)
        self.assertIn("调整一下", self.fill.await_args.kwargs["user_query"])
        self.assertIn("使用北京时间", self.fill.await_args.kwargs["user_query"])
        self.assertEqual(third.get_cache("ir_session")["snapshot"]["feedback"][0]["content"], "使用北京时间")
        fourth = self.runtime()
        next_inv = prepare(fourth, "时区改为UTC")
        session.select_task(fourth, "continue")
        await ir_hooks.ensure_ir_ready(next_inv)
        query = self.fill.await_args.kwargs["user_query"]
        self.assertLess(query.index("使用北京时间"), query.index("时区改为UTC"))

    async def test_same_turn_clarification_cannot_be_self_confirmed(self):
        await self.first_turn()
        runtime = self.runtime()
        inv = prepare(runtime, "调整一下")
        session.select_task(runtime, "clarify")
        session.select_task(runtime, "continue", resolve_pending=True)
        with self.assertRaisesRegex(RuntimeError, "仍待用户澄清"):
            await ir_hooks.ensure_ir_ready(inv)
        inv.state["messages"].append(ToolMessage(content="继续下载任务，改成30天", name="request_human_feedback", tool_call_id="hitl"))
        await ir_hooks.ensure_ir_ready(inv)

    async def test_feedback_survives_restart_after_failed_refresh(self):
        await self.first_turn()
        runtime = self.runtime()
        inv = prepare(runtime, "改成30天")
        session.select_task(runtime, "continue")
        inv.state["messages"].append(ToolMessage(content="取消刷量过滤", name="request_human_feedback", tool_call_id="hitl"))
        self.fill.side_effect = TimeoutError
        with self.assertRaises(RuntimeError):
            await ir_hooks.ensure_ir_ready(inv)
        restarted = self.runtime()
        prepare(restarted, "继续")
        self.assertEqual(restarted.get_cache("ir_session")["snapshot"]["feedback"][0]["content"], "取消刷量过滤")

    async def test_expired_evidence_is_not_reused_and_new_read_supersedes_old(self):
        self.nodes = [SimpleNamespace(action="read_file", params={"path": "schema.md"}, output="old schema",
                                      success=True, created_at=datetime.now(UTC) + timedelta(seconds=1))]
        first = await self.first_turn()
        store = first.get_cache("ir_session")["store"]
        snapshot = store.load()
        snapshot["evidence_observed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        store.save(snapshot, expected_revision=snapshot["revision"])
        runtime = self.runtime()
        inv = prepare(runtime, "继续")
        session.select_task(runtime, "continue")
        self.nodes[0].output = "new schema"
        self.nodes[0].created_at = datetime.now(UTC)
        await ir_hooks.ensure_ir_ready(inv)
        self.assertIsNone(self.fill.await_args.kwargs["current_values"])
        self.assertNotIn("old schema", self.fill.await_args.kwargs["tool_evidence"])
        self.assertIn("new schema", self.fill.await_args.kwargs["tool_evidence"])

    async def test_user_isolation_and_subagents_do_not_restore_main_ir(self):
        await self.first_turn()
        runtime = self.runtime(user="other")
        prepare(runtime, "继续")
        self.assertIsNone(runtime.get_cache("ir_session")["snapshot"])
        child = self.runtime()
        state = {"user_query": "子任务", "user_id": "u", "session_id": "s", "sub_id": 1}
        validate_data_task_ir_config(state, child)
        self.assertFalse(session.enabled(child))

    async def test_incompatible_configuration_replays_user_requirements_without_old_fields(self):
        await self.first_turn()
        runtime = self.runtime()
        runtime.instructions = "Changed catalog/environment"
        inv = prepare(runtime, "继续")
        session.select_task(runtime, "continue")
        await ir_hooks.ensure_ir_ready(inv)
        self.assertIsNone(self.fill.await_args.kwargs["current_values"])
        self.assertIn("应用14天", self.fill.await_args.kwargs["user_query"])

    async def test_isolation_and_concurrent_writer_conflict(self):
        first = await self.first_turn()
        other = self.runtime(sid="different-session")
        prepare(other, "你好")
        self.assertIsNone(other.get_cache("ir_session")["snapshot"])
        a, b = self.runtime(), self.runtime()
        prepare(a, "改成30天")
        prepare(b, "改成60天")
        session.select_task(a, "continue")
        with self.assertRaises(SnapshotConflict):
            session.select_task(b, "continue")
        self.assertEqual(first.get_cache("ir_session")["store"].load()["queries"][-1], "改成30天")

    async def test_cross_process_restore_and_trace_paths_never_collide(self):
        first = await self.first_turn()
        store = first.get_cache("ir_session")["store"]
        program = '''
import json, sys
from pathlib import Path
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.session_store import IRSessionStore
snapshot = IRSessionStore(Path(sys.argv[1]), "u", "s").load()
print(json.dumps({"task": snapshot["task_id"], "fields": snapshot["fields"]}))
'''
        result = subprocess.run([sys.executable, "-c", program, str(store.root.parent.parent)],
                                capture_output=True, text=True, check=True)
        restored = json.loads(result.stdout)
        self.assertEqual(restored["fields"], [FIELD])
        self.assertEqual(restored["task"], store.load()["task_id"])
        second = self.runtime()
        prepare(second, "继续")
        self.assertNotEqual(ir_hooks._resolve_ir_trace_dir(first), ir_hooks._resolve_ir_trace_dir(second))

    async def test_internal_new_excludes_history_from_summary_and_fill(self):
        first = await self.first_turn()
        old = first.get_cache("ir_session")["snapshot"]["task_id"]
        runtime = self.runtime()
        runtime.relation_model.invoke.return_value.content = '{"relation":"new","reason":"独立曝光任务"}'
        inv = prepare(runtime, "按设备统计曝光")
        session.prepare_task_context(inv.state, runtime)
        self.assertNotIn("下载", inv.state["messages"][0].content)
        self.assertNotIn("未修改的要求保留", inv.state["messages"][0].content)
        await ir_hooks.ensure_ir_ready(inv)
        self.assertIsNone(self.fill.await_args.kwargs["current_values"])
        self.assertNotIn("下载", self.fill.await_args.kwargs["user_query"])
        self.assertNotEqual(old, runtime.get_cache("ir_session")["snapshot"]["task_id"])
        runtime.relation_model.invoke.assert_called_once()

    async def test_failed_first_fill_retains_evidence_for_related_turn(self):
        runtime = self.runtime()
        runtime.config["DATA_TASK_IR"]["failure_policy"] = "fallback_without_ir"
        self.nodes = [SimpleNamespace(action="metadata_recall", params={"query":"downloads"},
            output="download schema", success=True, created_at=datetime.now(UTC)+timedelta(seconds=1))]
        inv = prepare(runtime, "应用下载次数")
        self.fill.side_effect = TimeoutError
        await ir_hooks.ensure_ir_ready(inv)
        saved = runtime.get_cache("ir_session")["store"].load()
        self.assertEqual(saved["fields"], [])
        self.assertEqual(len(saved["evidence"]), 1)
        self.assertEqual(saved["status"], "draft")
        self.nodes = []
        self.fill.side_effect = None
        second = self.runtime()
        second.config["DATA_TASK_IR"]["failure_policy"] = "fallback_without_ir"
        inv = prepare(second, "改为7天")
        await ir_hooks.ensure_ir_ready(inv)
        self.assertIn("download schema", self.fill.await_args.kwargs["tool_evidence"])
        self.assertEqual(second.get_cache("ir_session")["snapshot"]["task_id"], saved["task_id"])

    async def test_internal_inspect_preserves_snapshot_and_avoids_field_calls(self):
        first = await self.first_turn()
        store = first.get_cache("ir_session")["store"]
        before = store.load()
        runtime = self.runtime()
        runtime.config["DATA_TASK_IR"]["failure_policy"] = "fallback_without_ir"
        runtime.relation_model.invoke.return_value.content = '{"relation":"inspect","reason":"只解释"}'
        inv = prepare(runtime, "解释一下刚才SQL")
        session.prepare_task_context(inv.state, runtime)
        self.assertIn("应用14天", inv.state["messages"][0].content)
        calls = self.fill.await_count
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(ir_hooks.get_ir_context(runtime), "")
        self.assertEqual(self.fill.await_count, calls)
        self.assertEqual(store.load(), before)

    async def test_relation_failure_or_uncertain_never_inherits_or_overwrites(self):
        first = await self.first_turn()
        store = first.get_cache("ir_session")["store"]
        previous = store.load()
        for output in ('garbage', '{"relation":"uncertain","reason":"指代不明"}', TimeoutError):
            runtime = self.runtime()
            runtime.config["DATA_TASK_IR"]["failure_policy"] = "fallback_without_ir"
            if output is TimeoutError:
                runtime.relation_model.invoke.side_effect = TimeoutError
            else:
                runtime.relation_model.invoke.return_value.content = output
            inv = prepare(runtime, "改一下")
            before = self.fill.await_count
            session.prepare_task_context(inv.state, runtime)
            for _ in range(2):
                await ir_hooks.ensure_ir_ready(inv)
                self.assertEqual(ir_hooks.get_ir_context(runtime), "")
            runtime.relation_model.invoke.assert_called_once()
            self.assertEqual(self.fill.await_count, before)
            self.assertEqual(store.load(), previous)
            self.assertNotIn("应用14天", inv.state["messages"][0].content)

    async def test_relation_failure_strict_and_configuration_errors_propagate(self):
        await self.first_turn()
        runtime = self.runtime()
        runtime.relation_model.invoke.side_effect = TimeoutError
        inv = prepare(runtime, "改一下")
        with self.assertRaisesRegex(RuntimeError, "任务关联"):
            await ir_hooks.ensure_ir_ready(inv)
        runtime = self.runtime()
        runtime.llm = Mock(side_effect=RuntimeError("model configuration missing"))
        inv = prepare(runtime, "改成7天")
        with self.assertRaisesRegex(RuntimeError, "configuration"):
            session.prepare_task_context(inv.state, runtime)

    async def test_business_summary_remains_stable_after_binding_and_repeat(self):
        await self.first_turn()
        runtime = self.runtime()
        inv = prepare(runtime, "改为7天")
        session.prepare_task_context(inv.state, runtime)
        before = inv.state["messages"][0].content
        self.assertIn("未修改的要求保留", before)
        await ir_hooks.ensure_ir_ready(inv)
        session.prepare_task_context(inv.state, runtime)
        self.assertEqual(inv.state["messages"][0].content, before)
        self.assertEqual(len(inv.state["messages"]), 1)
        runtime.relation_model.invoke.assert_called_once()

    async def test_first_turn_and_disabled_context_never_call_relation_model(self):
        runtime = self.runtime()
        inv = prepare(runtime, "你好")
        session.prepare_task_context(inv.state, runtime)
        self.assertEqual(inv.state["messages"], [])
        self.assertIsNone(runtime.get_cache("ir_session")["snapshot"])
        runtime.relation_model.invoke.assert_not_called()
        runtime = self.runtime()
        runtime.config["TOOLS"]["builtin"] = []
        inv = prepare(runtime, "统计下载次数")
        session.prepare_task_context(inv.state, runtime)
        runtime.relation_model.invoke.assert_not_called()
        self.assertIsNone(runtime.get_cache("ir_session"))

    async def test_suite_registers_context_hook_without_selection_tool(self):
        path = Path(self.tmp.name)/"config.yaml"
        path.write_text(yaml.safe_dump({"SUITE":{"include":["data_engineering"]}}))
        manager = ConfigManager()
        manager.reload(str(path), str(dataagent_package_path("core", "flex", "flex_default_configs.yaml")))
        entries = manager.get("TOOLS.local_functions")
        self.assertNotIn("select_data_task_ir", [e.get("function") for e in entries])
        pre = manager.get("HOOKS.nodes.planner.pre")
        self.assertTrue(any("prepare_data_task_context" in str(h) for h in pre))


class StoreTests(unittest.TestCase):
    def test_corrupt_snapshot_is_not_silently_overwritten(self):
        with TemporaryDirectory() as tmp:
            store = IRSessionStore(Path(tmp), "u", "s")
            store.root.mkdir(parents=True)
            (store.root / "current.json").write_text('{broken')
            with self.assertRaises(ValueError):
                store.save({"status": "draft"}, expected_revision=None)
            self.assertEqual((store.root / "current.json").read_text(), '{broken')


if __name__ == "__main__":
    unittest.main()
