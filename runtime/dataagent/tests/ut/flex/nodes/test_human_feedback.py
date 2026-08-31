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
"""回归测试：HITL 透明模式下，HumanFeedbackNode 早退分支返回的 messages 必须
只包含 hook 新增的 ToolMessage，否则 messages_full.json 的尾部去重会把它吞掉。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.flex.nodes.human_feedback import HumanFeedbackNode


def test_early_exit_returns_only_injected_tool_message() -> None:
    """hitl_auto_resolved=True 时只返回 hook 新增的 ToolMessage，不返回完整 messages 列表。"""

    tool_msg = ToolMessage(content="ok", tool_call_id="call_1", name="request_human_feedback")
    state = {
        "messages": [
            HumanMessage(content="<user_query>原始问题</user_query>"),
            AIMessage(content="please confirm"),
            tool_msg,
        ],
        "hitl_auto_resolved": True,
        "hitl_resolved_info": {"answer": "ok", "task_name": "q01"},
    }

    result = asyncio.run(HumanFeedbackNode()._aprocess(state))

    # 关键断言：只返回 1 条 ToolMessage，不是 3 条
    assert result["messages"] == [tool_msg]
    assert len(result["messages"]) == 1
    # 保留 hitl_resolved_info / need_human_feedback
    assert result["hitl_auto_resolved"] is True
    assert result["hitl_resolved_info"] == {"answer": "ok", "task_name": "q01"}
    assert result["need_human_feedback"] is False


def test_early_exit_fallback_when_last_message_is_not_tool_message() -> None:
    """防御性兜底：hitl_auto_resolved=True 但末条不是 ToolMessage 时不应抛错，回退到原 state。"""

    state = {
        "messages": [
            HumanMessage(content="<user_query>原始问题</user_query>"),
            AIMessage(content="some prior ai message"),
        ],
        "hitl_auto_resolved": True,
        "hitl_resolved_info": {"answer": "ok"},
    }

    result = asyncio.run(HumanFeedbackNode()._aprocess(state))

    # 兜底：返回 state（含完整 messages），避免吞掉对话历史
    assert result is state
