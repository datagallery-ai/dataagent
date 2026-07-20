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
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional, cast

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from loguru import logger

from dataagent.core.context.context import Context
from dataagent.core.context.flex_context_formatting import format_one_message
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.utils.constants import (
    DEFAULT_COMPRESS_FOLD_TEMPERATURE,
    DEFAULT_COMPRESS_LOW_WATER_RATIO,
    DEFAULT_COMPRESS_MAX_RETRIES,
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_COMPRESS_TOKEN_LIMIT,
)

# 保留旧名作为兼容别名，供外部引用（如 pruner.py）
DEFAULT_TOKEN_LIMIT = DEFAULT_COMPRESS_TOKEN_LIMIT
DEFAULT_MAX_RETRIES = DEFAULT_COMPRESS_MAX_RETRIES
DEFAULT_MESSAGE_CNT = DEFAULT_COMPRESS_MESSAGE_CNT
DEFAULT_FOLD_TEMPERATURE = DEFAULT_COMPRESS_FOLD_TEMPERATURE


@dataclass(frozen=True)
class CompressionPressure:
    """记录原始历史触发的压缩原因和一次性 token 计数结果。"""

    message_overflow: bool
    token_overflow: bool
    token_count: int


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
        # 折叠摘要是一段注入式上下文，不是真实的用户交互。盖 ``_folded`` 标记
        # 让 ``_compute_round_summaries`` 跳过其 ``_ts``（首次序列化时间可能远
        # 晚于该轮真实消息，否则会导致 elapsed_sec 出现负数）。
        # ``_ts`` 在创建时盖戳仅为一致性，``_compute_round_summaries`` 会跳过。
        folded_kw = {"_folded": True, "_ts": time.time()}
        content = getattr(response, "content", None)
        if content:
            return [HumanMessage(content=str(content), additional_kwargs=dict(folded_kw))]
        # fallback: 部分 reasoning 模型将实际输出放在 reasoning_content 中
        reasoning = getattr(response, "reasoning_content", None)
        if reasoning:
            return [HumanMessage(content=str(reasoning), additional_kwargs=dict(folded_kw))]
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
    low_water_ratio: float
    ignore_history_reasoning: bool

    def __init__(
        self,
        token_limit: int = DEFAULT_TOKEN_LIMIT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        message_cnt: int = DEFAULT_MESSAGE_CNT,
        low_water_ratio: float = DEFAULT_COMPRESS_LOW_WATER_RATIO,
        ignore_history_reasoning: bool = False,
    ):
        self.token_limit = token_limit
        self.max_retries = max_retries
        self.message_cnt = message_cnt
        self.low_water_ratio = low_water_ratio
        self.ignore_history_reasoning = ignore_history_reasoning
        if message_cnt < 2:
            self.message_cnt = DEFAULT_MESSAGE_CNT
        if token_limit < 1024:
            self.token_limit = DEFAULT_TOKEN_LIMIT
        if max_retries < 1:
            self.max_retries = DEFAULT_MAX_RETRIES
        if not 0 < low_water_ratio < 1:
            self.low_water_ratio = DEFAULT_COMPRESS_LOW_WATER_RATIO


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
    if not tot_messages:
        return 1
    if isinstance(tot_messages[0], SystemMessage):
        return 2
    return 1


def compression_window_selection(
    tot_messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
    *,
    pressure: Optional[CompressionPressure] = None,  # noqa: UP045 - project API convention requires Optional
    token_reserve: int = 0,
) -> list[AnyMessage]:
    """
    This function is used to select the messages to be compressed from all messages.
    Keep the first ``head_count`` message(s) and the last few messages.
    The middle messages will be compressed.
    """
    active_pressure = pressure or measure_compression_pressure(tot_messages, strategy)
    head_count = _find_head_count(tot_messages)
    if len(tot_messages) <= head_count:
        return []

    message_cut_off = head_count - 1
    if active_pressure.message_overflow:
        target_count = max(head_count + 1, int(strategy.message_cnt * strategy.low_water_ratio))
        message_cut_off = min(len(tot_messages) - 1, len(tot_messages) - target_count + head_count)

    token_cut_off = head_count - 1
    if active_pressure.token_overflow:
        target_tokens = int(strategy.token_limit * strategy.low_water_ratio)
        retained_budget = max(0, target_tokens - token_reserve)
        retained_tokens = count_tokens_approximately(tot_messages[:head_count])
        for index in range(len(tot_messages) - 1, head_count - 1, -1):
            message_tokens = count_tokens_approximately([tot_messages[index]])
            if retained_tokens + message_tokens > retained_budget:
                token_cut_off = index
                break
            retained_tokens += message_tokens

    security_cut_off = max(token_cut_off, message_cut_off)
    if security_cut_off < head_count:
        return []

    # Fold 完整的 AI/tool group，避免把 AIMessage 与其 ToolMessage 拆到窗口两侧。
    while security_cut_off + 1 < len(tot_messages) and isinstance(tot_messages[security_cut_off + 1], ToolMessage):
        security_cut_off += 1
    if security_cut_off >= head_count:
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
    pressure = measure_compression_pressure(tot_messages, strategy)
    if not pressure.message_overflow and not pressure.token_overflow:
        return tot_messages

    head_count = _find_head_count(tot_messages)
    compress_method = compress_method_selection(tot_messages, strategy, llm=llm)
    token_reserve = 0
    last_result = tot_messages

    for _ in range(strategy.max_retries):
        compressed_messages = compression_window_selection(
            tot_messages,
            strategy,
            pressure=pressure,
            token_reserve=token_reserve,
        )
        if not compressed_messages:
            return tot_messages

        last_result = (
            tot_messages[:head_count]
            + compress_method(compressed_messages)
            + tot_messages[head_count + len(compressed_messages) :]
        )
        if _meets_low_water_targets(last_result, strategy, pressure):
            return last_result

        if not pressure.token_overflow:
            break
        target_tokens = int(strategy.token_limit * strategy.low_water_ratio)
        token_overage = max(1, count_tokens_approximately(last_result) - target_tokens)
        token_reserve += token_overage + 64

    logger.warning("Compression result did not reach every active low-water target; keep the original history")
    return tot_messages


def measure_compression_pressure(
    tot_messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
) -> CompressionPressure:
    """Measure token and message pressure on the original visible history."""
    token_count = count_tokens_approximately(tot_messages)
    return CompressionPressure(
        message_overflow=len(tot_messages) > strategy.message_cnt,
        token_overflow=token_count > (1.2 * strategy.token_limit),
        token_count=token_count,
    )


def _meets_low_water_targets(
    messages: list[AnyMessage],
    strategy: compress_strategy,
    pressure: CompressionPressure,
) -> bool:
    """Return whether all targets activated by the original trigger are met."""
    head_count = _find_head_count(messages)
    if pressure.message_overflow:
        target_count = max(head_count + 1, int(strategy.message_cnt * strategy.low_water_ratio))
        if len(messages) > target_count:
            return False
    if pressure.token_overflow:
        target_tokens = int(strategy.token_limit * strategy.low_water_ratio)
        if count_tokens_approximately(messages) > target_tokens:
            return False
    return True


def _should_compress(
    tot_messages: list[AnyMessage],
    strategy: compress_strategy = DEFAULT_COMPRESS_STRATEGY,
) -> bool:
    """
    Check if the messages should be compressed according to the compress strategy.
    """
    pressure = measure_compression_pressure(tot_messages, strategy)
    logger.debug(f"The number of messages is {len(tot_messages)}")
    logger.debug(f"Total tokens: {pressure.token_count}")
    return pressure.message_overflow or pressure.token_overflow


async def infer_state_and_unpack_ir(
    context: Context,
    *,
    runtime: Any = None,
) -> tuple[dict[str, str], str]:
    """Merged single-LLM-call version of infer_perfect_state_space + unpack_data_ir.

    Returns (perfect_state_space_dict, unpacked_data_ir_string).
    """
    import ast

    from dataagent.utils.converter.ir_message_consumer import get_recent_read_files

    prompt = _prepare_prompt_to_infer_state_and_unpack(context, runtime=runtime)
    llm = llm_manager.get_default_llm()
    response = await llm.ainvoke(prompt)
    content_str = str(response.content) if response.content is not None else ""
    ir_unpack_enabled = runtime is not None and bool(runtime.get_config("CONTEXT.enable_profiling", False))

    # Parse perfect_state_space
    perfect_state_space_dict = {}
    state_match = re.search(
        r"<perfect_state_space>\s*(\{.*?\})\s*</perfect_state_space>",
        content_str,
        flags=re.DOTALL,
    )
    if state_match:
        try:
            parsed = json.loads(state_match.group(1))
            perfect_state_space_dict = {
                "goal_intent": parsed.get("goal_intent", ""),
                "belief_about_world": parsed.get("belief_about_world", ""),
                "action_history_summary": parsed.get("action_history_summary", ""),
                "current_position": parsed.get("current_position", ""),
                "available_actions": parsed.get("available_actions", ""),
                "user_feedback_state": parsed.get("user_feedback_state", ""),
                "epistemic_state": parsed.get("epistemic_state", ""),
            }
        except json.JSONDecodeError:
            pass

    if not ir_unpack_enabled:
        logger.debug("IR unpack skipped: CONTEXT.enable_profiling is false")
        return perfect_state_space_dict, ""

    # Parse unpack_data_ir
    recent_read_files = get_recent_read_files(context)
    unpacked_data_ir_list: list[str] = []
    ir_match = re.search(
        r"<unpack_data_ir>\s*(.*?)\s*</unpack_data_ir>",
        content_str,
        flags=re.DOTALL,
    )
    if ir_match:
        ir_content = ir_match.group(1).strip()
        try:
            unpacked_data_ir_list = ast.literal_eval(ir_content)
        except Exception:
            unpacked_data_ir_list = _extract_ir_tokens(ir_content)

    output_str = ""
    debug_str: list[str] = []
    from dataagent.core.context.utils_context_filesystem import lineage_path_key

    for i in unpacked_data_ir_list:
        path, content = context.get_full_data(graph_node_label=i)
        if path and lineage_path_key(p=path) in recent_read_files:
            continue
        if len(content) > 1000:
            content = content[:600] + f"\n[truncated: omitted middle {len(content) - 1000} chars]\n" + content[-400:]
        output_str += f"[IR Unpacked] {i}, path: {path}, content: {content}\n"
        debug_str.append(i)

    logger.warning(f"Unpacked data IR (merged): [{','.join(debug_str)}]")
    return perfect_state_space_dict, output_str


def _extract_ir_tokens(content_str: str) -> list[str]:
    """When model output is not a valid Python literal, extract tokens like 'File(file00001)'."""
    if not content_str:
        return []
    TOKEN_PATTERN = re.compile(r"\b(?:Table|File|Script)\([^)]*\)")
    tokens = TOKEN_PATTERN.findall(content_str)
    seen = set()
    result: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)

    return result


def _prepare_prompt_to_infer_state_and_unpack(
    context: Context,
    *,
    runtime: Any = None,
) -> list[dict[str, str]]:
    """Build infer prompt; IR-unpack sections controlled by template flags."""
    from dataagent.utils.converter.ir_message_consumer import (
        build_available_actions,
        build_history_context,
        build_past_action,
        build_past_perfect_state,
        build_query_and_instruction_text,
        format_data_lineage,
    )

    ir_unpack_enabled = runtime is not None and bool(runtime.get_config("CONTEXT.enable_profiling", False))
    enable_summary = runtime is not None and bool(runtime.get_config("CONTEXT.enable_summary", False))
    past_state_dict, past_state_string = build_past_perfect_state(context)

    prompt_variables: dict[str, Any] = {
        "past_state": past_state_string,
        "past_action": build_past_action(context),
        "available_actions": build_available_actions(runtime=runtime),
        "enable_ir_unpack": ir_unpack_enabled,
        "enable_summary": enable_summary,
        "user_query": "",
        "history_context": "",
        "data_lineage": format_data_lineage(context, past_state_dict) if ir_unpack_enabled else "",
    }
    if not past_state_string:
        prompt_variables["user_query"] = build_query_and_instruction_text(context)
        if enable_summary:
            prompt_variables["history_context"] = build_history_context(context)

    template_base = f"{PROMPT_MD_PREFIX}/context"
    system_prompt = PromptTemplate.from_package_relative(f"{template_base}/system_infer").apply_prompt_template(
        **prompt_variables
    )
    user_prompt = PromptTemplate.from_package_relative(f"{template_base}/user_infer").apply_prompt_template(
        **prompt_variables
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
