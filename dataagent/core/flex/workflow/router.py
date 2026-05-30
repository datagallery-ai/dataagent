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

from typing import Any

from loguru import logger

from dataagent.core.cbb.base_router import BaseRouter
from dataagent.core.flex.hooks.agent_turn import is_subagent


class LimitReachedError(Exception):
    """迭代次数或 token 超限，携带当前 state 供 chat() 拼装返回。"""

    def __init__(self, message: str, state: dict[str, Any] | None = None):
        super().__init__(message)
        self.state = state or {}


def _write_message_history(state: dict[str, Any]) -> None:
    """对齐 galatea GalateaRouter.process 的 append_history_messages 行为。

    在每个路由节点执行后将当前全量消息写入 session history，
    确保崩溃时已完成轮次的上下文不丢失。
    subagent 不写主 agent 的会话历史。
    """
    if is_subagent(state):
        return
    user_id = str(state.get("user_id") or "")
    session_id = str(state.get("session_id") or "")
    messages = state.get("messages") or []
    if not user_id or not session_id or not messages:
        return
    try:
        from dataagent.core.flex.hooks.history_writer import save_messages

        save_messages(user_id, session_id, messages)
    except Exception as e:
        logger.warning(f"[FlexRouter] 消息历史写入失败，本轮上下文可能丢失: {e}")


def _compute_total_tokens_from_messages(messages: list[Any]) -> int:
    """从 messages 累加 usage_metadata 得到总 token 数。"""
    total = 0
    for msg in messages or []:
        usage = getattr(msg, "usage_metadata", None) or {}
        if not isinstance(usage, dict):
            continue
        try:
            if "total_tokens" in usage:
                total += int(usage["total_tokens"] or 0)
            else:
                total += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass
    return total


class FlexRouter(BaseRouter):
    """
    Router for Flex workflow with three stages:
    1. Pre-workflow: Initial processing nodes (optional)
    2. Actor-workflow: Main actor nodes with loop capability
    3. Post-workflow: Final processing nodes (optional)

    The actor-workflow loops until state["complete"] is True.
    """

    def __init__(
        self,
        actor_nodes: list[str],
        pre_nodes: list[str] | None = None,
        post_nodes: list[str] | None = None,
        *,
        max_iter: int | None = None,
        token_limit: int | None = None,
    ):
        """
        Initialize FlexRouter.

        Args:
            actor_nodes: List of actor node names (required, must not be empty)
            pre_nodes: List of preprocessing node names (optional)
            post_nodes: List of postprocessing node names (optional)
            max_iter: Actor 循环迭代上限；与 ``state["curr_iter"]`` 比较。``None`` 表示不限制（YAML 未写
                ``AGENT_CONFIG.max_iter`` 或显式 null）。显式数值时才会触发上限。
            token_limit: 可选，累计 ``usage_metadata`` token 上限（YAML ``AGENT_CONFIG.token_limit``）

        Raises:
            ValueError: If actor_nodes is empty
        """
        if not actor_nodes:
            raise ValueError("Must specify at least one actor node in flex agent")
        pre_nodes = pre_nodes or []
        post_nodes = post_nodes or []
        self._max_iter: int | None = None if max_iter is None else int(max_iter)
        self._token_limit = token_limit
        entry_point = pre_nodes[0] if pre_nodes else actor_nodes[0]
        super().__init__(entry_point)

        # Build routing rules
        # 注意：不要一次性构建所有顺序路由，需要在 actor 节点间插入 HITL 检查
        self._build_sequential_routes(pre_nodes)

        # 为 actor 节点构建路由，第一个 actor 节点需要特殊处理（插入 HITL 检查）
        if len(actor_nodes) > 1:
            # 第一个 actor (通常是 Planner) 后检查 HITL
            def _route_after_first_actor(state):
                """第一个 actor 执行完后持久化消息，检查是否需要 HITL"""
                _write_message_history(state)

                # 检查 HITL 是否启用
                if not state.get("enable_human_feedback", False):
                    logger.debug("[Router] HITL 未启用，跳过检查")
                    return actor_nodes[1]

                # 检查是否需要 HITL
                need_hitl = state.get("need_human_feedback", False)
                already_in_hitl = state.get("__hitl_in_current_turn__", False)

                logger.debug(f"[Router] need_hitl: {need_hitl}, already_in_hitl: {already_in_hitl}")

                if need_hitl and not already_in_hitl:
                    logger.debug("[Router] 满足 HITL 条件，路由到 human_feedback")
                    return "human_feedback"

                # 否则继续到下一个 actor 节点（通常是 Executor）
                logger.debug(f"[Router] 不满足 HITL 条件，路由到 {actor_nodes[1]}")
                return actor_nodes[1]

            self.add_custom_rule(actor_nodes[0], _route_after_first_actor)

            # 其余 actor 节点的顺序路由（如果有多于2个节点）
            if len(actor_nodes) > 2:
                for current_node, next_node in zip(actor_nodes[1:-1], actor_nodes[2:], strict=False):
                    self.add_custom_rule(current_node, lambda _, next_node=next_node: next_node)

        self._build_sequential_routes(post_nodes)

        # Set up the completion routing
        if post_nodes:
            self.add_custom_rule(post_nodes[-1], lambda _: "__end__")
            node_after_loop = post_nodes[0]
        else:
            node_after_loop = "__end__"

        # Add loop routing for actor workflow (最后一个 actor 节点)
        def _route_after_last_actor(state):
            _write_message_history(state)

            # Priority 1: Check completion
            if state.get("complete", False):
                logger.debug("[Router] 检测到 complete=True，路由到后处理或结束")
                return node_after_loop

            # Priority 2: Check curr_iter against max_iter（仅 YAML 显式配置数值时启用）
            curr_iter = int(state.get("curr_iter", 0))
            if self._max_iter is not None and curr_iter >= self._max_iter:
                raise LimitReachedError(
                    f"已达迭代上限（max_iter={self._max_iter}，当前 curr_iter={curr_iter}）",
                    state=dict(state),
                )

            # Priority 3: 可选 token 上限（迭代轮次仅由上方 curr_iter vs max_iter 约束）
            reason = self._check_token_limit(state)
            if reason:
                raise LimitReachedError(f"已达{reason}，对话结束。", state=dict(state))

            # Priority 4: Continue loop (返回第一个 actor 节点)
            logger.debug(f"[Router] 继续 Actor 循环，返回 {actor_nodes[0]}")
            return actor_nodes[0]

        self.add_custom_rule(actor_nodes[-1], _route_after_last_actor)

        # HITL node routing (hardcoded)
        def _route_after_hitl(state):
            """HITL 执行完后持久化消息，固定返回 Actor 第一个节点"""
            _write_message_history(state)
            hitl_count = state.get("hitl_count", 0)
            logger.info(f"[Router] HITL 处理完成 (共 {hitl_count} 次)，返回 {actor_nodes[0]} 重新决策")
            return actor_nodes[0]

        self.add_custom_rule("human_feedback", _route_after_hitl)

    def _check_token_limit(self, state: dict[str, Any]) -> str | None:
        """可选累计 token 上限；返回原因字符串，未超限返回 None。"""
        if self._token_limit is None:
            return None
        current_tokens = _compute_total_tokens_from_messages(state.get("messages") or [])
        if current_tokens >= self._token_limit:
            return f"Token 上限（token_limit={self._token_limit}，当前 total_tokens={current_tokens}）"
        return None

    def _build_sequential_routes(self, nodes: list[str]) -> None:
        """Build sequential routing rules for a list of nodes.

        Args:
            nodes: List of node names to connect sequentially
        """
        for current_node, next_node in zip(nodes, nodes[1:], strict=False):
            self.add_custom_rule(current_node, lambda _, next_node=next_node: next_node)
