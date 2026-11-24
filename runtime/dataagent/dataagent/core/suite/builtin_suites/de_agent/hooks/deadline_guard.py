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
"""Planner post-hook：deadline 感知强制交付收口。

背景（历史实证）：守卫/扩池/过滤全部正确工作的前提下，
agent 在拒绝后的检索-固着循环里 churn 到 wall-clock 硬超时（10000s），以
NO_SQL（零分）收场——NO_SQL↔FAIL 的分界往往就差一个"被逼交付"。此前的
forcing 层只有 delivery_guard 的完成时拦截（complete=True 才检查），对
"永远在检索、迟迟不收尾"的中途 churn 无能为力。

机制：workspace 锚定任务起始时间（.memory/task_run_started_at.json，首个
planner 轮落盘、跨 chat 持久——不能用进程/proc 时间，评测 worker 进程复用
连续跑多题）；每轮检查 elapsed，进入 forcing window（预算 - 窗口）且任务
未收尾时注入硬交付指令，每任务只注入一次（.memory/deadline_forcing_injected.json
持久去重，两个注入位共享、先到先得）。

双注入位（战损 7:0 的修复）：
- planner.pre（deadline_guard_pre）：LLM 调用**之前**追加 HumanMessage——
  当轮 planner 即看到指令，抢在下一次长检索/长调用之前收敛。此前 7 次触发
  全部经 planner.post：注入落在已生成 AIMessage 的 content 里，其 tool_calls
  照常执行（executor 不读 content），指令要等下一轮才被看见——触发即已太晚、
  可见即再晚一轮，转化 0（全 NO_SQL）。
- planner.post（deadline_guard，既有）：轮完成时兜底（pre 未进窗、post 进窗
  的时序缝隙）。

迭代锚定分支：双臂实验定位的统一病理——
38/38 题全部命中 max_iter=30 轮帽优雅终止（router LimitReachedError →
agent.py 合成"已达运行上限"），e2e 最长仅 90min，远低于 8300s wall-clock
forcing 线=时间锚分支从不触发；非成对交付 8 题+零交付跨三代十一例，
统一病灶=迭代预算耗尽于 todo 清单未完（SQL 写作步之前）。iteration_guard：curr_iter 逼近 max_iter（剩余轮次 ≤ 窗口）且
任务未收尾时，用同一套注入位/去重标记/硬交付指令兜底——两个锚（时间/
迭代）先到先得，防止双触发。

门控：
- AGENT_CONFIG.task_wall_budget_seconds 未配置/非法 → 时间锚分支 no-op
  （通用配置零影响；仅评测/限时场景显式开启）；
- AGENT_CONFIG.max_iter 未配置 → 迭代锚分支 no-op；
- result.complete=True → 不注入（收尾路径由 delivery_guard 负责完成度）；
- run_id 非 0（评测追问 chat 等后续会话）→ 不注入（其工作锚定在首个任务
  chat，追问只需简答，强制交付语义不适用）；起始标记仍共享同一锚点。

注意：本文件具双重模块身份——运行期经 suite-root 单文件直载（hooks.yaml
条目 ``hooks.deadline_guard.deadline_guard``），测试/工具经包路径 import。
禁止模块级 mutable 状态，不支持包内相对导入（见 suite hooks.yaml 头注释）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

_RUN_STARTED_AT_REL = Path(".memory") / "task_run_started_at.json"
_FORCING_INJECTED_REL = Path(".memory") / "deadline_forcing_injected.json"
_DEFAULT_FORCING_WINDOW_SECONDS = 1500
_MIN_FORCING_WINDOW_SECONDS = 60
_DEFAULT_ITER_FORCING_WINDOW = 2
_MIN_ITER_FORCING_WINDOW = 1


def deadline_guard(
    state: Any,
    runtime: Any,
    *,
    original_state: Any = None,
) -> Any:
    """限时任务的 planner 工作轮注入硬交付指令（forcing window 内、每任务一次）。

    双锚触发：时间锚（task_wall_budget_seconds）与迭代锚（max_iter
    剩余轮次）任一进入 forcing window 即注入；共享去重标记先到先得。
    """
    result = state
    if not isinstance(result, dict) or result.get("complete"):
        # 收尾轮不注入：完成度核对是 delivery_guard 的职责（complete=True 分支）
        return result

    if _run_id_of(result, original_state, runtime) != 0:
        # 后续 chat（评测追问 run_id=1 等）不做强制交付——工作锚定在首个任务 chat
        return result

    budget, window = _deadline_config(runtime)
    max_iter, iter_window = _iter_config(runtime)
    if budget <= 0 and max_iter <= 0:
        return result

    source = original_state if isinstance(original_state, dict) else result
    workspace_root = _resolve_workspace_root(source, runtime)
    if workspace_root is None:
        return result

    if _already_injected(workspace_root):
        return result

    anchor = _forcing_anchor(workspace_root, result, source, budget, window, max_iter, iter_window)
    if anchor is None:
        return result

    directive = _build_directive(anchor)
    updated_messages = _append_directive(result.get("messages"), directive)
    if updated_messages is None:
        return result
    _mark_injected(workspace_root, anchor["elapsed"])
    logger.warning(
        f"[deadline_guard] 任务进入强制交付窗（{anchor['kind']}锚，{anchor['detail']}），注入硬交付指令"
    )
    return {**result, "messages": updated_messages}


def deadline_guard_pre(state: Any, runtime: Any) -> Any:
    """planner pre 注入位：LLM 调用前追加 HumanMessage 硬交付指令。

    与 planner.post 的 deadline_guard 共享门控与去重标记（先到先得，每任务
    一次）；区别在注入形态与生效时机——pre 返回的 state 直接作为本轮 planner
    的输入（base_node.aprocess：pre_hooks → _aprocess），追加的 HumanMessage
    当轮 LLM 调用即可见，抢在下一次长检索/长调用排队之前收敛。post 版注入
    落在已生成 AIMessage 的 content，其 tool_calls 照常执行、指令下一轮才被
    看见（7 触发 0 转化的机理）。pre-hook 无 original_state（恒 None），
    workspace/run_id 从 state 本身读。双锚（时间/迭代）行为同 post 版。
    """
    result = state
    if not isinstance(result, dict) or result.get("complete"):
        return result

    if _run_id_of(result, None, runtime) != 0:
        return result

    budget, window = _deadline_config(runtime)
    max_iter, iter_window = _iter_config(runtime)
    if budget <= 0 and max_iter <= 0:
        return result

    workspace_root = _resolve_workspace_root(result, runtime)
    if workspace_root is None:
        return result
    if _already_injected(workspace_root):
        return result

    anchor = _forcing_anchor(workspace_root, result, result, budget, window, max_iter, iter_window)
    if anchor is None:
        return result

    directive = _build_directive(anchor)
    messages = list(result.get("messages") or [])
    result["messages"] = [*messages, HumanMessage(content=directive)]
    _mark_injected(workspace_root, anchor["elapsed"])
    logger.warning(
        f"[deadline_guard] 任务进入强制交付窗（pre 注入，{anchor['kind']}锚，{anchor['detail']}），本轮 planner 前追加硬交付指令"
    )
    return result


def _build_directive(anchor: dict[str, Any]) -> str:
    """硬交付指令文本（pre/post 共用，措辞与 planner 纪律 0 同一优先级链）。"""
    if anchor["kind"] == "iteration":
        remaining_iters = int(anchor["remaining_iters"])
        deadline_desc = f"本任务迭代轮次即将耗尽（仅剩约 {remaining_iters} 轮工作机会）"
    else:
        remaining = max(int((anchor["budget"] - anchor["elapsed"]) // 60), 1)
        deadline_desc = f"本任务剩余执行时间已不足（约 {remaining} 分钟）"
    return (
        f"【任务收尾提醒·硬性要求】{deadline_desc}。"
        "按纪律 0（交付义务高于一切编号纪律）从现在起强制收敛：\n"
        "1. 立即停止一切新的元数据检索/候选扩池/非必要的确认提问；\n"
        "2. 基于已有证据直接完成交付：写出成对的 create_*.sql 与 insert_*.sql"
        "（内容完整、可执行）；\n"
        "3. 数据源用当前已确认或池内最接近需求的候选（禁止使用已被用户拒绝的表），"
        "剩余不确定项、假设、与需求的差距在交付说明中逐条写明；\n"
        "4. 不要再发起任何长时间等待的调用（重检索/外部执行）——到限未交付的任务"
        "按零分处理，按时交付的半成品远优于到限无交付；任何纪律、任何"
        "\"未完成/不确定\"状态都不得导致零交付。"
    )


def _iter_config(runtime: Any) -> tuple[int, int]:
    """读 AGENT_CONFIG.max_iter / task_iter_forcing_window（迭代锚）。

    未配置 max_iter → (0, 0)（迭代锚 no-op）；窗口缺省 2 轮，下限 1。
    """
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        return (0, 0)
    try:
        config = get_all_config()
    except Exception:
        return (0, 0)
    agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, Mapping) else {}
    if not isinstance(agent_config, Mapping):
        return (0, 0)
    try:
        max_iter = int(agent_config.get("max_iter", 0) or 0)
    except (TypeError, ValueError):
        max_iter = 0
    if max_iter <= 0:
        return (0, 0)
    raw_window = agent_config.get("task_iter_forcing_window")
    try:
        iter_window = int(raw_window) if raw_window is not None else _DEFAULT_ITER_FORCING_WINDOW
    except (TypeError, ValueError):
        iter_window = _DEFAULT_ITER_FORCING_WINDOW
    if iter_window < _MIN_ITER_FORCING_WINDOW:
        iter_window = _DEFAULT_ITER_FORCING_WINDOW
    return (max_iter, iter_window)


def _forcing_anchor(
    workspace_root: Path,
    result: Mapping[str, Any],
    source: Mapping[str, Any],
    budget: int,
    window: int,
    max_iter: int,
    iter_window: int,
) -> dict[str, Any] | None:
    """判定是否进入 forcing window，返回触发锚（时间/迭代，先到先得）。

    时间锚：elapsed ≥ budget - window（既有语义）；迭代锚：curr_iter ≥
    max_iter - iter_window。两锚均未配置/未进窗 → None。
    elapsed 字段恒供（迭代锚触发时也重锚起始时间，供注入标记留档）。
    """
    started_at = _ensure_run_started_at(workspace_root)
    if started_at is None:
        return None
    elapsed = (datetime.now(UTC) - started_at).total_seconds()

    if budget > 0 and elapsed >= budget - window:
        return {
            "kind": "time",
            "detail": f"elapsed={int(elapsed)}s, budget={budget}s, window={window}s",
            "elapsed": elapsed,
            "budget": budget,
        }

    if max_iter > 0:
        curr_iter = _curr_iter_of(result, source)
        if curr_iter is not None and curr_iter >= max_iter - iter_window:
            remaining_iters = max(max_iter - curr_iter, 0)
            return {
                "kind": "iteration",
                "detail": f"curr_iter={curr_iter}, max_iter={max_iter}, window={iter_window}",
                "elapsed": elapsed,
                "remaining_iters": remaining_iters,
            }
    return None


def _curr_iter_of(result: Mapping[str, Any], source: Mapping[str, Any]) -> int | None:
    """读当前迭代轮次：post 版优先 result（本轮 planner 已 curr_iter+1），pre 版读 source。"""
    for candidate in (result, source):
        if isinstance(candidate, Mapping):
            raw = candidate.get("curr_iter")
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return None


def _deadline_config(runtime: Any) -> tuple[int, int]:
    """读 AGENT_CONFIG.task_wall_budget_seconds / task_forcing_window_seconds。

    未配置预算 → (0, 0)（整体 no-op）；窗口缺省 1500s，下限 60s。
    """
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        return (0, 0)
    try:
        config = get_all_config()
    except Exception:
        return (0, 0)
    agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, Mapping) else {}
    if not isinstance(agent_config, Mapping):
        return (0, 0)
    try:
        budget = int(agent_config.get("task_wall_budget_seconds", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return (0, 0)
    raw_window = agent_config.get("task_forcing_window_seconds")
    try:
        window = int(raw_window) if raw_window is not None else _DEFAULT_FORCING_WINDOW_SECONDS
    except (TypeError, ValueError):
        window = _DEFAULT_FORCING_WINDOW_SECONDS
    if window < _MIN_FORCING_WINDOW_SECONDS:
        window = _DEFAULT_FORCING_WINDOW_SECONDS
    return (budget, window)


def _run_id_of(result: Mapping[str, Any], original_state: Any, runtime: Any) -> int:
    """当前 chat 的 run_id（首个任务 chat = 0；解析不出按 0 处理）。"""
    run_id = None
    for source in (original_state if isinstance(original_state, Mapping) else None, result):
        if isinstance(source, Mapping):
            run_id = source.get("run_id")
            if run_id is not None:
                break
    if run_id is None:
        run_id = getattr(runtime, "run_id", None)
    try:
        return int(run_id)
    except (TypeError, ValueError):
        return 0


def _resolve_workspace_root(source: Mapping[str, Any], runtime: Any) -> Path | None:
    workspace = source.get("workspace") if isinstance(source, Mapping) else None
    if not workspace:
        workspace = getattr(runtime, "workspace_dir", None)
    if not workspace:
        return None
    try:
        return Path(str(workspace)).expanduser().resolve()
    except Exception:
        return None


def _ensure_run_started_at(workspace_root: Path) -> datetime | None:
    """任务起始时间锚点：首个 hook 轮落盘、后续/chat 重入/agent 重建均复用。

    评测 worker 进程复用连续跑多题——不能用进程启动时间；workspace 每题
    独立，锚点跟着 workspace 走。标记损坏时按本轮重写（宁可晚锚不可误锚早）。
    """
    marker_path = workspace_root / _RUN_STARTED_AT_REL
    try:
        if marker_path.is_file():
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            started_at = str(payload.get("started_at", "")) if isinstance(payload, dict) else ""
            if started_at:
                return datetime.fromisoformat(started_at)
    except Exception as exc:
        logger.debug(f"[deadline_guard] 起始标记读取失败，按本轮重锚: {exc}")
    try:
        now = datetime.now(UTC)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"started_at": now.isoformat()}, ensure_ascii=False), encoding="utf-8"
        )
        return now
    except Exception as exc:
        logger.debug(f"[deadline_guard] 起始标记写入失败（放弃本轮检查）: {exc}")
        return None


def _already_injected(workspace_root: Path) -> bool:
    try:
        payload = json.loads(
            (workspace_root / _FORCING_INJECTED_REL).read_text(encoding="utf-8")
        )
        return bool(payload.get("injected")) if isinstance(payload, dict) else False
    except Exception:
        return False


def _mark_injected(workspace_root: Path, elapsed: float) -> None:
    try:
        marker_path = workspace_root / _FORCING_INJECTED_REL
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {"injected": True, "at": datetime.now(UTC).isoformat(), "elapsed_seconds": elapsed},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug(f"[deadline_guard] 注入标记写入失败: {exc}")


def _append_directive(messages: Any, directive: str) -> list[Any] | None:
    """把指令追加进最后一条 AIMessage 的 content（delivery_guard 同款追加点：
    post-hook 的 result["messages"] 是节点增量（裸 AIMessage 或增量 list），
    不能新增消息置尾——executor 要求 messages[-1] 为 AIMessage）。"""
    if isinstance(messages, AIMessage):
        return [_append_to_ai_message(messages, directive)]
    if isinstance(messages, list):
        for index in range(len(messages) - 1, -1, -1):
            if isinstance(messages[index], AIMessage):
                updated = _append_to_ai_message(messages[index], directive)
                new_messages = list(messages)
                new_messages[index] = updated
                return new_messages
    return None


def _append_to_ai_message(message: AIMessage, directive: str) -> AIMessage:
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    return AIMessage(
        content=f"{content}\n\n{directive}",
        additional_kwargs=dict(message.additional_kwargs or {}),
        response_metadata=dict(message.response_metadata or {}),
        id=message.id,
        name=message.name,
        tool_calls=list(message.tool_calls or []),
        invalid_tool_calls=list(message.invalid_tool_calls or []),
        usage_metadata=message.usage_metadata,
    )
