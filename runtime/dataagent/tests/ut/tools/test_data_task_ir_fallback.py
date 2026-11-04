"""IR failure policy at actual registered hooks, independent of conversation mode."""
import asyncio
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml
from langchain_core.messages import ToolMessage

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolHookRunner
from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config
from dataagent.actions.tools.hooks.examples import ir_hooks
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import session
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.session_store import SnapshotConflict
from dataagent.core.flex.nodes.executor import Executor, NormalizedToolExecution
from dataagent.core.suite.builtin_suites.de_agent.hooks.ir_config import validate_data_task_ir_config
from dataagent.utils.runtime_paths import dataagent_package_path
from tests.ut.tools.test_data_task_ir_session import FIELD, Runtime, prepare


class IRFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fill = AsyncMock(return_value=[deepcopy(FIELD)])
        self.gate = AsyncMock(return_value=([], True))
        self.nodes = []
        for name, replacement in [("fill_ir_fields", self.fill), ("_ir_consistency_gate", self.gate),
                                  ("get_action_nodes", lambda runtime: self.nodes)]:
            p = patch.object(ir_hooks, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        path = dataagent_package_path("core", "suite", "builtin_suites", "de_agent", "tools", "tools.yaml")
        self.entries = {e['function']: e for e in yaml.safe_load(path.read_text())['TOOLS']['local_functions']}

    def runtime(self, multi=True, refresh="eager", failure=None, sid="s"):
        runtime = Runtime(self.tmp.name, policy=refresh, sid=sid)
        runtime.config["DATA_TASK_IR"]["multi_turn"] = multi
        # These regression cases explicitly exercise the retained enforcement path.
        runtime.config["DATA_TASK_IR"]["consistency_policy"] = "enforce"
        if failure is not None:
            runtime.config["DATA_TASK_IR"]["failure_policy"] = failure
        return runtime

    async def consumer(self, runtime, state, tool):
        inv = ToolHookInvocation(tool_name=tool, tool_call_id="sql", tool_args={},
                                 runtime=runtime, metadata={}, state=state)
        hooks = load_tool_hooks_from_config(self.entries[tool]["hooks"])
        await ToolHookRunner.run_pre_hooks(hooks.pre, inv)
        # This is the consumer boundary. No IR may escape when fallback is active.
        context = ir_hooks.get_ir_context(runtime)
        inv.execution = NormalizedToolExecution(
            tool, "sql", {}, True, original_msg="SQL tool PASS", output_text="SQL output",
            frontend_msg="SQL display", raw_result={"original_msg": "SQL tool PASS"},
            metadata={"existing": "keep"},
        )
        await ToolHookRunner.run_post_hooks(hooks.post, inv)
        message = Executor._build_tool_message(None, inv.execution)
        return context, message, inv

    async def test_default_fallback_all_modes_runs_both_consumers_without_ir(self):
        for multi in (False, True):
            for refresh in ("eager", "on_consume"):
                with self.subTest(multi=multi, refresh=refresh):
                    runtime = self.runtime(multi, refresh, sid=f"{multi}-{refresh}")
                    inv = prepare(runtime, "统计14天下载次数")
                    self.gate.return_value = (["time_context：窗口冲突"], True)
                    before = self.fill.await_count
                    # Include todo hook: neither status nor any visible text changes.
                    inv.execution = NormalizedToolExecution(
                        "complete_current_todo", "todo", {}, True, original_msg="TODO completed",
                        output_text="TODO output", frontend_msg="TODO display",
                        raw_result={"original_msg": "TODO completed"},
                    )
                    await ir_hooks.update_ir(inv)
                    self.assertEqual(Executor._build_tool_message(None, inv.execution).content, "TODO completed")
                    self.assertTrue(inv.execution.success)
                    self.assertEqual(inv.execution.output_text, "TODO output")
                    self.assertEqual(inv.execution.frontend_msg, "TODO display")
                    self.assertEqual(inv.execution.raw_result, {"original_msg": "TODO completed"})
                    if refresh == "eager":
                        self.assertIn("窗口冲突", inv.execution.metadata["data_task_ir"]["reason"])
                    else:
                        self.assertNotIn("data_task_ir", inv.execution.metadata)
                    for tool in self.entries:
                        if tool not in ("wrapped_nl2sql_sub_agent_tool", "check_and_repair_sql"):
                            continue
                        context, message, result = await self.consumer(runtime, inv.state, tool)
                        self.assertEqual(context, "")
                        self.assertEqual(message.status, "success")
                        self.assertEqual(message.content, "SQL tool PASS")
                        self.assertNotIn("窗口冲突", message.model_dump_json())
                        self.assertEqual(result.execution.output_text, "SQL output")
                        self.assertEqual(result.execution.frontend_msg, "SQL display")
                        self.assertEqual(result.execution.raw_result, {"original_msg": "SQL tool PASS"})
                        self.assertEqual(result.execution.metadata['existing'], 'keep')
                        self.assertEqual(result.execution.metadata['data_task_ir']['status'], 'fallback_without_ir')
                        self.assertIn("窗口冲突", result.execution.metadata['data_task_ir']['reason'])
                    self.assertEqual(self.fill.await_count-before, 2)  # one extraction + targeted repair
                    if multi:
                        snap = runtime.get_cache("ir_session")["store"].load()
                        self.assertEqual(snap['status'], 'draft')
                        self.assertEqual(snap['fields'], [])
                        self.assertTrue(snap['fallback'])

    async def test_legacy_eager_consumer_without_plan_initializes_when_hooks_enabled(self):
        runtime = self.runtime(multi=False)
        inv = prepare(runtime, "统计14天")
        context, _, _ = await self.consumer(runtime, inv.state, "wrapped_nl2sql_sub_agent_tool")
        self.assertIn("DataTaskIR", context)
        self.fill.assert_awaited_once()

    async def test_default_warning_injects_once_without_repair_or_visible_diagnostics(self):
        for multi in (False, True):
            for refresh in ('eager', 'on_consume'):
                for failure in ('strict', 'fallback_without_ir'):
                    with self.subTest(multi=multi, refresh=refresh, failure=failure):
                        runtime = self.runtime(multi, refresh, failure, sid=f'warn-{multi}-{refresh}-{failure}')
                        runtime.config['DATA_TASK_IR'].pop('consistency_policy')
                        inv = prepare(runtime, '统计14天')
                        warning = 'time_context：窗口需核对，仅记录的测试诊断'
                        self.gate.return_value = ([warning], True)
                        fill_before, gate_before = self.fill.await_count, self.gate.await_count
                        logs = []
                        sink = ir_hooks.logger.add(lambda msg, logs=logs: logs.append(str(msg)), level='WARNING')
                        try:
                            await ir_hooks.update_ir(inv)
                            for tool in ('wrapped_nl2sql_sub_agent_tool', 'check_and_repair_sql'):
                                context, message, result = await self.consumer(runtime, inv.state, tool)
                                self.assertIn('DataTaskIR', context)
                                self.assertIn('已确认口径约束', context)
                                self.assertNotIn(warning, context)
                                self.assertEqual(message.content, 'SQL tool PASS')
                                self.assertEqual(result.execution.metadata, {'existing': 'keep'})
                        finally:
                            ir_hooks.logger.remove(sink)
                        self.assertTrue(any('consistency_warning' in text and warning in text for text in logs))
                        self.assertEqual(self.fill.await_count - fill_before, 1)
                        self.assertEqual(self.gate.await_count - gate_before, 1)
                        self.assertIsNone(runtime.get_cache('ir_fallback'))
                        self.assertEqual(runtime.get_cache('ir_gate_warnings'), [warning])
                        if multi:
                            snap = runtime.get_cache('ir_session')['store'].load()
                            self.assertEqual(snap['status'], 'committed')
                            self.assertEqual(snap['consistency_status'], 'warning')
                            self.assertEqual(snap['consistency_warnings'], [warning])

    async def test_warning_survives_restart_and_policy_change_rechecks(self):
        def make():
            runtime = self.runtime(sid='warning-restart')
            runtime.config['DATA_TASK_IR'].pop('consistency_policy')
            return runtime

        first = make()
        inv = prepare(first, '统计14天')
        self.gate.return_value = (['conflict'], True)
        await ir_hooks.ensure_ir_ready(inv)
        calls = self.gate.await_count
        second = make()
        inv = prepare(second, '统计14天')
        await ir_hooks.ensure_ir_ready(inv)
        self.assertEqual(self.gate.await_count, calls)  # identical requirements reuse snapshot
        self.assertEqual(second.get_cache('ir_gate_warnings'), ['conflict'])
        self.assertIn('已确认口径约束', ir_hooks.get_ir_context(second))
        self.assertEqual(second.get_cache('ir_session')['store'].load()['consistency_status'], 'warning')
        # Changing policy must invalidate the signature, even within one Runtime.
        second.config['DATA_TASK_IR']['consistency_policy'] = 'enforce'
        await ir_hooks.ensure_ir_ready(inv)
        self.assertGreater(self.gate.await_count, calls)
        self.assertEqual(ir_hooks.get_ir_context(second), '')

    async def test_invalid_consistency_policy_fails_before_inference(self):
        runtime = self.runtime()
        runtime.config['DATA_TASK_IR']['consistency_policy'] = 'typo'
        with self.assertRaisesRegex(ValueError, 'consistency_policy'):
            prepare(runtime, '统计14天')
        self.fill.assert_not_awaited()
        self.gate.assert_not_awaited()

    async def test_strict_still_blocks_both_modes_and_caches_semantic_failure(self):
        for multi in (False, True):
            runtime = self.runtime(multi, failure="strict", sid=str(multi))
            inv = prepare(runtime, "统计14天")
            self.gate.return_value = (["time_context：冲突"], True)
            before = self.fill.await_count
            for _ in range(3):
                with self.assertRaises(ir_hooks.IRRefreshError):
                    await self.consumer(runtime, inv.state, "wrapped_nl2sql_sub_agent_tool")
            self.assertEqual(self.fill.await_count-before, 2)
            with self.assertRaises(RuntimeError):
                ir_hooks.get_ir_context(runtime)

    async def test_extraction_and_gate_protocol_failures_fallback_without_retrying_same_input(self):
        for multi in (False, True):
            for kind in ('transport', 'degraded', 'protocol', 'structure'):
                runtime = self.runtime(multi, sid=f'{multi}-{kind}')
                runtime.config['DATA_TASK_IR'].pop('consistency_policy')
                inv = prepare(runtime, '统计14天')
                self.fill.side_effect = TimeoutError if kind == 'transport' else None
                self.fill.return_value = ([{**FIELD, '_extraction_error': 'timeout'}] if kind == 'degraded' else
                    [{'field_id': 'sources', 'value': {'facts': [{'filters': [{'operator': 'invented'}]}]}}]
                    if kind == 'structure' else [deepcopy(FIELD)])
                self.gate.return_value = (['invalid JSON'], False) if kind == 'protocol' else ([], True)
                before = self.fill.await_count
                for _ in range(2):
                    context, _, _ = await self.consumer(runtime, inv.state, 'check_and_repair_sql')
                    self.assertEqual(context, '')
                self.assertEqual(self.fill.await_count-before, 1)

    async def test_new_evidence_recovers_and_previous_ir_is_never_a_fallback(self):
        for multi in (False, True):
            runtime = self.runtime(multi, sid=str(multi))
            inv = prepare(runtime, "统计14天")
            self.gate.return_value = ([], True)
            await ir_hooks.ensure_ir_ready(inv)
            self.assertIn('DataTaskIR', ir_hooks.get_ir_context(runtime))
            self.nodes.append(SimpleNamespace(action='read_file', params={'path':'schema.md'}, output='changed',
                success=True, created_at=datetime.now(UTC)+timedelta(seconds=1)))
            self.gate.return_value = (['conflict'], True)
            await ir_hooks.ensure_ir_ready(inv)
            self.assertEqual(ir_hooks.get_ir_context(runtime), '')
            calls = self.fill.await_count
            await asyncio.gather(ir_hooks.ensure_ir_ready(inv), ir_hooks.ensure_ir_ready(inv))
            self.assertEqual(self.fill.await_count, calls)
            self.nodes[-1].output = 'corrected schema'
            self.gate.return_value = ([], True)
            await ir_hooks.ensure_ir_ready(inv)
            self.assertEqual(self.fill.await_count, calls+1)
            self.assertIn('DataTaskIR', ir_hooks.get_ir_context(runtime))
            self.assertIsNone(runtime.get_cache('ir_fallback'))

    async def test_related_new_and_restarted_turns_do_not_consume_old_ir_on_failure(self):
        first = self.runtime()
        inv = prepare(first, '统计14天下载')
        await ir_hooks.ensure_ir_ready(inv)
        old_id = first.get_cache('ir_session')['snapshot']['task_id']
        for relation, query in [('continue', '改为30天'), ('new', '统计曝光')]:
            runtime = self.runtime()
            inv = prepare(runtime, query)
            session.select_task(runtime, relation)
            self.fill.side_effect = TimeoutError
            await ir_hooks.ensure_ir_ready(inv)
            self.assertEqual(ir_hooks.get_ir_context(runtime), '')
            saved = runtime.get_cache('ir_session')['store'].load()
            self.assertEqual(saved['status'], 'draft')
            self.assertEqual(saved['task_id']==old_id, relation=='continue')
            restarted = self.runtime()
            prepare(restarted, query)
            session.select_task(restarted, 'continue')
            self.assertIsNone(restarted.get_cache('ir_input_signature'))
            self.assertIsNone(restarted.get_cache('ir_fallback'))  # failure cache is turn-local

    async def test_identical_evidence_reread_does_not_retry_in_either_mode(self):
        for multi in (False, True):
            with self.subTest(multi=multi):
                self.nodes.clear()
                runtime = self.runtime(multi, sid=str(multi))
                inv = prepare(runtime, '统计14天')
                node = SimpleNamespace(action='read_file', params={'path': 'schema.md'},
                                       output='schema', success=True, created_at=datetime.now(UTC))
                self.nodes.append(node)
                self.gate.return_value = (['time_context：冲突'], True)
                await ir_hooks.ensure_ir_ready(inv)
                calls = self.fill.await_count
                self.nodes.append(deepcopy(node))
                await ir_hooks.ensure_ir_ready(inv)
                self.assertEqual(self.fill.await_count, calls)
                self.assertEqual(ir_hooks.get_ir_context(runtime), '')
                self.nodes[-1].output = 'corrected schema'
                self.gate.return_value = ([], True)
                await ir_hooks.ensure_ir_ready(inv)
                self.assertEqual(self.fill.await_count, calls + 1)
                self.assertIsNone(runtime.get_cache('ir_fallback'))

    async def test_configuration_and_cancellation_errors_propagate_in_fill_and_repair(self):
        for multi in (False, True):
            for repair in (False, True):
                for error in (ValueError('invalid configuration'), asyncio.CancelledError()):
                    with self.subTest(multi=multi, repair=repair, error=type(error).__name__):
                        runtime = self.runtime(multi, sid=f'{multi}-{repair}-{type(error).__name__}')
                        inv = prepare(runtime, '统计14天')
                        self.gate.return_value = (['time_context：冲突'], True)
                        self.fill.side_effect = [[deepcopy(FIELD)], error] if repair else error
                        with self.assertRaises(type(error)):
                            await ir_hooks.ensure_ir_ready(inv)
                        self.assertIsNone(runtime.get_cache('ir_fallback'))
                        with self.assertRaises(RuntimeError):
                            ir_hooks.get_ir_context(runtime)

    async def test_false_feedback_is_ignored_and_real_feedback_reopens_input(self):
        for multi in (False, True):
            runtime = self.runtime(multi, sid=str(multi))
            inv = prepare(runtime, '统计14天')
            self.gate.return_value = (['conflict'], True)
            await ir_hooks.ensure_ir_ready(inv)
            calls = self.fill.await_count
            for status, content in [('error', "Tool 'request_human_feedback' not found"),
                                    ('success', 'Waiting for human feedback...'), ('success', '   ')]:
                inv.state['messages'].append(ToolMessage(content=content, name='request_human_feedback',
                                                         tool_call_id=content, status=status))
            await ir_hooks.ensure_ir_ready(inv)
            self.assertEqual(self.fill.await_count, calls)
            if multi:
                self.assertEqual(runtime.get_cache('ir_session')['feedback'], [])
            self.gate.return_value = ([], True)
            inv.state['messages'].append(ToolMessage(content='使用pt_d', name='request_human_feedback',tool_call_id='answer'))
            await ir_hooks.ensure_ir_ready(inv)
            self.assertEqual(self.fill.await_count, calls+1)
            call = self.fill.await_args.kwargs
            self.assertIn('使用pt_d', call['user_query']+call['confirmed_context'])

    async def test_config_selection_and_storage_errors_do_not_fallback(self):
        runtime = self.runtime(failure='typo')
        with self.assertRaisesRegex(ValueError, 'failure_policy'):
            prepare(runtime, 'q')
        runtime = self.runtime()
        runtime.config['TOOLS']['builtin'].pop()
        with self.assertRaisesRegex(ValueError, '必须同时'):
            prepare(runtime, 'q')
        runtime = self.runtime()
        inv = prepare(runtime, 'q')
        await ir_hooks.ensure_ir_ready(inv)
        second = self.runtime()
        inv = prepare(second, '继续')
        session.prepare_task_context(inv.state, second)
        self.gate.return_value = (['conflict'], True)
        with (patch.object(session, 'record_fallback', side_effect=SnapshotConflict('revision conflict')),
              self.assertRaises(SnapshotConflict)):
            await ir_hooks.ensure_ir_ready(inv)
        # A failed tool response must not unlock an explicit relationship clarification.
        third = self.runtime()
        inv = prepare(third, '改一下')
        session.select_task(third, 'clarify')
        session.select_task(third, 'continue', resolve_pending=True)
        inv.state['messages'].append(ToolMessage(content='tool failed', name='request_human_feedback',
                                                tool_call_id='bad', status='error'))
        with self.assertRaisesRegex(RuntimeError, '仍待用户澄清'):
            await ir_hooks.ensure_ir_ready(inv)

    async def test_failed_sql_tool_retains_error_with_diagnostics_only_in_metadata(self):
        runtime = self.runtime()
        inv = prepare(runtime, 'q')
        self.gate.return_value = (['conflict'], True)
        await ir_hooks.ensure_ir_ready(inv)
        inv.execution = NormalizedToolExecution('check_and_repair_sql','bad',{},False,error_text='SQL invalid')
        await ir_hooks.record_ir_diagnostics(inv)
        message = Executor._build_tool_message(None, inv.execution)
        self.assertEqual(message.status, 'error')
        self.assertEqual(message.content, 'SQL invalid')
        self.assertIn('conflict', inv.execution.metadata['data_task_ir']['reason'])
        self.assertNotIn('conflict', message.model_dump_json())

    async def test_diagnostics_use_per_call_fallback_after_another_call_recovers(self):
        runtime = self.runtime()
        prepared = prepare(runtime, '统计14天')
        inv = ToolHookInvocation(tool_name='wrapped_nl2sql_sub_agent_tool', tool_call_id='sql',
                                 tool_args={}, runtime=runtime, metadata={}, state=prepared.state)
        self.gate.return_value = (['time_context：窗口冲突'], True)
        await ir_hooks.ensure_ir_ready(inv)
        captured = deepcopy(inv.hook_context['ir_fallback'])
        runtime.set_cache('ir_fallback', None)
        inv.execution = NormalizedToolExecution(inv.tool_name, 'sql', {}, True, output_text='SQL ready')
        await ir_hooks.record_ir_diagnostics(inv)
        self.assertEqual(inv.execution.metadata['data_task_ir'],
                         {'status': 'fallback_without_ir', **captured})
        self.assertEqual(Executor._build_tool_message(None, inv.execution).content, 'SQL ready')
        self.assertIsNone(inv.execution.original_msg)
        self.assertIsNone(inv.execution.frontend_msg)

    async def test_successful_ir_keeps_tool_result_and_metadata_unchanged(self):
        runtime = self.runtime()
        inv = prepare(runtime, '统计14天')
        context, message, result = await self.consumer(runtime, inv.state, 'wrapped_nl2sql_sub_agent_tool')
        self.assertIn('DataTaskIR', context)
        self.assertEqual(message.content, 'SQL tool PASS')
        self.assertEqual(result.execution.metadata, {'existing': 'keep'})
