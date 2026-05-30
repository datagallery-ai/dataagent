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
"""
Unit tests for IRMessageConsumer.

测试场景：
- render_ir_summary: 各 DataNode 类型的渲染
- build_messages(context=...): 分层替换、graceful fallback、边界条件
- assign_turn_indices / should_replace: 辅助逻辑
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.utils.converter.ir_message_consumer import (
    assign_turn_indices,
    render_ir_summary,
    should_replace,
    try_replace_with_ir,
)

# ── Fake DataNode classes (avoid importing context_trajectory) ───


@dataclass
class _FakeBaseIR:
    label: str
    description: str | None
    session_id: str = ""
    run_id: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _FakeDataNode(_FakeBaseIR):
    def get_schema(self) -> dict[str, Any]:
        return {"label": self.label, "description": self.description}


@dataclass
class _FakeTableNode(_FakeDataNode):
    path: str = ""

    def __post_init__(self):
        self.__class__.__name__ = "TableNode"


@dataclass
class _FakeKnowledgeNode(_FakeDataNode):
    knowledge_type: str = "tool_output"
    knowledge_content: str = ""

    def __post_init__(self):
        self.__class__.__name__ = "KnowledgeNode"


@dataclass
class _FakeScriptNode(_FakeDataNode):
    script_content: str = ""
    script_type: str = "sql"
    path: str | None = None
    related_data_list: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.__class__.__name__ = "ScriptNode"


@dataclass
class _FakeFileNode(_FakeDataNode):
    path: str = ""
    source: str = ""

    def __post_init__(self):
        self.__class__.__name__ = "FileNode"


def _patch_isinstance():
    """Monkey-patch isinstance checks for fake nodes by using real IR class registries."""
    import dataagent.utils.converter.ir_message_consumer as mod
    from dataagent.core.context.contextIR import (
        ColumnNode,
        FileNode,
        KnowledgeNode,
        ScriptNode,
        SkillNode,
        TableNode,
        ToolNode,
    )

    # We need the isinstance checks in _render_single_node to work with our fakes.
    # Instead, we'll test via render_ir_summary which calls _render_single_node,
    # and rely on the __class__.__name__ matching for the type dispatch.
    # But the actual code uses isinstance(), so let's just test with the real classes
    # when possible, or test the summary output format.


def _build_messages_with_ir(messages, context, recent_turns=2):
    """Test helper that mirrors build_messages(context=...) without importing messages_utils."""
    if not messages:
        return []
    turn_indices = assign_turn_indices(messages)
    max_turn = max(turn_indices) if turn_indices else 0
    result = []
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            result.append(msg)
        elif isinstance(msg, ToolMessage):
            if should_replace(turn_indices[i], max_turn, recent_turns):
                msg = try_replace_with_ir(msg, context)
            result.append(msg)
        else:
            result.append(msg)
    return result


def _make_mock_context(ir_map: dict[str, list] | None = None) -> MagicMock:
    """Create a mock Context that returns IR nodes for given action labels.

    Args:
        ir_map: mapping from action_label (e.g. "Action(tc1)") to list of DataNode objects.
                If action_label not in map, get_next_data_node raises ValueError.
    """
    if ir_map is None:
        ir_map = {}

    ctx = MagicMock()

    def _get_next_data_node(action_label: str):
        if action_label in ir_map:
            return ir_map[action_label]
        raise ValueError(f"Action node '{action_label}' not found")

    ctx.get_next_data_node = MagicMock(side_effect=_get_next_data_node)
    return ctx


# ── Test assign_turn_indices ────────────────────────────────────


class TestAssignTurnIndices:
    def test_empty(self):
        assert assign_turn_indices([]) == []

    def test_single_ai_message(self):
        messages = [AIMessage(content="hello")]
        assert assign_turn_indices(messages) == [0]

    def test_ai_tool_pattern(self):
        messages = [
            AIMessage(content="plan", tool_calls=[{"id": "tc1", "name": "tool1", "args": {}}]),
            ToolMessage(content="result1", tool_call_id="tc1"),
            AIMessage(content="plan2", tool_calls=[{"id": "tc2", "name": "tool2", "args": {}}]),
            ToolMessage(content="result2", tool_call_id="tc2"),
            AIMessage(content="plan3", tool_calls=[{"id": "tc3", "name": "tool3", "args": {}}]),
            ToolMessage(content="result3", tool_call_id="tc3"),
        ]
        assert assign_turn_indices(messages) == [0, 0, 1, 1, 2, 2]

    def test_tool_before_ai(self):
        """AIMessage 之前的消息都属于 turn 0。"""
        messages = [
            HumanMessage(content="hi"),
            ToolMessage(content="orphan", tool_call_id="tc0"),
            AIMessage(content="first"),
        ]
        assert assign_turn_indices(messages) == [0, 0, 0]

    def test_multiple_tools_per_turn(self):
        messages = [
            AIMessage(
                content="parallel",
                tool_calls=[
                    {"id": "tc1", "name": "t1", "args": {}},
                    {"id": "tc2", "name": "t2", "args": {}},
                ],
            ),
            ToolMessage(content="r1", tool_call_id="tc1"),
            ToolMessage(content="r2", tool_call_id="tc2"),
            AIMessage(content="next"),
        ]
        assert assign_turn_indices(messages) == [0, 0, 0, 1]


# ── Test should_replace ─────────────────────────────────────────


class TestShouldReplace:
    def test_recent_not_replaced(self):
        assert not should_replace(turn_index=3, max_turn=4, recent_turns=2)
        assert not should_replace(turn_index=4, max_turn=4, recent_turns=2)

    def test_old_replaced(self):
        assert should_replace(turn_index=0, max_turn=4, recent_turns=2)
        assert should_replace(turn_index=2, max_turn=4, recent_turns=2)

    def test_boundary(self):
        assert should_replace(turn_index=2, max_turn=4, recent_turns=2)
        assert not should_replace(turn_index=3, max_turn=4, recent_turns=2)

    def test_zero_recent_turns(self):
        assert should_replace(turn_index=4, max_turn=4, recent_turns=0)

    def test_large_recent_turns(self):
        assert not should_replace(turn_index=0, max_turn=4, recent_turns=100)


# ── Test render_ir_summary (with real IR node classes) ───────────


class TestRenderIRSummary:
    @pytest.fixture(autouse=True)
    def _import_ir_classes(self):
        from dataagent.core.context.contextIR import (
            FileNode,
            KnowledgeNode,
            ScriptNode,
            TableNode,
        )

        self.TableNode = TableNode
        self.KnowledgeNode = KnowledgeNode
        self.ScriptNode = ScriptNode
        self.FileNode = FileNode

    def test_empty_nodes(self):
        result = render_ir_summary([], "my_tool")
        assert "[IR Summary]" in result
        assert "my_tool" in result
        assert "(no artifacts)" in result

    def test_table_node(self):
        node = self.TableNode(
            label="table00001",
            description="订单表",
            session_id="s",
            run_id=0,
            path="/tmp/orders.csv",
        )
        result = render_ir_summary([node], "query_tool")
        assert "Table(table00001)" in result
        assert "订单表" in result
        assert "/tmp/orders.csv" in result

    def test_file_node(self):
        node = self.FileNode(
            label="file00001",
            description="报告文件",
            session_id="s",
            run_id=0,
            path="/tmp/report.pdf",
            source="report_tool",
        )
        result = render_ir_summary([node], "report_tool")
        assert "File(file00001)" in result
        assert "/tmp/report.pdf" in result

    def test_multiple_nodes(self):
        nodes = [
            self.TableNode(label="t1", description="表1", session_id="s", run_id=0, path="/a.csv"),
            self.ScriptNode(
                label="s1",
                description="脚本1",
                session_id="s",
                run_id=0,
                script_content="SELECT 1",
                script_type="sql",
                path=None,
                related_data_list=[],
            ),
        ]
        result = render_ir_summary(nodes, "multi_tool")
        assert "Table(t1)" in result
        assert "Script(s1)" in result
        assert "(no artifacts)" not in result


# ── Test build_messages with IR context ──────────────────────────


class TestBuildIRAwareMessages:
    @pytest.fixture(autouse=True)
    def _import_ir_classes(self):
        from dataagent.core.context.contextIR import KnowledgeNode, TableNode

        self.TableNode = TableNode
        self.KnowledgeNode = KnowledgeNode

    def test_empty_messages(self):
        ctx = _make_mock_context()
        assert _build_messages_with_ir([], ctx) == []

    def test_no_tool_messages(self):
        ctx = _make_mock_context()
        messages = [AIMessage(content="hello"), AIMessage(content="world")]
        result = _build_messages_with_ir(messages, ctx)
        assert len(result) == 2
        assert result[0].content == "hello"
        assert result[1].content == "world"

    def test_recent_turns_kept_full(self):
        """最近 2 轮的 ToolMessage 保留完整内容。"""
        knowledge = self.KnowledgeNode(
            label="k1",
            description="知识",
            session_id="s",
            run_id=0,
            knowledge_type="tool_output",
            knowledge_content="full knowledge content",
        )
        ctx = _make_mock_context({"Action(tc1)": [knowledge], "Action(tc2)": []})

        messages = [
            AIMessage(content="turn1", tool_calls=[{"id": "tc1", "name": "t1", "args": {}}]),
            ToolMessage(content="full result from tool1", tool_call_id="tc1", name="t1"),
            AIMessage(content="turn2", tool_calls=[{"id": "tc2", "name": "t2", "args": {}}]),
            ToolMessage(content="full result from tool2", tool_call_id="tc2", name="t2"),
        ]
        result = _build_messages_with_ir(messages, ctx, recent_turns=2)
        assert len(result) == 4
        assert "full result from tool1" in result[1].content
        assert "full result from tool2" in result[3].content

    def test_old_turns_replaced_with_ir(self):
        """超出 recent_turns 范围的 ToolMessage 被 IR 摘要替换。"""
        knowledge = self.KnowledgeNode(
            label="k_old",
            description="旧轮次的分析结果",
            session_id="s",
            run_id=0,
            knowledge_type="tool_output",
            knowledge_content="detailed analysis...",
        )
        ctx = _make_mock_context({"Action(tc_old)": [knowledge]})

        messages = [
            AIMessage(content="old_turn", tool_calls=[{"id": "tc_old", "name": "old_tool", "args": {}}]),
            ToolMessage(content="very long original output " * 100, tool_call_id="tc_old", name="old_tool"),
            AIMessage(content="new_turn", tool_calls=[{"id": "tc_new", "name": "new_tool", "args": {}}]),
            ToolMessage(content="new result", tool_call_id="tc_new", name="new_tool"),
        ]
        result = _build_messages_with_ir(messages, ctx, recent_turns=1)
        assert "[IR Summary]" in result[1].content
        assert "Knowledge(" in result[1].content
        assert "new result" in result[3].content

    def test_graceful_fallback_no_ir(self):
        """Action 节点存在但无下游 IR 节点时，保留原始内容。"""
        ctx = _make_mock_context({"Action(tc_no_ir)": []})

        messages = [
            AIMessage(content="old_turn", tool_calls=[{"id": "tc_no_ir", "name": "tool1", "args": {}}]),
            ToolMessage(content="original output", tool_call_id="tc_no_ir", name="tool1"),
            AIMessage(content="new_turn", tool_calls=[{"id": "tc_new", "name": "tool2", "args": {}}]),
            ToolMessage(content="new result", tool_call_id="tc_new", name="tool2"),
        ]
        result = _build_messages_with_ir(messages, ctx, recent_turns=1)
        assert "original output" in result[1].content

    def test_graceful_fallback_no_action_node(self):
        """Action 节点不在 trajectory 中时，保留原始内容。"""
        ctx = _make_mock_context({})

        messages = [
            AIMessage(content="old", tool_calls=[{"id": "nonexistent_id", "name": "t", "args": {}}]),
            ToolMessage(content="keep me", tool_call_id="nonexistent_id", name="t"),
            AIMessage(content="new", tool_calls=[{"id": "tc_new", "name": "t2", "args": {}}]),
            ToolMessage(content="new result", tool_call_id="tc_new", name="t2"),
        ]
        result = _build_messages_with_ir(messages, ctx, recent_turns=1)
        assert "keep me" in result[1].content

    def test_three_turns_recent_two(self):
        """3 轮中保留最近 2 轮，第 1 轮被替换。"""
        table = self.TableNode(
            label="t1",
            description="表1",
            session_id="s",
            run_id=0,
            path="/data/t1.csv",
        )
        ctx = _make_mock_context({"Action(tc_t1)": [table]})

        messages = [
            AIMessage(content="turn1", tool_calls=[{"id": "tc_t1", "name": "t1", "args": {}}]),
            ToolMessage(content="long output " * 50, tool_call_id="tc_t1", name="t1"),
            AIMessage(content="turn2", tool_calls=[{"id": "tc_t2", "name": "t2", "args": {}}]),
            ToolMessage(content="result2", tool_call_id="tc_t2", name="t2"),
            AIMessage(content="turn3", tool_calls=[{"id": "tc_t3", "name": "t3", "args": {}}]),
            ToolMessage(content="result3", tool_call_id="tc_t3", name="t3"),
        ]
        result = _build_messages_with_ir(messages, ctx, recent_turns=2)
        assert "[IR Summary]" in result[1].content
        assert "Table(t1)" in result[1].content
        assert "result2" in result[3].content
        assert "result3" in result[5].content

    def test_human_messages_pass_through(self):
        """HumanMessage 应原样通过。"""
        ctx = _make_mock_context({})
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content="response"),
        ]
        result = _build_messages_with_ir(messages, ctx)
        assert len(result) == 2
        assert isinstance(result[0], HumanMessage)
        assert result[0].content == "hello"
