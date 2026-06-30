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
"""Tests for context_reference_rewriter agent pre-hook."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from loguru import logger

from dataagent.core.context.context import Context, ContextFactory
from dataagent.core.context.context_ir import QueryNode
from dataagent.core.flex.hooks.context_reference_rewriter import (
    DEFAULT_MAX_CANDIDATES,
    _build_analyze_prompt,
    _build_llm_prompt,
    _collect_candidates,
    _ensure_raw_user_query,
    _merge_analysis_filters,
    _parse_query_analysis,
    _validate_rewrite_plan,
    context_reference_rewriter,
)
from dataagent.core.flex.utils.planner_prompt_builder import _build_planner_system_and_user_messages
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG_PATH = dataagent_package_path("core", "flex", "flex_default_configs.yaml")


def _llm_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _default_analyze_json(
    *,
    needs_rewrite: bool = True,
    mentions: list[dict[str, Any]] | None = None,
    skip_reason: str = "",
) -> str:
    """构造 Stage A 默认 analyze LLM 响应。"""
    if not needs_rewrite:
        return _llm_json(
            {
                "needs_rewrite": False,
                "mentions": [],
                "skip_reason": skip_reason or "无指代",
            }
        )
    if mentions is None:
        mentions = [{"text": "刚才那个表", "target_types": ["Table"], "temporal_hint": "recent"}]
    return _llm_json(
        {
            "needs_rewrite": True,
            "mentions": mentions,
            "skip_reason": skip_reason,
        }
    )


def _make_runtime(
    llm_response: str | None = None,
    llm_side_effect: Exception | None = None,
    analyze_response: str | None = None,
    llm_invoke_side_effect: list[Any] | None = None,
) -> MagicMock:
    """构造带 mock LLM 的 runtime。

    默认在 Stage C 改写前注入 Stage A analyze 响应；仅传 ``analyze_response`` 时只 mock 一次调用。
    """
    runtime = MagicMock()
    if llm_invoke_side_effect is not None:
        llm = MagicMock()
        llm.invoke.side_effect = llm_invoke_side_effect
        runtime.llm.return_value = llm
        return runtime

    if llm_response is not None or llm_side_effect is not None or analyze_response is not None:
        llm = MagicMock()
        if llm_side_effect is not None:
            llm.invoke.side_effect = llm_side_effect
        elif llm_response is not None:
            responses = [
                MagicMock(content=analyze_response or _default_analyze_json()),
                MagicMock(content=llm_response),
            ]
            llm.invoke.side_effect = responses
        else:
            llm.invoke.return_value = MagicMock(content=analyze_response)
        runtime.llm.return_value = llm
    return runtime


@contextmanager
def _capture_loguru_in_caplog(caplog: pytest.LogCaptureFixture, level: int = logging.DEBUG):
    """将 loguru 日志桥接到 pytest caplog，便于断言日志内容。"""
    bridge_logger = logging.getLogger("context_reference_rewriter")

    def _emit(message: Any) -> None:
        record = message.record
        bridge_logger.log(record["level"].no, record["message"])

    handler_id = logger.add(_emit, format="{message}")
    with caplog.at_level(level, logger="context_reference_rewriter"):
        try:
            yield
        finally:
            logger.remove(handler_id)


def _setup_context_with_table(
    user_id: str = "ut_user",
    session_id: str = "ut_session",
    query: str = "分析销售数据",
) -> str:
    """注册 query 与 Table 节点，返回 Table node_id。"""
    ContextFactory.clear_context()
    context = ContextFactory.get_context(user_id=user_id, session_id=session_id, run_id=0, sub_id=0)
    context.register_query(query=query, additional_files=[])
    context.register_node(
        node_type="Action",
        description="聚合销售表",
        action="Tool(python_repl)",
        params={"code": "df.groupby"},
        output="ok",
        success=True,
        predecessor_node=["Query(query00000)"],
    )
    return context.register_node(
        node_type="Table",
        label="sales_agg",
        description="用户分组聚合后的销售数据",
        path="/workspace/result.csv",
        predecessor_node=["Action(action00000)"],
        edge_type="produces",
    )


def _setup_context_with_many_actions(
    user_id: str = "ut_many",
    session_id: str = "ut_many_session",
    action_count: int = 12,
) -> Context:
    """注册 query 与多条 Action，用于候选截断测试。"""
    ContextFactory.clear_context()
    context = ContextFactory.get_context(user_id=user_id, session_id=session_id, run_id=0, sub_id=0)
    context.register_query(query="历史轮次", additional_files=[])
    for index in range(action_count):
        label = f"act{str(index).zfill(5)}"
        context.register_node(
            node_type="Action",
            label=label,
            description=f"action {index}",
            action="Tool(python_repl)",
            params={"index": index},
            output=f"out-{index}",
            success=True,
            predecessor_node=["Query(query00000)"],
            add_pt=index > 0,
        )
    return context


def _setup_context_with_two_files(
    user_id: str = "ut_files",
    session_id: str = "ut_files_session",
) -> tuple[str, str]:
    """注册两个 File 节点，返回 (较早 file_id, 较晚 file_id)。"""
    ContextFactory.clear_context()
    context = ContextFactory.get_context(user_id=user_id, session_id=session_id, run_id=0, sub_id=0)
    context.register_query(query="生成文件", additional_files=[])
    file0 = context.register_node(
        node_type="File",
        label="file00000",
        description="第一个文件",
        path="/workspace/test1.txt",
        source="Tool(python_repl)",
        predecessor_node=["Query(query00000)"],
        edge_type="produces",
    )
    file1 = context.register_node(
        node_type="File",
        label="file00001",
        description="第二个文件",
        path="/workspace/test2.txt",
        source="Tool(python_repl)",
        predecessor_node=["Query(query00000)"],
        edge_type="produces",
        add_pt=True,
    )
    return file0, file1


class TestQueryNodeRawUserQuery:
    """register_query 应同时写入 query 与 raw_user_query。"""

    def test_register_query_sets_raw_user_query(self) -> None:
        ContextFactory.clear_context()
        context = ContextFactory.get_context(user_id="u1", session_id="s1", run_id=0, sub_id=0)
        context.register_query(query="原始问题", additional_files=[])
        ir = context.state.ir.get_IR(label="query00000", node_type="Query")
        assert isinstance(ir, QueryNode)
        assert ir.query == "原始问题"
        assert ir.raw_user_query == "原始问题"
        traj_node = context.state.trajectory.nodes["Query(query00000)"]
        assert traj_node["raw_user_query"] == "原始问题"


class TestContextReferenceRewriterHelpers:
    """辅助函数单测。"""

    def test_ensure_raw_user_query_setdefault(self) -> None:
        state: dict[str, Any] = {"user_query": "hello"}
        assert _ensure_raw_user_query(state) == "hello"
        assert state["raw_user_query"] == "hello"

    def test_ensure_raw_user_query_no_override(self) -> None:
        state: dict[str, Any] = {"user_query": "new", "raw_user_query": "old"}
        assert _ensure_raw_user_query(state) == "old"

    def test_collect_candidates_sorted_and_filtered(self) -> None:
        table_id = _setup_context_with_table()
        context = ContextFactory.get_context(user_id="ut_user", session_id="ut_session", run_id=0, sub_id=0)
        candidates = _collect_candidates(context, max_candidates=20)
        node_ids = [c["node_id"] for c in candidates]
        assert table_id in node_ids
        assert "Query(query00000)" not in node_ids
        table_entry = next(c for c in candidates if c["node_id"] == table_id)
        assert table_entry["path"] == "/workspace/result.csv"

    def test_collect_candidates_truncates_to_max(self) -> None:
        context = _setup_context_with_many_actions(action_count=12)
        limit = 5
        candidates = _collect_candidates(context, max_candidates=limit)
        assert len(candidates) == limit
        node_ids = [c["node_id"] for c in candidates]
        assert node_ids[0] == "Action(act00011)"
        assert node_ids[-1] == "Action(act00007)"

    def test_collect_candidates_filters_by_target_types(self) -> None:
        file0, file1 = _setup_context_with_two_files()
        context = ContextFactory.get_context(user_id="ut_files", session_id="ut_files_session", run_id=0, sub_id=0)
        candidates = _collect_candidates(
            context,
            max_candidates=20,
            target_types=frozenset({"File"}),
            temporal_hint="recent",
        )
        node_ids = [c["node_id"] for c in candidates]
        assert node_ids == [file1, file0]
        assert "Query(query00000)" not in node_ids

    def test_collect_candidates_earliest_orders_files_first(self) -> None:
        file0, file1 = _setup_context_with_two_files()
        context = ContextFactory.get_context(user_id="ut_files", session_id="ut_files_session", run_id=0, sub_id=0)
        candidates = _collect_candidates(
            context,
            max_candidates=20,
            target_types=frozenset({"File"}),
            temporal_hint="earliest",
        )
        node_ids = [c["node_id"] for c in candidates]
        assert node_ids == [file0, file1]

    def test_parse_query_analysis_and_merge_filters(self) -> None:
        parsed = {
            "needs_rewrite": True,
            "mentions": [
                {"text": "第一个文件", "target_types": ["File"], "temporal_hint": "earliest"},
            ],
            "skip_reason": "",
        }
        analysis = _parse_query_analysis(parsed)
        assert analysis is not None
        assert analysis.needs_rewrite is True
        target_types, temporal_hint = _merge_analysis_filters(analysis.mentions)
        assert target_types == frozenset({"File"})
        assert temporal_hint == "earliest"

    def test_build_analyze_prompt_contains_user_query(self) -> None:
        prompt = _build_analyze_prompt("第一个生成的文件里有什么？")
        assert "第一个生成的文件里有什么？" in prompt
        assert "needs_rewrite" in prompt

    def test_collect_candidates_default_max_matches_constant(self) -> None:
        context = _setup_context_with_many_actions(action_count=DEFAULT_MAX_CANDIDATES + 5)
        candidates = _collect_candidates(context, max_candidates=DEFAULT_MAX_CANDIDATES)
        assert len(candidates) == DEFAULT_MAX_CANDIDATES

    def test_build_llm_prompt_explains_ordinal_reference_order(self) -> None:
        candidates = [
            {"node_id": "File(file00000)", "node_type": "File", "path": "/workspace/test1.txt"},
            {"node_id": "File(file00001)", "node_type": "File", "path": "/workspace/test2.txt"},
        ]
        earliest_prompt = _build_llm_prompt(
            "第一个生成的文件里面有什么内容？",
            candidates,
            temporal_hint="earliest",
        )
        assert "候选数组已按时间由远到近排列" in earliest_prompt
        assert "不要误选数组末尾" in earliest_prompt
        assert "第一个生成" in earliest_prompt
        assert "最早" in earliest_prompt

        recent_prompt = _build_llm_prompt(
            "用刚才那个文件继续分析",
            list(reversed(candidates)),
            temporal_hint="recent",
        )
        assert "候选数组已按时间由近到远排列" in recent_prompt
        assert "不要误选数组第一个" in recent_prompt
        assert "刚才" in recent_prompt

    def test_validate_rewrite_plan_success(self) -> None:
        parsed = {
            "decision": "rewrite",
            "rewrite_query": "用表 Table(sales_agg)，路径 /workspace/result.csv 继续分析",
            "resolved_refs": [
                {
                    "mention": "刚才那个表",
                    "target_node": "Table(sales_agg)",
                    "reason": "最近产出的唯一 Table",
                }
            ],
            "skip_reason": "",
        }
        plan, skip = _validate_rewrite_plan(parsed, {"Table(sales_agg)"}, "用刚才那个表继续分析")
        assert skip == ""
        assert plan is not None
        assert plan.rewrite_query.startswith("用表 Table")

    def test_validate_rewrite_plan_target_not_in_candidates(self) -> None:
        parsed = {
            "decision": "rewrite",
            "rewrite_query": "改写",
            "resolved_refs": [
                {"mention": "表", "target_node": "Table(fake)", "reason": "x"},
            ],
        }
        plan, skip = _validate_rewrite_plan(parsed, {"Table(sales_agg)"}, "原问题")
        assert plan is None
        assert "target_not_in_candidates" in skip

    def test_validate_rewrite_plan_resolves_bare_label_target_node(self) -> None:
        parsed = {
            "decision": "rewrite",
            "rewrite_query": "File(file00000) 里有什么内容？",
            "resolved_refs": [
                {"mention": "第一个文件", "target_node": "file00000", "reason": "第一个 File"},
            ],
        }
        plan, skip = _validate_rewrite_plan(parsed, {"File(file00000)"}, "第一个生成的文件里有什么？")
        assert skip == ""
        assert plan is not None
        assert plan.target_nodes == ["File(file00000)"]
        assert plan.resolved_refs[0]["target_node"] == "File(file00000)"

    def test_validate_rewrite_plan_ambiguous_bare_label_target_node(self) -> None:
        parsed = {
            "decision": "rewrite",
            "rewrite_query": "查看 file00000",
            "resolved_refs": [
                {"mention": "那个", "target_node": "file00000", "reason": "r"},
            ],
        }
        plan, skip = _validate_rewrite_plan(
            parsed,
            {"File(file00000)", "Action(file00000)"},
            "那个文件里有什么？",
        )
        assert plan is None
        assert skip == "ambiguous_target_label:file00000"

    def test_validate_rewrite_plan_ambiguous_mention(self) -> None:
        parsed = {
            "decision": "rewrite",
            "rewrite_query": "改写后的问题",
            "resolved_refs": [
                {"mention": "那个表", "target_node": "Table(a)", "reason": "r1"},
                {"mention": "那个表", "target_node": "Table(b)", "reason": "r2"},
            ],
        }
        plan, skip = _validate_rewrite_plan(parsed, {"Table(a)", "Table(b)"}, "用那个表分析")
        assert plan is None
        assert skip == "ambiguous_mention:那个表"

    def test_validate_rewrite_plan_rewrite_same_as_raw(self) -> None:
        raw = "用刚才那个表继续分析"
        parsed = {
            "decision": "rewrite",
            "rewrite_query": raw,
            "resolved_refs": [
                {"mention": "刚才那个表", "target_node": "Table(sales_agg)", "reason": "r"},
            ],
        }
        plan, skip = _validate_rewrite_plan(parsed, {"Table(sales_agg)"}, raw)
        assert plan is None
        assert skip == "rewrite_same_as_raw"


class TestContextReferenceRewriterHook:
    """context_reference_rewriter 主入口。"""

    def teardown_method(self) -> None:
        ContextFactory.clear_context()

    def test_subagent_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        state: dict[str, Any] = {"user_query": "q", "sub_id": 1}
        runtime = _make_runtime()
        with _capture_loguru_in_caplog(caplog, level=logging.WARNING):
            out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == "q"
        runtime.llm.assert_not_called()
        assert "context_reference_rewriter" not in "\n".join(r.message for r in caplog.records)

    def test_empty_user_query_skips_without_llm(self) -> None:
        state: dict[str, Any] = {"user_query": "", "sub_id": 0}
        runtime = _make_runtime()
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == ""
        runtime.llm.assert_not_called()

    def test_whitespace_user_query_skips_without_llm(self) -> None:
        state: dict[str, Any] = {"user_query": "   ", "sub_id": 0}
        runtime = _make_runtime()
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == "   "
        runtime.llm.assert_not_called()

    def test_unexpected_error_skips_without_rewrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = "用刚才那个表继续分析"
        _setup_context_with_table(user_id="u_err", session_id="s_err", query=raw)

        def _raise_collect(_context: Any, _max_candidates: int) -> list[dict[str, Any]]:
            raise RuntimeError("collect boom")

        monkeypatch.setattr(
            "dataagent.core.flex.hooks.context_reference_rewriter._collect_candidates",
            _raise_collect,
        )
        state: dict[str, Any] = {
            "user_id": "u_err",
            "session_id": "s_err",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime()
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == raw
        llm = runtime.llm.return_value
        assert llm.invoke.call_count == 1

    def test_no_context_skips_without_rewrite(self) -> None:
        state: dict[str, Any] = {"user_query": "用刚才那个表", "run_id": 0, "sub_id": 0}
        runtime = _make_runtime(_llm_json({"decision": "skip", "skip_reason": "x"}))
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == "用刚才那个表"
        runtime.llm.assert_not_called()

    def test_no_candidates_skips_rewrite_llm(self) -> None:
        ContextFactory.clear_context()
        ContextFactory.get_context(user_id="u", session_id="s", run_id=0, sub_id=0)
        state: dict[str, Any] = {
            "user_id": "u",
            "session_id": "s",
            "run_id": 0,
            "sub_id": 0,
            "user_query": "用刚才那个表",
        }
        runtime = _make_runtime()
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == "用刚才那个表"
        llm = runtime.llm.return_value
        assert llm.invoke.call_count == 1

    def test_analyze_no_reference_skips_without_rewrite_llm(self) -> None:
        raw = "帮我生成 test2.txt，内容为 test2"
        _setup_context_with_table(user_id="u_no_ref", session_id="s_no_ref", query=raw)
        state: dict[str, Any] = {
            "user_id": "u_no_ref",
            "session_id": "s_no_ref",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime(analyze_response=_default_analyze_json(needs_rewrite=False))
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == raw
        llm = runtime.llm.return_value
        assert llm.invoke.call_count == 1

    def test_invalid_json_skips_rewrite(self) -> None:
        raw = "用刚才那个表继续分析"
        _setup_context_with_table(user_id="u_invalid", session_id="s_invalid", query=raw)
        state: dict[str, Any] = {
            "user_id": "u_invalid",
            "session_id": "s_invalid",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime("this is not json")
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == raw
        llm = runtime.llm.return_value
        assert llm.invoke.call_count == 2

    def test_llm_failed_skips_rewrite(self) -> None:
        raw = "用刚才那个表继续分析"
        _setup_context_with_table(user_id="u_fail", session_id="s_fail", query=raw)
        state: dict[str, Any] = {
            "user_id": "u_fail",
            "session_id": "s_fail",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime(
            llm_invoke_side_effect=[
                MagicMock(content=_default_analyze_json()),
                RuntimeError("llm boom"),
            ]
        )
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == raw

    def test_llm_skip_decision(self) -> None:
        raw = "用刚才那个表继续分析"
        _setup_context_with_table(user_id="u2", session_id="s2", query=raw)
        state: dict[str, Any] = {
            "user_id": "u2",
            "session_id": "s2",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime(
            _llm_json(
                {
                    "decision": "skip",
                    "rewrite_query": "",
                    "resolved_refs": [],
                    "skip_reason": "ambiguous",
                }
            )
        )
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == "用刚才那个表继续分析"
        assert out["raw_user_query"] == "用刚才那个表继续分析"

    def test_successful_rewrite_updates_state_and_query_node(self) -> None:
        raw = "用刚才那个表继续分析"
        table_id = _setup_context_with_table(user_id="u3", session_id="s3", query=raw)
        rewrite = f"用表 {table_id}，路径 /workspace/result.csv 继续分析"
        state: dict[str, Any] = {
            "user_id": "u3",
            "session_id": "s3",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime(
            _llm_json(
                {
                    "decision": "rewrite",
                    "rewrite_query": rewrite,
                    "resolved_refs": [
                        {
                            "mention": "刚才那个表",
                            "target_node": table_id,
                            "reason": "最近唯一 Table",
                        }
                    ],
                    "skip_reason": "",
                }
            )
        )
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == rewrite
        assert out["raw_user_query"] == raw

        context = ContextFactory.get_context(user_id="u3", session_id="s3", run_id=0, sub_id=0)
        ir = context.state.ir.get_IR(label="query00000", node_type="Query")
        assert isinstance(ir, QueryNode)
        assert ir.query == rewrite
        assert ir.raw_user_query == raw
        assert context.state.trajectory.nodes["Query(query00000)"]["query"] == rewrite
        assert context.state.trajectory.nodes["Query(query00000)"]["raw_user_query"] == raw

    def test_successful_rewrite_logs_raw_and_user_query(self, caplog: pytest.LogCaptureFixture) -> None:
        raw = "用刚才那个表继续分析"
        table_id = _setup_context_with_table(user_id="u_log", session_id="s_log", query=raw)
        rewrite = f"用表 {table_id}，路径 /workspace/result.csv 继续分析"
        state: dict[str, Any] = {
            "user_id": "u_log",
            "session_id": "s_log",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime(
            _llm_json(
                {
                    "decision": "rewrite",
                    "rewrite_query": rewrite,
                    "resolved_refs": [
                        {"mention": "刚才那个表", "target_node": table_id, "reason": "唯一 Table"},
                    ],
                    "skip_reason": "",
                }
            )
        )
        with _capture_loguru_in_caplog(caplog):
            context_reference_rewriter(state, runtime)

        log_text = caplog.text
        assert "query replaced" in log_text
        assert raw in log_text
        assert rewrite in log_text
        assert "decision=rewrite" in log_text

    def test_sync_failure_reverts_user_query(self) -> None:
        """modify_node 失败时不应保留改写后的 user_query。"""
        raw = "用刚才那个表"
        table_id = _setup_context_with_table(user_id="u4", session_id="s4", query=raw)
        context = ContextFactory.get_context(user_id="u4", session_id="s4", run_id=0, sub_id=0)
        context.state.initial_pt = "Invalid(label)"  # 触发 modify 失败

        rewrite = f"用表 {table_id} 继续"
        state: dict[str, Any] = {
            "user_id": "u4",
            "session_id": "s4",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
        }
        runtime = _make_runtime(
            _llm_json(
                {
                    "decision": "rewrite",
                    "rewrite_query": rewrite,
                    "resolved_refs": [
                        {"mention": "表", "target_node": table_id, "reason": "唯一 Table"},
                    ],
                    "skip_reason": "",
                }
            )
        )
        out = context_reference_rewriter(state, runtime)
        assert out["user_query"] == raw


class TestContextReferenceRewriterPlannerIntegration:
    """hook 改写后 Planner user prompt 应看到消解后的 query。"""

    def teardown_method(self) -> None:
        ContextFactory.clear_context()

    def test_planner_user_prompt_uses_rewritten_query_after_hook(self) -> None:
        raw = "用刚才那个表继续分析"
        table_id = _setup_context_with_table(user_id="u_int", session_id="s_int", query=raw)
        rewrite = f"用表 {table_id}，路径 /workspace/result.csv 继续分析"
        state: dict[str, Any] = {
            "user_id": "u_int",
            "session_id": "s_int",
            "run_id": 0,
            "sub_id": 0,
            "user_query": raw,
            "workspace": "/tmp",
        }
        runtime = _make_runtime(
            _llm_json(
                {
                    "decision": "rewrite",
                    "rewrite_query": rewrite,
                    "resolved_refs": [
                        {"mention": "刚才那个表", "target_node": table_id, "reason": "唯一 Table"},
                    ],
                    "skip_reason": "",
                }
            )
        )
        context_reference_rewriter(state, runtime)

        context = ContextFactory.get_context(user_id="u_int", session_id="s_int", run_id=0, sub_id=0)
        system_prompt = PromptTemplate.from_string("system")
        user_prompt = PromptTemplate.from_string("<user_query>{{ user_query }}</user_query>")
        _, user_message = _build_planner_system_and_user_messages(
            context,
            state,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            instruction="",
            agent_config={},
        )
        content = str(user_message.content or "")
        assert rewrite in content
        assert raw not in content
        assert state["raw_user_query"] == raw
        assert state["user_query"] == rewrite


@pytest.mark.parametrize(
    "hook_name",
    ["context_reference_rewriter"],
)
def test_builtin_hook_registry_resolves(hook_name: str) -> None:
    from dataagent.core.flex.hooks.registry import resolve_builtin_hook

    fn = resolve_builtin_hook(hook_name)
    assert callable(fn)


def test_default_yaml_registers_context_reference_rewriter_llm_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """default YAML 合并 MODEL 后应注册 context_reference_rewriter 的 llm_configs。"""
    from dataagent.core.flex.flex_runtime_from_config import build_llm_configs_from_flex_config

    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")

    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as file:
        default_config = yaml.safe_load(file)

    config = {
        "MODEL": {
            "chat_model": {
                "provider": "bailian",
                "model_type": "chat",
                "params": {"model": "deepseek-v4-flash"},
            }
        },
        **default_config,
    }
    llm_configs = build_llm_configs_from_flex_config(config)
    assert "context_reference_rewriter" in llm_configs
    assert llm_configs["context_reference_rewriter"]["api_base"] == "https://from-env/v1"
    assert llm_configs["context_reference_rewriter"]["model"] == "deepseek-v4-flash"

    hook_specs: list[str] = []
    agent_pre = config.get("HOOKS", {}).get("agent", {}).get("pre", [])
    for item in agent_pre:
        if isinstance(item, dict):
            hook_specs.append(str(item.get("name")))
    assert "context_reference_rewriter" in hook_specs
