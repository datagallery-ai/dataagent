# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# ============================================================================
"""L3+L4：delivery_guard（planner post 交付守卫）UT。

背景：检索全中但零交付 SQL（L3）；三轮确认 4 表、
SQL 只用 2 张（L4）。守卫在 planner 收尾前拦截并注入提醒，fire 上限 2 次。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from dataagent.core.suite.builtin_suites.de_agent.hooks.delivery_guard import delivery_guard


class RuntimeStub:
    def __init__(self, *, enable_hitl: bool = True, auto_resolve: bool = True):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {
                "enable_human_feedback": enable_hitl,
                "hitl_auto_resolve": auto_resolve,
            }
        }
        self._cache: dict[str, Any] = {}
        self.workspace_dir = None

    def get_all_config(self) -> dict[str, Any]:
        return self._config

    def get_cache(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def set_cache(self, key: str, value: Any) -> None:
        self._cache[key] = value


def _make_workspace(tmpdir: str, *, sqls: dict[str, str] | None = None, confirmed: list[str] | None = None) -> Path:
    root = Path(tmpdir)
    for name, content in (sqls or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if confirmed is not None:
        memory_dir = root / ".memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "hitl_confirmed_tables.json").write_text(
            json.dumps({"confirmed_tables": confirmed, "rounds": 1}), encoding="utf-8"
        )
    return root


def _result(
    *,
    complete: bool = True,
    messages: Any = None,
    workspace: str | None = None,
    enable_hitl: bool = True,
) -> dict[str, Any]:
    if messages is None:
        messages = AIMessage(content="任务完成，交付物已就绪。")
    result: dict[str, Any] = {
        "messages": messages,
        "complete": complete,
        "enable_human_feedback": enable_hitl,
        "hitl_auto_resolve": True,
    }
    if workspace:
        result["workspace"] = workspace
    return result


CREATE_X = "create table ads_result_dm (id bigint);"
INSERT_X = "insert into ads_result_dm select id from biads.dwd_src_dm;"
CREATE_Y = "create table ads_other_dm (id bigint);"


class TestDeliveryGuardL3:
    def test_no_delivery_sql_blocks(self):
        """L3 型：工作区零交付 SQL → 拦截，complete=False，提醒写入 AI content。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, sqls={"explore_sample.sql": "select 1;"})
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded["complete"] is False
            assert "未发现任何 create_*.sql / insert_*.sql" in guarded["messages"][-1].content
            assert runtime.get_cache("delivery_guard.fire_count") == 1

    def test_create_without_insert_blocks(self):
        """L3 配对：create 建表无 INSERT 目标覆盖 → 拦截并点名。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, sqls={"create_result.sql": CREATE_X})
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded["complete"] is False
            assert "create_result.sql" in guarded["messages"][-1].content
            assert "缺少对应的数据写入脚本" in guarded["messages"][-1].content

    def test_paired_delivery_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(
                tmpdir, sqls={"create_result.sql": CREATE_X, "insert_result.sql": INSERT_X}
            )
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded is result
            assert runtime.get_cache("delivery_guard.fire_count") is None

    def test_stem_fallback_when_content_unparseable(self):
        """内容解析不出建表名 → 文件名 stem 配对（create_X ↔ insert_X）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(
                tmpdir,
                sqls={
                    "create_result.sql": "-- 外部工具方言\nSOME NONSTMT setup;\n",
                    "insert_result.sql": "-- 数据写入\nSOME NONSTMT load;\n",
                },
            )
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded is result


class TestDeliveryGuardL4:
    def test_confirmed_table_unused_blocks(self):
        """L4 型：已确认源表未在交付 SQL 的 FROM/JOIN 中引用 → 拦截提醒。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(
                tmpdir,
                sqls={"create_result.sql": CREATE_X, "insert_result.sql": INSERT_X},
                confirmed=["biads.dwd_src_dm", "biads.dwd_missing_dm"],
            )
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded["complete"] is False
            content = guarded["messages"][-1].content
            assert "dwd_missing_dm" in content
            assert "dwd_src_dm" not in content.replace("dwd_src_dm 已确认", "")  # 已用的不点名

    def test_bare_name_confirmed_matches_full_reference(self):
        """确认清单裸名（省 db 前缀）与 SQL 全名引用比对通过。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(
                tmpdir,
                sqls={"create_result.sql": CREATE_X, "insert_result.sql": INSERT_X},
                confirmed=["dwd_src_dm"],
            )
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded is result

    def test_confirmed_file_absent_passes(self):
        """无确认清单（未经历 TABLE_CONFIRM）→ L4 检查跳过。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(
                tmpdir, sqls={"create_result.sql": CREATE_X, "insert_result.sql": INSERT_X}
            )
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded is result


class TestDeliveryGuardGating:
    def test_incomplete_result_skips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            result = _result(complete=False, workspace=str(root))
            assert delivery_guard(result, runtime, original_state=dict(result)) is result

    def test_interactive_hitl_skips(self):
        """交互场景（enable_human_feedback=False）不受影响。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub(enable_hitl=False)
            result = _result(workspace=str(root), enable_hitl=False)
            assert delivery_guard(result, runtime, original_state=dict(result)) is result

    def test_fire_limit_two(self):
        """fire 上限 2 次：第三次直接放行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            first = delivery_guard(result, runtime, original_state=dict(result))
            assert first["complete"] is False
            second = delivery_guard(_result(workspace=str(root)), runtime, original_state=dict(result))
            assert second["complete"] is False
            third = delivery_guard(_result(workspace=str(root)), runtime, original_state=dict(result))
            assert third["complete"] is True
            assert runtime.get_cache("delivery_guard.fire_count") == 2

    def test_bare_ai_message_handled(self):
        """planner._build_result 裸 AIMessage（非列表）→ 追加提醒后成 [updated]。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            bare = AIMessage(content="任务完成。")
            result = _result(messages=bare, workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert isinstance(guarded["messages"], list)
            assert len(guarded["messages"]) == 1
            assert "交付守卫提醒" in guarded["messages"][0].content

    def test_reminder_appended_to_last_ai_in_list(self):
        """列表形态：提醒追加进最后一条 AI 的 content，不新增 HumanMessage 置尾。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            messages = [HumanMessage(content="原始任务"), AIMessage(content="任务完成。")]
            result = _result(messages=messages, workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert len(guarded["messages"]) == 2
            assert isinstance(guarded["messages"][-1], AIMessage)
            assert "交付守卫提醒" in guarded["messages"][-1].content
            assert guarded["messages"][-1].content.startswith("任务完成。")


class TestDeliveryGuardV42:
    """空文件穿透修复 + 硬指令 + fire 计数持久化（chat 重入不重置）。"""

    def test_hollow_files_not_counted_as_delivery(self):
        """文件名对但内容为空/仅注释 → 拦截 + 停止检索硬指令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(
                tmpdir,
                sqls={
                    "create_result.sql": "-- 计划占位\n/* TODO */\n",
                    "insert_result.sql": "\n\n",
                },
            )
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded["complete"] is False
            content = guarded["messages"][-1].content
            assert "不构成有效交付" in content
            assert "create_result.sql" in content
            assert "停止一切新的元数据检索" in content

    def test_no_files_gets_stop_retrieval_instruction(self):
        """零交付文件 → 拦截提醒带"停止检索立即写 SQL"硬指令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            content = guarded["messages"][-1].content
            assert "停止一切新的元数据检索" in content
            assert "直接修复" not in content

    def test_substantive_delivery_gets_fix_direct_instruction(self):
        """已有实质交付但配对缺失 → 硬指令为"直接修复勿再检索"。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir, sqls={"create_result.sql": CREATE_X})
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            content = guarded["messages"][-1].content
            assert "直接修复上述问题后交付" in content
            assert "停止一切新的元数据检索" not in content

    def test_second_fire_gets_last_warning(self):
        """第 2 次（最后一次）拦截 → 标题带"最后提醒"且含放行警告。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            result = _result(workspace=str(root))
            delivery_guard(result, runtime, original_state=dict(result))
            second = delivery_guard(
                _result(workspace=str(root)), runtime, original_state=dict(result)
            )
            content = second["messages"][-1].content
            header_line = next(line for line in content.splitlines() if "交付守卫提醒" in line)
            assert "最后提醒" in header_line
            assert "最后一次自动拦截" in content

    def test_fires_persist_across_runtime_cache_reset(self):
        """chat 重入 runtime cache 重置 → state 文件保住计数不 third-strike。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime_a = RuntimeStub()
            result = _result(workspace=str(root))
            delivery_guard(result, runtime_a, original_state=dict(result))
            # chat 重入：新 runtime 实例（cache 空），workspace 不变
            runtime_b = RuntimeStub()
            second = delivery_guard(
                _result(workspace=str(root)), runtime_b, original_state=dict(result)
            )
            assert second["complete"] is False
            assert runtime_b.get_cache("delivery_guard.fire_count") == 2
            state = json.loads((root / ".memory" / "delivery_guard_state.json").read_text())
            assert state["fires"] == 2
            # 第 3 次（又一次新 runtime）→ 持久化计数已达上限，直接放行
            runtime_c = RuntimeStub()
            third = delivery_guard(
                _result(workspace=str(root)), runtime_c, original_state=dict(result)
            )
            assert third["complete"] is True

    def test_runtime_cache_higher_than_state_wins(self):
        """fires = max(cache, state)：cache 计数高于文件时以 cache 为准。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_workspace(tmpdir)
            runtime = RuntimeStub()
            runtime.set_cache("delivery_guard.fire_count", 2)
            result = _result(workspace=str(root))
            guarded = delivery_guard(result, runtime, original_state=dict(result))
            assert guarded["complete"] is True
