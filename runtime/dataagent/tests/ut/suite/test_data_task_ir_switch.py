"""Checks for the paired DataTaskIR switch in the de_agent Agent pre-hook."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolHookRunner
from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config
from dataagent.actions.tools.hooks.examples import ir_hooks
from dataagent.config.config_manager import ConfigManager
from dataagent.core.flex.agent import FlexAgent
from dataagent.core.suite.validation import validate_merged_config
from dataagent.utils.runtime_paths import dataagent_package_path

IR_PREFIX = "dataagent.actions.tools.hooks.examples.ir_hooks."
SUITE_ROOT = dataagent_package_path("core", "suite", "builtin_suites", "de_agent")
IR_CONFIG_HOOK = "hooks.ir_config.validate_data_task_ir_config"


def load_ir_config_hook():
    return FlexAgent.import_hook_from_suite_root(
        IR_CONFIG_HOOK, root=SUITE_ROOT, suite_name="data_engineering", location="agent.pre",
    )


def ir_config(create: bool, update: bool, section: str = "builtin") -> dict:
    entries = []
    for tool_name, hook, enabled in (
        ("create_plan", "create_ir", create),
        ("complete_current_todo", "update_ir", update),
    ):
        entries.append({
            "module": "dataagent.actions.tools.local_tool.plan",
            "function": tool_name,
            "hooks": {"post": [IR_PREFIX + hook] if enabled else []},
        })
    return {"TOOLS": {section: entries}}


class IRSwitchConfigTests(unittest.TestCase):
    def setUp(self):
        self.hook = load_ir_config_hook()

    def check_config(self, config):
        state = {"user_query": "你好"}
        self.assertIs(self.hook(state, SimpleNamespace(get_all_config=lambda: config)), state)

    def test_all_configurations_load_but_single_hook_is_rejected_before_the_turn(self):
        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            manager = ConfigManager()
            for section in ("builtin", "local_functions"):
                for policy in ("eager", "on_consume"):
                    for create, update in ((False, False), (True, True), (True, False), (False, True)):
                        with self.subTest(section=section, policy=policy, create=create, update=update):
                            config = ir_config(create, update, section)
                            config["DATA_TASK_IR"] = {"refresh_policy": policy}
                            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
                            # The framework no longer knows about this Suite-specific rule.
                            manager.reload(str(config_path))
                            if create != update:
                                missing = "update_ir" if create else "create_ir"
                                with self.assertRaisesRegex(ValueError, "DataTaskIR 配置错误.*缺少 " + missing):
                                    self.check_config(manager.get_all())
                            else:
                                self.check_config(manager.get_all())

    def test_validates_hooks_after_default_and_user_layers_are_merged(self):
        with TemporaryDirectory() as temp:
            default_path = Path(temp) / "default.yaml"
            user_path = Path(temp) / "user.yaml"
            default_path.write_text(yaml.safe_dump(ir_config(True, False)), encoding="utf-8")
            user_path.write_text(yaml.safe_dump(ir_config(False, True, "local_functions")), encoding="utf-8")
            # local_functions replaces both builtin registrations, leaving only update_ir.
            manager = ConfigManager()
            manager.reload(str(user_path), str(default_path))
            with self.assertRaisesRegex(ValueError, "DataTaskIR 配置错误.*缺少 create_ir"):
                self.check_config(manager.get_all())
            user_path.write_text(yaml.safe_dump(ir_config(True, True, "local_functions")), encoding="utf-8")
            manager.reload(str(user_path), str(default_path))
            self.check_config(manager.get_all())

    def test_local_function_replacements_can_disable_both_builtin_hooks(self):
        config = ir_config(True, True)
        config["TOOLS"].update(ir_config(False, False, "local_functions")["TOOLS"])
        self.check_config(config)

    def test_suite_replaces_can_remove_a_builtin_ir_hook(self):
        config = ir_config(True, True)
        config["TOOLS"]["_replaces"] = ["create_plan"]
        with self.assertRaisesRegex(ValueError, "DataTaskIR 配置错误.*缺少 create_ir"):
            self.check_config(config)

    def test_ir_hooks_must_be_post_hooks(self):
        config = ir_config(True, True)
        hooks = config["TOOLS"]["builtin"][0]["hooks"]
        hooks["pre"] = hooks.pop("post")
        with self.assertRaisesRegex(ValueError, "DataTaskIR 配置错误.*hooks.post"):
            self.check_config(config)

    def test_commented_hooks_and_consumers_alone_do_not_enable_ir(self):
        config = ir_config(False, False)
        config["TOOLS"]["builtin"][0]["hooks"]["post"] = None
        config["TOOLS"]["builtin"][1]["hooks"] = {"pre": [IR_PREFIX + "ensure_ir_ready"]}
        self.check_config(config)

    def test_suite_merges_and_runs_ir_check_before_metadata_check(self):
        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            for create, update in ((False, False), (True, True), (True, False), (False, True)):
                with self.subTest(create=create, update=update):
                    config = ir_config(create, update)
                    config["SUITE"] = {"include": ["data_engineering"]}
                    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
                    manager = ConfigManager()
                    manager.reload(
                        str(config_path), str(dataagent_package_path("core", "flex", "flex_default_configs.yaml")),
                    )
                    hooks = manager.get("HOOKS.agent.pre")
                    self.assertEqual(hooks[0]["name"], "data_engineering." + IR_CONFIG_HOOK)
                    self.assertEqual(hooks[1]["name"], "data_engineering.hooks.checker.check_metadata")
                    agent = object.__new__(FlexAgent)
                    agent.config_manager = manager
                    agent._run_builtin_agent_pre_hooks = lambda state, runtime: state
                    metadata_check = Mock(side_effect=lambda state, runtime: state)
                    agent._pre_hooks = [
                        agent._resolve_hook_callable(hooks[0]["name"], location="agent.pre"), metadata_check,
                    ]
                    runtime = SimpleNamespace(get_all_config=manager.get_all)
                    state = {"user_query": "你好"}
                    if create != update:
                        with self.assertRaisesRegex(ValueError, "DataTaskIR 配置错误"):
                            agent._run_agent_pre_hooks(state, runtime)
                        metadata_check.assert_not_called()
                    else:
                        self.assertIs(agent._run_agent_pre_hooks(state, runtime), state)
                        metadata_check.assert_called_once_with(state, runtime)


class IRSwitchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_hooks_do_not_infer_create_trace_or_supply_ir_to_consumers(self):
        with TemporaryDirectory() as temp:
            for policy in ("eager", "on_consume"):
                with self.subTest(policy=policy):
                    config = ir_config(False, False)
                    config["DATA_TASK_IR"] = {"refresh_policy": policy}
                    # Keep the Suite consumer hooks active, as in a real disabled run.
                    for consumer in ("wrapped_nl2sql_sub_agent_tool", "validate_deliverables"):
                        config["TOOLS"]["builtin"].append({
                            "module": "unused",
                            "function": consumer,
                            "hooks": {"pre": [IR_PREFIX + "ensure_ir_ready"]},
                        })
                    validate_merged_config(config)
                    load_ir_config_hook()({}, SimpleNamespace(get_all_config=lambda config=config: config))
                    cache = {}
                    runtime = SimpleNamespace(
                        workspace_dir=Path(temp), get_cache=cache.get,
                        set_cache=cache.__setitem__,
                        get_config=lambda key, default=None, policy=policy: (
                            policy if key == "DATA_TASK_IR.refresh_policy" else default
                        ),
                        llm=Mock(side_effect=AssertionError("Disabled IR must not request a model")),
                    )
                    with (
                        patch.object(ir_hooks, "fill_field_templates", AsyncMock()) as fill,
                        patch.object(ir_hooks, "_ir_consistency_gate", AsyncMock()) as gate,
                        patch.object(ir_hooks, "_resolve_ir_trace_dir", wraps=ir_hooks._resolve_ir_trace_dir) as trace,
                    ):
                        for entry in config["TOOLS"]["builtin"]:
                            hooks = load_tool_hooks_from_config(entry["hooks"])
                            inv = ToolHookInvocation(
                                tool_name=entry["function"], tool_call_id="test", tool_args={},
                                runtime=runtime, metadata={}, state={"user_query": "test"},
                                execution=SimpleNamespace(success=True),
                            )
                            await ToolHookRunner.run_pre_hooks(hooks.pre, inv)
                            await ToolHookRunner.run_post_hooks(hooks.post, inv)
                            self.assertEqual(ir_hooks.get_ir_context(runtime), "")
                        fill.assert_not_awaited()
                        gate.assert_not_awaited()
                        trace.assert_not_called()
                    runtime.llm.assert_not_called()
                    self.assertFalse((Path(temp) / ".memory" / "ir_trace").exists())


if __name__ == "__main__":
    unittest.main()
