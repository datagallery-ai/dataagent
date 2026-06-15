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
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import ToolMessage
from loguru import logger

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.framework_adapters.runtime.context import (
    get_stream_writer,
    interrupt,
)
from dataagent.utils.cli.rich_renderer import (
    render_active_human_feedback_prompt,
    resume_active_renderer,
    suspend_active_renderer,
)
from dataagent.utils.runtime_paths import resolve_session_root

if TYPE_CHECKING:
    from dataagent.core.context.context import Context


class HumanFeedbackNode(BaseNode):
    """
    Human Feedback 节点（基于工具调用）

    核心设计：
    1. 从最后一条 AIMessage 提取 request_human_feedback 的参数
    2. 收集用户反馈
    3. 添加 ToolMessage（让 Actor 看到完整对话历史）
    4. 返回 Actor 重新规划
    """

    def __init__(self, name: str = "human_feedback", **kwargs):
        super().__init__(name=name, chat_model_name=None, **kwargs)

    @staticmethod
    def _clear_human_feedback_resume_on_runtime(runtime: Any) -> None:
        """
        Clear ``__human_feedback_resume__`` on the active workflow session global state.

        Args:
            runtime: Per-invocation Runtime passed into :meth:`_aprocess`.
        """
        if runtime is None:
            return
        try:
            upd = getattr(runtime, "update_global_state", None)
            if callable(upd):
                upd({"__human_feedback_resume__": ""})
        except Exception:
            pass

    async def _aprocess(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        """
        收集人工反馈

        流程：
        1. 提取 request_human_feedback 的参数
        2. 构造提示
        3. 收集反馈（三路分支）
        4. 添加 ToolMessage
        5. 清除标志，返回 Actor
        """
        state = cast(FlexState, state)

        if not state["messages"]:
            raise ValueError("HumanFeedbackNode should not be the first node.")

        writer = get_stream_writer()

        # === 阶段1：提取请求信息 ===
        last_message = state["messages"][-1]
        request_info = self._extract_request_info(last_message)

        if not request_info:
            logger.error("[HITL] 未找到 request_human_feedback 工具调用")
            return {
                "need_human_feedback": False,
                "__hitl_processed__": True,
            }

        tool_call_id = request_info["tool_call_id"]
        reason = request_info["reason"]
        pending_action = request_info["pending_action"]

        # === 阶段2：构造反馈提示 ===
        feedback_msg = self._build_feedback_prompt(reason, pending_action)

        logger.info("中断等待用户输入...")

        # === 阶段3：获取用户反馈（三路分支）===
        updated_state: dict[str, Any] = {}

        if state.get("terminal_mode", False):
            # 路径1：终端模式
            # 在 debug 模式下，Rich Live spinner 会持续刷新终端，导致输入行被覆盖，表现为“打字不显示”。
            # 这里直接暂停 Live 更新，待输入完成后再恢复。
            suspend_active_renderer()
            try:
                rendered_by_renderer = render_active_human_feedback_prompt(reason=reason, pending_action=pending_action)
                prompt_text = "请提供您的意见： " if rendered_by_renderer else feedback_msg + "\n"
                user_feedback = await asyncio.to_thread(input, prompt_text)
            finally:
                resume_active_renderer()

        elif isinstance(state.get("__human_feedback_resume__"), str) and state.get("__human_feedback_resume__").strip():
            # 路径2：工作流 session 恢复
            resume_feedback = state["__human_feedback_resume__"]
            user_feedback = resume_feedback.strip()
            updated_state = {"__human_feedback_resume__": ""}

            self._clear_human_feedback_resume_on_runtime(runtime)

            # === 恢复 Context（必要时从存储重建当前 run 的 Context）===
            try:
                ctx = get_context_for_flex_state(state, runtime, swallow_errors=True)
                self._restore_context_from_storage_if_needed(state, ctx)
            except Exception as e:
                logger.warning(f"[HITL] 恢复 Context 时出错：{e}")

        else:
            # 路径3：正常模式（LangGraph / OpenJiuWen 中断）
            # 这里只负责触发中断；Context 的快照/最终持久化统一在 Agent.astream 中处理。
            try:
                ctx = get_context_for_flex_state(state, runtime, swallow_errors=True)
                if ctx is not None:
                    # 若这是跨 worker / 进程重启后的场景，可在此按需从存储重建 Context（目前主要在恢复路径使用）
                    self._restore_context_from_storage_if_needed(state, ctx)
            except Exception as e:
                logger.warning(f"[HITL] 在中断前尝试恢复 Context 时出错：{e}")

            # 对于 langgraph：
            # - 第一次调用：interrupt 会抛 GraphInterrupt，中断本轮执行；
            # - 恢复时：interrupt(Command.resume=...) 会直接返回用户输入字符串。
            user_feedback = interrupt(feedback_msg)
            updated_state = {}

        # === 阶段4：处理反馈 ===
        logger.info(f"用户反馈：{user_feedback}")

        writer({"type": "output_msg", "node_name": self.name, "content": f"✅ 已收到用户反馈：{user_feedback}"})
        writer({"type": "break"})

        # 添加 ToolMessage
        tool_message = ToolMessage(
            content=user_feedback,
            tool_call_id=tool_call_id,
            name="request_human_feedback",
        )

        # 清除标志
        with contextlib.suppress(Exception):
            state["need_human_feedback"] = False

        updated_state.update(
            {
                "need_human_feedback": False,
                "__hitl_in_current_turn__": True,  # 标记本轮已进入 HITL
                "hitl_count": 1,  # 使用 add reducer 累加
                "messages": [tool_message],
                "feedback": state.get("feedback", "") + user_feedback + "\n",
            }
        )

        logger.debug(f"[HITL] 返回 updated_state keys: {updated_state.keys()}")
        logger.debug(f"[HITL] ToolMessage: id={tool_call_id}, content='{user_feedback}'")

        return updated_state

    def _is_context_empty_for_restore(self, ctx: Any) -> bool:
        """
        判断 Context 是否“看起来是空的”，从而需要从存储恢复。

        注意：保持与旧逻辑一致——若获取 trajectory 失败，则视为非空（不触发恢复）。
        """
        if ctx is None:
            return False

        has_initial = bool(getattr(ctx, "has_initial_pt", False))
        if has_initial:
            return False

        try:
            traj = ctx.get_trajectory(trimmed=False)
            traj_empty = getattr(traj, "number_of_nodes", lambda: 0)() == 0
        except Exception:
            traj_empty = False

        return traj_empty

    def _safe_restore_previous_runs(
        self,
        ctx: Context,
        *,
        user_id: str,
        session_id: str,
        run_id: int,
        sub_id: int = 0,
    ) -> None:
        """Restore historical runs (run_id < current) from trajectory JSON snapshots."""
        try:
            if run_id > 0:
                ctx.restore_previous_runs(
                    user_id=user_id,
                    session_id=session_id,
                    current_run_id=run_id,
                    sub_id=sub_id,
                )
        except Exception as e:
            logger.warning(f"[HITL] 通过 restore_previous_runs 恢复历史 Context 失败：{e}")

    def _safe_load_context_meta(self, user_id: str, session_id: str, run_id: int, sub_id: int = 0) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        """_safe_load_context_meta"""
        try:
            # 延迟导入以避免循环依赖
            from dataagent.core.context.context import Context

            meta_val = (
                Context.load_meta_from_json(user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id) or {}
            )
            if isinstance(meta_val, dict):
                meta = meta_val
        except Exception as e:
            logger.warning(f"[HITL] 读取 meta JSON 失败：{e}")
        return meta

    def _safe_initialize_initial_pt_from_meta(
        self, ctx: Any, *, meta: dict[str, Any], user_id: str, session_id: str, run_id: int, sub_id: int = 0
    ) -> None:
        """_safe_initialize_initial_pt_from_meta"""
        try:
            if not (hasattr(ctx, "has_initial_pt") and not ctx.has_initial_pt):
                return

            initial_pt = meta.get("initial_pt")

            # 从 trajectory JSON 中尝试读取 query/额外文件，用于 register_query
            query_text = ""
            additional_files: list[str] = []
            if initial_pt:
                store_path = (
                    resolve_session_root(user_id=user_id, session_id=session_id)
                    / ".context"
                    / f"Run{run_id}_Sub{sub_id}.json"
                )
                with open(store_path, encoding="utf-8") as f:
                    trajectory_dict = json.load(f)
                # node_link_graph 会保留节点属性（包括 query / additional_files）
                import networkx as nx  # noqa: PLC0415

                g = nx.node_link_graph(data=trajectory_dict, edges="edges")
                attrs = g.nodes.get(initial_pt, {}) if initial_pt in g.nodes else {}
                query_text = str(attrs.get("query", "") or "")
                additional_files_val = attrs.get("additional_files", [])
                if isinstance(additional_files_val, list):
                    additional_files = [str(x) for x in additional_files_val]

            if query_text:
                ctx.register_query(query_text, additional_files)
        except Exception as e:
            logger.warning(f"[HITL] 初始化 Context.initial_pt 失败：{e}")

    def _safe_restore_trajectory_from_snapshot(
        self,
        ctx: Any,
        *,
        user_id: str,
        session_id: str,
        run_id: int,
        sub_id: int = 0,
    ) -> None:
        """_safe_restore_trajectory_from_snapshot"""
        try:
            store_path = (
                resolve_session_root(user_id=user_id, session_id=session_id)
                / ".context"
                / f"Run{run_id}_Sub{sub_id}.json"
            )
            with open(store_path, encoding="utf-8") as f:
                trajectory_dict = json.load(f)
            import networkx as nx  # noqa: PLC0415

            loaded = nx.node_link_graph(data=trajectory_dict, edges="edges")
            traj_ref = ctx.get_trajectory(trimmed=False)
            traj_ref.clear()
            traj_ref.add_nodes_from(loaded.nodes(data=True))
            traj_ref.add_edges_from(loaded.edges(data=True))
        except Exception as e:
            logger.warning(f"[HITL] 通过 JSON 快照重建当前 run 的轨迹失败：{e}")

    def _safe_apply_meta_to_context(self, ctx: Context, *, meta: dict[str, Any]) -> None:
        """_safe_apply_meta_to_context"""
        try:
            current_pt = meta.get("current_pt") or []
            if current_pt and hasattr(ctx, "get_active_branch"):
                active = ctx.get_active_branch()
                active.clear()
                active.update({str(x) for x in current_pt})
            ctx_msgs = meta.get("messages") or {}
            if isinstance(ctx_msgs, dict):
                ctx.state.messages.update(ctx_msgs)
        except Exception as e:
            logger.warning(f"[HITL] 应用 meta JSON（current_pt/messages）失败：{e}")

    def _restore_context_from_storage_if_needed(self, state: FlexState, ctx: Any) -> None:
        """
        从持久化存储中恢复当前 run 的 Context（IR + trajectory）。

        适用场景：
        - langgraph backend：HITL 中断前后请求落在不同 worker / 进程重启；
        - openjiuwen backend：``__human_feedback_resume__`` 分支恢复时。

        触发条件：
        - ctx 非空；
        - 当前 Context 看起来是“空的”（没有 initial_pt 且 _trajectory 为空）——
          这通常意味着命中了一个新的 worker 或进程已重启。
        """
        try:
            if ctx is None:
                return

            if not self._is_context_empty_for_restore(ctx):
                # 已经是一个正常的 Context（例如命中同一 worker 的内存实例）或无法判断为空，不需要从存储重建
                return

            run_id = int(state.get("run_id", 0) or 0)
            user_id = str(state.get("user_id", "") or "")
            session_id = str(state.get("session_id", "") or "")
            sub_id = int(state.get("sub_id", 0) or 0)

            # === 1) 历史 run：使用 Context 自身的恢复能力 ===
            self._safe_restore_previous_runs(
                ctx,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                sub_id=sub_id,
            )

            # === 2) 当前 run：从 JSON/meta 快照恢复 ===
            meta = self._safe_load_context_meta(user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id)
            self._safe_initialize_initial_pt_from_meta(
                ctx, meta=meta, user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id
            )
            self._safe_restore_trajectory_from_snapshot(
                ctx, user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id
            )
            self._safe_apply_meta_to_context(ctx, meta=meta)
        except Exception as e:
            logger.warning(f"[HITL] 从存储恢复 Context 时出错：{e}")

    def _extract_request_info(self, last_message) -> dict[str, Any] | None:
        """
        从 AIMessage 提取 request_human_feedback 的信息

        Returns:
            dict: {tool_call_id, reason, pending_action} 或 None
        """
        if not hasattr(last_message, "tool_calls"):
            return None

        for tool_call in last_message.tool_calls:
            if tool_call.get("name") == "request_human_feedback":
                args = tool_call.get("args", {})
                return {
                    "tool_call_id": tool_call["id"],
                    "reason": args.get("reason", "需要您的确认"),
                    "pending_action": args.get("pending_action", ""),
                }

        return None

    def _build_feedback_prompt(self, reason: str, pending_action: str) -> str:
        """构造反馈提示"""
        feedback_prompt = f"\n\n🤖 需要您的反馈\n\n原因：{reason}\n\n"

        if pending_action:
            feedback_prompt += f"待确认操作：{pending_action}\n\n"

        feedback_prompt += "请提供您的意见："

        return feedback_prompt
