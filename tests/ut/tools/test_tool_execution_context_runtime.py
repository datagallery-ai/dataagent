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
"""ToolExecutionContext.runtime injection via LocalToolWrapper."""

from dataagent.actions.tools.local import LocalToolWrapper
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.framework_adapters.runtime.context import clear_current_runtime, set_current_runtime

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.config.config_manager import ConfigManager


def _minimal_env(**kwargs) -> Env:
    return Env(
        llm_configs={},
        tavily_configs={},
        modules={},
        hooks={},
        **kwargs,
    )


def _tool_needing_context(*, _tool_context: ToolExecutionContext) -> dict:
    return {
        "runtime": _tool_context.runtime,
        "user_id": _tool_context.runtime.user_id if _tool_context.runtime else None,
    }


class TestToolExecutionContextRuntimeInjection:
    """LocalToolWrapper merges get_current_runtime() into _tool_context at call time."""

    def test_build_injected_tool_context_without_active_runtime(self):
        """Outside workflow, injected context has runtime=None."""
        cm = ConfigManager()
        wrapper = LocalToolWrapper(
            _tool_needing_context,
            name="ctx_probe",
            tool_context=ToolExecutionContext(config_manager=cm),
        )
        ctx = wrapper._build_injected_tool_context()
        assert ctx.config_manager is cm
        assert ctx.runtime is None

    def test_build_injected_tool_context_with_active_runtime(self):
        """Inside workflow ContextVar, injected context carries the active Runtime."""
        cm = ConfigManager()
        runtime = Runtime(_minimal_env(config_manager=cm))
        runtime.update_from_state(
            {
                "user_id": "u_test",
                "session_id": "s_test",
                "run_id": 7,
                "sub_id": 1,
            }
        )
        set_current_runtime(runtime)
        try:
            wrapper = LocalToolWrapper(
                _tool_needing_context,
                name="ctx_probe",
                tool_context=ToolExecutionContext(config_manager=cm),
            )
            ctx = wrapper._build_injected_tool_context()
            assert ctx.runtime is runtime
            assert ctx.runtime.user_id == "u_test"
            assert ctx.runtime.session_id == "s_test"
            assert ctx.runtime.run_id == 7
            assert ctx.runtime.sub_id == 1
        finally:
            clear_current_runtime()

    def test_acall_injects_runtime_from_contextvar(self):
        """acall path passes _tool_context with runtime when ContextVar is set."""
        cm = ConfigManager()
        runtime = Runtime(_minimal_env(config_manager=cm))
        runtime.update_from_state({"user_id": "from_rt", "session_id": "sess", "run_id": 2, "sub_id": 0})

        set_current_runtime(runtime)
        try:
            wrapper = LocalToolWrapper(
                _tool_needing_context,
                name="ctx_probe",
                tool_context=ToolExecutionContext(config_manager=cm),
            )
            import asyncio

            result = asyncio.run(wrapper.acall())
            assert result.success is True
            assert result.data["runtime"] is runtime
            assert result.data["user_id"] == "from_rt"
        finally:
            clear_current_runtime()

    def test_acall_merges_tool_config(self):
        """Per-tool YAML config keys still appear on injected context."""
        cm = ConfigManager()

        def _probe(*, _tool_context: ToolExecutionContext) -> str:
            cfg = _tool_context.tool_config or {}
            return f"{cfg.get('llm_model')}|{cfg.get('abc')}"

        wrapper = LocalToolWrapper(
            _probe,
            name="cfg_probe",
            tool_context=ToolExecutionContext(config_manager=cm),
            llm_model="deepseek",
            abc="custom",
        )
        import asyncio

        result = asyncio.run(wrapper.acall())
        assert result.success is True
        assert result.data == "deepseek|custom"
