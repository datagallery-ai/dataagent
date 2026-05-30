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
"""Flex 内置 pruner hook（planner 节点 pre-hook）。

当消息体积超过阈值时，用 compression_utils 的 token 计数、窗口选择和 prompt 模板
对中间历史消息进行语义折叠压缩，不产生文件产物。

Pruner 在判断前先做 IR 替换：通过 build_messages 将旧 ToolMessage 替换为紧凑 IR
摘要，再基于替换后的消息判断是否需要 LLM 折叠。这避免了“原始 ToolMessage 很长但
IR 替换后已很短”的场景下误触发压缩。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from langchain_core.messages import AnyMessage, RemoveMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.workflow.state import FlexState
from dataagent.utils.compression_utils import (
    compress_messages,
    compress_strategy,
)
from dataagent.utils.constants import (
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_PRUNER_TOKEN_LIMIT,
)

if TYPE_CHECKING:
    from dataagent.core.context.context_trajectory import Context


def pruner(state: FlexState, runtime: Runtime) -> FlexState:
    """Planner 节点 pre-hook：压缩超体积的中间历史消息。

    判断流程：
    1. 获取 context，用 ``build_messages`` 做 IR 替换（不写回 state）
    2. 基于 IR 替换后的消息列表判断是否需要压缩
    3. 如需压缩，对 IR 替换后的消息做 LLM 语义折叠
    4. 最终压缩结果写回 state

    注意：``messages`` 状态的 reducer 是 ``add_messages``（按 id 做 merge），
    直接替换 ``state["messages"]`` 不会删除旧消息。
    必须返回 ``RemoveMessage`` 标记被压缩的旧消息，再追加压缩后的新消息。
    """
    from dataagent.utils.messages_utils import build_messages

    messages: list[AnyMessage] = list(state.get("messages") or [])
    if not messages:
        return state

    # 1. 先做 IR 替换（不写回 state），得到精简后的消息列表
    context = get_context_for_flex_state(state, runtime, swallow_errors=True)
    ir_messages = build_messages(messages, context=context) if context else messages

    # 2. 基于 IR 替换后的消息判断是否需要压缩
    token_limit = runtime.env.compress_token_limit or DEFAULT_PRUNER_TOKEN_LIMIT
    message_cnt = runtime.env.compress_message_cnt or DEFAULT_COMPRESS_MESSAGE_CNT
    strategy = compress_strategy(token_limit=token_limit, message_cnt=message_cnt)
    llm = runtime.llm("planner")
    try:
        compressed = compress_messages(ir_messages, strategy, llm=llm)
    except Exception as e:
        logger.exception(f"[pruner] compression failed, skip pruning: {type(e).__name__}: {e}")
        return state

    if compressed is ir_messages:
        return state

    state["messages"] = cast(list[AnyMessage], [RemoveMessage(id="__remove_all__"), *compressed])
    return state
