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
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.managers.llm_manager.adapters import LLMResponse, LLMStreamChunk
from dataagent.interface.sdk.agent import DataAgent
from dataagent.utils.runtime_paths import dataagent_package_path


class _FakeChatModel:
    """最小 fake：满足 Planner 的 bind_tools + astream 协议，不发真实请求。"""

    def __init__(self) -> None:
        self._turn = 0

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> _FakeChatModel:
        return self

    async def astream(self, _chat_input: Any, **_kwargs: Any):
        self._turn += 1
        if self._turn == 1:
            final = LLMResponse(
                content="",
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                tool_calls=[{"id": "call-1", "name": "multiply", "args": {"a": 3, "b": 2}}],
            )
        elif self._turn == 2:
            final = LLMResponse(
                content="",
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                tool_calls=[{"id": "call-2", "name": "add", "args": {"a": 5, "b": 6}}],
            )
        else:
            final = LLMResponse(
                content="11",
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        yield LLMStreamChunk(
            done=True,
            final_response=final,
        )


@pytest.mark.parametrize("backend", ["langgraph", "openjiuwen"])
def test_st_arithmetic_yaml_with_mock_llm(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """ST（system test）: arithmetic.yaml 的 ReAct 闭环冒烟。

    **看护内容（核心链路）**
    - 以 `DataAgent.from_config(arithmetic.yaml)` 作为北向入口，验证配置加载与全局初始化（prompt/tool/llm manager）
      后，ReAct/Flex 工作流能在指定 backend（`langgraph` / `openjiuwen`）下完成一次完整闭环：
      planner -> executor -> planner -> executor -> planner（最终 complete）。

    **模拟的 ReAct 轨迹**
    - 第 1 次 planner：LLM 返回 `multiply(a=3,b=2)` tool_call
    - 第 2 次 planner：LLM 返回 `add(a=5,b=6)` tool_call
    - 第 3 次 planner：LLM 返回最终答案 `11`

    **mock / 打桩边界（避免真实外部依赖）**
    - **LLM**：monkeypatch `Runtime.llm()`，返回 `_FakeChatModel`（不发真实模型请求）。
    - **工具执行**：monkeypatch `Executor._invoke_manager_tool_async()`，对 `multiply/add` 直接返回结果，
      避免依赖 `ArithmeticEnv` 或 `tool_manager` 的真实注册与执行。
    """
    if backend == "openjiuwen":
        pytest.importorskip("openjiuwen")

    # flex_runtime_from_config 会在初始化阶段解析 provider 环境变量（如 BAILIAN_BASE_URL / BAILIAN_API_KEY）。
    monkeypatch.setenv("BAILIAN_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("BAILIAN_API_KEY", "test-key")

    from dataagent.core.context.context import Context

    monkeypatch.setattr(Context, "persist_to_json", lambda _self: None, raising=True)
    monkeypatch.setattr(Context, "persist_meta_to_json", lambda _self: None, raising=True)

    # 方式B：在 Executor 边界 mock 工具执行，避免依赖 env/tools 注册细节。
    from dataagent.core.flex.nodes.executor import Executor
    from dataagent.core.managers.action_manager import ToolResult

    async def _fake_invoke_manager_tool_async(
        _self: Executor, tool_name: str, tool_args: dict[str, Any], runtime: Any = None
    ) -> ToolResult:
        if tool_name == "multiply":
            return ToolResult(success=True, data=int(tool_args["a"]) * int(tool_args["b"]))
        if tool_name == "add":
            return ToolResult(success=True, data=int(tool_args["a"]) + int(tool_args["b"]))
        return ToolResult(success=False, error=f"unexpected tool: {tool_name}")

    monkeypatch.setattr(Executor, "_invoke_manager_tool_async", _fake_invoke_manager_tool_async, raising=True)

    # 关键：拦截 runtime.llm(...)，返回 fake model，避免创建真实 LLM client。
    fake_llm = _FakeChatModel()

    def _fake_runtime_llm(_self: Runtime, _name: str) -> Any:
        return fake_llm

    monkeypatch.setattr(Runtime, "llm", _fake_runtime_llm, raising=True)

    cfg_path = dataagent_package_path("core", "flex", "examples", "arithmetic.yaml")
    # 用 DataAgent.from_config 走一遍 global_init，确保 prompt_manager / tool_manager 初始化完成
    agent = DataAgent.from_config(cfg_path)
    agent.config.set("AGENT_CONFIG.backend", backend)

    async def _run() -> dict[str, Any]:
        # FlexAgent.chat 需要 initial_state（至少 workspace/run_id/sub_id）。
        state = {
            "run_id": 0,
            "sub_id": 0,
            "workspace": str(tmp_path),
        }
        out = await agent.chat("What is 5 + 3 * 2", initial_state=state)
        assert isinstance(out, dict)
        return out

    out = asyncio.run(_run())

    assert isinstance(out, dict)
    assert out.get("complete") is True
    assert out.get("messages")

    # 最小闭环断言：两次工具调用 + 最终回答
    assert out.get("num_turns") == 3
    assert out.get("num_valid_tool_calls") == 2
