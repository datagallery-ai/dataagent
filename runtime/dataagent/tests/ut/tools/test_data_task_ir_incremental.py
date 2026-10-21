"""Offline regressions for IR cache correctness and the shared SQL contract."""

import asyncio
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dataagent.actions.tools.hooks.base import ToolHookRunner
from dataagent.actions.tools.hooks.examples import ir_hooks
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import fill, render
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import (
    list_field_templates,
    load_template_catalog,
)

FIELD = {"field_id": "time_context", "value": {
    "output_bucket": {
        "time_field": "pt_d", "grain": "day", "timezone": None,
        "calendar": None, "week_start": None, "custom_definition": None,
    },
    "input_windows": [{
        "window_id": "w7d", "time_field": "event_time", "anchor": "current_partition",
        "range": {"direction": "past", "number": 7, "unit": "day"},
        "boundary": None,
    }],
}, "unresolved": []}
TEMPLATE = {"field_id": "time_context", "writable_template": deepcopy(FIELD)}


class FakeRuntime:
    workspace_dir = None

    def __init__(self):
        self.cache = {"ir_field_templates": [deepcopy(TEMPLATE)]}

    def get_cache(self, key, default=None):
        return self.cache.get(key, default)

    def set_cache(self, key, value):
        self.cache[key] = value

    def llm(self, name):
        return self


class IncrementalIRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.inv = SimpleNamespace(
            runtime=self.runtime, state={"user_query": "past 7 days", "messages": []},
            execution=SimpleNamespace(success=True), tool_name="complete_current_todo", tool_call_id="test",
        )
        self.nodes = []
        self.fill_mock = AsyncMock(return_value=[deepcopy(FIELD)])
        self.gate_mock = AsyncMock(return_value=([], True))
        self._fill_original = ir_hooks.fill_ir_fields
        for target, replacement in [
            ("get_action_nodes", lambda runtime: self.nodes),
            ("fill_ir_fields", self.fill_mock),
            ("_ir_consistency_gate", self.gate_mock),
        ]:
            patcher = patch.object(ir_hooks, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_on_consume(self):
        self.runtime.get_config = lambda key, default=None: (
            "on_consume" if key == "DATA_TASK_IR.refresh_policy" else default
        )

    async def test_deferred_updates_coalesce_at_consumer_with_full_latest_input(self):
        self.use_on_consume()
        for output in ["window 7", "window 14", "window 30"]:
            self.nodes.append(SimpleNamespace(action="read_file", params={"path": "a.md"},
                                              success=True, output=output))
            await ir_hooks.update_ir(self.inv)
        self.fill_mock.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(self.runtime)
        await asyncio.gather(ir_hooks.ensure_ir_ready(self.inv), ir_hooks.ensure_ir_ready(self.inv))
        self.assertEqual(self.fill_mock.await_count, 1)
        for output in ["window 7", "window 14", "window 30"]:
            self.assertIn(output, self.fill_mock.await_args.kwargs["tool_evidence"])
        self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))
        # Consumer sees new inputs even without a todo/update_ir call.
        self.nodes[0].output = "withdraw old window"
        await ir_hooks.ensure_ir_ready(self.inv)
        self.assertEqual(self.fill_mock.await_count, 2)
        self.assertIsNone(self.fill_mock.await_args.kwargs["current_values"])

    async def test_deferred_failure_blocks_stale_read_and_retries(self):
        self.use_on_consume()
        await ir_hooks.update_ir(self.inv)
        self.fill_mock.side_effect = TimeoutError
        # Soft-fail swallows the fill crash; first-time empty IR still needs a
        # successful refresh before on_consume consumers commit a signature.
        with self.assertRaisesRegex(RuntimeError, "consistency check incomplete"):
            await ir_hooks.ensure_ir_ready(self.inv)
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(self.runtime)
        self.assertFalse(self.runtime.get_cache("ir_refresh_failed"))
        self.fill_mock.side_effect = None
        await ir_hooks.ensure_ir_ready(self.inv)
        self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))
        await ir_hooks.update_ir(self.inv)
        await ir_hooks.ensure_ir_ready(self.inv)
        self.assertEqual(self.fill_mock.await_count, 2)  # Failure + retry; unchanged input skips.
        self.assertFalse(self.runtime.get_cache("ir_dirty"))

    async def test_consumer_hook_does_not_enable_disabled_ir(self):
        self.use_on_consume()
        await ir_hooks.ensure_ir_ready(self.inv)
        self.fill_mock.assert_not_awaited()
        self.runtime.set_cache("ir_initialized", True)
        self.runtime.get_config = lambda key, default=None: default
        await ir_hooks.ensure_ir_ready(self.inv)
        self.fill_mock.assert_not_awaited()

    async def test_deferred_gate_failure_blocks_consumer_and_retries_only_gate(self):
        self.use_on_consume()
        await ir_hooks.update_ir(self.inv)
        self.gate_mock.side_effect = [(["failed"], False), ([], True)]
        with self.assertRaisesRegex(RuntimeError, "consistency check incomplete"):
            await ir_hooks.ensure_ir_ready(self.inv)
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(self.runtime)
        await ir_hooks.ensure_ir_ready(self.inv)
        self.assertEqual(self.fill_mock.await_count, 1)
        self.assertEqual(self.gate_mock.await_count, 2)
        self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))

    async def test_real_pre_hook_without_execution_can_fill(self):
        self.use_on_consume()
        self.runtime.set_cache("ir_initialized", True)
        self.inv.phase = "pre"
        self.inv.execution = None
        with patch.object(ir_hooks, "fill_field_templates", AsyncMock(return_value=[FIELD])) as fields:
            # Run the same async pre-hook runner used by Executor, without an execution.
            with patch.object(ir_hooks, "fill_ir_fields", self._fill_original):
                await ToolHookRunner.run_pre_hooks([ir_hooks.ensure_ir_ready], self.inv)
            fields.assert_awaited_once()
            self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))

    async def test_deferred_consumer_without_state_fails_before_model_call(self):
        self.use_on_consume()
        self.runtime.set_cache("ir_initialized", True)
        self.inv.state = None
        with self.assertRaisesRegex(RuntimeError, "current workflow state"):
            await ir_hooks.ensure_ir_ready(self.inv)
        self.fill_mock.assert_not_awaited()

    async def test_identical_updates_and_concurrent_updates_call_models_once(self):
        await asyncio.gather(ir_hooks.update_ir(self.inv), ir_hooks.update_ir(self.inv))
        await ir_hooks.update_ir(self.inv)
        self.assertEqual(self.fill_mock.await_count, 1)
        self.assertEqual(self.gate_mock.await_count, 1)

    async def test_query_context_and_template_changes_invalidate_cache(self):
        await ir_hooks.update_ir(self.inv)
        self.inv.state["user_query"] = "past 30 days"
        await ir_hooks.update_ir(self.inv)
        self.inv.state["messages"] = [SimpleNamespace(content="User Context\nnew timezone\nGeneral Requirement")]
        await ir_hooks.update_ir(self.inv)
        self.runtime.cache["ir_field_templates"][0]["template_version"] = "v2"
        await ir_hooks.update_ir(self.inv)
        self.assertEqual(self.fill_mock.await_count, 4)
        for call in self.fill_mock.await_args_list:
            self.assertFalse(call.kwargs["reuse_field_ids"])

    async def test_append_reuses_unaffected_fields_but_replacement_refills(self):
        self.runtime.get_config = lambda key, default=None: (
            True if key == "DATA_TASK_IR.semantic_field_reuse" else default
        )
        await ir_hooks.update_ir(self.inv)
        self.nodes.append(SimpleNamespace(action="read_file", params={"path": "schema.md"},
                                          success=True, output="unrelated table"))
        with patch.object(ir_hooks, "detect_field_changes", AsyncMock(return_value=set())) as detector:
            await ir_hooks.update_ir(self.inv)
            self.assertEqual(self.fill_mock.await_args.kwargs["reuse_field_ids"], {"time_context"})
            self.nodes[0].output = "window now 30 days"
            await ir_hooks.update_ir(self.inv)
            self.assertEqual(detector.await_count, 1)
            self.assertFalse(self.fill_mock.await_args.kwargs["reuse_field_ids"])

    async def test_default_refills_on_changes_and_preserves_full_evidence(self):
        self.nodes.append(SimpleNamespace(action="read_file", params={"path": "a.md"},
                                          success=True, output="old constraint"))
        await ir_hooks.update_ir(self.inv)
        with patch.object(ir_hooks, "detect_field_changes", AsyncMock()) as detector:
            self.nodes.append(SimpleNamespace(action="read_file", params={"path": "b.md"},
                                              success=True, output="new constraint"))
            await ir_hooks.update_ir(self.inv)
            detector.assert_not_awaited()
        self.assertFalse(self.fill_mock.await_args.kwargs["reuse_field_ids"])
        for mock in [self.fill_mock, self.gate_mock]:
            self.assertIn("old constraint", mock.await_args.kwargs["tool_evidence"])
            self.assertIn("new constraint", mock.await_args.kwargs["tool_evidence"])
        self.nodes.pop(0)
        self.inv.state["user_query"] = "new question"
        await ir_hooks.update_ir(self.inv)
        self.assertNotIn("old constraint", self.fill_mock.await_args.kwargs["tool_evidence"])
        self.assertFalse(self.fill_mock.await_args.kwargs["reuse_field_ids"])
        self.assertIsNone(self.fill_mock.await_args.kwargs["current_values"])

    async def test_calibration_tables_read_file_excluded_from_ir_evidence(self):
        self.nodes.extend([
            SimpleNamespace(
                action="read_file",
                params={"path": "skill/feature_engineering/references/calibration_tables.md"},
                success=True,
                output="口径冻结表模板内容",
            ),
            SimpleNamespace(
                action="read_file",
                params={"path": "skill/sql_rules/references/ddl_rules.md"},
                success=True,
                output="ddl rules content",
            ),
            SimpleNamespace(
                action="metadata_recall",
                params={"query": "q"},
                success=True,
                output="table meta",
            ),
        ])
        await ir_hooks.update_ir(self.inv)
        evidence = self.fill_mock.await_args.kwargs["tool_evidence"]
        self.assertNotIn("口径冻结表模板内容", evidence)
        self.assertNotIn("calibration_tables", evidence)
        self.assertIn("ddl rules content", evidence)
        self.assertIn("table meta", evidence)

    async def test_failed_evidence_refresh_does_not_advance_committed_inputs(self):
        self.nodes.append(SimpleNamespace(action="read_file", params={"path": "a.md"},
                                          success=True, output="first"))
        await ir_hooks.update_ir(self.inv)
        previous = deepcopy(self.runtime.get_cache("ir_evidence_parts"))
        previous_context = ir_hooks.get_ir_context(self.runtime)
        self.nodes[0].output = "corrected"
        self.fill_mock.side_effect = TimeoutError
        await ir_hooks.update_ir(self.inv)
        self.assertEqual(self.runtime.get_cache("ir_evidence_parts"), previous)
        self.assertFalse(self.runtime.get_cache("ir_refresh_failed"))
        # Previous IR stays consumable for NL2SQL; evidence commit is not advanced.
        self.assertEqual(ir_hooks.get_ir_context(self.runtime), previous_context)
        self.fill_mock.side_effect = None
        await ir_hooks.update_ir(self.inv)
        self.assertIn("corrected", self.fill_mock.await_args.kwargs["tool_evidence"])
        self.assertIsNone(self.fill_mock.await_args.kwargs["current_values"])

    async def test_changing_completion_policy_invalidates_cache(self):
        await ir_hooks.update_ir(self.inv)
        options = {}
        self.runtime.get_config = lambda key, default=None: options.get(key, default)
        for name, value in [("completion_batch_size", 4), ("completion_policy", "partial_only"),
                            ("concise_rationale", True), ("field_order", "long_first"), ("compact_json", True)]:
            before = self.fill_mock.await_count
            options[f"DATA_TASK_IR.{name}"] = value
            await ir_hooks.update_ir(self.inv)
            self.assertEqual(self.fill_mock.await_count, before + 1)

    async def test_gate_failure_retries_gate_without_refilling(self):
        self.gate_mock.side_effect = [(["check failed"], False), (["conflicting window"], True)]
        await ir_hooks.update_ir(self.inv)
        self.assertEqual(self.runtime.get_cache("ir_gate_warnings"), ["check failed"])
        self.assertNotIn("check failed", ir_hooks.get_ir_context(self.runtime))
        self.assertIsNone(self.runtime.get_cache("ir_input_signature"))
        await ir_hooks.update_ir(self.inv)
        await ir_hooks.update_ir(self.inv)
        self.assertEqual(self.fill_mock.await_count, 1)
        self.assertEqual(self.gate_mock.await_count, 2)
        self.assertEqual(self.runtime.get_cache("ir_gate_warnings"), ["conflicting window"])
        self.assertNotIn("conflicting window", ir_hooks.get_ir_context(self.runtime))

    async def test_fill_failure_keeps_previous_ir_consumable(self):
        await ir_hooks.update_ir(self.inv)
        previous = ir_hooks.get_ir_context(self.runtime)
        self.assertIn("DataTaskIR", previous)
        self.inv.state["user_query"] = "changed"
        self.fill_mock.side_effect = TimeoutError
        await ir_hooks.update_ir(self.inv)
        # Total fill crash must not permanently block NL2SQL consumers.
        self.assertFalse(self.runtime.get_cache("ir_refresh_failed"))
        self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))
        self.fill_mock.side_effect = None
        await ir_hooks.update_ir(self.inv)
        self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))


class DetectorAndFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_fields_do_not_invent_questions_and_recheck_new_evidence(self):
        for batch_size in [1, 4]:
            with self.subTest(batch_size=batch_size), TemporaryDirectory() as trace, patch.object(
                fill, "ainvoke_json_object", AsyncMock(return_value=({"value": {}, "rationale": "No evidence"}, '{}')),
            ) as model:
                result = await fill.fill_field_templates(
                    [TEMPLATE], user_query="q", tool_evidence="unknown", llm=None,
                    completion_policy="partial_only", completion_batch_size=batch_size, trace_dir=trace,
                )
                self.assertEqual(model.await_count, 1)
                self.assertFalse(result[0].get("unresolved"))
                self.assertFalse(result[0].get("question"))
                self.assertEqual(result[0]["value"], {"output_bucket": None, "input_windows": None})
                self.assertFalse(fill.is_field_confirmed(result[0], TEMPLATE))
                meta = json.loads((Path(trace) / "time_context/meta.json").read_text())
                self.assertEqual(meta["completion_skipped_reason"], "no_structural_errors")
                self.assertFalse(meta["completion_pending"])
                model.return_value = (deepcopy(FIELD), json.dumps(FIELD))
                updated = await fill.fill_field_templates(
                    [TEMPLATE], user_query="q", tool_evidence="new: past seven days", llm=None,
                    current_values=result, reuse_field_ids={"time_context"}, completion_policy="partial_only",
                    completion_batch_size=batch_size,
                )
                self.assertEqual(model.await_count, 2)
                self.assertEqual(updated, [FIELD])
                self.assertIn("new: past seven days", model.await_args.args[1][1]["content"])

    async def test_partial_only_still_repairs_malformed_structure_and_invalid_json(self):
        partial = deepcopy(FIELD)
        partial["value"]["input_windows"] = "not an array"
        for first in [(partial, '{}'), (None, 'invalid'), TimeoutError()]:
            with self.subTest(first=first), patch.object(fill, "ainvoke_json_object", AsyncMock(
                side_effect=[first, (deepcopy(FIELD), json.dumps(FIELD))],
            )) as model:
                detail = await fill.fill_field_template_detailed(
                    TEMPLATE, user_query="q", tool_evidence="full evidence", llm=None,
                    completion_policy="partial_only", concise_rationale=True, compact_json=True,
                )
                self.assertTrue(detail["success"])
                self.assertIn(fill.COMPACT_JSON_INSTRUCTION, model.await_args.args[1][0]["content"])
                self.assertEqual(detail["output"], FIELD)
                self.assertEqual(model.await_count, 2)
                self.assertIn("full evidence", model.await_args.args[1][1]["content"])
        with patch.object(fill, "ainvoke_json_object", AsyncMock(side_effect=TimeoutError())), self.assertRaises(ValueError):
            await fill.fill_field_template(TEMPLATE, user_query="q", tool_evidence="e", llm=None,
                                           completion_policy="partial_only")

    async def test_long_fields_start_first_but_result_order_and_evidence_are_unchanged(self):
        templates = [{"field_id": field_id} for field_id in
                     ["pipeline", "sources", "output_features", "time_context"]]
        called = []

        async def model(field_template, **kwargs):
            called.append((field_template["field_id"], kwargs))
            output = {"field_id": field_template["field_id"], "value": {}, "rationale": "ok", "unresolved": []}
            return {
                "field_id": field_template["field_id"], "success": True, "attempts": 1,
                "output": output, "error": None, "completion_pending": False,
                "raw_responses": ["{}"], "missing_critical_paths": [],
            }

        with patch.object(fill, "fill_field_template_detailed", model):
            outputs = await fill.fill_field_templates(templates, user_query="query", tool_evidence="all evidence",
                                                     confirmed_context="full context", llm=None, field_order="long_first")
        self.assertEqual([item[0] for item in called],
                         ["output_features", "sources", "time_context", "pipeline"])
        self.assertEqual([o["field_id"] for o in outputs], [t["field_id"] for t in templates])
        for _, kwargs in called:
            self.assertEqual(kwargs["tool_evidence"], "all evidence")
            self.assertEqual(kwargs["confirmed_context"], "full context")

    async def test_concise_prompt_keeps_business_input_and_invalid_policies_fail_before_calls(self):
        normal = fill.build_field_messages(TEMPLATE, user_query="q", tool_evidence="all evidence")
        concise = fill.build_field_messages(TEMPLATE, user_query="q", tool_evidence="all evidence",
                                           concise_rationale=True, compact_json=True)
        self.assertEqual(normal[1], concise[1])
        self.assertIn("value 必须完整", concise[0]["content"])
        self.assertIn(fill.COMPACT_JSON_INSTRUCTION, concise[0]["content"])
        for kwargs in [{"field_order": "unknown"}, {"completion_policy": "never"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await fill.fill_field_templates([TEMPLATE], user_query="q", tool_evidence="e", llm=None, **kwargs)

    async def test_batch_repairs_structure_but_accepts_unknown_business_values(self):
        templates = [deepcopy(TEMPLATE), {"field_id": "sequence",
                     "writable_template": {"field_id": "sequence", "value": {
                         "partition_fields": [], "null_equality": None, "ordering": [],
                         "dedup": None, "truncation": None, "window_partition": None,
                     }}}]
        calls = []

        async def model(llm, messages):
            calls.append(messages)
            # Batch second-opinion prompt uses a dedicated system role (not message count).
            if messages and "补全审查员" in messages[0].get("content", ""):
                # Deliberately reversed order: association must use field_id.
                fields = [{"field_id": t["field_id"], "value": {}, "rationale": "No evidence"}
                          for t in reversed(templates)]
                return {"fields": fields}, json.dumps({"fields": fields})
            field_id = "sequence" if "field_id: sequence" in messages[1]["content"] else "time_context"
            value = {"ordering": "broken"} if field_id == "sequence" else {"input_windows": "broken"}
            return {"value": value, "rationale": "Malformed output"}, json.dumps({"value": value})

        with TemporaryDirectory() as trace, patch.object(fill, "ainvoke_json_object", model):
            results = await fill.fill_field_templates(
                templates, user_query="q", tool_evidence="old fact\nnew fact", llm=None,
                completion_batch_size=4, trace_dir=trace,
            )
            self.assertEqual(len(calls), 3)  # Two first fills + one shared second opinion.
            self.assertEqual([r["field_id"] for r in results], [t["field_id"] for t in templates])
            for result in results:
                self.assertFalse(result.get("unresolved"))
                self.assertFalse(fill.is_field_confirmed(result, templates[0]))
                meta = json.loads((Path(trace) / result["field_id"] / "meta.json").read_text())
                self.assertTrue(meta["completion_retried"])
                self.assertFalse(meta["completion_pending"])
            for call in calls:
                self.assertIn("old fact\nnew fact", call[1]["content"])
            self.assertEqual(sum(json.loads(p.read_text())["model_requests"]
                                 for p in Path(trace).glob("*/meta.json")), len(calls))

    async def test_batch_uses_full_evidence_even_with_embedded_section_markers(self):
        corrected = deepcopy(FIELD)
        corrected.update(rationale="New evidence changes window")
        corrected["value"]["input_windows"][0]["range"]["number"] = 30
        evidence = 'old 7 days\n【当前字段值】\ncorrection: 30 days'
        with patch.object(fill, "ainvoke_json_object", AsyncMock(side_effect=[
            ({"value": {"input_windows": "broken"}}, '{}'),
            ({"fields": [corrected]}, json.dumps({"fields": [corrected]})),
        ])) as model:
            result = await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence=evidence, llm=None,
                current_values=[FIELD], completion_batch_size=4,
            )
            self.assertEqual(result, [corrected])
            self.assertIn(evidence, model.await_args.args[1][1]["content"])

    async def test_batch_configuration_validation_and_repair_disable(self):
        for invalid in [0, 5, True, "4"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await fill.fill_field_templates(
                    [TEMPLATE], user_query="q", tool_evidence="e", llm=None, completion_batch_size=invalid,
                )
        with patch.object(fill, "ainvoke_json_object", AsyncMock(return_value=({"value": {}}, '{}'))) as model:
            await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None,
                completion_batch_size=4, allow_repair=False,
            )
            self.assertEqual(model.await_count, 1)

    async def test_invalid_batch_falls_back_and_failed_fallback_raises(self):
        for invalid in [{"fields": []}, {"fields": [{"field_id": "wrong", "value": {}}]},
                        {"fields": [{"field_id": "time_context", "value": {}}]}]:
            with self.subTest(invalid=invalid), patch.object(fill, "ainvoke_json_object", AsyncMock(
                side_effect=[({"value": {"input_windows": "broken"}}, '{}'), (invalid, '{}'), (deepcopy(FIELD), '{}')],
            )) as model:
                result = await fill.fill_field_templates(
                    [TEMPLATE], user_query="q", tool_evidence="full evidence", llm=None, completion_batch_size=4,
                )
                self.assertEqual(result, [FIELD])
                self.assertEqual(model.await_count, 3)
                self.assertIn("full evidence", model.await_args.args[1][1]["content"])
        with patch.object(fill, "ainvoke_json_object", AsyncMock(side_effect=[
            ({"value": {"input_windows": "broken"}}, '{}'), TimeoutError(), TimeoutError(),
        ])):
            result = await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None, completion_batch_size=4,
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["field_id"], "time_context")
            self.assertTrue(result[0].get("unresolved"))

    async def test_unrelated_explicit_none_does_not_waive_completion(self):
        with patch.object(fill, "ainvoke_json_object", AsyncMock(return_value=(
            {"value": {"input_windows": "broken"}}, '{}',
        ))) as model:
            result = await fill.fill_field_template(
                TEMPLATE, user_query="last 14 days", tool_evidence="other_field evidence_status: explicit_none",
                llm=None,
            )
            self.assertEqual(model.await_count, 2)
            self.assertTrue(result["unresolved"])

    async def test_prompt_common_prefix_and_verbatim_evidence(self):
        evidence = 'line one\n"quoted"\n末行'
        first = fill.build_field_messages(TEMPLATE, user_query="q", tool_evidence=evidence)
        other = deepcopy(TEMPLATE)
        other["purpose"] = "different field"
        second = fill.build_field_messages(other, user_query="q", tool_evidence=evidence, current_value=FIELD)
        split_at = "【当前字段值】"
        self.assertEqual(first[1]["content"].split(split_at)[0], second[1]["content"].split(split_at)[0])
        self.assertIn(evidence, first[1]["content"])

    async def test_detector_failure_and_invalid_ids_refill_all(self):
        for response in [None, {}, {"reestimate": ["time_windows"]}, {"reestimate": [1]}]:
            with self.subTest(response=response), patch.object(
                fill, "ainvoke_json_object", AsyncMock(return_value=(response, "")),
            ):
                result = await fill.detect_field_changes(
                    new_evidence="30 days", confirmed_fields=[FIELD], llm=None,
                )
                self.assertEqual(result, {"time_context"})
        with patch.object(fill, "ainvoke_json_object", AsyncMock(side_effect=TimeoutError)):
            self.assertEqual(await fill.detect_field_changes(
                new_evidence="30 days", confirmed_fields=[FIELD], llm=None,
            ), {"time_context"})

    async def test_detector_keeps_prefix_and_falls_back_when_over_budget(self):
        evidence = "CORRECTION: 30 days\n" + "x" * 7000
        with patch.object(fill, "ainvoke_json_object", AsyncMock(return_value=({"reestimate": []}, ""))) as llm:
            self.assertEqual(await fill.detect_field_changes(
                new_evidence=evidence, confirmed_fields=[FIELD], llm=None,
            ), set())
            self.assertIn(evidence, llm.await_args.args[1][1]["content"])
            self.assertEqual(await fill.detect_field_changes(
                new_evidence=evidence, confirmed_fields=[FIELD], llm=None, max_input_chars=100,
            ), {"time_context"})
            self.assertEqual(llm.await_count, 1)

    async def test_fill_only_reuses_explicitly_authorized_complete_fields(self):
        detailed = AsyncMock(return_value={
            "field_id": "time_context", "success": True, "attempts": 1,
            "output": deepcopy(FIELD), "error": None, "completion_pending": False,
            "raw_responses": ["{}"], "missing_critical_paths": [],
        })
        with TemporaryDirectory() as trace, patch.object(fill, "fill_field_template_detailed", detailed):
            result = await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None,
                current_values=[FIELD], reuse_field_ids={"time_context"}, trace_dir=trace,
            )
            self.assertEqual(result, [FIELD])
            self.assertEqual(detailed.await_count, 0)
            meta = json.loads((Path(trace) / "time_context/meta.json").read_text())
            self.assertTrue(meta["skipped_incremental"])
            await fill.fill_field_templates(
                [TEMPLATE], user_query="changed", tool_evidence="e", llm=None, current_values=[FIELD],
            )
            self.assertEqual(detailed.await_count, 1)
            partial = deepcopy(FIELD)
            partial["value"]["input_windows"][0]["time_field"] = None
            await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None,
                current_values=[partial], reuse_field_ids={"time_context"},
            )
            self.assertEqual(detailed.await_count, 2)

    async def test_first_fill_preserves_template_and_records_timing(self):
        with TemporaryDirectory() as trace, patch.object(
            fill, "ainvoke_json_object", AsyncMock(return_value=(deepcopy(FIELD), json.dumps(FIELD))),
        ) as model:
            await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None, trace_dir=trace,
            )
            self.assertIn("time_field: event_time", model.await_args.args[1][1]["content"])
            meta = json.loads((Path(trace) / "time_context/meta.json").read_text())
            self.assertIsInstance(meta["model_call_seconds"], float)

    async def test_failed_field_degrades_without_blocking_others(self):
        async def model(field_template, **kwargs):
            field_id = field_template["field_id"]
            if field_id == "broken":
                raise ValueError("model failure")
            return {
                "field_id": field_id,
                "success": True,
                "attempts": 1,
                "output": deepcopy(FIELD) if field_id == TEMPLATE["field_id"] else {
                    "field_id": field_id, "value": {}, "rationale": "ok", "unresolved": [],
                },
                "error": None,
                "completion_pending": False,
                "raw_responses": ["{}"],
                "missing_critical_paths": [],
            }

        with patch.object(fill, "fill_field_template_detailed", model):
            result = await fill.fill_field_templates(
                [{"field_id": "broken"}, TEMPLATE], user_query="q", tool_evidence="e", llm=None,
            )
        self.assertEqual([item["field_id"] for item in result], ["broken", TEMPLATE["field_id"]])
        self.assertTrue(result[0].get("unresolved"))
        self.assertEqual(result[1], FIELD)

    async def test_failed_completion_is_not_published_as_success(self):
        partial = deepcopy(FIELD)
        partial["value"]["input_windows"] = "not an array"
        with patch.object(
            fill, "ainvoke_json_object", AsyncMock(side_effect=[(partial, "{}"), TimeoutError()]),
        ), self.assertRaises(ValueError):
            await fill.fill_field_template(TEMPLATE, user_query="q", tool_evidence="e", llm=None)

    async def test_gate_receives_flat_values_and_failure_is_visible(self):
        runtime = FakeRuntime()
        runtime.get_config = lambda key, default=None: True if key == "DATA_TASK_IR.compact_json" else default
        with patch.object(ir_hooks, "ainvoke_json_object", AsyncMock(return_value=(
            {"warnings": [], "consistent": True}, "{}",
        ))) as model:
            warnings, completed = await ir_hooks._ir_consistency_gate(
                runtime, user_query="q", tool_evidence="e", field_results=[FIELD],
            )
            self.assertTrue(completed)
            self.assertEqual(warnings, [])
            self.assertIn(fill.COMPACT_JSON_INSTRUCTION, model.await_args.args[1][0]["content"])
            self.assertIn('"time_field": "event_time"', model.await_args.args[1][1]["content"])
        with patch.object(ir_hooks, "ainvoke_json_object", AsyncMock(return_value=({}, "{}"))):
            warnings, completed = await ir_hooks._ir_consistency_gate(
                runtime, user_query="q", tool_evidence="e", field_results=[FIELD],
            )
            self.assertFalse(completed)
            self.assertTrue(warnings)


class IncompleteInformationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = {t["field_id"]: t for t in list_field_templates(load_template_catalog())}

    async def test_all_six_empty_units_make_six_calls_without_completion_or_fake_questions(self):
        for policy in ["always", "partial_only"]:
            for batch_size in [1, 4]:
                with self.subTest(policy=policy, batch_size=batch_size), TemporaryDirectory() as trace, patch.object(
                    fill, "ainvoke_json_object", AsyncMock(return_value=(
                        {"value": {}, "rationale": "当前证据未提供本字段信息"}, '{}',
                    )),
                ) as model:
                    results = await fill.fill_field_templates(
                        list(self.templates.values()), user_query="看看数据", tool_evidence="", llm=None,
                        completion_policy=policy, completion_batch_size=batch_size, trace_dir=trace,
                    )
                    self.assertEqual(model.await_count, 6)
                    for result in results:
                        self.assertFalse(result.get("unresolved"))
                        self.assertFalse(result.get("question"))
                        self.assertFalse(fill.is_field_confirmed(result, self.templates[result["field_id"]]))
                        meta = json.loads((Path(trace) / result["field_id"] / "meta.json").read_text())
                        self.assertEqual(meta["attempts"], 1)
                        self.assertFalse(meta["completion_retried"])
                        self.assertEqual(meta["missing_critical_paths"], [])
                    self.assertFalse(list(Path(trace).glob("_completion_*")))

    async def test_partial_units_preserve_unknown_subfields_without_retry(self):
        cases = {
            "identity": {"components": [{"field_ref": "device_id"}]},
            "sources": {"facts": [{"source_ref": "downloads", "filters": [{"field_ref": "status"}]}],
                        "dimensions": [{"source_ref": "apps", "join": {"fact_source_ref": "downloads"}}]},
            "time_context": {"input_windows": [{"window_id": "w7d", "range": {"number": 7}}]},
            "output_features": {"layout": "cartesian", "axes": [{"axis_id": "window", "values": []}],
                                "measures": [{"measure_id": "downloads", "aggregation": None}]},
            "sequence": {"ordering": [{"field_ref": "downloads", "direction": None}]},
        }
        for field_id, value in cases.items():
            with self.subTest(field_id=field_id), patch.object(fill, "ainvoke_json_object", AsyncMock(
                return_value=({"value": deepcopy(value), "rationale": "仅提取已有信息"}, '{}'),
            )) as model:
                result = await fill.fill_field_template_detailed(
                    self.templates[field_id], user_query="近7天下载次数", tool_evidence="partial evidence", llm=None,
                )
                self.assertEqual(model.await_count, 1)
                self.assertEqual(result["missing_critical_paths"], [])
                self.assertFalse(result["output"].get("question"))
                self.assertFalse(fill.is_field_confirmed(result["output"], self.templates[field_id]))
        # Local normalization restores keys, never example values or business defaults.
        self.assertEqual(fill._normalize_field_value(
            {"input_windows": [{"window_id": "w7d", "range": {"number": 7}}]},
            self.templates["time_context"]["value_schema"], TEMPLATE["writable_template"]["value"],
        )["input_windows"][0]["range"], {"number": 7, "direction": None, "unit": None})

    async def test_real_unresolved_survives_without_repeated_completion_then_new_evidence_updates(self):
        partial = deepcopy(FIELD)
        partial["value"]["input_windows"][0]["time_field"] = None
        partial.update(unresolved=["下载事件时间字段尚未确认"], question="下载事件应使用哪个时间字段？")
        with patch.object(fill, "ainvoke_json_object", AsyncMock(return_value=(partial, json.dumps(partial)))) as model:
            result = await fill.fill_field_templates(
                [TEMPLATE], user_query="近7天下载次数", tool_evidence="时间字段未知", llm=None,
            )
            self.assertEqual(model.await_count, 1)
            self.assertEqual(result[0]["unresolved"], partial["unresolved"])
            self.assertEqual(result[0]["question"], partial["question"])
            model.return_value = (deepcopy(FIELD), json.dumps(FIELD))
            updated = await fill.fill_field_templates(
                [TEMPLATE], user_query="近7天下载次数", tool_evidence="时间字段为 event_time", llm=None,
                current_values=result, reuse_field_ids={"time_context"},
            )
            self.assertEqual(model.await_count, 2)
            self.assertEqual(updated, [FIELD])
            self.assertIn("时间字段为 event_time", model.await_args.args[1][1]["content"])

    async def test_invalid_enum_and_container_types_still_request_structure_repair(self):
        for invalid in ["invented_policy", [], {}]:
            good = {"value": {"components": [{"field_ref": "device_id", "null_semantics": None}]},
                    "rationale": "空值规则无证据，留空"}
            bad = deepcopy(good)
            bad["value"]["components"][0]["null_semantics"] = invalid
            with self.subTest(invalid=invalid), patch.object(fill, "ainvoke_json_object", AsyncMock(
                side_effect=[(bad, json.dumps(bad)), (good, json.dumps(good))],
            )) as model:
                detail = await fill.fill_field_template_detailed(
                    self.templates["identity"], user_query="每设备下载次数", tool_evidence="device_id", llm=None,
                )
                self.assertEqual(model.await_count, 2)
                self.assertTrue(detail["completion_retried"])
                self.assertIsNone(detail["output"]["value"]["components"][0]["null_semantics"])
                self.assertIn("只使用用户原始问题和工具证据", model.await_args.args[1][-1]["content"])


class CriticalPathsTests(unittest.TestCase):
    def test_empty_optional_containers_are_not_missing(self):
        # 宁缺毋滥: empty filters/dimensions/windows/sequence must not force a refill.
        self.assertEqual(fill._find_missing_critical_paths(
            {"field_id": "sources"},
            {"facts": [{"source_ref": "evt", "filters": []}], "dimensions": []},
        ), [])
        self.assertEqual(fill._find_missing_critical_paths(
            {"field_id": "time_context"},
            {"output_bucket": {"time_field": None, "grain": None}, "input_windows": []},
        ), [])
        self.assertEqual(fill._find_missing_critical_paths(
            {"field_id": "sequence"},
            {"partition_fields": [], "ordering": [], "dedup": None, "truncation": None},
        ), [])
        self.assertEqual(fill._find_missing_critical_paths(
            {"field_id": "pipeline"},
            {"deduplication_before_join": None, "deduplication_before_aggregation": None,
             "operation_order": []},
        ), [])

    def test_present_items_still_require_structural_leaves(self):
        missing = fill._find_missing_critical_paths(
            {"field_id": "sources"},
            {"facts": [{"source_ref": "evt", "filters": [{"operator": "eq"}]}], "dimensions": []},
        )
        self.assertIn("facts[].filters[].field_ref", missing)
        missing = fill._find_missing_critical_paths(
            {"field_id": "output_features"},
            {"layout": "cartesian", "axes": [{"axis_id": "window", "values": []}],
             "measures": [], "features": []},
        )
        self.assertEqual(missing, [])  # Empty axis values are incomplete evidence, not broken structure.


class RenderTests(unittest.TestCase):
    def test_unknown_rank_partition_is_not_rendered_as_confirmed_global_partition(self):
        for partition in [None, []]:
            value = {"layout": "cartesian", "axes": [], "measures": [{
                "measure_id": "downloads", "name_pattern": "downloads", "attach_rank": {
                    "rank_type": "ntile", "bucket_count": 100, "partition_by": partition,
                },
            }]}
            columns = render.expand_output_features(value)
            self.assertEqual(columns[1]["rank"]["partition_by"], partition)

    def test_partial_output_count_does_not_cap_the_task_output_columns(self):
        fields = [
            {"field_id": "identity", "value": {"components": [{"field_ref": "device_id"}]}},
            {"field_id": "output_features", "value": {
                "layout": "cartesian", "axes": [{"axis_id": "window", "values": []}],
                "measures": [{"measure_id": "downloads", "aggregation": None}],
            }},
        ]
        text = render.render_constraint_context(fields)
        self.assertIn("device_id", text)
        self.assertIn("不能视为信息不完备时的任务总列数", text)
        self.assertNotIn("总输出列数（按语义项计数）应为", text)
        self.assertIn("尚未明确的轴或指标不以零列解释", text)
        self.assertIn("不得为了填满 IR 猜测业务值", text)

    def test_shared_contract_includes_policy_once_and_omits_warnings(self):
        runtime = FakeRuntime()
        self.assertEqual(ir_hooks.get_ir_context(runtime), "")
        runtime.set_cache("ir_field_values", [FIELD])
        runtime.set_cache("ir_gate_warnings", ["window conflict"])
        text = ir_hooks.get_ir_context(runtime)
        self.assertEqual(text.count("【约束遵循规则 - 必须遵守】"), 1)
        self.assertNotIn("window conflict", text)
        self.assertNotIn("口径一致性风险提示", text)
        self.assertEqual(runtime.get_cache("ir_gate_warnings"), ["window conflict"])
        self.assertEqual(text, render.render_constraint_context([FIELD]))
        self.assertIn("事实记录身份不等于去重", text)
        self.assertEqual(render.render_constraint_context([]), "")


if __name__ == "__main__":
    unittest.main()
