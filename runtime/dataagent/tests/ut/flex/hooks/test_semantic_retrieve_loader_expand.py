# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# ============================================================================
"""L1b-heavy：semantic_retrieve_context_loader 的拒绝后扩池检索 UT。

机制（见 hitl_table_selection_probe.md 10-15 追记）：hitl_auto_resolver 在源表
被拒时写 workspace/.semantic/hitl_expand_retrieve_request.json；loader 消费
标记，用 mr 子代理本轮 query 的业务词再打一次 semantic/retrieve——新 query →
新 sha256 缓存键 → 新 context 文件 → 评测侧 recalled 池（全部 context 文件
并集）单调扩大。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from dataagent.core.flex.hooks.semantic_retrieve import (
    _extract_expand_query,
    _maybe_expand_retrieve_after_rejection,
)

BUSINESS_QUERY = "用户本轮的检索需求：搜索问答结果表 NLU 标签 得分。"
SCHEMA_QUERY = "用户本轮的检索需求：查看 agsearch.dws_x_dm 表的表结构和分区信息。"


def _make_workspace(tmpdir: str, *, context_files: int = 1, marker: dict | None = None) -> Path:
    root = Path(tmpdir)
    semantic_dir = root / ".semantic"
    semantic_dir.mkdir(parents=True, exist_ok=True)
    for i in range(context_files):
        (semantic_dir / f"semantic_retrieve_context_fake{i:03d}.json").write_text(
            json.dumps({"query": f"q{i}", "tables": ["db.t"]}), encoding="utf-8"
        )
    if marker is not None:
        (semantic_dir / "hitl_expand_retrieve_request.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
    return root


def _marker(round_index: int = 1) -> dict:
    return {
        "trigger": "table_confirm_rejected",
        "rejected_tables": ["db.ads_wrong_dm"],
        "original_query": "原始任务",
        "rejection_round": round_index,
        "created_at": "2026-10-15T00:00:00",
    }


def _payload(tables: list[str] | None = None) -> dict[str, Any]:
    # 注意：tables=[] 是合法取值（零表），不能用 `or` 兜底（空列表 falsy 会落到默认）
    if tables is None:
        tables = ["db.new_candidate_dm"]
    rows = "".join(f"\n{t} 描述：d，数据留存期（天）：" for t in tables)
    raw_tables = [{"db": t.split(".")[0], "table": t.split(".")[-1]} for t in tables]
    return {
        "source": "hitl_expand_retrieve",
        "query": "搜索问答结果表 NLU 标签 得分",
        "tables": tables,
        "context_text": f"匹配原始query的表如下：（query为 x）{rows}",
        "raw_response": {"dataAccessPlan": {"tables": raw_tables}},
    }


class TestExtractExpandQuery:
    def test_extracts_business_terms(self):
        assert _extract_expand_query(BUSINESS_QUERY) == "搜索问答结果表 NLU 标签 得分"

    def test_cuts_appended_original_task(self):
        q = _extract_expand_query(
            "用户本轮的检索需求：NLU 二级类目 标签。\n需要同时检索和原始任务相关的元数据，"
            "以下是原始任务的描述：xxx"
        )
        assert q == "NLU 二级类目 标签"

    def test_schema_check_query_returns_none(self):
        assert _extract_expand_query(SCHEMA_QUERY) is None

    def test_empty_returns_none(self):
        assert _extract_expand_query("") is None
        assert _extract_expand_query("用户本轮的检索需求：。") is None


class TestExpandRetrieveFlow:
    def _state(self, query: str = BUSINESS_QUERY) -> dict[str, Any]:
        return {
            "user_query": (
                f"{query}\n\n<semantic_retrieve_context>\n主 context\n</semantic_retrieve_context>"
            ),
            "messages": [],
        }

    def _run(self, state, workspace_root, config_manager=MagicMock()):
        with patch(
            "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
        ) as mock_client_cls, patch(
            "dataagent.core.flex.hooks.semantic_retrieve.get_semantic_retrieve_context",
            return_value=_payload(),
        ) as mock_retrieve, patch(
            "dataagent.core.flex.hooks.semantic_retrieve.read_semantic_retrieve_context_cache",
            return_value=None,
        ):
            mock_client_cls.from_config.return_value = MagicMock()
            result = _maybe_expand_retrieve_after_rejection(
                state, str(state["user_query"]).split("\n")[0], workspace_root, config_manager, None, None
            )
        return result, mock_retrieve

    def test_marker_consumed_and_extra_context_injected(self):
        """标记 + 业务词 query → 打重端点（新 query）→ 消费标记 → 注入第二个语义块。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state()
            result, mock_retrieve = self._run(state, root)
            mock_retrieve.assert_called_once()
            call = mock_retrieve.call_args
            assert call.args[0] == "搜索问答结果表 NLU 标签 得分"
            assert call.kwargs["source"] == "hitl_expand_retrieve"
            assert not (root / ".semantic" / "hitl_expand_retrieve_request.json").exists()
            assert result["user_query"].count("<semantic_retrieve_context>") == 2
            assert "补充检索" in result["user_query"]
            assert "db.new_candidate_dm" in result["user_query"]

    def test_schema_check_query_keeps_marker(self):
        """schema 核查型本轮不扩池，保留标记等下一轮业务词检索。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state(SCHEMA_QUERY)
            result, mock_retrieve = self._run(state, root)
            mock_retrieve.assert_not_called()
            assert (root / ".semantic" / "hitl_expand_retrieve_request.json").exists()

    def test_cap_consumes_marker_without_retrieve(self):
        """context 文件数达上限（3）→ 消费标记、不再扩池。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, context_files=3, marker=_marker())
            state = self._state()
            result, mock_retrieve = self._run(state, root)
            mock_retrieve.assert_not_called()
            assert not (root / ".semantic" / "hitl_expand_retrieve_request.json").exists()

    def test_dedup_keeps_marker_waiting_for_new_terms(self):
        """该业务词 query 已有缓存 → 跳过但保留标记（等不同业务词的下一轮）。"""
        from dataagent.core.flex.hooks import semantic_retrieve as sr

        def fake_cache_read(query, *, workspace_root):
            if query == "搜索问答结果表 NLU 标签 得分":
                return {"query": query, "tables": ["db.t"]}
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state()
            with patch.object(sr, "read_semantic_retrieve_context_cache", side_effect=fake_cache_read):
                result = _maybe_expand_retrieve_after_rejection(
                    state,
                    "搜索问答结果表 NLU 标签 得分",
                    root,
                    MagicMock(),
                    None,
                    None,
                )
            assert (root / ".semantic" / "hitl_expand_retrieve_request.json").exists()

    def test_retrieve_failure_tolerated_marker_consumed(self):
        """扩池检索失败仅告警不终止（state 原样返回），标记已消费（单次尝试）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state()
            with patch(
                "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
            ) as mock_client_cls, patch(
                "dataagent.core.flex.hooks.semantic_retrieve.get_semantic_retrieve_context",
                side_effect=RuntimeError("service down"),
            ), patch(
                "dataagent.core.flex.hooks.semantic_retrieve.read_semantic_retrieve_context_cache",
                return_value=None,
            ):
                mock_client_cls.from_config.return_value = MagicMock()
                result = _maybe_expand_retrieve_after_rejection(
                    state, "搜索问答结果表 NLU 标签 得分", root, MagicMock(), None, None
                )
            assert result is state
            assert "complete" not in result
            assert not (root / ".semantic" / "hitl_expand_retrieve_request.json").exists()

    def test_empty_payload_skips_injection(self):
        """扩池检索返回零表 → 不注入（标记已消费）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state()
            with patch(
                "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
            ) as mock_client_cls, patch(
                "dataagent.core.flex.hooks.semantic_retrieve.get_semantic_retrieve_context",
                return_value=_payload([]),
            ), patch(
                "dataagent.core.flex.hooks.semantic_retrieve.read_semantic_retrieve_context_cache",
                return_value=None,
            ):
                mock_client_cls.from_config.return_value = MagicMock()
                result = _maybe_expand_retrieve_after_rejection(
                    state, "搜索问答结果表 NLU 标签 得分", root, MagicMock(), None, None
                )
            assert result["user_query"].count("<semantic_retrieve_context>") == 1

    def test_no_marker_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            state = self._state()
            result, mock_retrieve = self._run(state, root)
            mock_retrieve.assert_not_called()
            assert result is state

    def _run_with_payload(self, state, root, payload):
        with patch(
            "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
        ) as mock_client_cls, patch(
            "dataagent.core.flex.hooks.semantic_retrieve.get_semantic_retrieve_context",
            return_value=payload,
        ) as mock_retrieve, patch(
            "dataagent.core.flex.hooks.semantic_retrieve.read_semantic_retrieve_context_cache",
            return_value=None,
        ):
            mock_client_cls.from_config.return_value = MagicMock()
            result = _maybe_expand_retrieve_after_rejection(
                state, "搜索问答结果表 NLU 标签 得分", root, MagicMock(), None, None
            )
        return result, mock_retrieve

    def test_expand_payload_filters_rejected_tables(self):
        """扩池被拒表过滤：扩池候选混入被拒表 → 注入前剔除，打破拒→带回→再提→再拒循环。

        磁盘 context 缓存保持端点原貌（口径不变），仅注入文本与内存 payload 过滤。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state()
            payload = _payload(["db.ads_wrong_dm", "db.new_candidate_dm"])
            result, mock_retrieve = self._run_with_payload(state, root, payload)
            mock_retrieve.assert_called_once()
            assert result["user_query"].count("<semantic_retrieve_context>") == 2
            assert "db.new_candidate_dm" in result["user_query"]
            assert "db.ads_wrong_dm" not in result["user_query"]
            # 被拒表的描述行也不得混入（context_text 按行过滤）
            assert "db.ads_wrong_dm 描述" not in result["user_query"]

    def test_expand_all_rejected_skips_injection(self):
        """扩池候选全部是被拒表 → 过滤后无剩余 → 跳过注入（不注入空块）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            state = self._state()
            payload = _payload(["db.ads_wrong_dm"])
            result, mock_retrieve = self._run_with_payload(state, root, payload)
            mock_retrieve.assert_called_once()
            assert result["user_query"].count("<semantic_retrieve_context>") == 1
            assert not (root / ".semantic" / "hitl_expand_retrieve_request.json").exists()

    def test_expand_unrelated_tables_not_filtered(self):
        """被拒表与候选无宽松等价命中 → payload 原样注入（不过滤无关表）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = _marker()
            marker["rejected_tables"] = ["db.ads_other_dm"]
            root = _make_workspace(tmpdir, marker=marker)
            state = self._state()
            payload = _payload(["db.ads_wrong_dm", "db.new_candidate_dm"])
            result, mock_retrieve = self._run_with_payload(state, root, payload)
            mock_retrieve.assert_called_once()
            assert result["user_query"].count("<semantic_retrieve_context>") == 2
            assert "db.ads_wrong_dm" in result["user_query"]
            assert "db.new_candidate_dm" in result["user_query"]


class TestFollowupAskSkip:
    """追问 chat 防浪费：应答型文本开头命中 → 整轮跳过检索（不打分钟级重端点）。"""

    FOLLOWUP_QUERY = "请简要回答你选择数据源表的原因"

    def test_followup_ask_skips_retrieve(self):
        from dataagent.core.flex.hooks.semantic_retrieve import (
            semantic_retrieve_context_loader,
        )

        state = {"user_query": self.FOLLOWUP_QUERY, "messages": []}
        with patch(
            "dataagent.core.flex.hooks.semantic_retrieve.get_current_query",
            return_value=self.FOLLOWUP_QUERY,
        ), patch(
            "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
        ) as mock_client_cls, patch(
            "dataagent.core.flex.hooks.semantic_retrieve.get_semantic_retrieve_context"
        ) as mock_retrieve:
            result = semantic_retrieve_context_loader(state, MagicMock())
        assert result is state
        assert result["user_query"] == self.FOLLOWUP_QUERY
        mock_client_cls.from_config.assert_not_called()
        mock_retrieve.assert_not_called()
        assert "complete" not in result

    def test_normal_task_query_proceeds_to_injection(self):
        """非应答型 query（普通任务原文）不受 guard 影响，照常注入检索 context。"""
        from dataagent.core.flex.hooks.semantic_retrieve import (
            semantic_retrieve_context_loader,
        )

        task_query = "请基于近30天应用下载明细开发设备维度特征表"
        state = {"user_query": task_query, "messages": [], "workspace": None}
        with tempfile.TemporaryDirectory() as tmpdir:
            state["workspace"] = tmpdir
            with patch(
                "dataagent.core.flex.hooks.semantic_retrieve.get_current_query",
                return_value=task_query,
            ), patch(
                "dataagent.core.flex.hooks.semantic_retrieve.read_semantic_retrieve_context_cache",
                return_value=_payload(),
            ), patch(
                "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
            ) as mock_client_cls:
                result = semantic_retrieve_context_loader(state, MagicMock())
        mock_client_cls.from_config.assert_not_called()
        assert "<semantic_retrieve_context>" in result["user_query"]
        assert "db.new_candidate_dm" in result["user_query"]
        assert "complete" not in result


class TestBasePoolRejectedLedgerFilter:
    """基础池/扩池注入前的被拒表过滤（持久台账消费）。

    - 基础 context（原始 query 的 sha256 缓存）全程复用，被拒表常驻注入块
      是死局的回流主通道——注入前按台账过滤（滤空保护）；
    - 扩池过滤并入台账历史轮被拒表（标记只带本轮拒绝）。
    - 磁盘缓存保持端点原貌：过滤仅作用于内存 payload。
    """

    GOLDEN_LIKE = "db.dwd_b_dm"
    REJECTED = "db.ads_wrong_dm"

    def _ledger(self, tables: list[str], rounds: int = 1) -> dict:
        return {"rejected_tables": tables, "rounds": rounds, "want_tokens": ["stat"]}

    def test_base_result_filters_rejected_tables(self):
        from dataagent.core.flex.hooks.semantic_retrieve import (
            _filter_base_result_by_rejected_ledger,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".memory").mkdir(parents=True)
            (root / ".memory" / "hitl_rejected_tables.json").write_text(
                json.dumps(self._ledger([self.REJECTED])), encoding="utf-8"
            )
            payload = _payload([self.REJECTED, self.GOLDEN_LIKE])
            filtered = _filter_base_result_by_rejected_ledger(payload, root)
            assert filtered is not payload
            assert filtered["tables"] == [self.GOLDEN_LIKE]
            assert self.REJECTED not in filtered["context_text"]
            assert self.GOLDEN_LIKE in filtered["context_text"]
            # 磁盘缓存口径不受影响：raw_response 保留端点原貌
            assert filtered["raw_response"] == payload["raw_response"]

    def test_base_result_unchanged_without_ledger(self):
        from dataagent.core.flex.hooks.semantic_retrieve import (
            _filter_base_result_by_rejected_ledger,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = _payload([self.REJECTED, self.GOLDEN_LIKE])
            assert _filter_base_result_by_rejected_ledger(payload, root) is payload

    def test_base_result_unchanged_when_no_overlap(self):
        from dataagent.core.flex.hooks.semantic_retrieve import (
            _filter_base_result_by_rejected_ledger,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".memory").mkdir(parents=True)
            (root / ".memory" / "hitl_rejected_tables.json").write_text(
                json.dumps(self._ledger(["db.unrelated_dm"])), encoding="utf-8"
            )
            payload = _payload()
            assert _filter_base_result_by_rejected_ledger(payload, root) is payload

    def test_base_result_keeps_original_when_all_rejected(self):
        """滤空保护：全为被拒表时保留原结果（宁可看见已知被拒候选也不注入空清单）。"""
        from dataagent.core.flex.hooks.semantic_retrieve import (
            _filter_base_result_by_rejected_ledger,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".memory").mkdir(parents=True)
            (root / ".memory" / "hitl_rejected_tables.json").write_text(
                json.dumps(self._ledger([self.REJECTED])), encoding="utf-8"
            )
            payload = _payload([self.REJECTED])
            assert _filter_base_result_by_rejected_ledger(payload, root) is payload

    def test_bare_name_ledger_entry_filters_qualified(self):
        """台账裸名/限定名混写容忍：裸表名也过滤限定名候选。"""
        from dataagent.core.flex.hooks.semantic_retrieve import (
            _filter_base_result_by_rejected_ledger,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".memory").mkdir(parents=True)
            (root / ".memory" / "hitl_rejected_tables.json").write_text(
                json.dumps(self._ledger(["ads_wrong_dm"])), encoding="utf-8"
            )
            payload = _payload([self.REJECTED, self.GOLDEN_LIKE])
            filtered = _filter_base_result_by_rejected_ledger(payload, root)
            assert filtered["tables"] == [self.GOLDEN_LIKE]

    def test_expand_filter_unions_ledger_history(self):
        """扩池过滤并入台账历史轮被拒表：标记 rejected + 台账 rejected 全部剔除。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, marker=_marker())
            (root / ".memory").mkdir(parents=True)
            (root / ".memory" / "hitl_rejected_tables.json").write_text(
                json.dumps(self._ledger(["db.ledger_rejected_dm"])), encoding="utf-8"
            )
            # 扩池端点返回：标记本轮被拒表 + 台账历史被拒表 + 新候选
            payload = _payload(["db.ads_wrong_dm", "db.ledger_rejected_dm", "db.new_candidate_dm"])
            state = {
                "user_query": (
                    f"{BUSINESS_QUERY}\n\n<semantic_retrieve_context>\n主 context\n"
                    "</semantic_retrieve_context>"
                ),
                "messages": [],
            }
            with patch(
                "dataagent.core.flex.hooks.semantic_retrieve.SemanticServiceClient"
            ) as mock_client_cls, patch(
                "dataagent.core.flex.hooks.semantic_retrieve.get_semantic_retrieve_context",
                return_value=payload,
            ), patch(
                "dataagent.core.flex.hooks.semantic_retrieve.read_semantic_retrieve_context_cache",
                return_value=None,
            ):
                mock_client_cls.from_config.return_value = MagicMock()
                result = _maybe_expand_retrieve_after_rejection(
                    state,
                    str(state["user_query"]).split("\n")[0],
                    root,
                    MagicMock(),
                    None,
                    None,
                )
            injected = result["user_query"]
            assert "db.new_candidate_dm" in injected
            assert "db.ads_wrong_dm 描述" not in injected
            assert "db.ledger_rejected_dm 描述" not in injected
