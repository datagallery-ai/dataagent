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
"""把历史里没有结果的模型调用补成错误消息。

checkpoint 恢复、用户中断、或者模型吐出截断的参数时，对话里会留下
「助手宣布要调工具，但后面没有工具消息」的缺口。下一次请求模型时，
多数接口会因为配不平而直接报错。

`before_agent` 在这一轮开始前扫完整段 `messages`：凡是还没有被
`type == "tool"` 的消息认领的调用，就在它所属的那条助手消息后面补一条
`status="error"` 的工具消息，再用一条清除全部消息的 `RemoveMessage`
把通道整表换掉。这样补丁落在原来的调用旁边，而不是被追加到历史末尾。

异步入口不在这里实现。图在发现没有覆盖异步钩子时，会退回调用这个同步方法。
"""

from typing import Any

import langchain.agents.middleware as agent_mw
import langchain_core.messages as lc_messages
import langgraph.graph.message as lg_message
import langgraph.runtime as lg_runtime
from langgraph.types import TracePolicy, omit_payload


def _pending_calls(message: lc_messages.AIMessage):
    """一条助手消息上要检查的调用：合法的在前，解析失败的在后。"""
    return (*message.tool_calls, *message.invalid_tool_calls)


def _closed_call_ids(history) -> set:
    """整段历史里已经被工具消息认领的 id。

    只看 `.type == "tool"`，不要求它是 `ToolMessage` 实例，也不看它站在
    助手消息的前面还是后面。缺属性时让异常自己冒出来。
    """
    return {item.tool_call_id for item in history if item.type == "tool"}


def _any_unanswered(history, closed: set) -> bool:
    """第二遍：有没有任何 id 既不是 None、又没被认领。

    条件式写两次下标。第一次不是 None 时必须再读一次，`in` 用第二次的值。
    """
    for item in history:
        if not isinstance(item, lc_messages.AIMessage):
            continue
        for spec in _pending_calls(item):
            if spec["id"] is not None and spec["id"] not in closed:
                return True
    return False


def _error_result(spec: dict, call_id):
    """按调用自己的 type 字段选文案，并做成一条错误工具消息。

    `call_id` 在算 `name or "unknown"` 之前已经读好。这里不再读 `["id"]`。
    """
    label = spec["name"] or "unknown"
    if spec.get("type") == "invalid_tool_call":
        text = f"Tool call {label} with id {call_id} could not be executed - arguments were malformed or truncated."
    else:
        text = f"Tool call {label} with id {call_id} did not complete - no result was recorded. It may have been cancelled or interrupted."
    return lc_messages.ToolMessage(content=text, name=label, tool_call_id=call_id, status="error")


class PatchToolCallsMiddleware(agent_mw.AgentMiddleware):
    """在 agent 开始前补上悬空的工具调用。

    没有构造参数。`runtime` 只是框架按名字注入的钩子参数，这里不使用它。
    """

    trace_policy = TracePolicy(process_inputs=omit_payload)

    def before_agent(
        self,
        state: agent_mw.AgentState,
        runtime: lg_runtime.Runtime[Any],
    ) -> dict[str, Any] | None:
        """三遍直接迭代调用方给的 messages，不先拷成 list。"""
        history = state["messages"]
        if not history:
            return None
        closed = _closed_call_ids(history)
        if not _any_unanswered(history, closed):
            return None
        rebuilt: list = []
        for item in history:
            rebuilt.append(item)
            if not isinstance(item, lc_messages.AIMessage):
                continue
            for spec in _pending_calls(item):
                call_id = spec["id"]
                if call_id is None or call_id in closed:
                    continue
                rebuilt.append(_error_result(spec, call_id))
        clear = lc_messages.RemoveMessage(id=lg_message.REMOVE_ALL_MESSAGES)
        return {"messages": [clear, *rebuilt]}
