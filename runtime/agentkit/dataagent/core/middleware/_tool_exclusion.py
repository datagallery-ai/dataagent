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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""按名字屏蔽工具的中间件。

画像（`HarnessProfile.excluded_tools`）声明某些工具不该出现在这个模型面前时，
由这里负责落实。两道关卡：

1. 模型调用前：把被屏蔽的名字从工具列表里摘掉，模型看不见它们。
2. 工具执行前：万一模型仍然发出了这个名字的调用（例如历史消息里带着），
   直接返回错误结果，不真的执行。

第 2 道关卡不是安全边界——执行器里这些工具仍然注册着，它只是保证
「执行行为」和「对模型宣称的可用范围」对得上。真正的路径权限在文件工具那边。

放置位置：必须在中间件栈的末尾。所有会注入工具的中间件都跑完之后再摘，
否则后注入进来的工具会漏过筛子。
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from langchain.agents.middleware import types as lc
from langchain_core.messages import ToolMessage
from langgraph.prebuilt import tool_node
from langgraph.types import Command

#: 模型请求与响应：我们对泛型参数不做区分，统一用 Any
Request: TypeAlias = "lc.ModelRequest[Any]"
Reply: TypeAlias = "lc.ModelResponse[Any]"
#: 工具执行的结果：要么一条工具消息，要么一个状态更新指令
ToolOutcome: TypeAlias = "ToolMessage | Command"
ToolCall: TypeAlias = "tool_node.ToolCallRequest"


def _label(spec: Any) -> str | None:
    """取出工具的名字；取不到或不是字符串就返回 None。

    工具有两种形态：dict（provider 原生工具）和 `BaseTool` 实例。
    """
    raw = spec.get("name") if isinstance(spec, dict) else getattr(spec, "name", None)
    return raw if isinstance(raw, str) else None


class _ToolExclusionMiddleware(lc.AgentMiddleware[Any, Any, Any]):
    """把画像声明为屏蔽的工具，从模型请求与工具执行两处挡掉。

    Args:
        excluded: 要屏蔽的工具名集合。传空集合时本中间件全程不做事。
    """

    def __init__(self, *, excluded: frozenset[str]) -> None:
        self._blocked = excluded

    # ------- 关卡 1：模型请求 -------

    def _trim(self, request: Request) -> Request:
        """摘掉被屏蔽的工具后返回新请求；没有要屏蔽的就原样返回。"""
        if not self._blocked:
            return request
        keep = [spec for spec in request.tools if _label(spec) not in self._blocked]
        return request.override(tools=keep)

    def wrap_model_call(self, request: Request, handler: Callable[[Request], Reply]) -> Reply:
        """摘掉被屏蔽的工具，再交给下一层。"""
        return handler(self._trim(request))

    async def awrap_model_call(self, request: Request, handler: Callable[[Request], Awaitable[Reply]]) -> Reply:
        """`wrap_model_call` 的异步版。"""
        return await handler(self._trim(request))

    # ------- 关卡 2：工具执行 -------

    def _deny(self, request: ToolCall) -> ToolMessage | None:
        """命中屏蔽名单就返回错误结果；否则返回 None 表示放行。"""
        called = request.tool_call["name"]
        if called not in self._blocked:
            return None
        return ToolMessage(
            content=f"Error: {called} is not available.",
            tool_call_id=request.tool_call["id"] or "",
            name=called,
            status="error",
        )

    def wrap_tool_call(self, request: ToolCall, handler: Callable[[ToolCall], ToolOutcome]) -> ToolOutcome:
        """命中屏蔽名单就直接拒绝，不执行工具。"""
        return self._deny(request) or handler(request)

    async def awrap_tool_call(self, request: ToolCall, handler: Callable[[ToolCall], Awaitable[ToolOutcome]]) -> ToolOutcome:
        """`wrap_tool_call` 的异步版。"""
        return self._deny(request) or await handler(request)
