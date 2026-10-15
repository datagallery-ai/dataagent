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
