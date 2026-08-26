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
"""Flex planner pre-hook for event-driven layered context compression."""

from __future__ import annotations

from typing import cast

from langchain_core.messages import AnyMessage, RemoveMessage
from langchain_core.messages.utils import count_tokens_approximately
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.workflow.state import FlexState
from dataagent.utils.compression_utils import (
    compress_messages,
    compress_strategy,
    measure_compression_pressure,
)
from dataagent.utils.constants import (
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_IR_RECENT_TURNS,
    DEFAULT_PRUNER_TOKEN_LIMIT,
)
from dataagent.utils.converter.ir_message_consumer import build_ir_candidate


def pruner(state: FlexState, runtime: Runtime) -> FlexState:
    """Planner 节点 pre-hook：按触发原因执行一次分层压缩事件。

    判断流程：
    1. 原始历史未超限时不改写 state
    2. message 超限时跳过 IR，直接 Fold 原始历史
    3. 仅 token 超限时构建一次 IR candidate；到达 0.6T 则接受
    4. candidate 未到 0.6T 时丢弃 candidate，并 Fold 原始历史

    注意：``messages`` 状态的 reducer 是 ``add_messages``（按 id 做 merge），
    直接替换 ``state["messages"]`` 不会删除旧消息。
    必须返回 ``RemoveMessage`` 标记被压缩的旧消息，再追加压缩后的新消息。
    """
    messages: list[AnyMessage] = list(state.get("messages") or [])
    if not messages:
        return state

    token_limit = runtime.env.compress_token_limit or DEFAULT_PRUNER_TOKEN_LIMIT
    message_cnt = runtime.env.compress_message_cnt or DEFAULT_COMPRESS_MESSAGE_CNT
    strategy = compress_strategy(token_limit=token_limit, message_cnt=message_cnt)
    pressure = measure_compression_pressure(messages, strategy)
    if not pressure.message_overflow and not pressure.token_overflow:
        return state

    if not pressure.message_overflow:
        context = get_context_for_flex_state(state, runtime, swallow_errors=True)
        if context is not None:
            recent_turns = getattr(runtime.env, "ir_recent_turns", None)
            if recent_turns is None:
                recent_turns = DEFAULT_IR_RECENT_TURNS
            candidate = build_ir_candidate(messages, context, ir_recent_turns=recent_turns)
            target_tokens = int(strategy.token_limit * strategy.low_water_ratio)
            if count_tokens_approximately(candidate) <= target_tokens:
                state["messages"] = cast(list[AnyMessage], [RemoveMessage(id="__remove_all__"), *candidate])
                return state

    try:
        compressed = compress_messages(messages, strategy, llm=runtime.llm("planner"))
    except Exception as e:
        logger.exception(f"[pruner] compression failed, skip pruning: {type(e).__name__}: {e}")
        return state

    if compressed is messages:
        return state

    state["messages"] = cast(list[AnyMessage], [RemoveMessage(id="__remove_all__"), *compressed])
    return state
