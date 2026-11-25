# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# ============================================================================
"""metadata_recall 检索不限次 + 口径对照提示 UT。

（用户指令）：取消同 query 熔断与拒后业务词检索
轮次帽——检索次数不再受任何硬限制（防跑偏兜底由 AGENT_CONFIG.max_iter 与
deadline 守卫承担）。保留口径对照提示（拒后台账口径 token 对照
已检索池，mr 结果头部注入"优先核对"候选）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dataagent.actions.tools.semantic_tool.metadata_recall as mr_module
from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.metadata_recall import metadata_recall

QUERY_A = "查找应用市场搜索相关的表，包含搜索关键词与类目预测"
QUERY_B = "查找广告投放 oCPC 校准相关的表"


def _stub_config_manager(**overrides: Any) -> SimpleNamespace:
    """config_manager 替身：支持 .get(key, default)；未设键一律返回默认值
    （真实 ConfigManager 对未设的 HISTORY_SQL.enabled 返回 False，保持
    main 分支新入口守卫的关闭语义，与本文件用例无关）。"""
    table = dict(overrides)

    def _get(key: str, default: Any = None) -> Any:
        return table.get(key, default)

    return SimpleNamespace(get=_get)


def _install_stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    calls: list[str] = []

    async def _fake_sub_agent_tool(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs.get("query", "")))
        return {
            "original_msg": {},
            "frontend_msg": "",
            "state": {"messages": [{"type": "AIMessage", "content": "检索到 3 张相关表"}]},
        }

    @contextmanager
    def _fake_temp_config(temp_config: dict, prefix: str = "", workspace_root: Path | None = None):
        yield "/tmp/fake_sub_agent_config.yaml"

    monkeypatch.setattr(mr_module, "get_current_sandbox", lambda: SimpleNamespace(workspace_root=tmp_path))
    monkeypatch.setattr(mr_module, "_build_metadata_recall_sub_agent_config", lambda *a, **k: {"MODEL": {}})
    monkeypatch.setattr(mr_module, "temporary_sub_agent_config", _fake_temp_config)
    monkeypatch.setattr(mr_module, "sub_agent_tool", _fake_sub_agent_tool)
    return SimpleNamespace(calls=calls)


def _call(query: str, add_user_query: bool = False) -> dict[str, Any]:
    ctx = ToolExecutionContext(config_manager=_stub_config_manager())
    return asyncio.run(metadata_recall(query, add_user_query, _tool_context=ctx))


# ── 检索不限次（熔断/轮次帽已移除，防回归）──────────────────────────────


def test_same_query_never_blocked(monkeypatch, tmp_path):
    """同 query 重复调用始终放行起子代理（熔断已移除）。"""
    stubs = _install_stubs(monkeypatch, tmp_path)

    for _ in range(4):
        r = _call(QUERY_A)
        assert "检索到 3 张相关表" in r["original_msg"]
        assert "熔断提示" not in r["original_msg"]
    assert len(stubs.calls) == 4
    # 熔断计数文件不再落盘
    assert not (tmp_path / ".memory" / "metadata_recall_query_stats.json").exists()


def test_post_rejection_calls_uncapped(monkeypatch, tmp_path):
    """拒后台账存在时，业务词检索也不设轮次帽（轮次帽已移除）。"""
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_rejected_ledger(tmp_path, rounds=1)

    for q in [
        "查找应用市场搜索相关的表",
        "查找广告投放 oCPC 校准相关的表",
        "查找游戏中心充值流水的表",
        "查找视频播放时长明细的表",
        "查找阅读器书架行为的表",
    ]:
        r = _call(q)
        assert "检索轮次提示" not in r["original_msg"]
        assert "检索到 3 张相关表" in r["original_msg"]
    assert len(stubs.calls) == 5
    # 轮次帽状态文件不再落盘
    assert not (tmp_path / ".memory" / "mr_post_rejection_phase.json").exists()


def test_stale_cap_config_ignored(monkeypatch, tmp_path):
    """残留的 post_rejection_call_cap 工具配置不再生效（无该开关）。"""
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_rejected_ledger(tmp_path, rounds=1)

    ctx = ToolExecutionContext(
        config_manager=_stub_config_manager(), tool_config={"post_rejection_call_cap": 1}
    )
    for i in range(3):
        r = asyncio.run(metadata_recall(f"查找业务词{i}相关的表", False, _tool_context=ctx))
        assert "检索轮次提示" not in r["original_msg"]
    assert len(stubs.calls) == 3


# ── 口径对照提示（保留）────────────────────────────────────────────


def _write_rejected_ledger(tmp_path: Path, *, rounds: int = 1, tables: list | None = None,
                           want_tokens: list | None = None) -> None:
    record: dict[str, Any] = {
        "rejected_tables": tables or ["db.ads_wrong_dm"],
        "rounds": rounds,
    }
    if want_tokens:
        record["want_tokens"] = want_tokens
        record["want_phrases"] = ["统计汇总数据" for _ in want_tokens]
    (tmp_path / ".memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".memory" / "hitl_rejected_tables.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def _write_pool_context(tmp_path: Path, tables: list[str]) -> None:
    (tmp_path / ".semantic").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".semantic" / "semantic_retrieve_context_fake001.json").write_text(
        json.dumps({"query": "q", "tables": tables}, ensure_ascii=False), encoding="utf-8"
    )


def test_pool_caliber_note_prepended_on_success(monkeypatch, tmp_path):
    """拒后台账带口径 token 且池内有 token 命中表 → mr 结果头部注入口径对照提示。"""
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_rejected_ledger(tmp_path, rounds=1, want_tokens=["stat"])
    _write_pool_context(tmp_path, ["db.dwd_xtr_list_stat_dm", "db.ads_wrong_dm", "db.other_dm"])

    r = _call("查找业务词新组合相关的表")
    msg = r["original_msg"]
    assert msg.startswith("【检索口径对照】")
    assert "统计汇总数据" in msg  # 口径词转译自 want_phrases
    assert "db.dwd_xtr_list_stat_dm" in msg
    assert "get_table_schema" in msg
    assert "db.ads_wrong_dm" in msg  # 被拒表清单显式列出（不要再提案）
    assert "以用户答复为准" in msg # 用户反馈优先对冲句
    assert "检索到 3 张相关表" in msg  # 子代理原结果保留在提示之后
    assert len(stubs.calls) == 1


def test_pool_caliber_note_absent_without_ledger(monkeypatch, tmp_path):
    """无台账/无口径 token/池内无命中 → 不注入（回落原结果）。"""
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_pool_context(tmp_path, ["db.dwd_stat_dm"])

    r = _call("查找业务词相关的表")
    assert not r["original_msg"].startswith("【检索口径对照】")
    assert "检索到 3 张相关表" in r["original_msg"]

    # 台账无 want_tokens（远变体拒绝）同样不注入
    _write_rejected_ledger(tmp_path, rounds=1)
    r2 = _call("查找另一个业务词相关的表")
    assert not r2["original_msg"].startswith("【检索口径对照】")


def test_pool_note_skips_rejected_and_caps_three(monkeypatch, tmp_path):
    """对照候选剔除被拒表、上限 3 张（token 命中数降序）。"""
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_rejected_ledger(tmp_path, rounds=1, want_tokens=["stat", "xtr"],
                           tables=["db.ads_wrong_dm"])
    _write_pool_context(tmp_path, [
        "db.t_stat_dm", "db.t_xtr_dm", "db.t_stat_xtr_dm", "db.t_stat_2_dm",
        "db.ads_wrong_dm", "db.t_other_dm",
    ])
    r = _call("查找业务词组合相关的表")
    msg = r["original_msg"]
    assert msg.startswith("【检索口径对照】")
    assert "db.t_stat_xtr_dm" in msg  # 双 token 命中居首
    # 恰好 3 张对照候选
    assert msg.count("db.t_") == 3
    assert "db.t_other_dm" not in msg


# ── 口径对照平分决胜（新扩池优先+服务位次，防字典序截断回归）────────────


def _write_pool_context_file(tmp_path: Path, name: str, tables: list[str],
                             created_at: str = "") -> None:
    (tmp_path / ".semantic").mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"query": "q", "tables": tables}
    if created_at:
        payload["created_at"] = created_at
    (tmp_path / ".semantic" / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_pool_note_tiebreak_prefers_newest_expand_and_service_order(monkeypatch, tmp_path):
    """单 token 弱口径平分决胜：新扩池文件里的服务前排表优先于字典序更靠前的
    老池表（防"golden 恰列字典序第 4 被上限 3 截断"回归）。

    构造复刻实测形态：基础池文件（早 created_at）带 2 张字典序靠前的 app 表，
    拒后扩池文件（晚 created_at）带 3 张 app 表（服务位次 0/1/2，其中位次 1
    是口径正解形态表）——期望 top3 全部来自扩池文件且按位次排。
    """
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_rejected_ledger(tmp_path, rounds=1, want_tokens=["app"],
                           tables=["db.ads_rejected_app_dm"])
    _write_pool_context_file(
        tmp_path, "semantic_retrieve_context_base01.json",
        ["db.a_app_old1_dm", "db.b_app_old2_dm", "db.ads_rejected_app_dm"],
        created_at="2026-11-19T02:40:14.000000+00:00",
    )
    _write_pool_context_file(
        tmp_path, "semantic_retrieve_context_new01.json",
        ["db.ads_noise_app_dm", "db.z_gold_app_target_dm", "db.c_app_new3_dm"],
        created_at="2026-11-19T02:57:59.000000+00:00",
    )
    r = _call("查找业务词新组合相关的表")
    msg = r["original_msg"]
    assert msg.startswith("【检索口径对照】")
    # 新扩池三张按服务位次进 top3（noise@0、golden@1、new3@2）
    assert "db.z_gold_app_target_dm" in msg
    assert "db.ads_noise_app_dm" in msg
    assert "db.c_app_new3_dm" in msg
    # 老池字典序靠前的两张被新文件决胜挤出（上限 3）
    assert "db.a_app_old1_dm" not in msg
    assert "db.b_app_old2_dm" not in msg
    # 被拒表不进对照候选
    assert "db.ads_rejected_app_dm 描述" not in msg


def test_pool_note_tiebreak_within_same_file_uses_service_position(monkeypatch, tmp_path):
    """同文件内平分（同 created_at）→ 服务位次决胜：位次靠前者先进 top3。"""
    stubs = _install_stubs(monkeypatch, tmp_path)
    _write_rejected_ledger(tmp_path, rounds=1, want_tokens=["app"],
                           tables=["db.ads_rejected_app_dm"])
    # 同一文件 4 张单 token 命中表（位次 0/1/2/3）+ 无 created_at 的第二文件
    # 一张字典序最靠前的表——同池同分时位次优先于表名
    _write_pool_context_file(
        tmp_path, "semantic_retrieve_context_same01.json",
        ["db.z_app_pos0_dm", "db.a_app_pos1_dm", "db.m_app_pos2_dm", "db.b_app_pos3_dm"],
        created_at="2026-11-19T03:00:00.000000+00:00",
    )
    _write_pool_context_file(
        tmp_path, "semantic_retrieve_context_same02.json",
        ["db.aaa_app_nocstamp_dm"],
    )
    r = _call("查找业务词新组合相关的表")
    msg = r["original_msg"]
    assert msg.startswith("【检索口径对照】")
    assert "db.z_app_pos0_dm" in msg
    assert "db.a_app_pos1_dm" in msg
    assert "db.m_app_pos2_dm" in msg
    # 第 4 位与无 created_at 的表都被挤出
    assert "db.b_app_pos3_dm" not in msg
    assert "db.aaa_app_nocstamp_dm" not in msg
