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
"""de_agent suite hitl_auto_resolver hook 的场景单元测试（历史行为基线）。

通用 HITL 基座（调用检测/载荷提取/消息注入/门控）的测试在
``tests/ut/flex/hooks/test_hitl_common.py``；分类机制框架测试在
``test_hitl_classifier.py``。本文件只保留场景行为：golden 匹配、
action 分类口径、程序化答复、泄漏/假确认防线、分类器注册表接线。
"""

from __future__ import annotations

import json
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
        """回归：LLM 生成失败走兜底回复，不再 (None, None) 解包炸 TypeError。"""
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
# 行为测试（提取拼接 / 分类修正 / 时间窗口具体化）
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyHitlAction:
    def test_mixed_question_without_tables_reroutes_to_ambiguity(self):
        """命中 TABLE_CONFIRM 关键词但无候选表名 + 标识关键词 → AMBIGUITY_CLARIFY。"""
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
        """部分确认回复含"不用再问/直接推进"指令；补充候选限定已检索池。"""
        golden = "SELECT a.id FROM db.dwd_a_dm t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"
        reply = self._run(golden, "源表确认：使用 db.dwd_a_dm，是否可以？")
        assert "不用再就这些表来问我" in reply
        assert "推进" in reply
        # 补充数据源候选从已检索池整理，不再引导重新检索
        assert "已检索到的候选" in reply
        assert "不必为此重新检索" in reply
        # 不泄漏 agent 未提到的 golden 表
        assert "dwd_b_dm" not in reply

    def test_mixed_confirm_rejection_directs_proceed_no_research(self):
        """混合提案（部分正确）确认正确表后不再要求换词重检索，改推进优先。

        实测死循环：agent 手握已确认表仍被"换业务词重新检索"指令
        推回检索，4 轮拒绝-重检索后迭代耗尽零交付。
        """
        golden = "SELECT a.id FROM db.dwd_a_dm t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"
        reply = self._run(golden, "源表候选：db.dwd_a_dm 和 db.ads_wrong_dm")
        assert "符合我的需求" in reply
        assert "就此定下来" in reply
        assert "不是我想要的数据来源" in reply
        # 确认存在时不得再出现换词重检索指令
        assert "换用需求里的业务词重新检索" not in reply
        assert "重新检索" not in reply
        # 推进优先 + 替代候选限定在已检索池
        assert "直接基于已确认的表推进" in reply
        assert "已检索到的候选" in reply
        # 不泄漏 agent 未提到的 golden 表
        assert "dwd_b_dm" not in reply

    def test_all_wrong_rejection_still_advises_new_keywords(self):
        """边界锁定：全错提案（无任何确认表）保留换词重检索指令。"""
        golden = "SELECT a.id FROM db.dwd_a_dm t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"
        reply = self._run(golden, "源表候选：db.ads_wrong1_dm 和 db.ads_wrong2_dm")
        assert "不是我想要的数据来源" in reply
        assert "换用需求里的业务词重新检索" in reply
        assert "dwd_a_dm" not in reply
        assert "dwd_b_dm" not in reply

    def test_bare_agent_table_not_judged_wrong(self):
        """MR453 复核 (a)：agent 裸提（省 db 前缀）正确源表不再被判成错表。"""
        golden = "SELECT a FROM dwd.dwd_a JOIN dim.dim_b ON 1=1"
        reply = self._run(golden, "源表确认：使用 dwd_a，是否可以？")
        assert "符合我的需求" in reply
        assert "不是我想要的数据来源" not in reply

    def test_unprefixed_golden_sql_activates_programmatic(self):
        """MR453 复核 (c)：golden SQL 无 db 前缀时程序化判定仍生效（此前退回 LLM）。"""
        golden = "select a from dwd_user_event join dim_b on 1=1"
        reply = self._run(golden, "源表确认：使用 dwd_user_event，是否可以？")
        assert reply is not None
        assert "符合我的需求" in reply


# ─────────────────────────────────────────────────────────────────────────────
# L1 近变体口径判别：被拒表与 golden 表 token 差集 → 客户口吻口径词提示
# 靶例：billing/event 互选、oCPD/oCPC、30d/1y+ubr 窗口口径
# ─────────────────────────────────────────────────────────────────────────────


class TestNearVariantCaliberHint:
    def _hint(self, golden: list[str], rejected: list[str], **kwargs) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _near_variant_caliber_hint,
        )

        return _near_variant_caliber_hint(golden, rejected, **kwargs)

    def test_q51_billing_event_variant(self):
        """billing/event 同族近变体 → 要"事件流水"不要"计费流水"。"""
        hint = self._hint(
            ["biods.ods_hispace_hiad_event_log_hm"],
            ["biods.ods_hispace_hiad_billing_log_hm"],
        )
        assert "事件流水" in hint
        assert "计费流水" in hint
        assert hint.startswith("另外把数据口径")

    def test_q57_ocpc_ocpd_variant(self):
        """oCPD/oCPC 近变体 → 口径词直接说破。"""
        hint = self._hint(
            ["biads.ads_agalg_ocpc_single_frame_calibration_pcvr_siteid_appid_expid_multi_gran_original_period_hm"],
            ["biads.ads_agalg_ocpd_calibration_pcvr_siteid_appid_expid_original_period_new_hm"],
        )
        assert "oCPC" in hint
        assert "oCPD" in hint

    def test_q64_window_and_ubr_variant(self):
        """30d/1y 窗口 + all/ubr 口径差集 → 窗口与 UBR 口径提示。"""
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

    def test_confirmed_exist_tail_checks_pool_not_retrieval(self):
        """确认表存在时尾句指向核对已检索候选，不再指向检索新候选。"""
        hint = self._hint(
            ["biods.ods_hispace_hiad_event_log_hm"],
            ["biods.ods_hispace_hiad_billing_log_hm"],
            confirmed_exist=True,
        )
        assert "核对你已检索到的候选数据源" in hint
        assert "检索候选数据源" not in hint.replace("核对你已检索到的候选数据源", "")

    def test_confirmed_exist_false_keeps_retrieval_tail(self):
        """边界：无确认表（全错）时尾句保留检索指引。"""
        hint = self._hint(
            ["biods.ods_hispace_hiad_event_log_hm"],
            ["biods.ods_hispace_hiad_billing_log_hm"],
        )
        assert "核对、检索候选数据源" in hint

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
        """共因：复述放宽到 300 字，尾部筛选条件不再被 80 字截丢。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _customer_restate_query,
        )

        tail_marker = "尾部筛选条件：排除计费类型为负的记录"
        query = "开发事件序列特征。" + "业务描述填充。" * 30 + tail_marker
        assert len(query) > 200
        restate = _customer_restate_query(query)
        assert tail_marker in restate
        assert restate.endswith("…") is False or len(query) > 300

    def test_harness_boilerplate_stripped_from_restate(self):
        """evo-eval harness 在任务文本后拼接"资源目录：…评测产物…"协议段
        （agent_adapter_base._query_text），复述素材截到该标记为止——协议
        boilerplate 不进客户口吻答复（用户指令，历史实证）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _customer_restate_query,
        )

        query = (
            "开发卸载应用清单序列特征，输出用户维度清单。"
            "资源目录：/home/zhanghao/.evo_eval/agent/20261111_2058_0de4/33 "
            "这个也是你的工作区，所有改动必须在这个目录内。 "
            "请提交以下评测产物，并严格使用指定的产物名称与格式： "
            "1. sql: sql（必须，允许多份）；SQL 交付文件，允许多份。"
        )
        restate = _customer_restate_query(query)
        assert "开发卸载应用清单序列特征" in restate
        assert "资源目录" not in restate
        assert "评测产物" not in restate
        assert "工作区" not in restate
        # 干净短需求不截断、不误伤
        assert _customer_restate_query("开发事件序列特征") == "开发事件序列特征"


# ─────────────────────────────────────────────────────────────────────────────
# 混合提问分类修复（已确认引用 → AMBIGUITY_CLARIFY）
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyConfirmedReferenceReroute:
    def test_q52_mid_question_with_confirmed_table_reroutes(self):
        """回归：引用已确认源表的 Mid 追问（标识+时间）不再被判成选表问题。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        q = (
            "Mid阶段需确认用户标识字段和时间窗口口径。源表已确认为 biads.dws_x_dm，"
            "时间窗口是否包含当天存在多种理解，请确认。"
        )
        assert classify_hitl_action(q) == "AMBIGUITY_CLARIFY"

    def test_q62_time_question_with_confirmed_table_reroutes(self):
        """回归：时间口径追问引用已定数据源 → AMBIGUITY_CLARIFY。"""
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
# 复盘：口径决策提问误路由修复（TABLE_CONFIRM → AMBIGUITY_CLARIFY）
# 实测 10 条提问为"时间窗口/口径"
# 决策类提问但提到表名，被 TABLE_CONFIRM 模板答非所问，agent 拿不到口径答复后独自定错口径。
# ─────────────────────────────────────────────────────────────────────────────


class TestCaliberQuestionReroute:
    def _classify(self, q: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def test_q61_combined_caliber_question_reroutes(self):
        """回归：标识+时间窗口口径合并提问，表名仅作上下文 → AMBIGUITY_CLARIFY。"""
        q = (
            "Mid阶段需要确认两个口径：1）用户标识字段——该任务为内容级特征，判断不需要用户标识字段，"
            "需用户确认；2）时间窗口口径——源表是每日快照，需确认取哪天的数据。"
            "## Mid 阶段：用户标识字段 & 时间窗口口径确认\n"
            "源表 `biads.ads_agalg_ue_content_ucb_score_dm` 是每日快照表。"
            "请问您需要取哪一天的数据？方案A（推荐）：取最新一天分区；方案B：取近N天的数据。"
        )
        assert self._classify(q) == "AMBIGUITY_CLARIFY"

    def test_q61_final_caliber_choice_reroutes(self):
        """回归：预算内最后一次"请选择取数口径 A/B"→ AMBIGUITY_CLARIFY。"""
        q = (
            "系统提示交互预算已用3/4，这是最后一次可获得答复的提问。时间窗口口径是最致命的未决口径"
            "——它直接影响SQL的分区过滤条件。源表 `biads.ads_agalg_ue_content_ucb_score_dm` 是每日快照表，"
            "ucb_score本身已基于近7日曝光计算（源表已内含窗口）。请选择取数口径："
            "方案A（推荐）：取最新一天分区；方案B：取近N天数据。"
        )
        assert self._classify(q) == "AMBIGUITY_CLARIFY"

    def test_q42_embedded_window_caliber_reroutes(self):
        """回归：源表已内含窗口、按分区读取的口径确认 → AMBIGUITY_CLARIFY。"""
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
# 混合提问改判极性修正：确认请求形 ≠ 已确认引用；字段候选 ≠ 表候选
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedQuestionPolarityV48:
    """极性修正（实测铁例 + 回放语料 5 翻转审计）。

    两类极性反转：①确认请求形（"请您确认/需要您确认/请问您确认使用"）曾被子串
    "您确认/确认使用"误判为已确认引用——新表提案问（含口径关键词）被改判
    AMBIGUITY 后表提案无人应答，agent 带着错误兄弟表往下走（冒烟案例选
    _1y_dm 而非 golden _1y_v2_dm FAIL）；②裸"候选"曾把标识字段候选枚举/
    否定形（"没有其他候选"）误判为表候选，口径决策问被 TABLE_CONFIRM 模板
    答非所问。回放门基线重定为 51/46/82/3（审计见 replay_check.py 头注释）。
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

        该问曾被改判 AMBIGUITY_CLARIFY，程序化答复只给标识/时间口径，
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
        """回放翻转 pin："请您确认数据源表选择"+候选表+表字段清单 → TABLE_CONFIRM。"""
        q = (
            "Initial阶段需要确认数据源表选择，已通过元数据检索锁定两张候选表，"
            "需要用户确认后再推进后续步骤。 我倾向的方案如下，请您确认数据源表选择：  "
            "**事实表（用户下载详情列表）：** `biads.dm_a` - 字段包含：imei、app_type"
        )
        assert self._classify(q) == "TABLE_CONFIRM"

    def test_q34_shape_candidate_options_stays_table_confirm(self):
        """回放翻转 pin：候选数据源选项 + 方案枚举 + 口径子问 → TABLE_CONFIRM。"""
        q = (
            "用户要求给出2-3个候选数据源选项。已整理3个方案：候选1（单表）、候选2（+应用维表）、"
            "候选3（+回传表）。推荐候选2。剩余2次提问机会，需要确认源表选择后才能推进后续步骤。"
            "同时需要确认总记录数量的计算口径。"
        )
        assert self._classify(q) == "TABLE_CONFIRM"

    def test_q59_identifier_field_candidates_reroute_ambiguity(self):
        """回放翻转 pin：标识字段候选枚举/否定形不是表提案 → AMBIGUITY_CLARIFY。

        此前裸"候选"（"三个候选：方案A：device_id"）误拦改判，TABLE_CONFIRM
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
        """守护（既有语义保留）："候选 biads.x 或 db.y"（候选接 db.table 名）
        仍是表候选提案，带口径词也保持 TABLE_CONFIRM。"""
        assert (
            self._classify(
                "数据源尚未确认，候选 biads.dws_x_dm 或 db.dwd_b_dm，时间窗口口径怎么算？"
            )
            == "TABLE_CONFIRM"
        )

    def test_interpretation_lean_not_table_proposal(self):
        """口径理解确认的"我倾向于理解为"不会被误判入表提案
        （已从 _TABLE_PROPOSAL_RE 移除裸"我倾向"，此 pin 防将来回潮）。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _proposes_table_candidates,
        )

        assert not _proposes_table_candidates("我倾向于理解为按设备维度统计，请确认这个理解是否正确")
        assert _proposes_table_candidates("计划使用 biads.t1 作为源表")
        assert _proposes_table_candidates("候选 biads.dws_x_dm 或 db.dwd_b_dm")
        assert not _proposes_table_candidates("有三个候选：方案A（推荐）：device_id")
        assert not _proposes_table_candidates("源表中只有 did，没有其他候选")


# ─────────────────────────────────────────────────────────────────────────────
# 列枚举死局提问转向
# ─────────────────────────────────────────────────────────────────────────────


class TestColumnEnumerationRedirect:
    def test_q58_enumeration_ask_detected(self):
        """回归：“期望的5列具体包含哪些列”是列枚举死局。"""
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
# AMBIGUITY_CLARIFY 组合答复（标识 + 时间）
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
# 口径缺口问型：UNA → AMBIGUITY 改判 + 程序化去重答复（query 缺口六模式）
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
        """回放门 pin：确认引用 + 陈述形"去重"不改判——
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
# AMBIGUITY 标识短路（golden 不在选项中 → 概念化转向，不经 LLM 兜底）
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
# OUTPUT_SCHEMA 聚合粒度概念化（不泄漏 agent 未提到的字段名）
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
# TABLE_CONFIRM 连续选错升级（特征级指引打破检索-拒绝死局）
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
# 第 3 次拒绝切换"自主决策交付"最终话术（顽疾六 grounding 死锁出口）
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
# 复合提问内嵌口径子问复合答复（TABLE_CONFIRM/OUTPUT 各出口经 _with_caliber
# 追加口径段；靶例：复合问里内嵌"标识+时间窗口"
# 子问被整段丢弃，agent 被拒表后拿不到口径）
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbeddedCaliberCompositeReplyV53(TestTableConfirmThirdRejectionAutonomy):
    def test_third_reject_embedded_caliber_pick_from_options(self):
        """形态（选项含 golden 标识）：第3拒自主交付话术 + 标识选定 + 时间口径。"""
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
        """真实录形态（选项 uid/imei 不含 golden 标识）：概念粒度转向 + 时间口径。"""
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
        """回归锁：纯表提案（无口径词）第3拒话术不追加口径段（行为保持）。"""
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
        """形态：粒度转向罐头 + 内嵌时间窗口/标识子问一并答复。"""
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
        """回归锁：纯输出结构问（无口径词）不追加口径段（行为保持）。"""
        reply = self._run("输出结构确认：我计划输出明细宽表")
        assert reply is not None
        assert "汇总结果" in reply
        assert "我一并答复" not in reply
        assert "时间口径" not in reply
        assert "用户标识" not in reply


# ─────────────────────────────────────────────────────────────────────────────
# 事后防线：LLM 兜底回复的假确认子句审查与改写
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

    def test_descriptor_negation_no_longer_bypasses_vet(self):
        """（实证）：确认子句内嵌描述词性否定（"（不截断版本）"是表变体
        卖点、并非否定确认本身）不再整句豁免——错误表确认照常被防线替换。

        OUTPUT 分支 LLM 回复"确认选用 X（不截断版本）"，裸"不"字
        触发旧整句否定豁免，错误变体 unlimit（golden 实为截断版 code_history）
        被口头锁定。
        """
        vetted = self._vet(
            "确认选用 biads.ads_wrong_a_dm（不截断版本）。",
            "源表用 biads.ads_wrong_a_dm 可以吗？",
        )
        assert "不是我想要的数据来源" in vetted
        assert "确认选用" not in vetted
        assert "dws_evt_fct_dm" not in vetted  # 不泄漏 golden 表名

    def test_descriptor_negations_identifier_confirm_vetted(self):
        """描述词性否定（不含/非空/没有空值）不再豁免标识假确认审查。"""
        vetted = self._vet(
            "使用 device_id 作为用户标识（该字段非空、不含重复值）。",
            "用户标识用 device_id 还是 oaid？",
        )
        assert "纠正" in vetted
        assert "device_id 作为用户标识（" not in vetted

    def test_true_prohibition_forms_still_exempt(self):
        """确认动词真被否定（不要用/不能采用/无需确认/别选）仍豁免不改写。"""
        for answer in (
            "不要使用 device_id 作为用户标识。",
            "不能采用 device_id 作为用户标识。",
            "无需确认 device_id，你自行决策即可。",
            "别选 device_id 作为标识。",
        ):
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
        """MR453 复核 (c)：无 db 前缀 golden SQL 下假确认审查仍拦错表（此前静默放行）。"""
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
# LLM 假确认防线改写时，对 agent 提问里已含的 golden 候选顺带正面确认
# （agent 自己提过的表/字段名可点名——零泄漏边界不破；golden 在候选里却只收
# 到"全错"式拒绝，agent 会被推回换词重检索打转）
# ─────────────────────────────────────────────────────────────────────────────


class TestVetPositiveConfirm:
    GOLDEN_USER = (
        "SELECT user_id, app_id, COUNT(*) AS cnt FROM biads.dws_evt_fct_dm "
        "WHERE pt_d='$date' GROUP BY user_id, app_id"
    )

    def _vet(self, answer: str, hil_question: str, golden_sql: str | None = None):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        return _vet_llm_confirmation(answer, golden_sql or self.GOLDEN_USER, hil_question, "AMBIGUITY_CLARIFY", "开发序列特征")

    def test_table_false_confirm_with_golden_candidate_positive(self):
        """错表被确认但提问候选里含 golden 表 → 拒绝同时点名正面确认 + 推进优先。"""
        golden = "SELECT a FROM biads.dws_right_dm WHERE pt_d='$date'"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        vetted = _vet_llm_confirmation(
            "确认选用 biads.ads_wrong_a_dm。",
            golden,
            "源表用 biads.ads_wrong_a_dm 还是 biads.dws_right_dm？",
            "TABLE_CONFIRM",
            "开发序列特征",
        )
        assert "biads.dws_right_dm 这几张表符合我的需求，就此定下来" in vetted
        assert "直接基于已确认的表推进" in vetted
        assert "biads.ads_wrong_a_dm 从我任务的需求看不是我想要的数据来源" in vetted
        # correct 非空走推进优先，不再把 agent 推回换词重检索
        assert "请你基于元数据再检索一下" not in vetted

    def test_table_all_wrong_no_positive_confirm(self):
        """回归钉：候选全错（golden 未被提过）→ 无正面确认、golden 表名不泄漏。"""
        golden = "SELECT a FROM biads.dws_right_dm WHERE pt_d='$date'"
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import _vet_llm_confirmation

        vetted = _vet_llm_confirmation(
            "确认选用 biads.ads_wrong_a_dm。",
            golden,
            "源表用 biads.ads_wrong_a_dm 还是 biads.ods_wrong_b？",
            "TABLE_CONFIRM",
            "开发序列特征",
        )
        assert "请你基于元数据再检索一下" in vetted
        assert "符合我的需求，就此定下来" not in vetted
        assert "biads.dws_right_dm" not in vetted  # golden 未被 agent 提过 → 不泄漏

    def test_identifier_golden_in_candidates_named(self):
        """标识确认错误但 golden 字段在提问候选里 → 纠正顺带点名"直接用它"。"""
        vetted = self._vet(
            "使用 device_id 作为用户标识。",
            "用户标识用 device_id 还是 user_id？",
        )
        assert "账号ID" in vetted  # 概念化纠正主体不变
        assert "user_id 就是我要的，直接用它" in vetted


# ─────────────────────────────────────────────────────────────────────────────
# MR453 复核：源表名裸名/限定名统一（提取 + 防泄漏）
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
        """ads_ 层裸表名同样提取（实测 golden 源表在 biads.ads_* 层）。"""
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
        """MR453 复核 (b)：LLM 裸名报出 agent 未提的 golden 表 → 拦截（此前放行）。"""
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

    两题实测：表名前后紧贴中文导致提取为空，纯选表提问被
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
# L1b-heavy + L4：拒绝写扩池标记 / 确认写已确认源表清单
# ─────────────────────────────────────────────────────────────────────────────


class TestL1bHeavyMarkerAndConfirmedTables:
    """TABLE_CONFIRM 程序化判定的两个 workspace 副作用。

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
        """L4 缺口修复：部分正确轮（correct+wrong 皆非空）的正确表同样累积写 L4。

        实证 12/17 任务有口头锁定（"这几张表符合我的需求，就此定下来"）
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


class TestV45RejectedLedger:
    """被拒表持久台账（起类内只保留台账测试）。

    - 被拒表持久台账 .memory/hitl_rejected_tables.json（跨轮 union +
      rounds 递增 + 近变体口径契约 want_tokens/want_phrases）——loader 基础
      池过滤与 mr 工具（轮次帽/口径对照）的消费数据源；
    - 池内表名 token 对照段已移除（用户指令：resolver 答复不
      指名候选池表名，降臃肿；表名级引导由工具侧检索口径对照承担）。
    """

    # golden 含 xtr/list/stat 口径 token，被拒表与之近变体（Jaccard=2/6=1/3）
    GOLDEN_VARIANT = "SELECT a.id FROM db.dwd_xtr_list_stat t1 JOIN db.dwd_b_dm t2 ON t1.id=t2.id"

    def _workspace(self) -> tuple[Any, Any, Any, Any]:
        import tempfile
        from pathlib import Path as _P

        tmp = tempfile.TemporaryDirectory()
        path = _P(tmp.name)
        return tmp, path, path / ".semantic", path / ".memory"

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

    def test_rejection_reply_no_pool_table_hint_v61(self):
        """回归：池内有口径 token 命中表，拒绝答复也不再指名池内表
        （对照段移除，用户指令降臃肿）；口径词提示保留。"""
        import json

        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_table_confirm_programmatic,
        )

        tmp, root, semantic_dir, _ = self._workspace()
        with tmp:
            semantic_dir.mkdir(parents=True, exist_ok=True)
            (semantic_dir / "semantic_retrieve_context_fake001.json").write_text(
                json.dumps({"query": "q", "tables": ["db.dwd_xtr_list_stat_dm"]}),
                encoding="utf-8",
            )
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_xtr_list_sample，是否可以？",
                "开发序列特征",
                None,
                workspace_root=root,
            )
            assert "不是我想要的数据来源" in reply
            assert "为帮你把口径落到实处" not in reply
            assert "db.dwd_xtr_list_stat_dm" not in reply
            # 近变体口径词提示仍应在（概念词，不涉表名）
            assert "另外把数据口径" in reply

    def test_escalated_rejection_no_pool_table_hint_v61(self):
        """回归：升级答复（连续选错）同样不带池内表名对照段。"""
        import json

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
            semantic_dir.mkdir(parents=True, exist_ok=True)
            (semantic_dir / "semantic_retrieve_context_fake001.json").write_text(
                json.dumps({"query": "q", "tables": ["db.dwd_xtr_list_stat_dm"]}),
                encoding="utf-8",
            )
            reply = _answer_table_confirm_programmatic(
                self.GOLDEN_VARIANT,
                "源表候选：db.ads_xtr_list_v2，是否可以？",
                "开发序列特征",
                [prior_reject],
                workspace_root=root,
            )
            assert "为帮你把口径落到实处" not in reply
            assert "db.dwd_xtr_list_stat_dm" not in reply


# ─────────────────────────────────────────────────────────────────────────────
# UNA 误路由修正（收敛确认→TABLE_CONFIRM / 列构成确认→OUTPUT）
#      + OUTPUT 纯粒度问退 LLM persona
# ─────────────────────────────────────────────────────────────────────────────


class TestTableProposalUnaOverride:
    """收敛确认误路由（铁例）："候选池中最接近的表…请您确认"零关键词命中落 UNA。"""

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
        """铁例：交付后礼节性提问不含提案表述，保持 UNA。"""
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
        """回放门 pin："我倾向于理解为…请确认这个理解是否正确"是口径
        歧义问不是表提案——宽信号"我倾向"（_TABLE_PROPOSAL_RE）会误改判，
        收敛确认标记只认表为确认对象的签名。"""
        q = (
            "关于\"卸载时间序列前7位\"的口径，我倾向于理解为：从卸载时间列表中按时间排序"
            "取前7个卸载时间值。请确认：1）这个理解是否正确？2）或者您是指时间字符串本身"
            "截取前7个字符？"
        )
        assert self._classify(q) == "UNA"


class TestSchemaDerivationUnaOverride:
    """列构成确认误路由（铁例）："输出schema"无空格 / "列构成"≠关键词"列结构"。"""

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


class TestTableSelectionOutputOverride:
    """表选择问型升判（q40 r1 铁例）：三连问被"目标表名"关键词整问抢进 OUTPUT，
    源表子问（v2_dm 还是 v1_dm）零应答，agent 沿错误源表交付 FAIL；签名 =
    选用动词 + ≥2 个表候选 → 升判 TABLE_CONFIRM 走程序化源表 diff。"""

    Q40_R1_Q1 = (
        "任务需聚合当天新鲜游戏的单框应用ID和加权值键值对序列。元数据检索发现两个近变体源表"
        "（v1版15字段 vs v2版9字段），均含 app_id 和 final_weight。"
        "目标表 biads.ads_agalg_ue_freshgame_weight_v2_dcs_dm 的 insert_key 字段需写入 'weight_game_v1'。\n"
        "倾向方案：源表选用 biads.ads_agalg_ue_freshgame_weight_v2_dm（v2版），"
        "因为目标表名为 v2_dcs_dm，版本对应一致。\n\n"
        "候选方案B：源表改用 biads.ads_agalg_ue_freshgame_weight_dm（v1版），该表字段更丰富。\n\n"
        "请确认：\n1. 源表选择 v2_dm 还是 v1_dm？\n2. 输出目标表是否为 "
        "biads.ads_agalg_ue_freshgame_weight_v2_dcs_dm？"
    )

    def _classify(self, q: str) -> str:
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import classify_hitl_action

        return classify_hitl_action(q)

    def test_q40_r1_hijacked_selection_promoted(self):
        """q40 r1 实录形态（含"目标表名"OUTPUT 关键词劫持）→ 升判 TABLE_CONFIRM。"""
        assert self._classify(self.Q40_R1_Q1) == "TABLE_CONFIRM"

    def test_plan_ab_form_promoted(self):
        """r2 同型（方案A还是方案B + ≥2 表 + OUTPUT 关键词并存）→ 升判。"""
        q = (
            "输出列名需要确认，目标表名固定为 biads.ads_t_dcs_dm。\n"
            "方案A：源表 = biads.ads_t_v2_dm（v2版）；方案B：源表 = biads.ads_t_dm（v1版）。\n"
            "请确认使用方案A还是方案B？"
        )
        assert self._classify(q) == "TABLE_CONFIRM"

    def test_selection_without_output_keyword_unaffected(self):
        """无 OUTPUT 关键词的表选择问 base 本就是 TABLE_CONFIRM（不经升判路径）。"""
        assert self._classify("源表选择 biads.a_v2_dm 还是 biads.a_dm？") == "TABLE_CONFIRM"

    def test_final_delivery_confirm_stays_output(self):
        """最终交付确认（2 表但无选用动词）保持 OUTPUT——列数/列名对比是正确答复。"""
        q = (
            "最终交付确认：目标表 biads.ads_agalg_ue_freshgame_weight_v2_dcs_dm 输出共 2 列。\n"
            "源表：biads.ads_agalg_ue_freshgame_weight_v2_dm，按 pt_d 当天分区读取。\n"
            "请确认以上输出列数（2列）、列名、列顺序是否符合预期。"
        )
        assert self._classify(q) == "OUTPUT_SCHEMA_PREVIEW"

    def test_single_table_with_selection_verb_stays_output(self):
        """单表 + 选用动词不构成表选择对比（≥2 候选是签名一半）。"""
        q = "目标表名已定，输出字段顺序如何排列？源表选用 biads.ads_t_v2_dm 即可。"
        assert self._classify(q) == "OUTPUT_SCHEMA_PREVIEW"

    def test_output_ab_question_without_tables_stays_output(self):
        """方案A还是B 的输出结构问（无表候选）不升判（r2 存储模式问型，0 表）。"""
        q = (
            "目标表名固定。请确认使用方案A（单行序列模式）还是方案B（按 insert_key 多行存储）？"
            "输出列名顺序同时确认。"
        )
        assert self._classify(q) == "OUTPUT_SCHEMA_PREVIEW"


class TestColumnCountFallbackPrefixGuard:
    """兜底正则前置词守卫（q40 r1 次生噪声）："v1版15字段 vs v2版9字段"是
    近变体表字段数对比，不是计划输出列数——曾被误读成"计划输出 15 列"，
    OUTPUT 列数对比答复带偏 agent。"""

    def _count(self, text: str):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _extract_agent_column_count,
        )

        return _extract_agent_column_count(text)

    def test_version_prefixed_table_fields_blocked(self):
        """q40 r1 实录："（v1版15字段 vs v2版9字段）" → 不算计划列数。"""
        q = "元数据检索发现两个近变体源表（v1版15字段 vs v2版9字段），均含 app_id 和 final_weight，需确认输出口径。"
        assert self._count(q) is None

    def test_table_attribute_lead_blocked(self):
        """表属性语引导的"N字段"（该表/张表/表字段/字段数）不算计划列数。"""
        assert self._count("这张表有15字段，我担心字段太多") is None
        assert self._count("下载明细表字段精简（16字段）是我的优势") is None
        assert self._count("该表的字段数为9字段") is None

    def test_plan_definition_forms_pass(self):
        """agent 自己的输出结构描述（字段定义/确认的N列方案）仍算计划列数。"""
        assert self._count("系统IR自动推导的字段定义（8列，含统计主体）") == 8
        assert self._count("推导的输出字段定义与用户确认的11列方案一致") == 11

    def test_prefixed_and_bare_plan_counts_unaffected(self):
        """前置词形（输出N列）与无表属性语的裸形兜底行为不变。

        已知边界（超出守卫范围）："该表共15字段"经前置词"共"走的是首条
        正则而非兜底——q40 实录仅兜底误读有据，动首条会波及回放语料
        20+ 个 first 形匹配，保持原行为。
        """
        assert self._count("计划输出8列，请确认") == 8
        assert self._count("我的方案是8列") == 8

    def test_table_paren_form_blocked(self):
        """q44 r1 实录形："行为明细表（54字段，留存100天）" → 不算计划列数。"""
        assert self._count("该表为广告推荐场景用户行为明细表（54字段，留存100天），每行一条行为记录") is None


class TestRejectedTableReflowTail:
    """OUTPUT/AMBIGUITY 答复的已拒表回流拦截（q44 r1 重提案循环实录：
    被拒表经"期望输出21列"类问题回流，OUTPUT 程序化只答列数、被拒表无人
    点名，agent 下一轮继续提，循环耗尽预算零交付）。"""

    Q44_Q4 = (
        "用户反馈期望输出21列，与第一张被拒表 biads.ads_t_x_dm 的21个字段完全吻合。"
        "现按您确认的口径重新提案：源表 biads.ads_t_x_dm，请确认输出列数是否按21列执行。"
    )

    @staticmethod
    def _write_ledger(workspace: Path, tables: list[str]) -> None:
        (workspace / ".memory").mkdir(parents=True, exist_ok=True)
        (workspace / ".memory" / "hitl_rejected_tables.json").write_text(
            json.dumps({"rejected_tables": tables, "rounds": 1}, ensure_ascii=False), encoding="utf-8"
        )

    def _tail(self, q: str, workspace: Path):
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _rejected_table_reflow_tail,
        )

        return _rejected_table_reflow_tail(q, workspace)

    def test_tail_names_rejected_table(self, tmp_path):
        """问题内表名 ∩ 台账 → 尾段点名"已否定过、不要再提"。"""
        self._write_ledger(tmp_path, ["biads.ads_t_x_dm"])
        tail = self._tail(self.Q44_Q4, tmp_path)
        assert tail is not None
        assert "biads.ads_t_x_dm" in tail
        assert "已经明确否定过" in tail
        assert "不要再把它们作为候选提回来" in tail

    def test_tail_absent_without_ledger_or_overlap(self, tmp_path):
        """无台账 / 问题表不在台账 → 不追加（零影响）。"""
        assert self._tail(self.Q44_Q4, tmp_path) is None  # 无台账
        self._write_ledger(tmp_path, ["biads.ads_other_dm"])
        assert self._tail(self.Q44_Q4, tmp_path) is None  # 无交集

    def test_output_reply_appends_tail_end_to_end(self, task_index_file, tmp_path):
        """q44 msg[34] 形态端到端：OUTPUT 列数答复 + 拒源尾段一起注入。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            classify_hitl_action,
            hitl_auto_resolver,
        )

        assert classify_hitl_action(self.Q44_Q4) == "OUTPUT_SCHEMA_PREVIEW"  # 劫持形态钉
        self._write_ledger(tmp_path, ["biads.ads_t_x_dm"])
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数，统计每个用户的订单总量</user_query>"),
                AIMessage(
                    content="完成",
                    tool_calls=[_make_hil_tool_call(call_id="c1", pending_action=self.Q44_Q4)],
                ),
            ],
            hitl_task_index_path=task_index_file,
            workspace=str(tmp_path),
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        reply = result["messages"][-1].content
        assert "期望输出 1 列" in reply  # OUTPUT 列数答复主体保留
        assert "已经明确否定过" in reply  # 拒源尾段追加
        assert "biads.ads_t_x_dm" in reply

    def test_output_reply_without_ledger_unchanged(self, task_index_file, tmp_path):
        """无台账时 OUTPUT 答复原样（回归锁：主体不含拒源尾段）。"""
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数，统计每个用户的订单总量</user_query>"),
                AIMessage(
                    content="完成",
                    tool_calls=[_make_hil_tool_call(call_id="c1", pending_action=self.Q44_Q4)],
                ),
            ],
            hitl_task_index_path=task_index_file,
            workspace=str(tmp_path),
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        reply = result["messages"][-1].content
        assert "已经明确否定过" not in reply
        assert "期望输出 1 列" in reply


class TestOutputPureGrainDeferToLlm:
    """OUTPUT 分支 3 细化：纯主体/主键/粒度问（golden 无 GROUP BY 特征）退 LLM persona。"""

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
        """问主体/主键、golden 无 GROUP BY → None 退 persona
        （OUTPUT persona 角色约束本就聚焦"一行代表什么/按什么粒度聚合"）。"""
        q = "我需要确认输出表的主体（主键）是什么：候选A 搜索词为主键，每行一个搜索词；候选B 用户为主键。"
        assert self._run(q) is None

    def test_mixed_grain_and_col_ask_keeps_programmatic(self):
        """（主键+列构成 混合问）：保留列数程序化答复——PASS 路径不动。"""
        q = "需要确认输出表的主键和列构成：主键是 random_weight_key 字符串吗？"
        reply = self._run(q)
        assert reply is not None
        assert "期望输出 4 列" in reply

    def test_plain_col_ask_keeps_programmatic(self):
        reply = self._run("输出列数确认一下可以吗")
        assert reply is not None
        assert "期望输出 4 列" in reply


# ─────────────────────────────────────────────────────────────────────────────
# 答非所问双病灶修复：用户变体请求形剥离 + WITH-CTE golden 输出列数 + 无标识 golden 粒度转向
# （三连实录：源表提案问/标识选择问被改判 AMBIGUITY 拿时间口径答复、
#   17 列正确提案被"纠正"成首 CTE 的 4 列——agent 被带偏交付 4 列 KV 结构 FAIL）
# ─────────────────────────────────────────────────────────────────────────────


class TestUserVariantRequestFormV53b(TestMixedQuestionPolarityV48):
    """确认请求形的"用户"变体（只剥"您"）。

    实录："需要用户确认使用哪个作为主源"——"用户"二字挡住
    请求形剥离，残余"确认使用"再度极性反转为已确认引用，全套表提案
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
        """铁例 pin：源表提案 + 用户变体请求形 + 时间窗口子问 → TABLE_CONFIRM
        （拿 golden-grounded 表判定/拒绝，而非时间口径独占答复）。"""
        assert self._classify(self.Q34_Q1) == "TABLE_CONFIRM"

    def test_q34_q2_identifier_ask_not_confirmed_reference(self):
        """标识选择问 + 源表已默认同意（真引用形不受影响）+
        用户变体请求形 → 不再因残余"确认使用"判已确认引用整问改判。"""
        assert not self._confirmed_refs(self.Q34_Q2)


class TestBareXuRequestFormV62(TestMixedQuestionPolarityV48):
    """确认请求形的裸"需"缩略变体（同家族第三种措辞）。

    实录：agent 省"要"字写"需您确认选哪一套"/"需确认使用哪张
    作为主源表"——"需"单字不在请求前缀集合（需要/要），剥离漏过，残余"您确认/
    确认使用"第三度极性反转为已确认引用，表选型问（含口径子问）被改判
    AMBIGUITY：三选一近变体问只拿到 imei 口径答复、错误变体默认锁定；
    五表两方案问只拿到标识/时间答复、错误源表组合默认锁定。
    """

    Q58_Q1 = (
        "任务需要同时获取\"应用安装时间\"和\"首次打开时间\"，涉及安装行为表和打开行为表的选型。"
        "检索到两组同族候选：ads层聚合日表（字段精简、标识统一）vs dwd层事实明细表（字段丰富、"
        "标识未统一）。两套方案的JOIN key和标识字段不同，影响后续SQL复杂度和口径一致性，"
        "需您确认选哪一套。\n"
        "倾向方案：安装行为表选 biads.ads_agalg_common_user_install_bhv_dm + "
        "打开行为表选 biads.ads_agalg_common_user_open_bhv_dm + "
        "应用维表选 agapdw.dim_hispace_app_info_ds。\n"
        "候选方案B：安装表选 agapdw.dwd_hispace_client_install_tf_dm + "
        "打开表选 bicoredata.dwd_onl_app_open_hasdk_tf_dm + "
        "维表 agapdw.dim_hispace_app_info_ds。"
    )

    Q70_Q1 = (
        "任务要求开发\"用户下载应用（剔除卸载后）的键值对序列特征\"。元数据检索发现3张近变体"
        "候选源表（均为\"下载未卸载\"主题），需确认使用哪张作为主源表。\n"
        "倾向选择候选C：`biads.ads_agrec_user_ubr_down_app_list_tail_dm` 作为主源表。"
        "该表按 IMEI+尾号 聚合，字段已是键值对拼接列表，与任务要求高度吻合。"
    )

    def test_bare_xu_request_form_not_confirmed_reference(self):
        """请求极性"需（您/用户）?确认(使用)"（省"要"缩略形）不是已确认引用。"""
        assert not self._confirmed_refs("需您确认选哪一套")
        assert not self._confirmed_refs("需确认使用哪张作为主源表")
        assert not self._confirmed_refs(self.Q58_Q1)
        assert not self._confirmed_refs(self.Q70_Q1)

    def test_bare_xu_still_strips_with_yonghu_variant(self):
        """裸"需"与"用户"变体叠加（需用户确认使用）同样剥离。"""
        assert not self._confirmed_refs("需用户确认使用哪个作为主标识")

    def test_true_reference_forms_with_xu_still_confirmed(self):
        """真引用形不受裸"需"扩展影响："无需确认"非引用载体；"已确认"形照常命中。"""
        assert self._confirmed_refs("源表已确认为 biads.t1，现在确认标识口径")
        # "无需确认"否定形被剥离但剩余文本不含引用标记 → 不是已确认引用
        assert not self._confirmed_refs("这些口径无需确认已按冻结表执行")

    def test_q58_table_set_proposal_stays_table_confirm(self):
        """铁例 pin：表组合选型问（"表选 db.table"提案形 + JOIN key/口径子问）
        → TABLE_CONFIRM（拿 golden-grounded 全错拒绝 + 统一行为口径提示，
        而非标识/时间口径独占答复）。"""
        assert self._classify(self.Q58_Q1) == "TABLE_CONFIRM"

    def test_q70_near_variant_proposal_stays_table_confirm(self):
        """铁例 pin：近变体三选一问（需确认使用哪张）→ TABLE_CONFIRM。"""
        assert self._classify(self.Q70_Q1) == "TABLE_CONFIRM"

    def test_biaoxuan_table_form_is_proposal(self):
        """"<角色>表选 db.table"（安装行为表选 X/应用维表选 Y）是表提案信号；
        "总表选哪个字段"（表选后接疑问词）不是。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _proposes_table_candidates,
        )

        assert _proposes_table_candidates("安装行为表选 biads.ads_agalg_common_user_install_bhv_dm")
        assert _proposes_table_candidates("应用维表选 agapdw.dim_hispace_app_info_ds")
        assert not _proposes_table_candidates("总表选哪个字段作为主键")
        assert not _proposes_table_candidates("分表选择策略请确认")

    def test_true_reference_tablexuan_context_reroutes(self):
        """真引用 + 表选（"已确认源表选 X，现在确认时间口径"）仍走引用分支改判
        AMBIGUITY（branch 1 先行拦截，不因"表选"新支回潮）。"""
        q = "已确认源表选 biads.ads_agalg_common_user_unified_bhv_dm，现在确认时间窗口口径是否包含当天"
        assert self._classify(q) == "AMBIGUITY_CLARIFY"


# ─────────────────────────────────────────────────────────────────────────────
# 引用标记表域限定：标识/口径上的"已确认" ≠ 表已确认引用
# ─────────────────────────────────────────────────────────────────────────────


class TestMarkerScopeV62(TestMixedQuestionPolarityV48):
    """表域限定（铁例）。

    "用户已确认 user_id 标识和单日分区快照口径。需确认源表选择…"——"已确认"
    落在标识口径上（±10 窗无表词、所在子句无 db.table 限定名），此前的全局
    子串匹配把同问新表提案（方案A/B/C 三候选）误判已确认引用，改判
    AMBIGUITY：表判定无人应答，agent 沿用已被拒的 dsid 30d 表直至最终交付。
    表域限定后该问回 TABLE_CONFIRM（程序化答复逐候选对 golden 判定：拒 4 张
    30d 明细/维表 + 确认 agent 自行检索到的 1y 列表表=golden 源）。
    双信号：①±10 窗表语境词（表/数据源/方案/候选 或限定名）；②标记所在
    分隔子句含完整 db.table 限定名（括注形"…ads_x_dm**（您已确认符合
    需求）"——表名与标记同句但 40+ 字符在窗外，该问保持 AMBIGUITY 且回放
    门零回归）。真引用形标记天然邻接表词/含限定名，不受影响（继承套件+下方 pin）。
    """

    Q65_R2 = (
        "用户拒绝了之前的3张30天明细表候选，要求换用需求里的业务词重新检索。经多轮不同业务词检索后，"
        "元数据系统中能作为\"30天使用明细\"源表的仍为这3张表（无其他30天使用明细表）。但本次分析有新进展："
        "(1)确认目标产出表为 biads.ads_agalg_feature_userid_user_ubr_usedapp_list_1y_dm（28字段，user_id 主键）；"
        "(2)ads_agalg_dsid_user_usage_detail_30d_dm 是唯一同时含 user_id + developer_id + app_third_type + "
        "全部5指标的源表，与目标表主键直接对齐；(3)用户已确认 user_id 标识和单日分区快照口径。"
        "需确认源表选择以推进SQL开发。\n"
        "倾向方案A：源表选用 biads.ads_agalg_dsid_user_usage_detail_30d_dm（30天使用明细底表，"
        "含 user_id + app_id + app_cn_name + app_type + app_third_type + app_package_name + developer_id + "
        "5个统计指标：usage_cnt_avg_per_day/usage_cnt_max_per_day/usage_days/usage_duration_per_day/"
        "last_open_time，跨12个月分区汇总后按 user_id + app_type 聚合，用 ConcatWithRank 拼接前7位应用序列）。"
        "该表是池中唯一同时具备 user_id 主键和全部所需应用属性+指标的源表。\n"
        "候选B：biads.ads_agrec_adv_common_up_usedapp_detail_30d_dm（30天明细，主键 up_id 非 user_id，"
        "缺 developer_id 和 app_third_type，需 JOIN 维表 biads.ads_noah_fulllist_appinfo_ds 补齐）\n"
        "候选C：biads.ads_agrec_adv_common_user_usedapp_detail_30d_dm（30天明细，主键 imei 非 user_id，"
        "缺 developer_id 和 app_third_type，需 JOIN 维表补齐且需 imei→user_id 映射）\n"
        "请确认源表选择。"
    )

    def test_q65_identifier_confirmed_not_table_reference(self):
        """铁例 pin：标识/口径上的"已确认"不算表引用 → 新表提案保持 TABLE_CONFIRM。"""
        assert not self._confirmed_refs(self.Q65_R2)
        assert self._classify(self.Q65_R2) == "TABLE_CONFIRM"

    def test_clause_char_word_alone_not_enough(self):
        """子句含"表"字但无 db.table 限定名、±10 窗亦无表词 → 仍不算（子句级
        信号只认限定名——裸标识 user_id 无点、字符表词必须邻接）。"""
        q = "用户已确认 user_id 标识和单日分区快照表口径，需确认源表选择以推进"
        assert not self._confirmed_refs(q)

    def test_parenthetical_annotation_form_still_confirmed_reference(self):
        """回放锚点：括注形"…ads_x_dm**（您已确认符合需求）"——表名与标记
        同句但距 ±10 窗外（表名 40+ 字符），子句级限定名信号保真引用判定，
        该问保持 AMBIGUITY_CLARIFY（实录 AMBIGUITY 答复正确："site_id
        对应榜单维度，可以使用"；agent 已用对表，FAIL 在 SQL 细节非选表层）。"""
        q = (
            "用户确认了ads_agalg_adv_common_list_app_xtr_stat_dm是符合需求的表，"
            "但该表没有list_id字段。需要确认site_id是否对应榜单维度，"
            "或者用户是否有其他包含list_id的表源。\n"
            "基于重新检索，我找到以下候选数据源：\n"
            "**候选1：agapads.ads_agalg_adv_common_list_app_xtr_stat_dm**（您已确认符合需求）\n"
            "- 关键字段：site_id、app_id、action_cnt\n"
            "**候选2：agapdw.dim_hispace_app_info_ds**（您已确认符合需求）\n"
            "- 关键字段：app_id（主键）、app_cn_name\n"
            "请问：1. 候选1中的 site_id 是否就是您所说的\"各榜单\"的维度？"
            "2. 或者您是否知道其他包含 list_id（榜单编号）字段且与CPD相关的表？"
        )
        assert self._confirmed_refs(q)
        assert self._classify(q) == "AMBIGUITY_CLARIFY"

    def test_clause_qualified_name_minimal_form(self):
        """子句级限定名信号最小形：限定名与标记同句（无邻接表词）即计。"""
        assert self._confirmed_refs("agapads.ads_x_dm（您已确认符合需求）")
        assert self._confirmed_refs("先看 biads.t1 的字段，您已确认这张表可用")
        # 限定名在另一子句（分隔符隔断）且 ±10 窗外 → 不计
        assert not self._confirmed_refs("用户已确认 user_id 标识和单日分区快照口径。biads.t1 另议")

    def test_identifier_confirm_with_far_table_proposal(self):
        """标识已确认 + 远处（另一段落）新表提案：全局匹配曾误判的混合形。"""
        q = (
            "用户已确认 device_id 作为设备标识。"
            "接下来需确认数据来源：候选源表为 biads.ads_evt_stat_dm 与 biads.ads_evt_detail_dm，"
            "请选择。"
        )
        assert not self._confirmed_refs(q)
        assert self._classify(q) == "TABLE_CONFIRM"


class TestGoldenOutputColumnCountV53b:
    """WITH-CTE golden 的输出列数取最终输出 SELECT（非首个 CTE）。

    实录：agent 推导 17 列（2 主键+15 指标，与 golden 输出一致），
    _count_golden_output_columns 取首 CTE（pt_d/app_id/两个聚合列=4 列）回复
    "期望输出 4 列，你计划输出 17 列"——正确提案被带偏，agent 交付 4 列 KV 结构。
    同型（首 CTE 恰 5 列 vs 真输出 4/11/9 列）。
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
        """端到端 pin：形态（17 列正确提案 + CTE golden）→ 期望输出=输出 SELECT 列数，
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
    """golden 无用户标识字段（应用/站点级聚合）时标识选择问的粒度转向。

    实录：agent 问 device_id/user_id/did 选哪个，golden 按应用
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
        """回归锁：golden 标识在 agent 选项中 → 原选定答复不变（行为）。"""
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


# ─────────────────────────────────────────────────────────────────────────────
# v64 多部件应答合成：复合提问（一次问表选择 + 语义解读/输出列数）原先整问按
# 主 action 单应答——程序化部分覆盖（非 None）阻断 LLM 兜底（兜底仅在 None 时
# 触发），其余子问永久无答复。实录（q49 msg28）：表选择 A/B/C 有 golden-grounded
# 判定，_p1/_p2 后缀业务含义子问零应答，agent 按自己的倾向（单双框架拆分）
# 猜错拆分维度直至交付。合成 = 其余部件能程序化的程序化（表 diff/列数对比）、
# 语义缺口一次 LLM 补答（golden 作内部判定素材，防泄漏双防线照常）、
# "另外，"段追加在主应答之后。
# ─────────────────────────────────────────────────────────────────────────────
class TestMultipartCompositionV64:
    GOLDEN_Q49 = (
        "INSERT OVERWRITE TABLE biads.ads_agalg_did_c_push_user_label_dm PARTITION(pt_d='$date') "
        "SELECT did, "
        "concat_ws('#', collect_list(CASE WHEN split_element LIKE '%_p1' "
        "THEN regexp_replace(split_element, '_p1$', '') END)) AS p1_label, "
        "concat_ws('#', collect_list(CASE WHEN split_element LIKE '%_p2' "
        "THEN regexp_replace(split_element, '_p2$', '') END)) AS p2_label "
        "FROM agapads.ads_persona_push_label_category_extend_ds "
        "LATERAL VIEW explode(split(push_label_category_extend, '#')) t AS split_element "
        "WHERE pt_d='$date' GROUP BY did"
    )
    Q49_MSG28_REASON = (
        "Initial阶段需确认两个核心阻塞点：(1) _p1/_p2后缀的业务含义——这决定了整个"
        "特征设计的拆分维度；(2) 数据源表选择——候选池中存在多张push相关表。"
    )
    Q49_MSG28_PENDING = """## 待确认1：_p1/_p2后缀的业务含义

我倾向于 **解读A（单双框架拆分）**：
- `_p1` = 单框架设备的push新闻资讯标签序列特征
- `_p2` = 双框架设备的push新闻资讯标签序列特征

候选B（标签层级拆分）：
- `_p1` = 一级标签分类序列
- `_p2` = 二级标签分类序列

## 待确认2：数据源表选择

我倾向于 **候选A：`bicoredata.dwd_cam_feature_push_class2_list_ds`**（主键dsid，天分区pt_d）
候选B：`bicoredata.dwd_cam_feature_push_class2_list_udid_ds`（主键udid）
候选C：`agapads.ads_persona_push_label_category_extend_ds`（主键did，push新闻资讯8分类人群扩展表）

请确认或选择以上两项。"""
    GAP_LLM_REPLY = "按候选B（标签层级拆分）来：_p1 是一级标签分类序列，_p2 是二级标签分类序列。"

    def _q49_question(self) -> str:
        return f"{self.Q49_MSG28_REASON}\n{self.Q49_MSG28_PENDING}"

    def _compose(self, hil_question: str, golden_sql: str, llm_response: str = ""):
        """走真实分派序（分类 → 主 action 程序化应答 → 合成）返回 (composed, llm_used, primary)。"""
        from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_auto_resolver import (
            _answer_ambiguity_clarify_programmatic,
            _answer_output_schema_programmatic,
            _answer_table_confirm_programmatic,
            _compose_multipart_answer,
            classify_hitl_action,
        )

        action = classify_hitl_action(hil_question)
        if action == "TABLE_CONFIRM":
            primary = _answer_table_confirm_programmatic(
                golden_sql, hil_question, "开发push新闻资讯标签序列特征", None, None
            )
        elif action == "OUTPUT_SCHEMA_PREVIEW":
            primary = _answer_output_schema_programmatic(golden_sql, hil_question)
        else:
            primary = _answer_ambiguity_clarify_programmatic(golden_sql, hil_question)
        if primary is None:
            return None, False, None
        composed, llm_used = _compose_multipart_answer(
            primary,
            action,
            hil_question,
            golden_sql,
            "开发push新闻资讯标签序列特征",
            None,
            None,
            RuntimeStub(llm_response=llm_response),
        )
        return composed, llm_used, primary

    def test_q49_form_both_parts_answered(self):
        """q49 msg28 活靶：表选择部件（golden 表确认 + 错表拒绝）与 _p1/_p2
        语义部件（LLM 缺口补答）在一条合成回复里都有答案。"""
        composed, llm_used, _ = self._compose(
            self._q49_question(), self.GOLDEN_Q49, llm_response=self.GAP_LLM_REPLY
        )
        assert llm_used
        assert "agapads.ads_persona_push_label_category_extend_ds 这几张表符合我的需求" in composed
        assert "从我任务的需求看不是我想要的数据来源" in composed
        assert "bicoredata.dwd_cam_feature_push_class2_list_ds" in composed
        assert f"另外，{self.GAP_LLM_REPLY}" in composed

    def test_q49_form_llm_failure_keeps_table_part(self):
        """缺口 LLM 补答失败时主应答原样交付（表判定不因补答失败丢失）。"""
        composed, llm_used, primary = self._compose(
            self._q49_question(), self.GOLDEN_Q49, llm_response=""
        )
        assert not llm_used
        assert composed == primary
        assert "符合我的需求" in composed
        assert self.GAP_LLM_REPLY not in composed

    def test_single_part_table_question_unchanged(self):
        """单部件表提案问零合成（无其余部件信号，行为与合成前完全一致）。"""
        q = "我计划使用 biads.dws_evt_fct_dm 作为源表开发特征，请确认源表选择。"
        composed, llm_used, primary = self._compose(q, self.GOLDEN_Q49, llm_response=self.GAP_LLM_REPLY)
        assert composed == primary
        assert not llm_used

    def test_recap_section_not_reanswered(self):
        """已确认口径回述节（描述性、非问句形态）不得被当作输出列问再答一遍。"""
        q = (
            "Mid阶段核心阻塞点：_p1和_p2两套特征的区分维度未明确。\n"
            "## 已确认口径（根据您的反馈）\n"
            "- 输出粒度：按设备ID聚合，单key单行\n"
            "- 每张表输出3列\n"
            "## 待确认：_p1和_p2两套特征的区分维度\n"
            "**倾向方案A：按标识维度拆分**（来源 `bicoredata.dwd_cam_feature_push_class2_list_ds`）\n"
            "**候选方案B：按行为类型拆分**\n"
            "请确认采用哪种方案。"
        )
        composed, llm_used, _ = self._compose(q, self.GOLDEN_Q49, llm_response=self.GAP_LLM_REPLY)
        assert llm_used
        assert f"另外，{self.GAP_LLM_REPLY}" in composed
        # 回述节零应答：不得出现输出列/粒度程序化应答段
        assert "输出应为" not in composed
        assert "期望输出" not in composed

    def test_output_section_programmatic_append(self):
        """输出列部件能程序化的程序化：主 TC 表确认 + 输出列数对比段合成，
        无语义缺口时不消耗 LLM。"""
        q = (
            "## 待确认1：数据源表选择\n"
            "我倾向于 **候选A：`agapads.ads_persona_push_label_category_extend_ds`**（主键did）。\n"
            "## 待确认2：输出列数确认\n"
            "我计划输出 5 列，请确认输出列数是否正确。"
        )
        composed, llm_used, _ = self._compose(q, self.GOLDEN_Q49, llm_response=self.GAP_LLM_REPLY)
        assert not llm_used
        assert "确认：就用" in composed
        assert "输出列数需要修正：期望输出 3 列，你计划输出 5 列" in composed

    def test_output_primary_multipart_unchanged(self):
        """OUTPUT 主 action 的多节输出结构问（Final 阶段 schema 确认）全部件
        已被主应答覆盖——零合成、零变化。"""
        q = (
            "### _p1 表\n目标表名：`biads.ads_agalg_push_news_label_seq_p1_dm`，输出3列。\n"
            "### 说明\n序列列名相同，仅主键不同。请确认以上schema是否符合预期。"
        )
        composed, llm_used, primary = self._compose(q, self.GOLDEN_Q49, llm_response=self.GAP_LLM_REPLY)
        assert composed == primary
        assert not llm_used

    def test_clause_fallback_semantic_gap(self):
        """无节头复合问走子句兜底：语义缺口子句收进 LLM 补答（"是什么"问句
        形态命中），含表子句跳过（表 ask 跨子句散布防误判）。"""
        q = (
            "数据源已确认为 agapads.ads_persona_push_label_category_extend_ds，"
            "现在有个字段语义疑问：后缀 _p1 的业务含义是什么？"
        )
        composed, llm_used, _ = self._compose(q, self.GOLDEN_Q49, llm_response=self.GAP_LLM_REPLY)
        assert llm_used
        assert f"另外，{self.GAP_LLM_REPLY}" in composed

    def test_gap_llm_leak_sanitized(self):
        """缺口补答泄漏 agent 未提及的 golden 表名 → 防泄漏护栏整段替换
        （客户不知道数仓表名，合成回复不得成为答案通道）。"""
        q = (
            "## 待确认1：标签字段的业务含义\n"
            "push_label_category_extend 字段里的标签是什么粒度的数据？\n"
            "## 待确认2：数据源表选择\n"
            "我计划使用 `bicoredata.dwd_cam_feature_push_class2_list_ds` 作为源表，请确认。"
        )
        leaky = "用 agapads.ads_persona_push_label_category_extend_ds 就可以。"
        composed, llm_used, _ = self._compose(q, self.GOLDEN_Q49, llm_response=leaky)
        assert llm_used
        # 泄漏段被 TABLE_CONFIRM 固定模板整体替换，golden 表名不出现
        assert "先做功课" in composed
        assert "agapads.ads_persona_push_label_category_extend_ds 就可以" not in composed

    def test_end_to_end_composed_reply_injected(self, tmp_path):
        """端到端：hitl_auto_resolver 主入口对复合提问注入的 ToolMessage
        同时携带表判定与语义补答（q49 形态缩样 + 本地任务索引）。"""
        golden_file = tmp_path / "g.sql"
        golden_file.write_text(self.GOLDEN_Q49, encoding="utf-8")
        index = [
            {
                "task_id": "0",
                "name": "q01",
                "query": "开发push新闻资讯标签序列特征",
                "golden_sql_path": str(golden_file),
            }
        ]
        index_file = tmp_path / "task_index.json"
        index_file.write_text(json.dumps(index), encoding="utf-8")
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>开发push新闻资讯标签序列特征</user_query>"),
                _make_ai_message_with_hil(pending_action=self.Q49_MSG28_PENDING),
            ],
            hitl_task_index_path=str(index_file),
            workspace=str(tmp_path),
        )
        runtime = RuntimeStub(task_index_path=str(index_file), llm_response=self.GAP_LLM_REPLY)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))
        assert result["hitl_auto_resolved"] is True
        assert isinstance(result["messages"][-1], ToolMessage)
        content = result["messages"][-1].content
        assert "符合我的需求" in content
        assert f"另外，{self.GAP_LLM_REPLY}" in content
        assert result["hitl_resolved_info"]["reply_mode"] == "programmatic+gap_llm"
