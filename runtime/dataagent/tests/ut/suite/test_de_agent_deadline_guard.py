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
"""deadline_guard UT。

死局（实证）：守卫/扩池/过滤全部正确工作，agent 仍在
拒绝后的检索循环里 churn 到 wall-clock 硬超时（10000s）以 NO_SQL 零分收场。
deadline_guard 在 forcing window 内向 planner 工作轮注入硬交付指令（每任务
一次，workspace 锚定起始时间）。

覆盖：窗口判定/注入/去重/门控（complete、run_id、预算未配置）/锚点
持久与损坏重锚/消息增量形态（裸 AIMessage 与增量 list）。
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.suite.builtin_suites.de_agent.hooks.deadline_guard import (
    _deadline_config,
    _iter_config,
    deadline_guard,
    deadline_guard_pre,
)

BUDGET = 600
WINDOW = 300


class _Runtime:
    def __init__(self, workspace: Path, agent_config: dict[str, Any] | None = None) -> None:
        self.run_id = 0
        self.workspace_dir = str(workspace)
        self._config = {"AGENT_CONFIG": agent_config if agent_config is not None else {
            "task_wall_budget_seconds": BUDGET,
            "task_forcing_window_seconds": WINDOW,
        }}

    def get_all_config(self) -> dict[str, Any]:
        return self._config


def _state(messages: list | Any | None = None, **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": messages if messages is not None else [AIMessage(content="thinking")],
        "complete": False,
        "run_id": 0,
    }
    state.update(extra)
    return state


def _rewind_anchor(workspace: Path, elapsed_seconds: int) -> None:
    marker = workspace / ".memory" / "task_run_started_at.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    started = (datetime.now(UTC) - timedelta(seconds=elapsed_seconds)).isoformat()
    marker.write_text(json.dumps({"started_at": started}), encoding="utf-8")


def test_pre_window_no_op_but_anchor_written():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        out = deadline_guard(state, _Runtime(root))
        assert out is state
        assert (root / ".memory" / "task_run_started_at.json").is_file()


def test_in_window_injects_directive_and_marks():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard(state, _Runtime(root))  # 锚定起始时间
        _rewind_anchor(root, BUDGET - WINDOW + 50)  # elapsed 350 > 300 进窗
        out = deadline_guard(state, _Runtime(root))
        assert out is not state
        assert "任务收尾提醒" in out["messages"][0].content
        assert "create_" in out["messages"][0].content or "insert_" in out["messages"][0].content
        injected = json.loads(
            (root / ".memory" / "deadline_forcing_injected.json").read_text(encoding="utf-8")
        )
        assert injected["injected"] is True


def test_second_call_deduped_per_task():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        deadline_guard(state, _Runtime(root))
        out2 = deadline_guard(state, _Runtime(root))
        assert out2 is state


def test_complete_turn_skipped():
    """收尾轮不注入：完成度核对是 delivery_guard 的职责。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(complete=True)
        deadline_guard(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out = deadline_guard(state, _Runtime(root))
        assert out is state
        assert not (root / ".memory" / "deadline_forcing_injected.json").is_file()


def test_followup_chat_run_id_skipped():
    """run_id=1（评测追问 chat）不注入——工作锚定在首个任务 chat。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(run_id=1)
        runtime = _Runtime(root)
        runtime.run_id = 1
        deadline_guard(state, runtime)
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out = deadline_guard(state, runtime)
        assert out is state
        assert not (root / ".memory" / "deadline_forcing_injected.json").is_file()


def test_unset_budget_no_op():
    """预算未配置 → 整体 no-op（通用配置零影响）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard(state, _Runtime(root))
        _rewind_anchor(root, 10 * BUDGET)
        out = deadline_guard(state, _Runtime(root, agent_config={}))
        assert out is state
        # 窗口缺省 1500：仅配预算时 window 取缺省
        budget, window = _deadline_config(
            _Runtime(root, agent_config={"task_wall_budget_seconds": 9800})
        )
        assert (budget, window) == (9800, 1500)


def test_invalid_budget_no_op():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        runtime = _Runtime(root, agent_config={"task_wall_budget_seconds": "abc"})
        deadline_guard(state, runtime)
        _rewind_anchor(root, 10 * BUDGET)
        out = deadline_guard(state, runtime)
        assert out is state


def test_invalid_window_falls_back_default():
    budget, window = _deadline_config(
        _Runtime(Path("/tmp/nonexistent-deadline-ut"), agent_config={
            "task_wall_budget_seconds": 600,
            "task_forcing_window_seconds": "bad",
        })
    )
    assert (budget, window) == (600, 1500)


def test_corrupt_anchor_reanchored_to_now():
    """锚点损坏按本轮重写（宁可晚锚不可误锚早）→ elapsed≈0 不注入。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        marker = root / ".memory" / "task_run_started_at.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("not-json{", encoding="utf-8")
        out = deadline_guard(state, _Runtime(root))
        assert out is state
        rewritten = json.loads(marker.read_text(encoding="utf-8"))
        assert datetime.fromisoformat(rewritten["started_at"]) > datetime.now(UTC) - timedelta(minutes=1)


def test_messages_increment_list_appends_to_last_ai():
    """post-hook result["messages"] 是节点增量 list（含 ToolMessage）——指令
    追加进最后一条 AIMessage，不新增消息置尾（executor 要求 messages[-1]
    为 AIMessage）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        messages = [
            AIMessage(content="plan"),
            ToolMessage(content="tool output", tool_call_id="c1"),
            AIMessage(content="final"),
        ]
        state = _state(messages=messages)
        deadline_guard(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out = deadline_guard(state, _Runtime(root))
        assert isinstance(out["messages"], list)
        assert len(out["messages"]) == 3
        assert "任务收尾提醒" in out["messages"][2].content
        assert "任务收尾提醒" not in out["messages"][0].content
        # 原消息列表不被就地改动
        assert "任务收尾提醒" not in messages[2].content


def test_runtime_without_config_api_no_op():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        runtime = SimpleNamespace(run_id=0, workspace_dir=str(root))  # 无 get_all_config
        _rewind_anchor(root, 10 * BUDGET)
        out = deadline_guard(state, runtime)
        assert out is state


def test_remaining_minutes_in_directive():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - 100)  # 剩余约 100s → 至少 1 分钟
        out = deadline_guard(state, _Runtime(root))
        assert "分钟" in out["messages"][0].content


# ============ deadline_guard_pre（planner.pre 注入位） ============


def test_pre_in_window_appends_human_message():
    """pre 注入：LLM 调用前追加 HumanMessage（当轮可见），不改动既有消息。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard_pre(state, _Runtime(root))  # 锚定起始时间
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out = deadline_guard_pre(state, _Runtime(root))
        assert isinstance(out, dict)
        messages = out["messages"]
        assert len(messages) == 2
        assert isinstance(messages[-1], HumanMessage)
        assert "任务收尾提醒" in messages[-1].content
        assert "纪律 0" in messages[-1].content # 优先级链措辞
        assert messages[0].content == "thinking"  # 既有消息不动
        assert json.loads(
            (root / ".memory" / "deadline_forcing_injected.json").read_text(encoding="utf-8")
        )["injected"] is True


def test_pre_then_post_shared_dedup():
    """pre 已注入 → post 同任务去重（共享标记，先到先得）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard_pre(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out_pre = deadline_guard_pre(state, _Runtime(root))
        assert len(out_pre["messages"]) == 2
        out_post = deadline_guard(out_pre, _Runtime(root))
        assert out_post is out_pre  # post 不再注入


def test_post_then_pre_shared_dedup():
    """post 已注入 → pre 同任务去重（反向先到先得）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out_post = deadline_guard(state, _Runtime(root))
        assert "任务收尾提醒" in out_post["messages"][0].content
        out_pre = deadline_guard_pre(out_post, _Runtime(root))
        assert out_pre is out_post  # pre 不再注入


def test_pre_before_window_no_op():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard_pre(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW - 100)  # 未进窗
        out = deadline_guard_pre(state, _Runtime(root))
        assert out is state
        assert not (root / ".memory" / "deadline_forcing_injected.json").is_file()


def test_pre_gate_complete_and_run_id_and_budget():
    """pre 门控：complete 轮 / run_id≠0 / 预算未配置 均 no-op。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # complete=True
        state = _state(complete=True)
        deadline_guard_pre(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        assert deadline_guard_pre(state, _Runtime(root)) is state
        # run_id=1
        state = _state(run_id=1)
        runtime = _Runtime(root)
        runtime.run_id = 1
        deadline_guard_pre(state, runtime)
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        assert deadline_guard_pre(state, runtime) is state
        # 预算未配置
        state = _state()
        deadline_guard_pre(state, _Runtime(root, agent_config={}))
        _rewind_anchor(root, 10 * BUDGET)
        assert deadline_guard_pre(state, _Runtime(root, agent_config={})) is state


def test_pre_directive_mentions_no_delivery_deadlock():
    """指令文本含'任何纪律…不得导致零交付'（合规死锁对冲条款）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()
        deadline_guard_pre(state, _Runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        out = deadline_guard_pre(state, _Runtime(root))
        assert "不得导致零交付" in out["messages"][-1].content


# ============ 迭代锚定尾段交付保障（max_iter 剩余轮次触发） ============

MAX_ITER = 30
ITER_WINDOW = 2


def _iter_runtime(workspace: Path, agent_config: dict[str, Any] | None = None) -> _Runtime:
    """仅迭代锚配置（无时间预算）——隔离两锚互不代偿。"""
    merged = {"max_iter": MAX_ITER, "task_iter_forcing_window": ITER_WINDOW}
    merged.update(agent_config or {})
    return _Runtime(workspace, agent_config=merged)


def test_iter_anchor_injects_directive():
    """curr_iter 进入尾段窗（≥ max_iter - window）→ 注入硬交付指令。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(curr_iter=MAX_ITER - ITER_WINDOW)
        out = deadline_guard(state, _iter_runtime(root))
        assert isinstance(out, dict) and out is not state
        content = out["messages"][0].content
        assert "任务收尾提醒" in content
        assert "迭代轮次即将耗尽" in content
        assert f"仅剩约 {MAX_ITER - (MAX_ITER - ITER_WINDOW)} 轮" in content
        assert (root / ".memory" / "deadline_forcing_injected.json").is_file()


def test_iter_anchor_below_window_no_op():
    """curr_iter 未进尾段窗 → no-op（起始锚点仍落盘）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(curr_iter=MAX_ITER - ITER_WINDOW - 1)
        out = deadline_guard(state, _iter_runtime(root))
        assert out is state
        assert not (root / ".memory" / "deadline_forcing_injected.json").is_file()
        assert (root / ".memory" / "task_run_started_at.json").is_file()


def test_iter_anchor_missing_curr_iter_no_op():
    """state 无 curr_iter（异常形态）→ 迭代锚 no-op 不误触发。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state()  # 无 curr_iter 键
        out = deadline_guard(state, _iter_runtime(root))
        assert out is state


def test_iter_anchor_unset_max_iter_no_op():
    """AGENT_CONFIG 未配置 max_iter → 迭代锚 no-op（通用配置零影响）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(curr_iter=29)
        out = deadline_guard(
            state, _Runtime(root, agent_config={"task_iter_forcing_window": 2})
        )
        assert out is state


def test_iter_anchor_pre_position_appends_human_message():
    """pre 注入位：迭代锚触发 → planner LLM 调用前追加 HumanMessage（当轮可见）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(curr_iter=MAX_ITER - ITER_WINDOW)
        out = deadline_guard_pre(state, _iter_runtime(root))
        assert isinstance(out, dict)
        messages = out["messages"]
        assert isinstance(messages[-1], HumanMessage)
        assert "迭代轮次即将耗尽" in messages[-1].content
        assert messages[0].content == "thinking"


def test_iter_anchor_shared_dedup_with_time_anchor():
    """双锚共享去重标记：迭代锚先注入 → 时间锚随后进窗不再注入。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state = _state(curr_iter=MAX_ITER - ITER_WINDOW)
        out_iter = deadline_guard(state, _iter_runtime(root))
        assert "迭代轮次即将耗尽" in out_iter["messages"][0].content
        # 同任务随后把时间锚拉进窗（elapsed 拉满）
        _rewind_anchor(root, BUDGET + 100)
        out_time = deadline_guard(out_iter, _Runtime(root))
        assert out_time is out_iter  # 去重生效


def test_time_anchor_priority_when_both_in_window():
    """双锚同轮进窗 → 时间锚优先（信息量更大：含剩余分钟数）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # 首轮 curr_iter=27 未进迭代窗 → 仅锚定起始时间不注入（避免占用去重标记）
        state = _state(curr_iter=MAX_ITER - ITER_WINDOW - 1)
        deadline_guard(state, _iter_runtime(root))
        _rewind_anchor(root, BUDGET - WINDOW + 50)
        both_runtime = _Runtime(root, agent_config={
            "task_wall_budget_seconds": BUDGET, "task_forcing_window_seconds": WINDOW,
            "max_iter": MAX_ITER, "task_iter_forcing_window": ITER_WINDOW,
        })
        out = deadline_guard(_state(curr_iter=MAX_ITER - ITER_WINDOW), both_runtime)
        content = out["messages"][0].content
        assert "剩余执行时间已不足" in content
        assert "迭代轮次即将耗尽" not in content


def test_iter_config_window_floors():
    """_iter_config：窗口缺省 2、下限 1（非法值回缺省）；未配置 max_iter → (0,0)。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # 缺省窗口
        assert _iter_config(_Runtime(root, agent_config={"max_iter": 30})) == (30, 2)
        # 非法窗口回缺省
        assert _iter_config(_Runtime(root, agent_config={
            "max_iter": 30, "task_iter_forcing_window": "abc"})) == (30, 2)
        # 窗口 0 → 回缺省 2
        assert _iter_config(_Runtime(root, agent_config={
            "max_iter": 30, "task_iter_forcing_window": 0})) == (30, 2)
        # 窗口 5 → 尊重
        assert _iter_config(_Runtime(root, agent_config={
            "max_iter": 30, "task_iter_forcing_window": 5})) == (30, 5)
        # 未配置
        assert _iter_config(_Runtime(root, agent_config={})) == (0, 0)
        # 无 config API
        assert _iter_config(SimpleNamespace()) == (0, 0)
