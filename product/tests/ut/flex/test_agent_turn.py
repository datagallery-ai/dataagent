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
"""Unit tests for session_history_restore: full-history restore without folding."""

from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.flex.hooks.agent_turn import is_subagent, session_history_restore


def _make_messages(n_turns: int) -> list:
    """Build interleaved [AI(tool_calls), TOOL] message pairs."""
    msgs = []
    for i in range(n_turns):
        msgs.append(AIMessage(content=f"plan{i}", tool_calls=[{"id": f"tc{i}", "name": "t", "args": {}}]))
        msgs.append(ToolMessage(content=f"result{i}", tool_call_id=f"tc{i}", name="t"))
    return msgs


class TestSessionHistoryRestore:
    """session_history_restore 直接加载全部历史，不再折叠/压缩。"""

    def test_restores_full_history_ignoring_max_history_messages(self):
        """max_history_messages 不再生效，全部消息原样恢复。"""
        messages = _make_messages(8)  # 16 messages

        with patch("dataagent.core.flex.hooks.history_writer.load_messages", return_value=messages):
            state = {
                "user_id": "u1",
                "session_id": "s1",
                "max_history_messages": 4,  # 已废弃，应被忽略
            }
            result = session_history_restore(state, runtime=None)

        assert result["messages"] is messages
        assert len(result["messages"]) == 16

    def test_restores_full_history_without_max_history_messages(self):
        """未设置 max_history_messages 时也原样恢复全部消息。"""
        messages = _make_messages(4)

        with patch("dataagent.core.flex.hooks.history_writer.load_messages", return_value=messages):
            state = {"user_id": "u1", "session_id": "s1"}
            result = session_history_restore(state, runtime=None)

        assert result["messages"] is messages

    def test_skips_when_messages_already_present(self):
        """state 已有 messages 时不恢复，避免覆盖。"""
        existing = [HumanMessage(content="hi")]
        state = {"user_id": "u1", "session_id": "s1", "messages": existing}

        with patch("dataagent.core.flex.hooks.history_writer.load_messages", return_value=_make_messages(4)):
            result = session_history_restore(state, runtime=None)

        assert result["messages"] is existing

    def test_skips_for_subagent(self):
        """subagent 不做历史恢复。"""
        state = {"user_id": "u1", "session_id": "s1", "sub_id": 1}
        assert is_subagent(state)

        with patch("dataagent.core.flex.hooks.history_writer.load_messages", return_value=_make_messages(4)):
            result = session_history_restore(state, runtime=None)

        assert "messages" not in result

    def test_skips_when_missing_user_or_session(self):
        """缺少 user_id / session_id 时跳过恢复。"""
        with patch("dataagent.core.flex.hooks.history_writer.load_messages", return_value=_make_messages(4)):
            assert "messages" not in session_history_restore({"session_id": "s1"}, runtime=None)
            assert "messages" not in session_history_restore({"user_id": "u1"}, runtime=None)

    def test_swallows_load_errors(self):
        """load_messages 抛异常时不传播，仅 debug 日志。"""
        with patch("dataagent.core.flex.hooks.history_writer.load_messages", side_effect=RuntimeError("boom")):
            result = session_history_restore({"user_id": "u1", "session_id": "s1"}, runtime=None)
        assert "messages" not in result
