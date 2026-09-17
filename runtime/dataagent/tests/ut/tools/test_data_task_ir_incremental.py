"""Offline regressions for IR cache correctness and the shared SQL contract."""

import asyncio
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dataagent.actions.tools.hooks.examples import ir_hooks
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import fill, render

FIELD = {"field_id": "time_window", "value": {
    "time_field": "event_time", "range": {"direction": "past", "number": 7, "unit": "day"},
}, "unresolved": []}
TEMPLATE = {"field_id": "time_window", "writable_template": deepcopy(FIELD)}


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
        for target, replacement in [
            ("get_action_nodes", lambda runtime: self.nodes),
            ("fill_ir_fields", self.fill_mock),
            ("_ir_consistency_gate", self.gate_mock),
        ]:
            patcher = patch.object(ir_hooks, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

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
        await ir_hooks.update_ir(self.inv)
        self.nodes.append(SimpleNamespace(action="read_file", params={"path": "schema.md"},
                                          success=True, output="unrelated table"))
        with patch.object(ir_hooks, "detect_field_changes", AsyncMock(return_value=set())) as detector:
            await ir_hooks.update_ir(self.inv)
            self.assertEqual(self.fill_mock.await_args.kwargs["reuse_field_ids"], {"time_window"})
            self.nodes[0].output = "window now 30 days"
            await ir_hooks.update_ir(self.inv)
            self.assertEqual(detector.await_count, 1)
            self.assertFalse(self.fill_mock.await_args.kwargs["reuse_field_ids"])

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

    async def test_fill_failure_does_not_serve_old_snapshot(self):
        await ir_hooks.update_ir(self.inv)
        self.inv.state["user_query"] = "changed"
        self.fill_mock.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await ir_hooks.update_ir(self.inv)
        with self.assertRaises(RuntimeError):
            ir_hooks.get_ir_context(self.runtime)
        self.fill_mock.side_effect = None
        await ir_hooks.update_ir(self.inv)
        self.assertIn("DataTaskIR", ir_hooks.get_ir_context(self.runtime))


class DetectorAndFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_detector_failure_and_invalid_ids_refill_all(self):
        for response in [None, {}, {"reestimate": ["time_windows"]}, {"reestimate": [1]}]:
            with self.subTest(response=response), patch.object(
                fill, "ainvoke_json_object", AsyncMock(return_value=(response, "")),
            ):
                result = await fill.detect_field_changes(
                    new_evidence="30 days", confirmed_fields=[FIELD], llm=None,
                )
                self.assertEqual(result, {"time_window"})
        with patch.object(fill, "ainvoke_json_object", AsyncMock(side_effect=TimeoutError)):
            self.assertEqual(await fill.detect_field_changes(
                new_evidence="30 days", confirmed_fields=[FIELD], llm=None,
            ), {"time_window"})

    async def test_detector_keeps_prefix_and_falls_back_when_over_budget(self):
        evidence = "CORRECTION: 30 days\n" + "x" * 7000
        with patch.object(fill, "ainvoke_json_object", AsyncMock(return_value=({"reestimate": []}, ""))) as llm:
            self.assertEqual(await fill.detect_field_changes(
                new_evidence=evidence, confirmed_fields=[FIELD], llm=None,
            ), set())
            self.assertIn(evidence, llm.await_args.args[1][1]["content"])
            self.assertEqual(await fill.detect_field_changes(
                new_evidence=evidence, confirmed_fields=[FIELD], llm=None, max_input_chars=100,
            ), {"time_window"})
            self.assertEqual(llm.await_count, 1)

    async def test_fill_only_reuses_explicitly_authorized_complete_fields(self):
        with TemporaryDirectory() as trace, patch.object(
            fill, "fill_field_template", AsyncMock(return_value=deepcopy(FIELD)),
        ) as model:
            result = await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None,
                current_values=[FIELD], reuse_field_ids={"time_window"}, trace_dir=trace,
            )
            self.assertEqual(result, [FIELD])
            self.assertEqual(model.await_count, 0)
            meta = json.loads((Path(trace) / "time_window/meta.json").read_text())
            self.assertTrue(meta["skipped_incremental"])
            await fill.fill_field_templates(
                [TEMPLATE], user_query="changed", tool_evidence="e", llm=None, current_values=[FIELD],
            )
            self.assertEqual(model.await_count, 1)
            partial = deepcopy(FIELD)
            partial["value"]["range"]["unit"] = None
            await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None,
                current_values=[partial], reuse_field_ids={"time_window"},
            )
            self.assertEqual(model.await_count, 2)

    async def test_first_fill_preserves_template_and_records_timing(self):
        with TemporaryDirectory() as trace, patch.object(
            fill, "ainvoke_json_object", AsyncMock(return_value=(deepcopy(FIELD), json.dumps(FIELD))),
        ) as model:
            await fill.fill_field_templates(
                [TEMPLATE], user_query="q", tool_evidence="e", llm=None, trace_dir=trace,
            )
            self.assertIn("time_field: event_time", model.await_args.args[1][1]["content"])
            meta = json.loads((Path(trace) / "time_window/meta.json").read_text())
            self.assertIsInstance(meta["model_call_seconds"], float)

    async def test_failed_batch_cancels_other_inflight_fields(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def model(template, **kwargs):
            if template["field_id"] == "broken":
                await started.wait()
                raise ValueError("model failure")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(fill, "fill_field_template", model), self.assertRaises(ValueError):
            await fill.fill_field_templates(
                [{"field_id": "broken"}, TEMPLATE], user_query="q", tool_evidence="e", llm=None,
            )
        self.assertTrue(cancelled.is_set())

    async def test_failed_completion_is_not_published_as_success(self):
        partial = deepcopy(FIELD)
        partial["value"]["range"]["unit"] = None
        with patch.object(
            fill, "ainvoke_json_object", AsyncMock(side_effect=[(partial, "{}"), TimeoutError()]),
        ), self.assertRaises(ValueError):
            await fill.fill_field_template(TEMPLATE, user_query="q", tool_evidence="e", llm=None)

    async def test_gate_receives_flat_values_and_failure_is_visible(self):
        runtime = FakeRuntime()
        with patch.object(ir_hooks, "ainvoke_json_object", AsyncMock(return_value=(
            {"warnings": [], "consistent": True}, "{}",
        ))) as model:
            warnings, completed = await ir_hooks._ir_consistency_gate(
                runtime, user_query="q", tool_evidence="e", field_results=[FIELD],
            )
            self.assertTrue(completed)
            self.assertEqual(warnings, [])
            self.assertIn('"time_field": "event_time"', model.await_args.args[1][1]["content"])
        with patch.object(ir_hooks, "ainvoke_json_object", AsyncMock(return_value=({}, "{}"))):
            warnings, completed = await ir_hooks._ir_consistency_gate(
                runtime, user_query="q", tool_evidence="e", field_results=[FIELD],
            )
            self.assertFalse(completed)
            self.assertTrue(warnings)


class RenderTests(unittest.TestCase):
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
