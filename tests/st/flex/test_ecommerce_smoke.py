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
from dataagent.core.managers.action_manager import ToolResult
from dataagent.core.managers.llm_manager.adapters import LLMResponse, LLMStreamChunk
from dataagent.interface.sdk.agent import DataAgent
from dataagent.utils.runtime_paths import dataagent_package_path


class _FakeChatModel:
    """按回合返回 tool_calls，用于模拟 ReAct planner→executor 闭环。"""

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
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "perceive_metadata_from_memory",
                        "args": {"keywords_list": ["订单", "客户", "购买", "金额"]},
                    }
                ],
            )
        elif self._turn == 2:
            final = LLMResponse(
                content="",
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                tool_calls=[
                    {
                        "id": "call-2",
                        "name": "report_generator",
                        "args": {
                            "query": "生成电商分析报告（测试桩）",
                            "output_path": "/tmp/report.md",
                            "analysis_path": "/tmp/statistical_analysis.json",
                            "images_path": "/tmp/plots.json",
                        },
                    }
                ],
            )
        else:
            final = LLMResponse(
                content="done",
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )

        yield LLMStreamChunk(done=True, final_response=final)


@pytest.mark.parametrize("backend", ["langgraph", "openjiuwen"])
def test_st_ecommerce_yaml_with_mock_llm_and_tools(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """ST（system test）: ecommerce_agent.yaml 的最小 ReAct 闭环冒烟。

    **看护内容（核心链路）**
    - 以 `DataAgent.from_config(ecommerce_agent.yaml)` 作为入口，验证 Flex/ReAct 编排链路能在指定 backend
      下完成一次最小闭环：planner -> executor -> planner -> executor -> planner（最终 complete）。

    **mock / 打桩边界（避免真实外部依赖）**
    - **LLM**：monkeypatch `Runtime.llm()` 返回 `_FakeChatModel`（不发真实模型请求）。
    - **工具执行**：monkeypatch `Executor._invoke_manager_tool_async()`，对工具名返回 `ToolResult`（不执行真实工具/MCP/DB）。
    - **Tools 初始化**：monkeypatch `tool_manager.init_from_config/enable_auto_discover` 为 no-op，避免 MCP 发现与子进程启动。
    - **Context 持久化**：禁用 `Context.persist_to_pg/json/meta`，避免真实 PG/落盘副作用。
    """
    if backend == "openjiuwen":
        pytest.importorskip("openjiuwen")

    # provider env（flex_runtime_from_config 解析时要求）
    monkeypatch.setenv("BAILIAN_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("BAILIAN_API_KEY", "test-key")

    # embedding provider ("embedding") reads from env vars; set dummy values so global_init won't fail.
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9998/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")

    # ecommerce_agent.yaml 里有 $env{} 占位，给 dummy 值避免解析期报错
    monkeypatch.setenv("MEMORY_LONG_TERM_STORAGE_URL", "http://127.0.0.1:9200")
    monkeypatch.setenv("MEMORY_SHORT_TERM_STORAGE_URL", "postgresql://user:pass@127.0.0.1:5432/db")
    monkeypatch.setenv("DATASOURCE_DATABASE_ADDRESS", "mysql+pymysql://user:pass@127.0.0.1:3306/Ecommerce")

    from dataagent.core.context.context import Context

    monkeypatch.setattr(Context, "persist_to_json", lambda _self: None, raising=True)
    monkeypatch.setattr(Context, "persist_meta_to_json", lambda _self: None, raising=True)

    # 禁用 ToolManager 初始化/自动发现（避免启动 MCP server 子进程）
    from dataagent.core.managers.action_manager.manager import ToolManager

    monkeypatch.setattr(ToolManager, "init_from_config", lambda _self, _cfg: None, raising=True)
    monkeypatch.setattr(ToolManager, "enable_auto_discover", lambda _self: None, raising=True)

    # 方式B：在 Executor 边界 mock 工具执行
    from dataagent.core.flex.nodes.executor import Executor

    async def _fake_invoke_manager_tool_async(_self: Executor, tool_name: str, tool_args: dict[str, Any]) -> ToolResult:
        if tool_name == "perceive_metadata_from_memory":
            return ToolResult(success=True, data="Found tables/columns (stub)")
        if tool_name == "report_generator":
            return ToolResult(success=True, data="# Report (stub)\n")
        return ToolResult(success=False, error=f"unexpected tool: {tool_name}")

    monkeypatch.setattr(Executor, "_invoke_manager_tool_async", _fake_invoke_manager_tool_async, raising=True)

    # Fake LLM：同一个实例跨回合计数
    fake_llm = _FakeChatModel()

    def _fake_runtime_llm(_self: Runtime, _name: str) -> Any:
        return fake_llm

    monkeypatch.setattr(Runtime, "llm", _fake_runtime_llm, raising=True)

    cfg_path = dataagent_package_path("core", "flex", "examples", "ecommerce_agent.yaml")
    agent = DataAgent.from_config(cfg_path)
    agent.config.set("AGENT_CONFIG.backend", backend)
    workspace = tmp_path.resolve()
    agent.config.set("WORKSPACE.path", str(workspace))

    async def _run() -> dict[str, Any]:
        state = {
            "run_id": 0,
            "sub_id": 0,
            "workspace": str(workspace),
        }
        out = await agent.chat("请基于订单数据生成分析报告（ST stub）", initial_state=state)
        assert isinstance(out, dict)
        return out

    out = asyncio.run(_run())

    assert out.get("complete") is True
    assert out.get("num_turns") == 3
    assert out.get("num_valid_tool_calls") == 2
