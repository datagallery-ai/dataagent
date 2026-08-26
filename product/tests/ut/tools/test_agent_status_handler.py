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
"""Unit tests for agent_status_handler — NL2SQL & ReAct subagent status extraction."""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.local_tool.agent_status_handler import (
    _NL2SQLStatusHandler,
    _ReActStatusHandler,
    _subagent_handlers,
    _subagent_last_status,
    extract_subagent_status,
    reset_subagent_status,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_collector() -> tuple[list[tuple[str, str]], Any]:
    events: list[tuple[str, str]] = []

    def cb(tool_call_id: str, text: str) -> None:
        events.append((tool_call_id, text))

    return events, cb


def _reset_all() -> None:
    for h in _subagent_handlers:
        h.reset_all()
    _subagent_last_status.clear()


# ====================================================================
# _ReActStatusHandler
# ====================================================================


class TestReActStatusHandler:
    def setup_method(self):
        self.handler = _ReActStatusHandler()

    def test_tool_start(self):
        """▶ 调用工具 / 调用工具 → '正在调用工具'"""
        assert self.handler.try_extract("▶ 调用工具", "tid") == "正在调用工具"
        assert self.handler.try_extract(" 调用工具", "tid") == "正在调用工具"

    def test_tool_done(self):
        """✅ xxx 完成 → '{tool_name} 完成'"""
        assert self.handler.try_extract("✅ calculator_sub_agent_tool 完成", "tid") == "calculator_sub_agent_tool 完成"
        assert self.handler.try_extract("✅ bash 完成", "tid") == "bash 完成"

    def test_tool_fail(self):
        """❌ 执行失败 / ❌ xxx 工具执行失败"""
        assert self.handler.try_extract("❌ 执行失败", "tid") == "执行失败"
        assert self.handler.try_extract("❌ my_tool 工具执行失败", "tid") == "my_tool 执行失败"

    def test_thinking_done(self):
        assert self.handler.try_extract("思考完毕", "tid") == "思考完毕"

    def test_unmatched_line(self):
        """不匹配任何模式的行返回 None"""
        assert self.handler.try_extract("随便输出一行", "tid") is None
        assert self.handler.try_extract("", "tid") is None


# ====================================================================
# _NL2SQLStatusHandler
# ====================================================================


class TestNL2SQLStatusHandler:
    def setup_method(self):
        self.handler = _NL2SQLStatusHandler()
        self.handler.reset_all()

    def test_node_header(self):
        """=== NodeName === 被识别并激活 node 上下文"""
        text = self.handler.try_extract("=== Coordinator ===", "tid")
        assert text == "Coordinator"
        assert self.handler._node_context.get("tid") == "Coordinator"

    def test_unknown_node_header_is_ignored(self):
        """不在白名单的节点名不会激活 NL2SQL 状态机"""
        assert self.handler.try_extract("=== UnknownNode ===", "tid") is None
        assert "tid" not in self.handler._node_context

    def test_content_line_under_node(self):
        """激活 node 后，有意义的内容行被提取"""
        self.handler.try_extract("=== Generator ===", "tid")
        text = self.handler.try_extract("SELECT COUNT(*) FROM orders", "tid")
        assert text == "Generator: SELECT COUNT(*) FROM orders"
        assert self.handler._node_line_count["tid"] == 1

    def test_meaningful_line_regex(self):
        """白名单内的模式被推送"""
        for line, label in [
            ("Score: 0.85, Issues: []", "Score"),
            ('{"sql": "SELECT 1"}', "JSON dict"),
            ('"final_answer": "ok"', "key-value"),
            ("schema: table(col1, col2)", "schema"),
            ("joins: [(a.id, b.id)]", "joins"),
        ]:
            self.handler.reset_all()  # 避免 _MAX_CONTENT_LINES 干扰
            self.handler.try_extract("=== Validator ===", "tid")
            assert self.handler.try_extract(line, "tid") is not None, f"expected match for {label}: {line!r}"

    def test_noise_line_under_node_is_filtered(self):
        """Traceback / File 等噪音行被静默丢弃（不计数，不推送）"""
        self.handler.try_extract("=== Generator ===", "tid")
        assert self.handler.try_extract("Traceback (most recent call last):", "tid") is None
        assert self.handler.try_extract('  File "/path/to/code.py", line 42', "tid") is None
        # node_line_count 在 === Generator === 时初始化为 0，噪音行不应递增
        assert self.handler._node_line_count["tid"] == 0

    def test_more_rows_summary(self):
        """... and N more rows 被正确提取（不计入内容行上限）"""
        self.handler.try_extract("=== Executor ===", "tid")
        # 第 1 条内容
        assert self.handler.try_extract("[(1, 'a')]", "tid") is not None
        assert self.handler._node_line_count["tid"] == 1
        # N more rows — 不应计数
        text = self.handler.try_extract("... and 96 more rows", "tid")
        assert text == "Executor: ... and 96 more rows"
        assert self.handler._node_line_count["tid"] == 1  # 未增加

    def test_content_line_limit(self):
        """每个 node 最多推送 _MAX_CONTENT_LINES 条内容行"""
        self.handler.try_extract("=== Generator ===", "tid")
        for i in range(5):
            self.handler.try_extract(f"SELECT {i}", "tid")
        # 第 0,1,2 条 → 有值；第 3,4 条 → None（超限）
        # 实际上 _node_line_count 从 0 开始，第 3 次调用时 count=3 >= 3 → 截断
        assert self.handler._node_line_count["tid"] == 3  # 只计了 3 条

    def test_reset_clears_per_tid_state(self):
        """reset 清除指定 tool_call_id 的上下文"""
        self.handler.try_extract("=== Generator ===", "tid_a")
        self.handler.try_extract("=== Coordinator ===", "tid_b")
        self.handler.reset("tid_a")
        assert "tid_a" not in self.handler._node_context
        assert "tid_b" in self.handler._node_context  # 不受影响

    def test_reset_all_clears_all(self):
        self.handler.try_extract("=== Generator ===", "tid_a")
        self.handler.try_extract("=== Coordinator ===", "tid_b")
        self.handler.reset_all()
        assert self.handler._node_context == {}
        assert self.handler._node_line_count == {}

    def test_final_result_more_rows(self):
        """Final Result 后的 ... and N more rows 正确加上前缀"""
        self.handler.try_extract("=== Final Result ===", "tid")
        text = self.handler.try_extract("... and 364 more rows", "tid")
        assert text == "Final Result: ... and 364 more rows"


# ====================================================================
# _extract_subagent_status — 集成派发 + 去重
# ====================================================================


class TestExtractSubagentStatus:
    def setup_method(self):
        _reset_all()

    def test_dispatch_nl2sql_header(self):
        """=== Coordinator === 被 NL2SQL handler 捕获"""
        events, cb = _make_collector()
        extract_subagent_status("=== Coordinator ===", "t1", cb)
        assert events == [("t1", "Coordinator")]

    def test_dispatch_react_tool_start(self):
        """▶ 调用工具 被 ReAct handler 捕获"""
        events, cb = _make_collector()
        extract_subagent_status("▶ 调用工具", "t1", cb)
        assert events == [("t1", "正在调用工具")]

    def test_nl2sql_precedence_over_react(self):
        """NL2SQL handler 先于 ReAct handler 匹配"""
        events, cb = _make_collector()
        extract_subagent_status("=== Generator ===", "t1", cb)
        assert events[0][1] == "Generator"
        # 后续内容行也应该由 NL2SQL handler 消费
        extract_subagent_status("SELECT 1", "t1", cb)
        assert events[1][1] == "Generator: SELECT 1"

    def test_empty_line(self):
        events, cb = _make_collector()
        extract_subagent_status("", "t1", cb)
        assert events == []

    def test_long_line_filtered(self):
        events, cb = _make_collector()
        extract_subagent_status("x" * 201, "t1", cb)
        assert events == []

    def test_none_callback_or_tool_call_id(self):
        """progress_callback 或 tool_call_id 为 None/空 时直接返回"""
        events, cb = _make_collector()
        extract_subagent_status("=== Coordinator ===", "t1", None)  # callback is None
        assert events == []
        extract_subagent_status("=== Coordinator ===", "", cb)  # 空 tool_call_id
        assert events == []

    def test_dedup(self):
        """相同状态文本不重复推送"""
        events, cb = _make_collector()
        extract_subagent_status("=== Generator ===", "t1", cb)
        extract_subagent_status("=== Generator ===", "t1", cb)  # 重复
        assert len(events) == 1

    def test_dedup_per_tool_call_id(self):
        """不同 tool_call_id 不互相去重"""
        events, cb = _make_collector()
        extract_subagent_status("=== Generator ===", "t1", cb)
        extract_subagent_status("=== Generator ===", "t2", cb)
        assert len(events) == 2

    def test_unmatched_line_dropped(self):
        events, cb = _make_collector()
        extract_subagent_status("这是什么奇怪输出", "t1", cb)
        assert events == []

    def test_nl2sql_content_under_unknown_node_is_dropped(self):
        """没有激活 node 上下文时，有意义的行也不应该被 NL2SQL 消费；日志行由通用 fallback 捕获"""
        events, cb = _make_collector()
        extract_subagent_status("SELECT 1", "t1", cb)
        # SELECT 1 不该匹配任何 handler
        assert events == []
        extract_subagent_status("loguru WARNING 测试", "t1", cb)
        assert len(events) == 1
        assert events[0][1] == "[WARNING] 测试"

    def test_finally_reset(self):
        """reset 清理后，同名 tool_call_id 的旧上下文不残留"""
        events, cb = _make_collector()
        extract_subagent_status("=== Coordinator ===", "t1", cb)

        # 模拟 finally 中的清理
        _subagent_last_status.pop("t1", None)
        for h in _subagent_handlers:
            h.reset("t1")

        # 新调用不应受旧 context 影响
        extract_subagent_status("SELECT 1", "t1", cb)
        # 没有 node 上下文 → SELECT 1 不被 NL2SQL 消费
        assert len(events) == 1  # 只有之前的 Coordinator

    def test_log_level_fallback(self):
        """通用 loguru 日志级别 fallback（所有 agent 类型共享）"""
        events, cb = _make_collector()
        extract_subagent_status("loguru WARNING 连接池已满", "t1", cb)
        assert events == [("t1", "[WARNING] 连接池已满")]

    def test_log_level_fallback_multiple(self):
        """WARNING / ERROR / CRITICAL / TRACE 均被 fallback 匹配"""
        events, cb = _make_collector()
        extract_subagent_status("loguru ERROR 数据库连接失败: timeout", "t1", cb)
        extract_subagent_status("loguru CRITICAL 系统异常", "t1", cb)
        extract_subagent_status("loguru TRACE 详细跟踪", "t1", cb)
        assert events == [
            ("t1", "[ERROR] 数据库连接失败: timeout"),
            ("t1", "[CRITICAL] 系统异常"),
            ("t1", "[TRACE] 详细跟踪"),
        ]


# ====================================================================
# 注册表 —— 确认 handler 顺序
# ====================================================================


class TestHandlerRegistry:
    def test_nl2sql_handler_before_react(self):
        """NL2SQL handler 排在 ReAct 之前，确保更具体的匹配优先"""
        handler_types = [type(h).__name__ for h in _subagent_handlers]
        assert handler_types == ["_NL2SQLStatusHandler", "_ReActStatusHandler"]

    def test_handlers_are_singletons(self):
        """_subagent_handlers 列表不可为空"""
        assert len(_subagent_handlers) >= 1
        for h in _subagent_handlers:
            assert hasattr(h, "try_extract")
            assert hasattr(h, "reset")
