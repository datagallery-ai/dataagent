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
"""L1 ReActAgent：组合 AgentBuilder，运行时委托 DataAgent（Flex/React 路径）。"""

from __future__ import annotations

from typing import Any

from dataagent.interface.sdk.agent import DataAgent
from dataagent.interface.sdk.builder import AgentBuilder

# 与 AgentBuilder 对外配置面一致（由 __getattr__ 转发，便于链式调用仍返回 ReActAgent）
_BUILDER_FORWARD_NAMES = frozenset(
    {
        "set_name",
        "set_base_config",
        "set_models",
        "set_scenario",
        "set_actions",
        "set_history",
        "set_knowledge_base",
        "set_metavisor",
        "set_database",
        "set_ontology",
        "from_config",
    }
)


class ReActAgent:
    """北向 L1：配置能力与 `AgentBuilder` 相同，可直接链式调用下列方法（均转发到内部 builder）

    运行时：首次 ``await chat()`` 会内部执行 L0 ``AgentBuilder.build()``；不提供公开的 ``build()``。
    ``astream`` 需先完成一次 ``chat``（或直接使用 ``builder`` 自行构建）。只读 ``builder`` 可访问原生 `AgentBuilder`。
    """

    __slots__ = ("_builder", "_data_agent", "name")

    def __init__(self, builder: AgentBuilder | None = None) -> None:
        self._builder = builder or AgentBuilder()
        self._data_agent: DataAgent | None = None

    def __dir__(self) -> list[str]:
        merged = {*object.__dir__(self), *_BUILDER_FORWARD_NAMES, *dir(self._builder)}
        merged -= {"build"}
        return sorted(merged)

    def __getattr__(self, name: str) -> Any:
        if name == "build":
            raise AttributeError(
                "'ReActAgent' object has no attribute 'build'. Use `await chat(...)` to load the agent。 "
            )

        attr_name = "set_raw_models" if name == "set_models" else name
        attr = getattr(self._builder, attr_name)
        if not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if result is self._builder or result is None:
                return self
            return result

        return wrapped

    @property
    def builder(self) -> AgentBuilder:
        """底层 `AgentBuilder`（只读）。需要 IDE 对 ``set_*`` 补全时可写 ``agent.builder.set_...``。"""
        return self._builder

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        """Chat with the agent."""
        await self._ensure_built()
        assert self._data_agent is not None
        return await self._data_agent.chat(*args, **kwargs)

    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        """Stream responses from the agent."""
        await self._ensure_built()
        assert self._data_agent is not None
        return self._data_agent.astream(*args, **kwargs)

    async def _ensure_built(self) -> None:
        if self._data_agent is None:
            (
                self._builder.set_base_config(
                    name=self._builder.name or "ReActAgent", description="", agent_type="deep_analyze"
                )
            )
            self._data_agent = await self._builder.build()
