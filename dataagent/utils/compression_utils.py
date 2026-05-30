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
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from loguru import logger

from dataagent.core.context.flex_context_formatting import format_one_message
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.utils.constants import (
    DEFAULT_COMPRESS_FOLD_TEMPERATURE,
    DEFAULT_COMPRESS_MAX_RETRIES,
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_COMPRESS_TOKEN_LIMIT,
)

# 保留旧名作为兼容别名，供外部引用（如 pruner.py）
DEFAULT_TOKEN_LIMIT = DEFAULT_COMPRESS_TOKEN_LIMIT
DEFAULT_MAX_RETRIES = DEFAULT_COMPRESS_MAX_RETRIES
DEFAULT_MESSAGE_CNT = DEFAULT_COMPRESS_MESSAGE_CNT
DEFAULT_FOLD_TEMPERATURE = DEFAULT_COMPRESS_FOLD_TEMPERATURE


def _build_fold_prompt(
    folding_messages: list[AnyMessage],
) -> str:
    """构建统一的折叠提示词，兼容消息历史和 run-history 轨迹。"""
    folding_str = "\n".join([format_one_message(m) for m in folding_messages])
    return PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/fold_messages").apply_prompt_template(
        folding_str=folding_str
    )


def direct_fold(
    folding_messages: list[AnyMessage],
    *,
    use_async: bool = False,
    temperature: float = DEFAULT_FOLD_TEMPERATURE,
    llm: Any = None,
) -> list[AnyMessage] | Awaitable[list[AnyMessage]]:
    """
    统一的语义折叠入口。

    - 默认同步执行，保持现有压缩链路不变
    - `use_async=True` 时返回可 await 的对象，供异步场景复用
    - `llm` 可选：传入自定义 LLM 实例；否则使用 `llm_manager.get_default_llm()`
    """
    prompt_str = _build_fold_prompt(folding_messages)
    active_llm = llm if llm is not None else llm_manager.get_default_llm()

    def _build_result(response: Any) -> list[AnyMessage]:
        content = getattr(response, "content", None)
        if content:
            return [HumanMessage(content=str(content))]
        # fallback: 部分 reasoning 模型将实际输出放在 reasoning_content 中
        reasoning = getattr(response, "reasoning_content", None)
        if reasoning:
            return [HumanMessage(content=str(reasoning))]
        raise ValueError("No response from any LLM. Please check the LLM configuration.")

    if use_async:

        async def _runner() -> list[AnyMessage]:
            response = await active_llm.ainvoke([HumanMessage(content=prompt_str)], temperature=temperature)
            return _build_result(response)

        return _runner()

    response = active_llm.invoke([HumanMessage(content=prompt_str)], temperature=temperature)
    return _build_result(response)


def del_planned_content(redundant_messages: list[AnyMessage]) -> list[AnyMessage]:
    """
    Delete the planned content from the messages.
    """
    new_messages = []
    for m in redundant_messages:
        if isinstance(m, AIMessage):
            m.content = ""
            new_messages.append(m)
        else:
            new_messages.append(m)
    return new_messages


class compress_strategy:
    token_limit: int
    max_retries: int
    message_cnt: int
    ignore_history_reasoning: bool

    def __init__(
        self,
        token_limit: int = DEFAULT_TOKEN_LIMIT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        message_cnt: int = DEFAULT_MESSAGE_CNT,
        ignore_history_reasoning: bool = False,
    ):
        self.token_limit = token_limit
        self.max_retries = max_retries
        self.message_cnt = message_cnt
        self.ignore_history_reasoning = ignore_history_reasoning
        if message_cnt < 2:
            self.message_cnt = DEFAULT_MESSAGE_CNT
        if token_limit < 1024:
            self.token_limit = DEFAULT_TOKEN_LIMIT
        if max_retries < 1:
            self.max_retries = DEFAULT_MAX_RETRIES


DEFAULT_COMPRESS_STRATEGY = compress_strategy()


def compress_method_selection(
    messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
    llm: Any = None,
) -> Callable[[list[AnyMessage]], list[AnyMessage]]:
    """
    Select the compression method based on the messages.
    """
    if strategy.ignore_history_reasoning:
        return del_planned_content
    return lambda folding_messages: cast(list[AnyMessage], direct_fold(folding_messages, llm=llm))


def _find_head_count(tot_messages: list[AnyMessage]) -> int:
    """找到需要保留的头部消息数量。

    如果首条是 SystemMessage，保留前 2 条（System + User）；
    否则只保留第 1 条（User 直接作为首条）。
    """
    if tot_messages and isinstance(tot_messages[0], SystemMessage):
        return 2
    return 1


def compression_window_selection(
    tot_messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
) -> list[AnyMessage]:
    """
    This function is used to select the messages to be compressed from all messages.
    Keep the first ``head_count`` message(s) and the last few messages.
    The middle messages will be compressed.
    """
    head_count = _find_head_count(tot_messages)
    if len(tot_messages) <= head_count:
        return []

    message_cut_off = 0
    if strategy.message_cnt < len(tot_messages):
        message_cut_off = len(tot_messages) - strategy.message_cnt + head_count

    token_cnt = count_tokens_approximately(tot_messages[:head_count])
    token_cut_off = len(tot_messages)
    while token_cnt < strategy.token_limit * 0.8 and token_cut_off > head_count:
        token_cnt += count_tokens_approximately([tot_messages[token_cut_off - 1]])
        token_cut_off -= 1

    if token_cut_off == len(tot_messages):
        raise ValueError(
            "The token limit is too small. Please increase the token limit. \
                (Usually caused by the system/user message is too long.)"
        )
    security_cut_off = token_cut_off if token_cut_off > message_cut_off else message_cut_off
    if isinstance(tot_messages[security_cut_off], AIMessage):
        security_cut_off -= 1
    while security_cut_off + 1 < len(tot_messages) and isinstance(tot_messages[security_cut_off + 1], ToolMessage):
        security_cut_off += 1
    if security_cut_off > head_count:
        return tot_messages[head_count : security_cut_off + 1]
    return []


def compress_messages(
    tot_messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
    llm: Any = None,
) -> list[AnyMessage]:
    """
    Compress the messages.
    Example:
        # Set the token limit to 6000, and the message count to 40.
        compressed_messages = compress_messages(
            tot_messages = state["messages"],
            strategy = compress_strategy(token_limit=6000, message_cnt=40),
        )
        # The compressed messages will be returned.
    """
    if not _should_compress(tot_messages, strategy):
        return tot_messages
    head_count = _find_head_count(tot_messages)
    compress_method = compress_method_selection(tot_messages, strategy, llm=llm)
    compressed_messages = compression_window_selection(tot_messages, strategy)
    if len(compressed_messages) > 0:
        return (
            tot_messages[:head_count]
            + compress_method(compressed_messages)
            + tot_messages[head_count + len(compressed_messages) :]
        )
    return tot_messages


def _should_compress(
    tot_messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
) -> bool:
    """
    Check if the messages should be compressed according to the compress strategy.
    """
    logger.debug(f"The number of messages is {len(tot_messages)}")
    logger.debug(f"Total tokens: {count_tokens_approximately(tot_messages)}")

    if len(tot_messages) > strategy.message_cnt:
        return True
    return count_tokens_approximately(tot_messages) > (1.2 * strategy.token_limit)
