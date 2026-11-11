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
"""de_agent suite hitl_auto_resolver hook 的场景单元测试（v3.3-v3.6 行为基线）。

通用 HITL 基座（调用检测/载荷提取/消息注入/门控）的测试在
``tests/ut/flex/hooks/test_hitl_common.py``；分类机制框架测试在
``test_hitl_classifier.py``。本文件只保留场景行为：golden 匹配、
action 分类口径、程序化答复、泄漏/假确认防线、分类器注册表接线。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
    _match_golden_sql_by_path,
    hitl_auto_resolver,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def task_index_file():
    """Create temp dir with JSON index and golden SQL files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        # Golden SQL files (放在 tmpdir 下)
        (p / "q01.sql").write_text("SELECT * FROM feature_q01", encoding="utf-8")
        (p / "q02.sql").write_text("SELECT * FROM feature_q02", encoding="utf-8")

        # JSON index file with golden_sql_path 使用相对路径（相对于 JSON 文件所在目录）
        index_data = [
            {
                "task_id": "0",
                "name": "q01",
                "query": "查询用户订单数，统计每个用户的订单总量",
                "golden_sql_path": "q01.sql",  # 相对路径
            },
            {
                "task_id": "1",
                "name": "q02",
                "query": "计算月销售额，汇总所有产品的月度收入",
                "golden_sql_path": "q02.sql",  # 相对路径
            },
        ]
        index_file = p / "task_index.json"
        index_file.write_text(__import__("json").dumps(index_data), encoding="utf-8")

        yield str(index_file)


class MockLLM:
    """Mock LLM that returns a fixed response."""

    def __init__(self, response: str = "这是自动回复内容"):
        self.response = response

    def invoke(self, messages: list) -> MagicMock:
        mock = MagicMock()
        mock.content = self.response
        return mock


class RuntimeStub:
    """Minimal runtime stub for testing."""

    def __init__(
        self,
        auto_resolve: bool = True,
        task_index_path: str | None = None,
        llm_response: str = "这是自动回复内容",
    ):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {
                "hitl_auto_resolve": auto_resolve,
                "hitl_task_index_path": task_index_path,
            }
        }
        self._llm_response = llm_response

    def get_all_config(self) -> dict[str, Any]:
        return self._config

    def llm(self, name: str = "planner") -> MockLLM:
        return MockLLM(self._llm_response)


# ─────────────────────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_state(
    messages: list,
    hitl_auto_resolve: bool = True,
    hitl_task_index_path: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": messages,
        "hitl_auto_resolve": hitl_auto_resolve,
        "hitl_task_index_path": hitl_task_index_path,
        "complete": False,
        "need_human_feedback": True,
    }
    state.update(extra)
    return state


def _make_hil_tool_call(
    call_id: str = "call_123", reason: str = "请确认", pending_action: str = "数据口径是否正确"
) -> dict:
    return {
        "id": call_id,
        "name": "request_human_feedback",
        "args": {"reason": reason, "pending_action": pending_action},
        "type": "tool_call",
    }


def _make_ai_message_with_hil(
    content: str = "完成", call_id: str = "call_123", *, pending_action: str = "数据口径是否正确"
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[_make_hil_tool_call(call_id=call_id, pending_action=pending_action)],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for helper functions
# ─────────────────────────────────────────────────────────────────────────────


class TestMatchGoldenSqlByPath:
    def test_matches_by_path(self, task_index_file):
        query = "查询用户订单数，统计每个用户的订单总量"
        result, matched_task = _match_golden_sql_by_path(query, task_index_file)
        assert result == "SELECT * FROM feature_q01"
        assert matched_task["name"] == "q01"

    def test_matches_similar_query(self, task_index_file):
        """Query shares keywords with task.query (after extraction)"""
        # 模拟从 <user_query>"query": "...",</user_query> 提取后的纯 query
        query = "用户订单数统计"
        result, matched_task = _match_golden_sql_by_path(query, task_index_file)
        assert result == "SELECT * FROM feature_q01"
        assert matched_task["name"] == "q01"

    def test_returns_none_when_no_match(self, task_index_file):
        result, matched_task = _match_golden_sql_by_path("完全不相关的查询内容 xyz123", task_index_file)
        assert result is None
        assert matched_task is None

    def test_returns_none_for_invalid_index_path(self):
        result, matched_task = _match_golden_sql_by_path("查询", "/nonexistent/index.json")
        assert result is None
        assert matched_task is None

    def test_relative_path_resolved_from_json_dir(self):
        """Test that relative golden_sql_path is resolved from JSON file's directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            (p / "golden").mkdir()
            (p / "golden" / "q01.sql").write_text("SELECT * FROM relative_path_test", encoding="utf-8")

            index_data = [
                {
                    "task_id": "0",
                    "name": "q01",
                    "query": "相对路径测试",
                    "golden_sql_path": "golden/q01.sql",  # 相对路径子目录
                },
            ]
            index_file = p / "task_index.json"
            index_file.write_text(__import__("json").dumps(index_data), encoding="utf-8")

            result, matched_task = _match_golden_sql_by_path("相对路径测试", str(index_file))
            assert result == "SELECT * FROM relative_path_test"
            assert matched_task["name"] == "q01"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for main hook function
# ─────────────────────────────────────────────────────────────────────────────


class TestHitlAutoResolver:
    def test_skips_when_disabled(self, task_index_file):
        state = _make_state(
            messages=[HumanMessage(content="查询用户订单数"), _make_ai_message_with_hil()],
            hitl_auto_resolve=False,
            hitl_task_index_path=task_index_file,
        )
        runtime = RuntimeStub(auto_resolve=False, task_index_path=task_index_file)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))
        assert result is state

    def test_skips_when_no_hil_call(self, task_index_file):
        state = _make_state(
            messages=[HumanMessage(content="查询用户订单数"), AIMessage(content="完成")],
            hitl_task_index_path=task_index_file,
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        assert result is state

    def test_fallback_reply_when_no_golden_match(self, task_index_file):
        """golden 匹配失败时注入兜底回复（不再原样透传 state）。

        提问需可分类（TABLE_CONFIRM）以越过 UNA 拒绝、走到 golden 匹配一步。
        """
        state = _make_state(
            messages=[
                HumanMessage(content="完全不相关的查询"),
                _make_ai_message_with_hil(pending_action="数据源选择需要您确认"),
            ],
            hitl_task_index_path=task_index_file,
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert isinstance(result["messages"][-1], ToolMessage)
        assert "自主判断" in result["messages"][-1].content

    def test_una_question_gets_programmatic_rejection(self, task_index_file):
        """无法归类的问题（UNA）直接程序化拒绝，不消耗 LLM。"""
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数</user_query>"),
                _make_ai_message_with_hil(pending_action="数据口径是否正确"),
            ],
            hitl_task_index_path=task_index_file,
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert isinstance(result["messages"][-1], ToolMessage)
        assert "无法直接确认" in result["messages"][-1].content

    def test_resolves_hil_and_marks_hitl_resolved(self, task_index_file):
        """可分类但程序化解析不出信号时走 LLM 兜底。

        fixture golden（SELECT * FROM feature_q01）提取不到 db.table 源表，
        TABLE_CONFIRM 程序化分支返回 None → LLM 生成回复。
        """
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数</user_query>"),
                _make_ai_message_with_hil(pending_action="数据源选择需要您确认"),
            ],
            hitl_task_index_path=task_index_file,
        )
        runtime = RuntimeStub(task_index_path=task_index_file)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))

        # Should inject tool message and mark as resolved (not complete, let workflow continue)
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert len(result["messages"]) == 3
        assert isinstance(result["messages"][-1], ToolMessage)
        assert result["messages"][-1].content == "这是自动回复内容"

    def test_llm_failure_falls_back_without_exception(self, task_index_file):
        """MR453 复核 P0-1 回归：LLM 生成失败走兜底回复，不再 (None, None) 解包炸 TypeError。"""
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数</user_query>"),
                _make_ai_message_with_hil(pending_action="数据源选择需要您确认"),
            ],
            hitl_task_index_path=task_index_file,
        )
        runtime = RuntimeStub(task_index_path=task_index_file, llm_response="")
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert isinstance(result["messages"][-1], ToolMessage)
        assert "自主判断" in result["messages"][-1].content
        assert result["hitl_resolved_info"]["fallback_reason"] == "reply_generation_failed"

    def test_uses_runtime_fallback_for_config(self, task_index_file):
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数</user_query>"),
                _make_ai_message_with_hil(),
            ],
            hitl_auto_resolve=False,  # not in state
            hitl_task_index_path=None,
        )
        runtime = RuntimeStub(auto_resolve=True, task_index_path=task_index_file)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False

    def test_skips_with_no_runtime_and_no_state_config(self):
        state = _make_state(
            messages=[HumanMessage(content="查询"), _make_ai_message_with_hil()],
            hitl_auto_resolve=False,
            hitl_task_index_path=None,
        )
        result = hitl_auto_resolver(cast(Any, state), None)
        assert result is state


# ─────────────────────────────────────────────────────────────────────────────
# P0-P4 行为测试（v3.3：提取拼接 / 分类修正 / 时间窗口具体化）
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyHitlAction:
    def test_mixed_question_without_tables_reroutes_to_ambiguity(self):
        """P2：命中 TABLE_CONFIRM 关键词但无候选表名 + 标识关键词 → AMBIGUITY_CLARIFY。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        assert classify_hitl_action("源表已定，确认用户标识用 device_id 还是 user_id？") == "AMBIGUITY_CLARIFY"
        assert classify_hitl_action("数据源已定，时间窗口是否包含当天？") == "AMBIGUITY_CLARIFY"

    def test_real_table_question_stays_table_confirm(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        assert (
            classify_hitl_action("数据源候选：biads.dws_x_dm 还是 agapdw.dwd_y_dm？") == "TABLE_CONFIRM"
        )
        assert classify_hitl_action("数据源你倾向方案A还是方案B？") == "TABLE_CONFIRM"

    def test_ir_hitl_identity_routes_to_ambiguity(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = (
            "DataTaskIR 未确认口径（请给出倾向方案，并附 1-2 个具体候选供确认）：\n"
            "【特征主键】特征主键 components 尚未确定\n"
            "请在答复中选定一个具体方案（表名/字段名/时间窗口等具体值），不要只回答“请你自主判断”。"
        )
        assert classify_hitl_action(q) == "AMBIGUITY_CLARIFY"

    def test_ir_hitl_sources_una_routes_to_table_confirm(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = (
            "DataTaskIR 未确认口径：\n"
            "【数据源与关联预处理】缺少可用事实表证据\n"
            "请选定具体源表。"
        )
        assert classify_hitl_action(q) == "TABLE_CONFIRM"

    def test_ir_hitl_output_features_avoids_output_schema_preview(self):
        """IR output-feature口径 must not take the OUTPUT_SCHEMA template path."""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = (
            "DataTaskIR 未确认口径（请给出倾向方案，并附 1-2 个具体候选供确认）：\n"
            "【输出特征目录】输出列格式未定，是否需要 TopN？输出列名是什么？\n"
            "候选：两列宽表 / 先去重再聚合"
        )
        assert classify_hitl_action(q) == "AMBIGUITY_CLARIFY"


class TestDescribeGoldenTimeWindow:
    def _describe(self, golden_sql: str) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _describe_golden_time_window

        return _describe_golden_time_window(golden_sql, "pt_d")

    def test_single_day_snapshot(self):
        sql = "INSERT OVERWRITE TABLE t PARTITION(pt_d='$date') SELECT a FROM s WHERE pt_d='$date'"
        assert "单日快照" in self._describe(sql)
        assert "跨天滚动" in self._describe(sql)

    def test_bounded_range_with_inclusive_lower(self):
        sql = "SELECT 1 FROM s WHERE pt_d<='$date' AND pt_d>='${start_time,-30,yyyyMMdd}'"
        desc = self._describe(sql)
        assert "过去31天" in desc
        assert "（含）" in desc

    def test_bounded_range_with_exclusive_lower(self):
        sql = "SELECT 1 FROM s WHERE pt_d<='$date' AND pt_d>'${start_time,-365,yyyyMMdd}'"
        desc = self._describe(sql)
        assert "过去365天" in desc
        assert "不含" in desc

    def test_partition_clause_not_misread_as_filter(self):
        """INSERT/DROP PARTITION 子句里的 pt_d='$date' 是写入目标，不算过滤口径。"""
        sql = "DROP IF EXISTS PARTITION (pt_d = '$date');\nINSERT OVERWRITE TABLE t PARTITION (pt_d = '$date')"
        assert self._describe(sql) == ""

    def test_multi_window_sql_lists_all(self):
        sql = (
            "SELECT 1 FROM s1 WHERE pt_d<='$date' AND pt_d>='${start_time,-30,yyyyMMdd}'\n"
            "UNION ALL SELECT 2 FROM s2 WHERE pt_d='${date}'"
        )
        desc = self._describe(sql)
        assert "过去31天" in desc
        assert "单日快照" in desc

    def test_date_sub_rolling_window(self):
        sql = "SELECT 1 FROM s WHERE pt_d >= bicoredata.DateFormat(DATE_SUB('$date_ep', 6))"
        desc = self._describe(sql)
        assert "往前6天" in desc or "最近7天" in desc.lower() or "7天" in desc


class TestAnswerTableConfirmProgrammatic:
    def _run(self, golden_sql: str, hil_question: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _answer_table_confirm_programmatic

        return _answer_table_confirm_programmatic(golden_sql, hil_question, "开发序列特征")

    def test_partial_confirm_directs_proceed(self):
        """P4：部分确认回复含"不用再问/直接推进"指令。"""
        golden = "SELECT a.id FROM db.dwd_a_dm t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"
        reply = self._run(golden, "源表确认：使用 db.dwd_a_dm，是否可以？")
        assert "不用再就这些表来问我" in reply
        assert "推进" in reply
        # 不泄漏 agent 未提到的 golden 表
        assert "dwd_b_dm" not in reply

    def test_wrong_table_rejection_advises_new_keywords(self):
        """P5 配套：拒绝错表时要求换业务词重新检索，不要重复候选。"""
        golden = "SELECT a.id FROM db.dwd_a_dm t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"
        reply = self._run(golden, "源表候选：db.dwd_a_dm 和 db.ads_wrong_dm")
        assert "不是我想要的数据来源" in reply
        assert "换用需求里的业务词重新检索" in reply
        assert "dwd_b_dm" not in reply

    def test_bare_agent_table_not_judged_wrong(self):
        """MR453 复核 P1(a)：agent 裸提（省 db 前缀）正确源表不再被判成错表。"""
        golden = "SELECT a FROM dwd.dwd_a JOIN dim.dim_b ON 1=1"
        reply = self._run(golden, "源表确认：使用 dwd_a，是否可以？")
        assert "符合我的需求" in reply
        assert "不是我想要的数据来源" not in reply

    def test_unprefixed_golden_sql_activates_programmatic(self):
        """MR453 复核 P1(c)：golden SQL 无 db 前缀时程序化判定仍生效（此前退回 LLM）。"""
        golden = "select a from dwd_user_event join dim_b on 1=1"
        reply = self._run(golden, "源表确认：使用 dwd_user_event，是否可以？")
        assert reply is not None
        assert "符合我的需求" in reply


# ─────────────────────────────────────────────────────────────────────────────
# v42 L1 近变体口径判别：被拒表与 golden 表 token 差集 → 客户口吻口径词提示
# 靶例：q51 billing/event 互选、q57 oCPD/oCPC、q64 30d/1y+ubr 窗口口径
# ─────────────────────────────────────────────────────────────────────────────


class TestNearVariantCaliberHint:
    def _hint(self, golden: list[str], rejected: list[str]) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _near_variant_caliber_hint,
        )

        return _near_variant_caliber_hint(golden, rejected)

    def test_q51_billing_event_variant(self):
        """q51 型：billing/event 同族近变体 → 要"事件流水"不要"计费流水"。"""
        hint = self._hint(
            ["biods.ods_hispace_hiad_event_log_hm"],
            ["biods.ods_hispace_hiad_billing_log_hm"],
        )
        assert "事件流水" in hint
        assert "计费流水" in hint
        assert hint.startswith("另外把数据口径")

    def test_q57_ocpc_ocpd_variant(self):
        """q57 型：oCPD/oCPC 近变体 → 口径词直接说破。"""
        hint = self._hint(
            ["biads.ads_agalg_ocpc_single_frame_calibration_pcvr_siteid_appid_expid_multi_gran_original_period_hm"],
            ["biads.ads_agalg_ocpd_calibration_pcvr_siteid_appid_expid_original_period_new_hm"],
        )
        assert "oCPC" in hint
        assert "oCPD" in hint

    def test_q64_window_and_ubr_variant(self):
        """q64 型：30d/1y 窗口 + all/ubr 口径差集 → 窗口与 UBR 口径提示。"""
        hint = self._hint(
            ["biads.ads_agrec_adv_common_user_ubr_uninstallapp_list_1y_dm"],
            ["biads.ads_agrec_adv_common_user_uninstallapp_list_30d_all_dm"],
        )
        assert "近1年窗口" in hint
        assert "卸载备份记录" in hint
        assert "近30天窗口" in hint

    def test_unrelated_table_no_hint(self):
        """完全无关的被拒表（Jaccard < 1/3）→ 无口径提示（防误伤）。"""
        hint = self._hint(
            ["biads.ads_agalg_ocpc_calibration_pcvr_siteid_appid_expid_hm"],
            ["biods.ods_user_profile_basic_info"],
        )
        assert hint == ""

    def test_near_variant_without_mapped_tokens_no_hint(self):
        """近变体但差集 token 无概念映射 → 空串回落现行为。"""
        hint = self._hint(
            ["db.ads_agalg_ocpc_x_hm"],
            ["db.ads_agalg_ocpc_new_hm"],
        )
        assert hint == ""

    def test_hint_injected_into_rejection_reply(self):
        """程序化拒绝答复带口径提示，且不泄漏 golden 表名。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        golden = "SELECT a FROM biods.ods_hispace_hiad_event_log_hm WHERE pt_d='$date'"
        reply = _answer_table_confirm_programmatic(
            golden, "源表候选：biods.ods_hispace_hiad_billing_log_hm，是否可以？", "开发事件序列特征"
        )
        assert "不是我想要的数据来源" in reply
        assert "事件流水" in reply
        assert "计费流水" in reply
        assert "event_log_hm" not in reply

    def test_hint_appended_to_escalated_reply(self):
        """升级答复（连续选错）同样带口径提示。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )
        from dataagent.core.flex.hooks.hitl_common import HUMAN_FEEDBACK_TOOL_NAME

        prior_reject = ToolMessage(
            content="biods.ods_hispace_hiad_other_log_hm 从我任务的需求看不是我想要的数据来源。请给我候选。",
            name=HUMAN_FEEDBACK_TOOL_NAME,
            tool_call_id="t1",
        )
        golden = "SELECT a FROM biods.ods_hispace_hiad_event_log_hm WHERE pt_d='$date'"
        reply = _answer_table_confirm_programmatic(
            golden,
            "源表候选：biods.ods_hispace_hiad_billing_log_hm，是否可以？",
            "开发事件序列特征",
            messages=[prior_reject],
        )
        assert "仍然不是我想要的数据来源" in reply
        assert "另外把数据口径" in reply
        assert "事件流水" in reply
        assert "event_log_hm" not in reply


class TestCustomerRestateQueryV42:
    def test_long_query_keeps_tail_conditions(self):
        """v41 q51 共因：复述放宽到 300 字，尾部筛选条件不再被 80 字截丢。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _customer_restate_query,
        )

        tail_marker = "尾部筛选条件：排除计费类型为负的记录"
        query = "开发事件序列特征。" + "业务描述填充。" * 30 + tail_marker
        assert len(query) > 200
        restate = _customer_restate_query(query)
        assert tail_marker in restate
        assert restate.endswith("…") is False or len(query) > 300


# ─────────────────────────────────────────────────────────────────────────────
# v3.4 方案1：混合提问分类修复（已确认引用 → AMBIGUITY_CLARIFY）
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyConfirmedReferenceReroute:
    def test_q52_mid_question_with_confirmed_table_reroutes(self):
        """回归（q52）：引用已确认源表的 Mid 追问（标识+时间）不再被判成选表问题。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = (
            "Mid阶段需确认用户标识字段和时间窗口口径。源表已确认为 biads.dws_x_dm，"
            "时间窗口是否包含当天存在多种理解，请确认。"
        )
        assert classify_hitl_action(q) == "AMBIGUITY_CLARIFY"

    def test_q62_time_question_with_confirmed_table_reroutes(self):
        """回归（q62）：时间口径追问引用已定数据源 → AMBIGUITY_CLARIFY。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = "时间窗口口径需确认：近7天是否含当天？数据源已定 agapdw.dwd_y_dm。"
        assert classify_hitl_action(q) == "AMBIGUITY_CLARIFY"

    def test_new_candidates_with_ambiguity_kw_stays_table_confirm(self):
        """真实新候选提案（无已确认标记）即使带口径关键词也保持 TABLE_CONFIRM。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = "数据源候选 db.dwd_a_dm 或 db.dwd_b_dm，用于按时间范围统计用户活跃，选哪个？"
        assert classify_hitl_action(q) == "TABLE_CONFIRM"

    def test_not_yet_confirmed_marker_not_falsely_triggering(self):
        """“尚未确认”不含“已确认”标记，不触发误改道。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = "数据源尚未确认，候选 biads.dws_x_dm 或 db.dwd_b_dm，时间窗口口径怎么算？"
        assert classify_hitl_action(q) == "TABLE_CONFIRM"

    def test_confirmed_reference_without_ambiguity_kw_stays_table_confirm(self):
        """无口径歧义关键词时，已确认引用的表问题仍走 TABLE_CONFIRM。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = "源表已确认为 biads.dws_x_dm，是否照此推进？"
        assert classify_hitl_action(q) == "TABLE_CONFIRM"


# ─────────────────────────────────────────────────────────────────────────────
# v3.5 复盘：口径决策提问误路由修复（TABLE_CONFIRM → AMBIGUITY_CLARIFY）
# v3.5 实测 10 条提问（q07/q20/q39/q42/q44/q61×2/q66/q67/q71）为"时间窗口/口径"
# 决策类提问但提到表名，被 TABLE_CONFIRM 模板答非所问，agent 拿不到口径答复后独自定错口径。
# ─────────────────────────────────────────────────────────────────────────────


class TestCaliberQuestionReroute:
    def _classify(self, q: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def test_q61_combined_caliber_question_reroutes(self):
        """回归（q61 Q3）：标识+时间窗口口径合并提问，表名仅作上下文 → AMBIGUITY_CLARIFY。"""
        q = (
            "Mid阶段需要确认两个口径：1）用户标识字段——该任务为内容级特征，判断不需要用户标识字段，"
            "需用户确认；2）时间窗口口径——源表是每日快照，需确认取哪天的数据。"
            "## Mid 阶段：用户标识字段 & 时间窗口口径确认\n"
            "源表 `biads.ads_agalg_ue_content_ucb_score_dm` 是每日快照表。"
            "请问您需要取哪一天的数据？方案A（推荐）：取最新一天分区；方案B：取近N天的数据。"
        )
        assert self._classify(q) == "AMBIGUITY_CLARIFY"

    def test_q61_final_caliber_choice_reroutes(self):
        """回归（q61 Q4）：预算内最后一次"请选择取数口径 A/B"→ AMBIGUITY_CLARIFY。"""
        q = (
            "系统提示交互预算已用3/4，这是最后一次可获得答复的提问。时间窗口口径是最致命的未决口径"
            "——它直接影响SQL的分区过滤条件。源表 `biads.ads_agalg_ue_content_ucb_score_dm` 是每日快照表，"
            "ucb_score本身已基于近7日曝光计算（源表已内含窗口）。请选择取数口径："
            "方案A（推荐）：取最新一天分区；方案B：取近N天数据。"
        )
        assert self._classify(q) == "AMBIGUITY_CLARIFY"

    def test_q42_embedded_window_caliber_reroutes(self):
        """回归（q42 Q2）：源表已内含窗口、按分区读取的口径确认 → AMBIGUITY_CLARIFY。"""
        q = (
            "需要确认时间窗口口径，源表已内含1年窗口，按单日分区读取。"
            "关于时间窗口口径：源表 `biads.ads_agrec_adv_common_user_ubr_clickapp_list_1y_plus_dm` "
            "表名含 _1y_，即源表本身已内含近1年窗口数据，按 pt_d 单日分区读取即可。请问是否确认此口径？"
        )
        assert self._classify(q) == "AMBIGUITY_CLARIFY"

    def test_table_proposal_with_caliber_kw_stays_table_confirm(self):
        """守护：仍在提新表候选（候选/我倾向/唯一匹配）时即使带口径词也保持 TABLE_CONFIRM。"""
        assert (
            self._classify(
                "数据源尚未确认，候选 biads.dws_x_dm 或 db.dwd_b_dm，时间窗口口径怎么算？"
            )
            == "TABLE_CONFIRM"
        )
        assert (
            self._classify(
                "Initial阶段：数据源表确认。根据元数据检索，唯一匹配的表为 "
                "agapads.ads_push_label_news_intent_infoflow_category_ds，请确认。"
            )
            == "TABLE_CONFIRM"
        )
        assert (
            self._classify(
                "需要确认数据源表选择。我倾向选择候选1 "
                "`biads.ads_agrec_adv_common_user_usedapp_detail_30d_dm` 作为主事实表，"
                "关联 `biads.ads_noah_hispace_app_info_ds` 获取二级分类，是否可以？"
            )
            == "TABLE_CONFIRM"
        )


# ─────────────────────────────────────────────────────────────────────────────
# v48 混合提问改判极性修正：确认请求形 ≠ 已确认引用；字段候选 ≠ 表候选
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedQuestionPolarityV48:
    """v48 极性修正（v47_smoke q01 铁例 + 回放语料 5 翻转审计）。

    两类极性反转：①确认请求形（"请您确认/需要您确认/请问您确认使用"）曾被子串
    "您确认/确认使用"误判为已确认引用——新表提案问（含口径关键词）被改判
    AMBIGUITY 后表提案无人应答，agent 带着错误兄弟表往下走（smoke q01 选
    _1y_dm 而非 golden _1y_v2_dm FAIL）；②裸"候选"曾把标识字段候选枚举/
    否定形（"没有其他候选"）误判为表候选，口径决策问被 TABLE_CONFIRM 模板
    答非所问。回放门基线随 v48 重定为 51/46/82/3（审计见 replay_check.py 头注释）。
    """

    def _classify(self, q: str) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def _confirmed_refs(self, q: str) -> bool:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _tables_are_confirmed_references,
        )

        return _tables_are_confirmed_references(q)

    SMOKE_Q01 = (
        "Initial阶段：需要确认数据源表选择。候选源表涉及多个近变体表，需要用户确认。\n"
        "## 数据源表选择确认\n\n"
        "根据元数据检索，我倾向以下方案，请您确认：\n\n"
        "### 主数据源（相似用户扩展后的下载应用ID序列）： `biads.dm_table_a`\n"
        "字段包含 imei、app_type。时间窗口：过去31天（含当天）。"
    )

    def test_smoke_q01_table_proposal_stays_table_confirm(self):
        """铁例 pin：近变体表提案 + 礼貌"请您确认" + 标识/时间子问 → TABLE_CONFIRM。

        pre-v48 该问被改判 AMBIGUITY_CLARIFY，程序化答复只给标识/时间口径，
        表提案无人应答（agent 按纪律提问却被抢答，错误兄弟表锁定）。
        """
        assert self._classify(self.SMOKE_Q01) == "TABLE_CONFIRM"

    def test_request_confirm_forms_not_confirmed_references(self):
        """确认请求形剥离：请求极性（请您/麻烦您/需要您/请问您确认使用）不是已确认引用。"""
        assert not self._confirmed_refs("请您确认数据源表选择")
        assert not self._confirmed_refs("麻烦您确认一下方案A是否可用")
        assert not self._confirmed_refs("需要您确认后再修正")
        assert not self._confirmed_refs("请问您确认使用 biads.t1 作为源表吗")

    def test_true_reference_forms_still_confirmed_references(self):
        """真引用形（不带请求前缀）不受剥离影响。"""
        assert self._confirmed_refs("源表已确认为 biads.t1，现在确认标识口径")
        assert self._confirmed_refs("按您确认的源表，拼接序列时同键重复值要不要去重")
        assert self._confirmed_refs("已确认使用 biads.t1")

    def test_q02_shape_polite_table_proposal_stays_table_confirm(self):
        """回放翻转 pin（q02）："请您确认数据源表选择"+候选表+表字段清单 → TABLE_CONFIRM。"""
        q = (
            "Initial阶段需要确认数据源表选择，已通过元数据检索锁定两张候选表，"
            "需要用户确认后再推进后续步骤。 我倾向的方案如下，请您确认数据源表选择：  "
            "**事实表（用户下载详情列表）：** `biads.dm_a` - 字段包含：imei、app_type"
        )
        assert self._classify(q) == "TABLE_CONFIRM"

    def test_q34_shape_candidate_options_stays_table_confirm(self):
        """回放翻转 pin（q34）：候选数据源选项 + 方案枚举 + 口径子问 → TABLE_CONFIRM。"""
        q = (
            "用户要求给出2-3个候选数据源选项。已整理3个方案：候选1（单表）、候选2（+应用维表）、"
            "候选3（+回传表）。推荐候选2。剩余2次提问机会，需要确认源表选择后才能推进后续步骤。"
            "同时需要确认总记录数量的计算口径。"
        )
        assert self._classify(q) == "TABLE_CONFIRM"

    def test_q59_identifier_field_candidates_reroute_ambiguity(self):
        """回放翻转 pin（q59/q49）：标识字段候选枚举/否定形不是表提案 → AMBIGUITY_CLARIFY。

        pre-v48 裸"候选"（"三个候选：方案A：device_id"）误拦改判，TABLE_CONFIRM
        模板答表而标识问题无人应答。
        """
        q1 = (
            "需要确认用户标识字段（device_id/uid/user_id）和时间窗口口径（是否按单日分区读取）。"
            "整理如下两个选项请您确认：源表 `agapdw.dwd_tf_dm` 中有三个候选："
            "方案A（推荐）：device_id"
        )
        assert self._classify(q1) == "AMBIGUITY_CLARIFY"
        q2 = (
            "Mid阶段需要确认用户标识字段和时间窗口口径，这是两个最关键的阻塞点。"
            "用户标识字段：did（设备唯一号）- 源表 `agapads.ads_label_ds` 中只有 did "
            "这一个用户标识字段，没有其他候选。请问：1. 用户标识字段用 did 是否确认？"
        )
        assert self._classify(q2) == "AMBIGUITY_CLARIFY"

    def test_bare_candidate_with_table_names_stays_table_confirm(self):
        """守护（v3.5 pin632 语义保留）："候选 biads.x 或 db.y"（候选接 db.table 名）
        仍是表候选提案，带口径词也保持 TABLE_CONFIRM。"""
        assert (
            self._classify(
                "数据源尚未确认，候选 biads.dws_x_dm 或 db.dwd_b_dm，时间窗口口径怎么算？"
            )
            == "TABLE_CONFIRM"
        )

    def test_interpretation_lean_not_table_proposal(self):
        """口径理解确认的"我倾向于理解为"（q64 型）不因 v48 收窄而误入表提案
        （v48 已从 _TABLE_PROPOSAL_RE 移除裸"我倾向"，此 pin 防将来回潮）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _proposes_table_candidates,
        )

        assert not _proposes_table_candidates("我倾向于理解为按设备维度统计，请确认这个理解是否正确")
        assert _proposes_table_candidates("计划使用 biads.t1 作为源表")
        assert _proposes_table_candidates("候选 biads.dws_x_dm 或 db.dwd_b_dm")
        assert not _proposes_table_candidates("有三个候选：方案A（推荐）：device_id")
        assert not _proposes_table_candidates("源表中只有 did，没有其他候选")


# ─────────────────────────────────────────────────────────────────────────────
# v3.4 方案4：列枚举死局提问转向
# ─────────────────────────────────────────────────────────────────────────────


class TestColumnEnumerationRedirect:
    def test_q58_enumeration_ask_detected(self):
        """回归（q58）：“期望的5列具体包含哪些列”是列枚举死局。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _is_column_enumeration_ask

        assert _is_column_enumeration_ask("需要澄清用户期望的5列具体包含哪些列")

    def test_field_enumeration_ask_detected(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _is_column_enumeration_ask

        assert _is_column_enumeration_ask("请明确输出包含哪些字段")

    def test_column_list_ask_detected(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _is_column_enumeration_ask

        assert _is_column_enumeration_ask("期望的列清单请确认一下")

    def test_own_plan_not_redirected(self):
        """agent 陈述自己计划的列构成（不要求用户枚举）不触发转向。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _is_column_enumeration_ask

        assert not _is_column_enumeration_ask("我计划输出以下5列：uid、日期、次数")

    def test_column_count_confirm_not_redirected(self):
        """列数确认类提问（给数字求确认）不是列枚举死局。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _is_column_enumeration_ask

        assert not _is_column_enumeration_ask("输出列数是否为8列，请确认")

    def test_empty_or_none_question(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _is_column_enumeration_ask

        assert not _is_column_enumeration_ask("")
        assert not _is_column_enumeration_ask(None)


# ─────────────────────────────────────────────────────────────────────────────
# v3.4 方案1：AMBIGUITY_CLARIFY 组合答复（标识 + 时间）
# ─────────────────────────────────────────────────────────────────────────────


class TestAnswerAmbiguityCombined:
    GOLDEN = (
        "SELECT device_id2, COUNT(*) AS cnt FROM db.dwd_event_dm "
        "WHERE pt_d='$date' GROUP BY device_id2"
    )

    def _run(self, hil_question: str, golden_sql: str | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _answer_ambiguity_clarify_programmatic

        return _answer_ambiguity_clarify_programmatic(golden_sql or self.GOLDEN, hil_question)

    def test_combined_identifier_and_time_answer(self):
        """混合提问（标识+时间）给组合答复，两部分口径都覆盖。"""
        reply = self._run("用户标识用 device_id 还是 device_id2？时间窗口是否包含当天？")
        assert reply is not None
        assert "用户标识使用 device_id2" in reply
        assert "时间口径" in reply
        assert "请按以上口径继续" in reply

    def test_identifier_only_backcompat(self):
        reply = self._run("用户标识用 device_id 还是 device_id2？")
        assert reply is not None
        assert "用户标识使用 device_id2" in reply
        assert "时间口径" not in reply

    def test_time_only_backcompat(self):
        reply = self._run("时间窗口是否包含当天？")
        assert reply is not None
        assert "时间口径" in reply
        assert "用户标识" not in reply

    def test_no_signal_returns_none(self):
        assert self._run("主键怎么选？") is None


# ─────────────────────────────────────────────────────────────────────────────
# v46 口径缺口问型：UNA → AMBIGUITY 改判 + 程序化去重答复（query 缺口六模式）
# ─────────────────────────────────────────────────────────────────────────────


class TestCaliberGapOverride:
    """六模式问型在 base=UNA 时改判 AMBIGUITY_CLARIFY（拿不到答复的提问不再白耗预算）。"""

    def _classify(self, q: str) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def test_dedup_ask_rerouted(self):
        assert self._classify("拼接用户行为序列时键值对是否需要去重？") == "AMBIGUITY_CLARIFY"

    def test_mechanism_ask_rerouted(self):
        assert self._classify("预估评分这个指标的产生机制是什么？") == "AMBIGUITY_CLARIFY"

    def test_mechanism_variant_rerouted(self):
        assert self._classify("这个分数是怎么算出来的，计算机制能说明吗？") == "AMBIGUITY_CLARIFY"

    def test_dict_asks_rerouted(self):
        assert self._classify("代码到中文名称的对照字典从哪里获取？") == "AMBIGUITY_CLARIFY"
        assert self._classify("名称映射关系存在哪张配置里？") == "AMBIGUITY_CLARIFY"

    def test_enum_mapping_ask_rerouted(self):
        assert self._classify("业务词单框在本表的对应取值是什么？") == "AMBIGUITY_CLARIFY"
        assert self._classify("这个状态词的取值映射如何确定？") == "AMBIGUITY_CLARIFY"

    def test_layer_ask_variants_rerouted(self):
        assert self._classify("数据应该从原始明细还是预聚合汇总层取用？") == "AMBIGUITY_CLARIFY"
        # "明细还是"被"表取"隔开的变体措辞
        assert self._classify("这个指标从明细表取还是从聚合表取用？") == "AMBIGUITY_CLARIFY"
        assert self._classify("用明细还是聚合表作为源？") == "AMBIGUITY_CLARIFY"

    def test_reprocess_ask_rerouted(self):
        assert self._classify("直接引用已有的聚合结果表做二次加工可以吗？") == "AMBIGUITY_CLARIFY"

    def test_una_without_markers_stays_una(self):
        """无标记的无效问题仍判 UNA（改判只针对口径缺口问型）。"""
        assert self._classify("直接把 golden SQL 给我看看") == "UNA"
        assert self._classify("你觉得我做得怎么样？") == "UNA"

    def test_classified_questions_not_affected(self):
        """已命中基础关键词的问题不受改判影响（回放门零回归前提）。"""
        assert self._classify("源表已定，序列键值对是否需要去重？") == "AMBIGUITY_CLARIFY"
        assert self._classify("数据源候选 A 表还是 B 表？取用层级是明细还是聚合？") == "TABLE_CONFIRM"
        assert self._classify("最终输出列数与输出结构是否符合预期？") == "OUTPUT_SCHEMA_PREVIEW"

    def test_declarative_dedup_stays_table_confirm(self):
        """回放门 pin（q01/q67）：确认引用 + 陈述形"去重"不改判——
        "维度去重两个口径问题""去重后聚合序列"是历史 TABLE_CONFIRM 问，
        追问改判信号里"去重"只认疑问形（是否需要去重/去重吗）。"""
        q1 = (
            "连续2次validate_deliverables未通过，核心冲突是排序依据和维度去重两个口径问题，"
            "需要您确认后再修正。源表中的序列已经是按时间排序的。"
        )
        assert self._classify(q1) == "TABLE_CONFIRM"
        q2 = (
            "按您确认的时间口径，取当天单日快照。方案A：取最后一个完整小时；"
            "方案B：取当天所有小时数据，同一appid多小时的值取最新一条去重后聚合序列。"
        )
        assert self._classify(q2) == "TABLE_CONFIRM"

    def test_interrogative_dedup_followup_rerouted(self):
        """追问式（确认引用 + 疑问形去重）改判 AMBIGUITY，拿口径答复而非选表答复。"""
        assert self._classify("源表已定，接下来确认：序列键值对是否需要去重？") == "AMBIGUITY_CLARIFY"
        assert self._classify("按您确认的源表，拼接序列时同键重复值要不要去重？") == "AMBIGUITY_CLARIFY"


class TestDedupCaliberProgrammatic:
    """去重口径程序化答复：collect_set/collect_list 确定性回答，混合/无信号退 LLM 兜底。"""

    SQL_SET = (
        "SELECT user_id, CONCAT_WS(',', COLLECT_SET(action_type)) AS act_seq "
        "FROM db.dwd_event_dm WHERE pt_d='$date' GROUP BY user_id"
    )
    SQL_LIST = (
        "SELECT user_id, CONCAT_WS(',', COLLECT_LIST(action_type)) AS act_seq "
        "FROM db.dwd_event_dm WHERE pt_d='$date' GROUP BY user_id"
    )
    SQL_BOTH = (
        "SELECT user_id, COLLECT_SET(action_type), COLLECT_LIST(item_id) "
        "FROM db.dwd_event_dm WHERE pt_d='$date' GROUP BY user_id"
    )
    SQL_NONE = "SELECT user_id, COUNT(*) AS cnt FROM db.dwd_event_dm WHERE pt_d='$date' GROUP BY user_id"

    def _run(self, golden_sql: str, hil_question: str = "序列键值对是否需要去重？"):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_ambiguity_clarify_programmatic,
        )

        return _answer_ambiguity_clarify_programmatic(golden_sql, hil_question)

    def test_collect_set_means_dedup(self):
        reply = self._run(self.SQL_SET)
        assert reply is not None and "去重口径" in reply and "只保留一条" in reply

    def test_collect_list_means_keep_all(self):
        reply = self._run(self.SQL_LIST)
        assert reply is not None and "去重口径" in reply and "不需要去重" in reply

    def test_mixed_aggregations_fall_back(self):
        assert self._run(self.SQL_BOTH) is None

    def test_no_aggregation_signal_falls_back(self):
        assert self._run(self.SQL_NONE) is None

    def test_dedup_answer_combines_with_time(self):
        """去重 + 时间混合提问给组合答复（不提前 return 晾着另一半）。"""
        reply = self._run(self.SQL_SET, "序列键值对是否需要去重？时间窗口是否包含当天？")
        assert reply is not None
        assert "去重口径" in reply and "时间口径" in reply


# ─────────────────────────────────────────────────────────────────────────────
# P2：AMBIGUITY 标识短路（golden 不在选项中 → 概念化转向，不经 LLM 兜底）
# ─────────────────────────────────────────────────────────────────────────────


class TestAmbiguityIdentifierShortCircuit:
    GOLDEN_USER = (
        "SELECT user_id, app_id, COUNT(*) AS cnt FROM db.dwd_event_dm "
        "WHERE pt_d='$date' GROUP BY user_id, app_id"
    )

    def _run(self, hil_question: str, golden_sql: str | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _answer_ambiguity_clarify_programmatic

        return _answer_ambiguity_clarify_programmatic(golden_sql or self.GOLDEN_USER, hil_question)

    def test_golden_not_in_options_gives_concept_redirect(self):
        """golden 标识（user_id）不在 agent 选项（oaid/device_id）中 → 概念转向。

        不报 golden 字段名（零泄漏），用业务概念"账号ID"描述统计粒度。
        """
        reply = self._run("用户标识字段用 oaid 还是 device_id？")
        assert reply is not None
        assert "账号ID" in reply
        assert "user_id" not in reply
        assert "不能从你给的选项里直接确认" in reply
        assert "字段说明核对" in reply

    def test_golden_not_in_options_with_variant_hint(self):
        """golden 是二代标识而 agent 只给一代 → 转向含"变体"提示。"""
        golden = (
            "SELECT device_id2, COUNT(*) AS cnt FROM db.dwd_event_dm "
            "WHERE pt_d='$date' GROUP BY device_id2"
        )
        reply = self._run("用户标识用 device_id 可以吗？", golden)
        assert reply is not None
        assert "设备ID（二代）" in reply
        assert "device_id2" not in reply
        assert "变体" in reply

    def test_non_identifier_question_no_redirect(self):
        """时间口径问题顺带提到标识字段（无标识/维度提问语）→ 不做标识转向。"""
        reply = self._run("时间窗口是否包含当天？关联用 oaid 行不行？")
        # 不出现标识纠正语（时间部分照常回答）
        assert reply is None or "不能从你给的选项里直接确认" not in reply

    def test_combined_redirect_and_time(self):
        """混合提问（golden 不在选项 + 时间）→ 标识转向 + 时间口径组合答复。"""
        reply = self._run("用户标识用 oaid 还是 imei？时间窗口是否包含当天？")
        assert reply is not None
        assert "账号ID" in reply
        assert "时间口径" in reply


# ─────────────────────────────────────────────────────────────────────────────
# P4(a-2)：OUTPUT_SCHEMA 聚合粒度概念化（不泄漏 agent 未提到的字段名）
# ─────────────────────────────────────────────────────────────────────────────


class TestOutputSchemaGrainConcept:
    def _run(self, hil_question: str, golden_sql: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _answer_output_schema_programmatic

        return _answer_output_schema_programmatic(golden_sql, hil_question)

    def test_grain_keys_not_mentioned_use_concept(self):
        """agent 未提到 GROUP BY 字段 → 粒度用业务概念描述，不报字段名。"""
        golden = (
            "INSERT OVERWRITE TABLE t PARTITION(pt_d='$date') "
            "SELECT user_id, app_type, COUNT(*) AS cnt FROM db.dwd_event_dm "
            "WHERE pt_d='$date' GROUP BY user_id, app_type"
        )
        reply = self._run("输出结构确认：我计划输出明细宽表", golden)
        assert reply is not None
        assert "账号ID" in reply
        assert "应用类型" in reply
        assert "user_id" not in reply
        assert "app_type" not in reply

    def test_grain_keys_agent_mentioned_keep_name(self):
        """agent 自己提到的字段名 → 保留原名（复述 agent 的词允许）。"""
        golden = (
            "INSERT OVERWRITE TABLE t PARTITION(pt_d='$date') "
            "SELECT user_id, app_type, COUNT(*) AS cnt FROM db.dwd_event_dm "
            "WHERE pt_d='$date' GROUP BY user_id, app_type"
        )
        reply = self._run("输出结构确认：按 user_id 和 app_type 聚合的汇总表对吗", golden)
        assert reply is not None
        assert "user_id" in reply
        assert "app_type" in reply

    def test_unmappable_key_falls_back_to_generic(self):
        """无法概念化的 GROUP BY 键 → 通用短语，不报字段名。"""
        golden = (
            "INSERT OVERWRITE TABLE t PARTITION(pt_d='$date') "
            "SELECT biz_flag_x9, COUNT(*) AS cnt FROM db.dwd_event_dm "
            "WHERE pt_d='$date' GROUP BY biz_flag_x9"
        )
        reply = self._run("输出结构确认：我计划输出明细宽表", golden)
        assert reply is not None
        assert "biz_flag_x9" not in reply
        assert "任务原文要求的聚合主体" in reply


# ─────────────────────────────────────────────────────────────────────────────
# P4(a-1)：TABLE_CONFIRM 连续选错升级（特征级指引打破检索-拒绝死局）
# ─────────────────────────────────────────────────────────────────────────────


class TestTableConfirmEscalation:
    GOLDEN = (
        "INSERT OVERWRITE TABLE db.dws_out_dm PARTITION(pt_d='$date') "
        "SELECT user_id, COUNT(*) AS cnt FROM biads.dws_evt_fct_dm "
        "WHERE pt_d>='${start_time,-30,yyyyMMdd}' AND pt_d<='$date' GROUP BY user_id"
    )

    def _run(self, hil_question: str, messages: list | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _answer_table_confirm_programmatic

        return _answer_table_confirm_programmatic(self.GOLDEN, hil_question, "开发序列特征", messages)

    @staticmethod
    def _prior_rejection_messages() -> list[ToolMessage]:
        return [
            ToolMessage(
                content="db.ods_wrong_a 从我任务的需求看不是我想要的数据来源。请你基于元数据再检索一下。",
                name="request_human_feedback",
                tool_call_id="c0",
            )
        ]

    def test_escalation_after_prior_rejection(self):
        """前轮已拒 + 本轮候选仍全错 → 特征级指引（层级/粒度/窗口），不报 golden 表名。"""
        reply = self._run("源表候选：db.ods_wrong_b，是否可以？", self._prior_rejection_messages())
        assert reply is not None
        assert "汇总层" in reply  # golden 表 dws_ 前缀 → 层级特征
        assert "天级" in reply  # _dm 后缀 → 粒度特征
        assert "账号ID" in reply  # GROUP BY user_id → 记录粒度概念
        assert "dws_evt_fct_dm" not in reply  # 不泄漏 golden 表名
        assert "user_id" not in reply  # 不泄漏 golden 字段名
        assert "最接近" in reply  # 检索不到时的兜底指引

    def test_first_rejection_stays_standard(self):
        """首轮选错（无前轮拒绝）→ 标准拒绝模板，不含特征级指引。"""
        reply = self._run("源表候选：db.ods_wrong_b，是否可以？", None)
        assert reply is not None
        assert "不是我想要的数据来源" in reply
        assert "汇总层" not in reply

    def test_partial_correct_no_escalation(self):
        """候选部分正确 → 标准模板（部分确认 + 拒绝错表），不升级。"""
        messages = self._prior_rejection_messages()
        reply = self._run("源表候选：biads.dws_evt_fct_dm 和 db.ods_wrong_b", messages)
        assert reply is not None
        assert "这几张表符合我的需求" in reply
        assert "不是我想要的数据来源" in reply
        assert "最接近" not in reply

    def test_no_prior_rejection_marker_no_escalation(self):
        """messages 里的 ToolMessage 不含拒绝标记语 → 不算前轮拒绝。"""
        messages = [
            ToolMessage(content="确认：就用 db.ods_x。", name="request_human_feedback", tool_call_id="c0")
        ]
        reply = self._run("源表候选：db.ods_wrong_b，是否可以？", messages)
        assert reply is not None
        assert "汇总层" not in reply


# ─────────────────────────────────────────────────────────────────────────────
# v51 P0-4：第 3 次拒绝切换"自主决策交付"最终话术（顽疾六 grounding 死锁出口）
# ─────────────────────────────────────────────────────────────────────────────


class TestTableConfirmThirdRejectionAutonomy(TestTableConfirmEscalation):
    @staticmethod
    def _two_prior_rejection_messages() -> list[ToolMessage]:
        return [
            ToolMessage(
                content="db.ods_wrong_a 从我任务的需求看不是我想要的数据来源。请你基于元数据再检索一下。",
                name="request_human_feedback",
                tool_call_id="c0",
            ),
            ToolMessage(
                content="db.ods_wrong_b 仍然不是我想要的数据来源——前面 1 轮你给的候选都不符合，"
                "这次我把需求特征说得更具体些……",
                name="request_human_feedback",
                tool_call_id="c1",
            ),
        ]

    def test_third_rejection_switches_to_autonomy(self):
        """前两轮已拒 + 本轮仍全错 → 不再索要候选，切换自主决策交付最终话术。"""
        reply = self._run(
            "源表候选：db.ods_wrong_c，是否可以？", self._two_prior_rejection_messages()
        )
        assert reply is not None
        assert "第三次" in reply
        assert "自主选择" in reply
        assert "直接完成 SQL 交付" in reply
        assert "最终答复" in reply
        # 不再走"给我 2-3 个候选"的循环指令
        assert "2-3 个候选" not in reply
        # 防泄漏边界保持
        assert "dws_evt_fct_dm" not in reply
        assert "user_id" not in reply

    def test_second_rejection_stays_escalated(self):
        """前一轮已拒（prior_rejects=1）→ 仍走特征级升级，不切最终话术。"""
        reply = self._run("源表候选：db.ods_wrong_b，是否可以？", self._prior_rejection_messages())
        assert reply is not None
        assert "第三次" not in reply
        assert "汇总层" in reply

    def test_autonomy_reply_not_counted_as_rejection_marker(self):
        """最终话术不含拒绝标记语 → 第 4 轮若再问，prior_rejects 仍为 2、
        继续给最终话术（稳定，不退回索要候选的循环）。"""
        messages = self._two_prior_rejection_messages()
        reply = self._run("源表候选：db.ods_wrong_c", messages)
        assert "不是我想要的数据来源" not in reply
        reply_again = self._run("源表候选：db.ods_wrong_d", messages)
        assert "第三次" in reply_again


# ─────────────────────────────────────────────────────────────────────────────
# v53：复合提问内嵌口径子问复合答复（TABLE_CONFIRM/OUTPUT 各出口经 _with_caliber
# 追加口径段；靶例 v52_r2 q54/q60 第4问——表提案/输出确认里内嵌"标识+时间窗口"
# 子问被整段丢弃，agent 被拒表后拿不到口径）
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbeddedCaliberCompositeReplyV53(TestTableConfirmThirdRejectionAutonomy):
    def test_third_reject_embedded_caliber_pick_from_options(self):
        """q54 形态（选项含 golden 标识）：第3拒自主交付话术 + 标识选定 + 时间口径。"""
        q = (
            "用户标识字段用 user_id 还是 imei 需要确认，时间窗口口径也需要确认。"
            "基于业务特征我自主决策选择 db.ods_wrong_c 作为数据源，是否可以？"
        )
        reply = self._run(q, self._two_prior_rejection_messages())
        assert reply is not None
        assert "第三次" in reply
        assert "自主选择" in reply
        assert "用户标识使用 user_id" in reply
        assert "时间口径" in reply

    def test_third_reject_embedded_caliber_concept_redirect(self):
        """q54 真实录形态（选项 uid/imei 不含 golden 标识）：概念粒度转向 + 时间口径。"""
        q = (
            "用户标识字段（uid 还是 imei）和时间窗口口径需要用户确认。"
            "我自主决策选择 db.ods_wrong_c 作为数据源。"
        )
        reply = self._run(q, self._two_prior_rejection_messages())
        assert reply is not None
        assert "第三次" in reply
        assert "账号ID" in reply  # golden user_id 不在选项中 → 概念粒度，不报字段名
        assert "时间口径" in reply
        assert "user_id" not in reply  # 泄漏防线保持

    def test_third_reject_pure_table_proposal_unchanged(self):
        """回归锁：纯表提案（无口径词）第3拒话术不追加口径段（v52 行为保持）。"""
        reply = self._run(
            "源表候选：db.ods_wrong_c，是否可以？", self._two_prior_rejection_messages()
        )
        assert reply is not None
        assert "第三次" in reply
        assert "我一并答复" not in reply
        assert "时间口径" not in reply
        assert "用户标识" not in reply

    def test_first_rejection_embedded_caliber_answered(self):
        """首轮拒绝 + 内嵌口径：标准拒绝模板 + 口径段一并给。"""
        q = (
            "源表选择 db.ods_wrong_b：用户标识字段用 user_id 还是 imei？"
            "时间窗口是否包含当天？"
        )
        reply = self._run(q, None)
        assert reply is not None
        assert "不是我想要的数据来源" in reply
        assert "用户标识使用 user_id" in reply
        assert "时间口径" in reply

    def test_confirm_path_embedded_caliber_answered(self):
        """确认路径 + 内嵌口径：确认话术 + 口径段（不再为口径另烧一轮提问预算）。"""
        q = (
            "源表确认使用 biads.dws_evt_fct_dm：用户标识字段用 user_id 还是 imei？"
            "时间窗口是否包含当天？"
        )
        reply = self._run(q, None)
        assert reply is not None
        assert ("符合我的需求" in reply) or ("数据源这块可以定了" in reply)
        assert "用户标识使用 user_id" in reply


class TestOutputEmbeddedCaliberV53:
    GOLDEN = (
        "INSERT OVERWRITE TABLE t PARTITION(pt_d='$date') "
        "SELECT user_id, app_type, COUNT(*) AS cnt FROM db.dwd_event_dm "
        "WHERE pt_d>='${start_time,-30,yyyyMMdd}' AND pt_d<='$date' "
        "GROUP BY user_id, app_type"
    )

    def _run(self, hil_question: str, golden_sql: str | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _answer_output_schema_programmatic

        return _answer_output_schema_programmatic(golden_sql or self.GOLDEN, hil_question)

    def test_grain_reply_embedded_time_window_q60(self):
        """q60 形态：粒度转向罐头 + 内嵌时间窗口/标识子问一并答复。"""
        q = (
            "输出按 user_id 和 app_type 聚合的汇总表对吗？"
            "用户标识 user_id 是否合适？时间窗口源表1年是否满足'过去365天不含当天'？"
        )
        reply = self._run(q)
        assert reply is not None
        assert "汇总结果" in reply
        assert "时间口径" in reply
        assert "用户标识使用 user_id" in reply

    def test_grain_reply_pure_structure_unchanged(self):
        """回归锁：纯输出结构问（无口径词）不追加口径段（v52 行为保持）。"""
        reply = self._run("输出结构确认：我计划输出明细宽表")
        assert reply is not None
        assert "汇总结果" in reply
        assert "我一并答复" not in reply
        assert "时间口径" not in reply
        assert "用户标识" not in reply


# ─────────────────────────────────────────────────────────────────────────────
# P2 事后防线：LLM 兜底回复的假确认子句审查与改写
# ─────────────────────────────────────────────────────────────────────────────


class TestVetLlmConfirmation:
    GOLDEN_USER = (
        "SELECT user_id, app_id, COUNT(*) AS cnt FROM biads.dws_evt_fct_dm "
        "WHERE pt_d='$date' GROUP BY user_id, app_id"
    )

    def _vet(self, answer: str, hil_question: str, golden_sql: str | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        return _vet_llm_confirmation(answer, golden_sql or self.GOLDEN_USER, hil_question, "AMBIGUITY_CLARIFY", "开发序列特征")

    def test_english_identifier_false_confirm_corrected(self):
        """golden=user_id 而 LLM 顺着 agent 确认 device_id → 概念纠正，不报 golden 字段名。"""
        vetted = self._vet(
            "使用 device_id 作为用户标识，按设备维度统计。",
            "用户标识用 device_id 还是 oaid？",
        )
        assert "账号ID" in vetted
        assert "设备ID" in vetted
        assert "user_id" not in vetted
        assert "纠正" in vetted

    def test_chinese_concept_false_confirm_corrected(self):
        """无英文字段名的概念式假确认（"按设备维度统计，用设备ID。"）同样被纠正。"""
        vetted = self._vet(
            "按设备维度统计，用设备ID。",
            "用户标识按什么维度统计？",
        )
        assert "账号ID" in vetted
        assert "设备ID" in vetted
        assert "user_id" not in vetted

    def test_correct_identifier_confirm_kept(self):
        """确认的标识与 golden 一致 → 原文保留。"""
        answer = "使用 user_id 作为用户标识字段。"
        assert self._vet(answer, "用户标识用 device_id 还是 user_id？") == answer

    def test_question_clause_not_vetted(self):
        """疑问子句（"用 device_id 还是 user_id？"）不是确认，不改写。"""
        answer = "请确认：用 device_id 还是 user_id？"
        assert self._vet(answer, "用户标识用哪个？") == answer

    def test_negation_clause_not_vetted(self):
        """否定子句（"不建议用 device_id"）不是确认，不改写。"""
        answer = "不建议用 device_id 作为用户标识。"
        assert self._vet(answer, "用户标识用 device_id 还是 oaid？") == answer

    def test_consecutive_false_confirms_single_redirect(self):
        """多个冲突子句 → 纠正语只出现一次，其余冲突子句丢弃。"""
        vetted = self._vet(
            "按设备维度统计，用设备ID。",
            "用户标识按什么维度统计？",
        )
        assert vetted.count("纠正") == 1
        assert "用设备ID" not in vetted

    def test_table_pronoun_all_wrong_corrected(self):
        """agent 候选全错时 LLM 仍"同意使用该表" → 替换为拒绝模板。"""
        golden = "SELECT a FROM biads.dws_right_dm WHERE pt_d='$date'"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        vetted = _vet_llm_confirmation(
            "同意使用该表。",
            golden,
            "源表用 db.ods_wrong_a 可以吗？",
            "AMBIGUITY_CLARIFY",
            "开发序列特征",
        )
        assert "不是我想要的数据来源" in vetted
        assert "同意使用该表" not in vetted
        assert "dws_right_dm" not in vetted  # 不泄漏 golden 表名

    def test_table_pronoun_partial_correct_kept(self):
        """agent 候选部分正确时代词指代不明 → 保守不改写。"""
        golden = "SELECT a FROM biads.dws_right_dm WHERE pt_d='$date'"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        answer = "同意使用该表。"
        vetted = _vet_llm_confirmation(
            answer,
            golden,
            "源表用 biads.dws_right_dm 还是 db.ods_wrong_a？",
            "AMBIGUITY_CLARIFY",
            "开发序列特征",
        )
        assert vetted == answer

    def test_correct_table_confirm_kept(self):
        """确认的表在 golden 中 → 原文保留。"""
        golden = "SELECT a FROM biads.dws_right_dm WHERE pt_d='$date'"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        answer = "确认使用 biads.dws_right_dm。"
        assert _vet_llm_confirmation(answer, golden, "源表用 biads.dws_right_dm？", "TABLE_CONFIRM") == answer

    def test_no_golden_signal_unchanged(self):
        """golden 无标识字段且无源表 → 防线不动原文。"""
        golden = "SELECT COUNT(*) FROM t"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        answer = "使用 device_id 作为用户标识。"
        assert _vet_llm_confirmation(answer, golden, "用户标识？", "AMBIGUITY_CLARIFY") == answer

    def test_join_key_clause_not_vetted(self):
        """join key 说明子句（app_id 关联）不属标识体系 vet 范围，不改写。"""
        answer = "用候选A，通过 app_id 关联补充二级分类。"
        vetted = self._vet(answer, "用哪个方案补齐分类？")
        assert vetted == answer

    def test_unprefixed_golden_vet_active(self):
        """MR453 复核 P1(c)：无 db 前缀 golden SQL 下假确认审查仍拦错表（此前静默放行）。"""
        golden = "select a from dwd_user_event"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        vetted = _vet_llm_confirmation(
            "确认用 ods_wrong_a 这张表。",
            golden,
            "源表用 ods_wrong_a 可以吗？",
            "TABLE_CONFIRM",
            "开发序列特征",
        )
        assert "不是我想要的数据来源" in vetted
        assert "dwd_user_event" not in vetted  # 不泄漏 golden 表名


# ─────────────────────────────────────────────────────────────────────────────
# MR453 复核 P1：源表名裸名/限定名统一（提取 + 防泄漏）
# ─────────────────────────────────────────────────────────────────────────────


class TestBareTableNameExtraction:
    def _golden(self, golden_sql: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _extract_golden_tables_set

        return _extract_golden_tables_set(golden_sql)

    def test_bare_names_extracted_when_no_db_prefix(self):
        """golden SQL 不带 db 前缀时仍提取到裸表名，三防线不静默失效。"""
        assert self._golden("select a from dwd_user_event join dim_b on 1=1") == [
            "dwd_user_event",
            "dim_b",
        ]

    def test_ads_prefix_bare_name_extracted(self):
        """ads_ 层裸表名同样提取（v3.6 实测 golden 源表在 biads.ads_* 层）。"""
        assert self._golden("select a from ads_evt_fct_dm") == ["ads_evt_fct_dm"]

    def test_qualified_names_still_extracted(self):
        """全限定名正常提取，且裸名已是全名后缀时不重复入集。"""
        assert self._golden("SELECT a FROM dwd.dwd_a JOIN dim.dim_b ON 1=1") == [
            "dwd.dwd_a",
            "dim.dim_b",
        ]
        # 混写：同一张表以限定名+裸名各出现一次 → 只保留一个
        assert self._golden("select a from dwd.dwd_a join dwd_a on 1=1") == ["dwd.dwd_a"]

    def test_cte_name_not_polluted(self):
        """CTE 名（无数仓层前缀）不进 golden 集。"""
        golden = "WITH tmp AS (SELECT 1) SELECT a FROM dwd.dwd_a JOIN tmp ON 1=1"
        assert self._golden(golden) == ["dwd.dwd_a"]

    def test_agent_bare_ads_table_extracted(self):
        """agent 口语裸提 ads_ 层表同样能提取（与 golden 侧前缀列表对齐）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _extract_agent_tables

        assert _extract_agent_tables("源表用 ads_evt_fct_dm 可以吗？") == ["ads_evt_fct_dm"]


class TestSanitizeLlmReplyLeak:
    GOLDEN = "SELECT a FROM dwd.dwd_user_event JOIN dim.dim_b ON 1=1"

    def _sanitize(self, answer: str, hil_question: str, action: str = "OUTPUT_SCHEMA_PREVIEW"):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _sanitize_llm_reply_leak

        return _sanitize_llm_reply_leak(answer, self.GOLDEN, hil_question, action)

    def test_bare_name_leak_blocked(self):
        """MR453 复核 P1(b)：LLM 裸名报出 agent 未提的 golden 表 → 拦截（此前放行）。"""
        sanitized = self._sanitize("我建议用 dwd_user_event 这张表", "输出结构是否满足需求？")
        assert "dwd_user_event" not in sanitized
        assert "符合我需求的数据表" in sanitized

    def test_qualified_echo_of_agent_bare_mention_not_overblocked(self):
        """agent 裸提、LLM 全名复述 → 豁免不算泄漏（此前误拦走兜底）。"""
        answer = "确认使用 dwd.dwd_user_event 这张表。"
        sanitized = self._sanitize(answer, "源表确认：使用 dwd_user_event 可以吗？")
        assert sanitized == answer

    def test_table_confirm_leak_replaced_by_template(self):
        """TABLE_CONFIRM 的泄漏整体替换为候选模板（既有行为保持）。"""
        sanitized = self._sanitize(
            "我建议用 dwd.dwd_user_event 这张表", "输出结构确认", action="TABLE_CONFIRM"
        )
        assert "数据源选择我需要你先做功课" in sanitized
        assert "dwd_user_event" not in sanitized


class TestChineseGluedTableNames:
    """中文与表名间无空格（"候选表biads.ads_x…dm已按"）时 \b 失效，用 ASCII 边界修复。

    v3.6 两题实测（q42 Q1）：表名前后紧贴中文导致提取为空，纯选表提问被
    误判 AMBIGUITY_CLARIFY，拿到答非所问的口径回复。
    """

    Q42_Q1 = (
        "Initial阶段需要确认数据源表选择。候选表biads.ads_agrec_adv_common_user_ubr_"
        "clickapp_list_1y_plus_dm已按imei+hour_buket聚合，且列表字段已保留集合前7位，"
        "与需求高度匹配。需要用户确认是否使用该表。"
    )

    def test_glued_qualified_name_extracted(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _extract_agent_tables

        assert _extract_agent_tables(self.Q42_Q1) == [
            "biads.ads_agrec_adv_common_user_ubr_clickapp_list_1y_plus_dm"
        ]

    def test_glued_bare_name_extracted(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _extract_agent_tables

        assert _extract_agent_tables("我打算用dwd_user_event这张表") == ["dwd_user_event"]

    def test_table_proposal_question_classifies_table_confirm(self):
        """修复后纯选表提问（含 imei 口径词干扰）保持 TABLE_CONFIRM。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        assert classify_hitl_action(self.Q42_Q1) == "TABLE_CONFIRM"

    def test_caliber_question_with_confirmed_ref_stays_ambiguity(self):
        """Mid 阶段口径追问（已确认引用+口径决策）仍转 AMBIGUITY_CLARIFY。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = (
            "Mid阶段需要确认用户标识字段和时间窗口口径。用户已确认imei作为用户标识，"
            "源表已内含近1年窗口，按单日分区读取。需要用户确认这两个口径。"
        )
        assert classify_hitl_action(q) == "AMBIGUITY_CLARIFY"

    def test_glued_leak_still_blocked(self):
        """LLM 回复中表名紧贴中文时泄漏检测不失效，仍能拦截替换。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _sanitize_llm_reply_leak

        golden = "SELECT a FROM biads.dwd_user_event JOIN biads.dim_b ON 1=1"
        sanitized = _sanitize_llm_reply_leak(
            "我建议使用dwd_user_event这张表来完成", golden, "输出结构确认", "AMBIGUITY_CLARIFY"
        )
        assert "dwd_user_event" not in sanitized
        assert "符合我需求的数据表" in sanitized

    def test_glued_agent_mention_exemption(self):
        """agent 紧贴中文提到的表 → LLM 复述该表不算泄漏（豁免不因 glue 失效）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _sanitize_llm_reply_leak

        golden = "SELECT a FROM biads.dwd_user_event JOIN biads.dim_b ON 1=1"
        answer = "确认使用dwd_user_event这张表没问题。"
        sanitized = _sanitize_llm_reply_leak(
            answer, golden, "源表确认：候选是dwd_user_event，可以用吗？", "TABLE_CONFIRM"
        )
        assert sanitized == answer


# ─────────────────────────────────────────────────────────────────────────────
# v41 L1b-heavy + L4：拒绝写扩池标记 / 确认写已确认源表清单
# ─────────────────────────────────────────────────────────────────────────────


class TestL1bHeavyMarkerAndConfirmedTables:
    """v41：TABLE_CONFIRM 程序化判定的两个 workspace 副作用。

    - 拒绝（wrong 非空）→ 写 .semantic/hitl_expand_retrieve_request.json（L1b-heavy）；
    - 确认（wrong 空，部分/全部）→ 累积写 .memory/hitl_confirmed_tables.json（L4）。
    - workspace_root=None（resolver 无 workspace 上下文）时全部静默 no-op，不炸。
    """

    GOLDEN = "SELECT a.id FROM db.dwd_a_dm t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"

    def _workspace(self) -> tuple[Any, Any, Any, Any]:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name)
        return tmp, path, path / ".semantic", path / ".memory"

    def test_wrong_table_writes_expand_marker(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表候选：db.dwd_a_dm 和 db.ads_wrong_dm",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "不是我想要的数据来源" in reply
            marker_path = semantic_dir / "hitl_expand_retrieve_request.json"
            assert marker_path.is_file()
            import json

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            assert marker["trigger"] == "table_confirm_rejected"
            assert marker["rejected_tables"] == ["db.ads_wrong_dm"]
            assert marker["rejection_round"] == 1

    def test_escalated_rejection_also_writes_marker(self):
        """连续选错的升级答复（特征级指引）同样写标记——两路拒绝共用写点。"""
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )
        from langchain_core.messages import ToolMessage

        prior_reject = ToolMessage(
            content="db.ads_wrong1_dm 从我任务的需求看不是我想要的数据来源。请你基于元数据再检索一下",
            name="request_human_feedback",
            tool_call_id="call_prev",
        )
        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表候选：db.ads_wrong2_dm，是否可以？",
                "开发序列特征",
                [prior_reject],
                workspace_root=root,
            )
            # 升级答复命中（golden 带层级前缀 dwd_ 可解析特征）
            assert "仍然不是我想要的数据来源" in reply
            marker = json.loads(
                (semantic_dir / "hitl_expand_retrieve_request.json").read_text(encoding="utf-8")
            )
            assert marker["rejected_tables"] == ["db.ads_wrong2_dm"]
            assert marker["rejection_round"] == 2

    def test_full_confirm_records_confirmed_tables(self):
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, _, memory_dir = self._workspace()
        with tmp:
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表确认：使用 db.dwd_a_dm 和 db.dwd_b_dm，可以吗？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "确认：就用" in reply
            record = json.loads((memory_dir / "hitl_confirmed_tables.json").read_text(encoding="utf-8"))
            assert record["confirmed_tables"] == ["db.dwd_a_dm", "db.dwd_b_dm"]
            assert record["rounds"] == 1

    def test_partial_confirm_records_and_accumulates(self):
        """部分确认记录本轮全对表；两轮确认 union 累积、rounds 递增。"""
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, _, memory_dir = self._workspace()
        with tmp:
            _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表确认：使用 db.dwd_a_dm，是否可以？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表确认：使用 db.dwd_a_dm 和 db.dwd_b_dm，可以吗？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            record = json.loads((memory_dir / "hitl_confirmed_tables.json").read_text(encoding="utf-8"))
            assert set(record["confirmed_tables"]) == {"db.dwd_a_dm", "db.dwd_b_dm"}
            assert record["rounds"] == 2

    def test_rejection_round_counts_prior_rejections(self):
        """rejection_round = 已拒轮数 + 1（标记内容可追溯拒绝进度）。"""
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )
        from langchain_core.messages import ToolMessage

        prior = ToolMessage(
            content="x 不是我想要的数据来源。请你基于元数据再检索一下",
            name="request_human_feedback",
            tool_call_id="call_prev",
        )
        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            # wrong 非空但 correct 也非空 → 标准拒绝（不升级），round 仍计 2
            _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表候选：db.dwd_a_dm 和 db.ads_wrong_dm",
                "开发序列特征",
                [prior],
                workspace_root=root,
            )
            marker = json.loads(
                (semantic_dir / "hitl_expand_retrieve_request.json").read_text(encoding="utf-8")
            )
            assert marker["rejection_round"] == 2

    def test_partial_correct_rejection_records_correct_tables(self):
        """v44 L4 缺口修复：部分正确轮（correct+wrong 皆非空）的正确表同样累积写 L4。

        v43_r1 实证 12/17 任务有口头锁定（"这几张表符合我的需求，就此定下来"）
        但无 .memory/hitl_confirmed_tables.json 记录——delivery_guard 的确认贯穿
        核对对这些表失明。修复后拒绝轮的 correct 部分也要进记录。
        """
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, semantic_dir, memory_dir = self._workspace()
        with tmp:
            # 第一轮：部分正确（correct=db.dwd_a_dm, wrong=db.ads_wrong_dm）→ 拒绝答复
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表候选：db.dwd_a_dm 和 db.ads_wrong_dm",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "这几张表符合我的需求" in reply
            record = json.loads((memory_dir / "hitl_confirmed_tables.json").read_text(encoding="utf-8"))
            assert record["confirmed_tables"] == ["db.dwd_a_dm"]
            assert record["rounds"] == 1
            # 拒绝标记照常写入（部分正确不豁免扩池标记）
            assert (semantic_dir / "hitl_expand_retrieve_request.json").is_file()
            # 第二轮：全对确认 → union 累积
            _answer_table_confirm_programmatic(
                self.GOLDEN,
                "源表确认：使用 db.dwd_a_dm 和 db.dwd_b_dm，可以吗？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            record = json.loads((memory_dir / "hitl_confirmed_tables.json").read_text(encoding="utf-8"))
            assert set(record["confirmed_tables"]) == {"db.dwd_a_dm", "db.dwd_b_dm"}
            assert record["rounds"] == 2

    def test_none_workspace_is_silent_noop(self):
        """workspace_root=None（无 workspace 上下文）：判定照常，副作用静默跳过。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        reply = _answer_table_confirm_programmatic(
            self.GOLDEN, "源表候选：db.ads_wrong_dm", "开发序列特征", None, workspace_root=None
        )
        assert "不是我想要的数据来源" in reply

    def test_resolve_agent_workspace_root_prefers_state(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _resolve_agent_workspace_root,
        )

        tmp = tempfile.TemporaryDirectory()
        with tmp:
            root = Path(tmp.name)
            state = {"workspace": str(root)}
            runtime = MagicMock()
            assert _resolve_agent_workspace_root(state, runtime) == root.resolve()
            # state 无 workspace 时回退 runtime.workspace_dir
            runtime.workspace_dir = str(root)
            assert _resolve_agent_workspace_root({}, runtime) == root.resolve()
            # 两者皆无 → None（不能 MagicMock：getattr 会自动造出 Mock 属性）
            class _BareRuntime:
                pass

            assert _resolve_agent_workspace_root({}, _BareRuntime()) is None


class TestV45RejectedLedgerAndPoolHint:
    """v45：拒绝副作用的两层扩展。

    - P0-1①：被拒表持久台账 .memory/hitl_rejected_tables.json（跨轮 union +
      rounds 递增 + 近变体口径契约 want_tokens/want_phrases）——loader 基础
      池过滤与 mr 工具（轮次帽/口径对照）的消费数据源；
    - P0-2：拒绝答复追加池内表名 token 对照段（口径 token 对照 agent 已检索
      池，指名道姓给"优先核对"候选，q55 型死局的 agent 侧根因修复）。
    """

    # golden 含 xtr/list/stat 口径 token，被拒表与之近变体（Jaccard=2/6=1/3）
    GOLDEN_VARIANT = "SELECT a.id FROM db.dwd_xtr_list_stat t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"

    def _workspace(self) -> tuple[Any, Any, Any, Any]:
        import tempfile
        from pathlib import Path as _P

        tmp = tempfile.TemporaryDirectory()
        path = _P(tmp.name)
        return tmp, path, path / ".semantic", path / ".memory"

    def _write_pool_context(self, semantic_dir: Any, tables: list[str]) -> None:
        import json as _json

        semantic_dir.mkdir(parents=True, exist_ok=True)
        (semantic_dir / "semantic_retrieve_context_fake001.json").write_text(
            _json.dumps({"query": "q", "tables": tables}), encoding="utf-8"
        )

    def test_rejection_writes_rejected_ledger(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, _, memory_dir = self._workspace()
        with tmp:
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.dwd_b_dm 和 db.ads_xtr_list_sample",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "不是我想要的数据来源" in reply
            import json

            record = json.loads((memory_dir / "hitl_rejected_tables.json").read_text(encoding="utf-8"))
            assert record["rejected_tables"] == ["db.ads_xtr_list_sample"]
            assert record["rounds"] == 1
            # 近变体口径契约：golden 独有 token 中有概念映射的（stat）写入口径词
            assert record["want_tokens"] == ["stat"]
            assert record["want_phrases"] == ["统计汇总数据"]

    def test_ledger_unions_across_rejection_rounds(self):
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, _, memory_dir = self._workspace()
        with tmp:
            _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_xtr_list_sample",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_wrong2_dm",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            record = json.loads((memory_dir / "hitl_rejected_tables.json").read_text(encoding="utf-8"))
            assert record["rejected_tables"] == ["db.ads_xtr_list_sample", "db.ads_wrong2_dm"]
            assert record["rounds"] == 2

    def test_far_variant_rejection_writes_ledger_without_tokens(self):
        """非近变体（Jaccard<1/3）：台账照写，但无口径 token（无对照基础）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        import json

        tmp, root, _, memory_dir = self._workspace()
        with tmp:
            _answer_table_confirm_programmatic(
                "SELECT a.id FROM db.dwd_a_dm t1",
                "源表候选：db.zzz_yyy_xxx",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            record = json.loads((memory_dir / "hitl_rejected_tables.json").read_text(encoding="utf-8"))
            assert record["rejected_tables"] == ["db.zzz_yyy_xxx"]
            assert "want_tokens" not in record

    def test_pool_token_hint_names_pool_matches(self):
        """口径 token 对照池内表名：命中的池表（未被拒）指名道姓进提示。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _pool_token_hint,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            self._write_pool_context(
                semantic_dir,
                ["db.dwd_xtr_list_stat_dm", "db.ads_xtr_list_sample", "db.other_dm"],
            )
            hint = _pool_token_hint(
                root,
                ["db.dwd_xtr_list_stat", "db.dwd_b_dm"],
                ["db.ads_xtr_list_sample"],
            )
            assert "为帮你把口径落到实处" in hint
            assert "db.dwd_xtr_list_stat_dm" in hint
            assert "get_table_schema" in hint
            # 被拒表本身不进对照候选
            assert "db.ads_xtr_list_sample_dm" not in hint.replace("db.ads_xtr_list_sample。", "")

    def test_pool_token_hint_empty_without_pool_match(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _pool_token_hint,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            self._write_pool_context(semantic_dir, ["db.other_dm"])
            hint = _pool_token_hint(
                root, ["db.dwd_xtr_list_stat"], ["db.ads_xtr_list_sample"]
            )
            assert hint == ""

    def test_pool_token_hint_empty_for_far_variant(self):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _pool_token_hint,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            self._write_pool_context(semantic_dir, ["db.dwd_xtr_list_stat_dm"])
            hint = _pool_token_hint(root, ["db.dwd_a_dm"], ["db.zzz_yyy_xxx"])
            assert hint == ""

    def test_match_pool_tables_by_tokens_ranking_and_limit(self):
        """命中 token 数降序（golden 型全 token 命中居首）、上限 3、被拒剔除。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _match_pool_tables_by_tokens,
        )

        pool = [
            "db.other_dm",
            "db.dwd_stat_only",
            "db.mid_xtr_list",
            "db.dwd_xtr_list_stat_dm",
            "db.dwd_xtr_list_stat_v2",
            "db.ads_xtr_list_sample",  # 被拒，应剔除
            "db.extra_stat_dm",
        ]
        matches = _match_pool_tables_by_tokens(
            pool, ["xtr", "list", "stat"], ["db.ads_xtr_list_sample"]
        )
        assert len(matches) == 3
        # 全 token 命中的 golden 型排最前（裸名序稳定）
        assert matches[0] == "db.dwd_xtr_list_stat_dm"
        assert all(not m.endswith("ads_xtr_list_sample") for m in matches)
        assert "db.other_dm" not in matches

    def test_rejection_reply_appends_pool_hint(self):
        """完整拒绝流：答复含池内 token 对照段（P0-2 拒绝答复通道）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            self._write_pool_context(semantic_dir, ["db.dwd_xtr_list_stat_dm", "db.ads_xtr_list_sample"])
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_xtr_list_sample，是否可以？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "不是我想要的数据来源" in reply
            assert "为帮你把口径落到实处" in reply
            assert "db.dwd_xtr_list_stat_dm" in reply

    def test_rejection_reply_without_pool_hint_when_no_match(self):
        """池内无口径命中：答复回落原行为（无对照段，不硬造）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            self._write_pool_context(semantic_dir, ["db.other_dm"])
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_xtr_list_sample，是否可以？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "不是我想要的数据来源" in reply
            assert "为帮你把口径落到实处" not in reply

    def test_escalated_rejection_also_appends_pool_hint(self):
        """升级答复（连续选错）同样带口径 token 对照段——两路拒绝共用 hint_tail。"""
        import json as _json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )
        from langchain_core.messages import ToolMessage

        prior_reject = ToolMessage(
            content="db.ads_xtr_list_sample 从我任务的需求看不是我想要的数据来源。请你基于元数据再检索一下",
            name="request_human_feedback",
            tool_call_id="call_prev",
        )
        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            self._write_pool_context(semantic_dir, ["db.dwd_xtr_list_stat_dm"])
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_xtr_list_v2，是否可以？",
                "开发序列特征",
                [prior_reject],
                workspace_root=root,
            )
            # 升级答复或标准拒绝两路之一，均应带对照段（池内已有命中）
            if "仍然不是我想要的数据来源" in reply:
                assert "为帮你把口径落到实处" in reply
            else:
                assert "不是我想要的数据来源" in reply
                assert "为帮你把口径落到实处" in reply


# ─────────────────────────────────────────────────────────────────────────────
# v47：UNA 误路由修正（收敛确认→TABLE_CONFIRM / 列构成确认→OUTPUT）
#      + OUTPUT 纯粒度问退 LLM persona
# ─────────────────────────────────────────────────────────────────────────────


class TestTableProposalUnaOverride:
    """v47 收敛确认误路由（q55 Q3 铁例）："候选池中最接近的表…请您确认"零关键词命中落 UNA。"""

    Q55_Q3 = (
        "检索轮次已超限，需要收敛交付。基于已有候选池选择最接近的表向用户确认。\n"
        "根据您提供的口径指引（应用维度、列表清单、统计汇总、xTR预估口径），我核对了您建议的表 "
        "biads.ads_agrec_adv_common_scenes_list_app_statistic_info_online_dm，"
        "发现它没有 app_id 字段，无法产出\"唯一应用ID序列\"。\n\n"
        "在未被拒绝的候选池中，我找到一张最接近的表：**biads.ads_agrec_stat_v2_dm**，请您确认是否接受。"
    )

    def _classify(self, q: str) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def test_q55_q3_convergence_confirm_rerouted(self):
        """收敛确认问改判 TABLE_CONFIRM → 按 golden 给确认锁/拒源重检索（可执行）。"""
        assert self._classify(self.Q55_Q3) == "TABLE_CONFIRM"

    def test_no_confirm_verbs_stays_una(self):
        """提案表述但无确认/选择/接受动词 → 不改判（非确认类陈述）。"""
        assert self._classify("候选池里的表字段我已经列好了") == "UNA"

    def test_post_completion_courtesy_stays_una(self):
        """q50 Q2 铁例：交付后礼节性提问不含提案表述，保持 UNA。"""
        q = (
            "任务已完成。所有步骤均已执行完毕，交付物已生成。\n\n"
            "请问您是否需要我展示SQL内容，或者您对交付结果有其他修改意见？"
        )
        assert self._classify(q) == "UNA"

    def test_classified_base_not_affected(self):
        """base=TABLE_CONFIRM 的问题不受影响（override 只看 UNA 侧）。"""
        q = "数据源表选择：候选A db.t1 还是候选B db.t2？请您确认。"
        assert self._classify(q) == "TABLE_CONFIRM"

    def test_q64_interpretation_ask_stays_una(self):
        """回放门 pin（q64）："我倾向于理解为…请确认这个理解是否正确"是口径
        歧义问不是表提案——宽信号"我倾向"（_TABLE_PROPOSAL_RE）会误改判，
        收敛确认标记只认表为确认对象的签名。"""
        q = (
            "关于\"卸载时间序列前7位\"的口径，我倾向于理解为：从卸载时间列表中按时间排序"
            "取前7个卸载时间值。请确认：1）这个理解是否正确？2）或者您是指时间字符串本身"
            "截取前7个字符？"
        )
        assert self._classify(q) == "UNA"


class TestSchemaDerivationUnaOverride:
    """v47 列构成确认误路由（q47 Q4 铁例）："输出schema"无空格 / "列构成"≠关键词"列结构"。"""

    Q47_Q4 = (
        "用户说期望输出11列，需要确认列构成。这是最后一次提问机会，"
        "需要确认最关键的输出schema问题。\n"
        "根据您期望的11列，我推导了以下列构成方案，请您确认：\n\n"
        "**搜索关键词侧（6列）：**\n1. search_keyword — 搜索关键词\n2. kw_second_cate — 搜索词二级类目预测"
    )

    def _classify(self, q: str) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def test_q47_q4_schema_derivation_rerouted(self):
        """列构成确认问改判 OUTPUT_SCHEMA_PREVIEW → 拿"期望输出 N 列"程序化答复。"""
        assert self._classify(self.Q47_Q4) == "OUTPUT_SCHEMA_PREVIEW"

    def test_column_plan_variant_rerouted(self):
        assert self._classify("我推导了新的列方案，请您帮我看下") == "OUTPUT_SCHEMA_PREVIEW"

    def test_no_markers_stays_una(self):
        assert self._classify("直接把 golden SQL 给我看看") == "UNA"

    def test_spaced_keyword_still_classified_by_keywords(self):
        """带空格"输出 schema"走既有关键词路径（历史行为不变），与无空格变体殊途同归。"""
        assert self._classify("请确认输出 schema 是否正确") == "OUTPUT_SCHEMA_PREVIEW"


class TestOutputPureGrainDeferToLlm:
    """v47 OUTPUT 分支 3 细化：纯主体/主键/粒度问（golden 无 GROUP BY 特征）退 LLM persona。"""

    GOLDEN_NO_GRAIN = (
        "INSERT OVERWRITE TABLE db.t_dm PARTITION(pt_d='$date') "
        "SELECT search_keyword, kw_second_cate, kw_third_cate, tag_text FROM agsearch.ads_q_dm "
        "WHERE pt_d='$date'"
    )

    def _run(self, hil_question: str, golden_sql: str | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_output_schema_programmatic,
        )

        return _answer_output_schema_programmatic(golden_sql or self.GOLDEN_NO_GRAIN, hil_question)

    def test_pure_grain_ask_defers_to_llm(self):
        """q47 Q8 型：问主体/主键、golden 无 GROUP BY → None 退 persona
        （OUTPUT persona 角色约束本就聚焦"一行代表什么/按什么粒度聚合"）。"""
        q = "我需要确认输出表的主体（主键）是什么：候选A 搜索词为主键，每行一个搜索词；候选B 用户为主键。"
        assert self._run(q) is None

    def test_mixed_grain_and_col_ask_keeps_programmatic(self):
        """q50 型（主键+列构成 混合问）：保留列数程序化答复——PASS 路径不动。"""
        q = "需要确认输出表的主键和列构成：主键是 random_weight_key 字符串吗？"
        reply = self._run(q)
        assert reply is not None
        assert "期望输出 4 列" in reply

    def test_plain_col_ask_keeps_programmatic(self):
        reply = self._run("输出列数确认一下可以吗")
        assert reply is not None
        assert "期望输出 4 列" in reply


# ─────────────────────────────────────────────────────────────────────────────
# v53b 答非所问双病灶修复：用户变体请求形剥离 + WITH-CTE golden 输出列数 + 无标识 golden 粒度转向
# （v52_r2 q34 三连实录：源表提案问/标识选择问被改判 AMBIGUITY 拿时间口径答复、
#   17 列正确提案被"纠正"成首 CTE 的 4 列——agent 被带偏交付 4 列 KV 结构 FAIL）
# ─────────────────────────────────────────────────────────────────────────────


class TestUserVariantRequestFormV53b(TestMixedQuestionPolarityV48):
    """v53b：确认请求形的"用户"变体（v48 只剥"您"）。

    v52_r2 q34 Q1 实录："需要用户确认使用哪个作为主源"——"用户"二字挡住
    v48 请求形剥离，残余"确认使用"再度极性反转为已确认引用，全套表提案
    （我倾向选择 db.table + 数据源表选择字样）被 _mixed_question_override
    改判 AMBIGUITY，表判定无人应答，agent 把时间口径答复误读为默认同意。
    """

    Q34_Q1 = (
        "Initial阶段：需要确认数据源表选择。候选源表涉及单框CVR宽表与推广回传留存汇总表，"
        "需要用户确认使用哪个作为主源。\n"
        "我倾向选择 **biads.ads_agalg_userid_instance_single_cvr_common_sample_dm**（单框CVR宽表）"
        "作为主数据源表。理由：直接包含1、3、7、14、30天共5个时间窗口的次留数量、下载总数、次留率。"
    )

    Q34_Q2 = (
        "Mid阶段：需要确认用户标识字段。该表有多个标识字段（device_id/user_id/did），"
        "需要用户确认使用哪个作为用户标识来聚合计算各应用的指标。\n"
        "关于源表选择，我理解您已默认同意使用 **biads.ads_agalg_userid_instance_single_cvr_common_sample_dm**"
        "作为主源表，同时补充了时间口径说明。我倾向使用 **device_id**（设备ID）。"
    )

    def test_user_variant_request_form_not_confirmed_reference(self):
        """请求极性"需要/要/请/麻烦+用户+确认(使用)"不是已确认引用。"""
        assert not self._confirmed_refs("需要用户确认使用哪个作为主源")
        assert not self._confirmed_refs("请用户确认数据源表选择")
        assert not self._confirmed_refs("要用户确认后再推进")
        assert not self._confirmed_refs(self.Q34_Q1)

    def test_true_user_reference_forms_still_confirmed(self):
        """"用户已确认/用户确认使用过"陈述形（带"已"或无请求前缀）仍是真引用。"""
        assert self._confirmed_refs("用户已确认安装源表，现在确认标识口径")
        assert self._confirmed_refs("用户确认使用方案A了，无需再问")

    def test_q34_q1_table_proposal_stays_table_confirm(self):
        """铁例 pin：q34 Q1 源表提案 + 用户变体请求形 + 时间窗口子问 → TABLE_CONFIRM
        （拿 golden-grounded 表判定/拒绝，而非时间口径独占答复）。"""
        assert self._classify(self.Q34_Q1) == "TABLE_CONFIRM"

    def test_q34_q2_identifier_ask_not_confirmed_reference(self):
        """q34 Q2：标识选择问 + 源表已默认同意（真引用形不受影响）+
        用户变体请求形 → 不再因残余"确认使用"判已确认引用整问改判。"""
        assert not self._confirmed_refs(self.Q34_Q2)


class TestGoldenOutputColumnCountV53b:
    """v53b：WITH-CTE golden 的输出列数取最终输出 SELECT（非首个 CTE）。

    v52_r2 q34 Q3 实录：agent 推导 17 列（2 主键+15 指标，与 golden 输出一致），
    _count_golden_output_columns 取首 CTE（pt_d/app_id/两个聚合列=4 列）回复
    "期望输出 4 列，你计划输出 17 列"——正确提案被带偏，agent 交付 4 列 KV 结构。
    q48/q51/q58 同型（首 CTE 恰 5 列 vs 真输出 4/11/9 列）。
    """

    GOLDEN_WITH_CTE = (
        "WITH retention_info AS (\n"
        "    SELECT pt_d, app_id, SUM(IF(retention_label='1',1,0)) AS retention_cnts,\n"
        "        COUNT(1) AS retention_dl_cnts\n"
        "    FROM biads.ads_evt_dm WHERE pt_d >= '${start_time,-30,yyyyMMdd}' AND pt_d <= '${date}'\n"
        "    GROUP BY pt_d, app_id\n"
        ")\n"
        "INSERT OVERWRITE TABLE biads.dws_out_dm PARTITION(pt_d='${date}')\n"
        "SELECT all_app.app_id, app_info.app_cn_name,\n"
        "    t1.retention_cnts AS retention_cnts_1_day, t1.retention_dl_cnts AS retention_dl_cnts_1_day,\n"
        "    t1.cvr AS retention_cvr_1_day, t3.retention_cnts AS retention_cnts_3_day,\n"
        "    t3.retention_dl_cnts AS retention_dl_cnts_3_day, t3.cvr AS retention_cvr_3_day,\n"
        "    t7.retention_cnts AS retention_cnts_7_day, t7.retention_dl_cnts AS retention_dl_cnts_7_day,\n"
        "    t7.cvr AS retention_cvr_7_day\n"
        "FROM (SELECT DISTINCT app_id FROM retention_info) all_app\n"
        "LEFT JOIN (SELECT app_id, 1 AS retention_cnts, 2 AS retention_dl_cnts, 0.5 AS cvr\n"
        "    FROM retention_info WHERE pt_d='${date}' GROUP BY app_id) t1 ON all_app.app_id = t1.app_id\n"
        "LEFT JOIN (SELECT app_id, 1 AS retention_cnts, 2 AS retention_dl_cnts, 0.5 AS cvr\n"
        "    FROM retention_info GROUP BY app_id) t3 ON all_app.app_id = t3.app_id\n"
        "LEFT JOIN (SELECT app_id, 1 AS retention_cnts, 2 AS retention_dl_cnts, 0.5 AS cvr\n"
        "    FROM retention_info GROUP BY app_id) t7 ON all_app.app_id = t7.app_id"
    )  # 输出 SELECT = 11 列（2 主键 + 3 窗口 × 3 指标）；首 CTE = 4 列

    GOLDEN_PLAIN = (
        "INSERT OVERWRITE TABLE db.t_dm PARTITION(pt_d='$date') "
        "SELECT search_keyword, kw_second_cate, kw_third_cate, tag_text FROM agsearch.ads_q_dm "
        "WHERE pt_d='$date'"
    )  # 无 CTE：4 列（回归锁——原实现在此形态本就正确）

    def _count(self, golden_sql: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _count_golden_output_columns,
        )

        return _count_golden_output_columns(golden_sql)

    def test_with_cte_golden_counts_output_select(self):
        """CTE 开头 golden：取 INSERT 后的输出 SELECT（11 列）而非首 CTE（4 列）。"""
        assert self._count(self.GOLDEN_WITH_CTE) == 11

    def test_plain_insert_golden_unchanged(self):
        """回归锁：无 CTE 的 INSERT...SELECT golden 列数不变（4 列）。"""
        assert self._count(self.GOLDEN_PLAIN) == 4

    def test_plain_select_golden_unchanged(self):
        """回归锁：纯 SELECT golden（无 INSERT）取唯一顶层 SELECT。"""
        assert self._count("SELECT a, b, c FROM db.t WHERE pt_d='$date'") == 3

    def test_string_literal_commas_not_counted(self):
        """字符串字面量内的逗号不误计顶层列数。"""
        assert self._count(
            "INSERT OVERWRITE TABLE db.t_dm SELECT a, concat_ws(',', b, c) AS kv FROM db.t2"
        ) == 2

    def test_q34_shape_correction_reply_uses_true_count(self):
        """端到端 pin：q34 Q3 形态（17 列正确提案 + CTE golden）→ 期望输出=输出 SELECT 列数，
        不再把正确提案"纠正"成 CTE 列数。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_output_schema_programmatic,
        )

        q = (
            "Final阶段：需要确认输出表的列数、列名与列顺序是否符合预期。"
            "根据任务原文推导了17列（2主键+15指标），需要用户确认。"
        )
        reply = _answer_output_schema_programmatic(self.GOLDEN_WITH_CTE, q)
        assert reply is not None
        assert "期望输出 11 列" in reply
        assert "你计划输出 17 列" in reply


class TestIdentifierlessGoldenGrainRedirectV53b:
    """v53b：golden 无用户标识字段（应用/站点级聚合）时标识选择问的粒度转向。

    v52_r2 q34 Q2 实录：agent 问 device_id/user_id/did 选哪个，golden 按应用
    聚合无任何标识字段——两条标识分支均因 golden_id 为空跳过，答复被时间
    口径段独占（答非所问）。现按 golden 聚合键给"非用户粒度"转向答复。
    """

    GOLDEN_APP_GRAIN = (
        "INSERT OVERWRITE TABLE biads.dws_out_dm PARTITION(pt_d='${date}') "
        "SELECT app_id, app_cn_name, SUM(cnt) AS total FROM biads.ads_evt_dm "
        "WHERE pt_d='${date}' GROUP BY app_id, app_cn_name"
    )  # 聚合键 app_id/app_cn_name 无标识样字段；无任何 _IDENTIFIER_FIELD_CANDIDATES 字段

    GOLDEN_USER_GRAIN = (
        "INSERT OVERWRITE TABLE db.dws_out_dm PARTITION(pt_d='$date') "
        "SELECT user_id, COUNT(*) AS cnt FROM biads.dws_evt_fct_dm "
        "WHERE pt_d>='${start_time,-30,yyyyMMdd}' AND pt_d<='$date' GROUP BY user_id"
    )  # 回归锁 fixture：golden 标识 user_id 在选项中 → 原选定分支

    def _parts(self, hil_question: str, golden_sql: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _build_caliber_answer_parts,
        )

        return _build_caliber_answer_parts(golden_sql, hil_question)

    def test_app_grain_golden_identifier_ask_gets_grain_redirect(self):
        """标识选择问 + 应用级 golden → "不按用户标识字段聚合"转向段。"""
        q = "该表有多个标识字段（device_id/user_id/did），需要用户确认使用哪个作为用户标识来聚合。"
        seg = self._parts(q, self.GOLDEN_APP_GRAIN)
        assert seg is not None
        assert "不按用户标识字段聚合" in seg
        assert "无需在这几个标识字段中选择" in seg

    def test_exotic_identifier_groupby_guard(self):
        """守卫：golden 聚合键含标识样 token（imei_sha256 型变体，候选清单外）时
        不断言"非用户粒度"（golden 可能用了清单外用户标识）——退原行为。"""
        golden = (
            "INSERT OVERWRITE TABLE db.dws_out_dm PARTITION(pt_d='$date') "
            "SELECT imei_sha256, COUNT(*) AS cnt FROM biads.dws_evt_fct_dm "
            "WHERE pt_d='$date' GROUP BY imei_sha256"
        )
        q = "该表有多个标识字段（device_id/user_id/did），需要确认使用哪个作为用户标识。"
        seg = self._parts(q, golden)
        assert seg is None or "不按用户标识字段聚合" not in seg

    def test_user_grain_golden_unchanged(self):
        """回归锁：golden 标识在 agent 选项中 → 原选定答复不变（v53 行为）。"""
        q = "用户标识字段用 user_id 还是 imei 需要确认。"
        seg = self._parts(q, self.GOLDEN_USER_GRAIN)
        assert seg is not None
        assert "确认：用户标识使用 user_id。" in seg
        assert "不按用户标识字段聚合" not in seg

    def test_no_identifier_mention_no_redirect(self):
        """回归锁：提问未提标识字段（mentioned 空）不触发转向段。"""
        q = "时间窗口是否包含当天需要确认。"
        seg = self._parts(q, self.GOLDEN_APP_GRAIN)
        assert seg is None or "不按用户标识字段聚合" not in seg
