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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""上下文溢出之后，把保留消息末尾那一截连续的工具结果改短。

调用方已经决定要裁，这里不再判断什么叫溢出。步骤是：按 ``keep`` 得到 token
阈值，找出列表末尾连续的 ``ToolMessage``，再用整段消息里的 ``tool_calls``
决定每一条怎么处理。

工具调用的名字是 ``read_file`` 且 ``file_path`` 是非空字符串时，不写盘，只留
前 4000 个码点，后面接续读通知。其它情况把抽出的文本交给驱逐模块落盘。

比较用 ``<``。计数等于阈值时要裁。``messages``、不认识的种类，以及
``fraction`` 在 ``max_input_tokens is None`` 时，阈值都是 5000。``False``
不当 ``None``。

至少裁成一条时，落盘返回 ``None`` 的那条原对象也放进替换列表。每一条都没写成
时，返回调用方原来的列表，但落盘函数已经调用过。只有裁剪结果自己的 ``id is
None`` 才补 ``uuid``；失败后放回去的原消息不补。
"""

import asyncio
import uuid
from typing import Any, TypeAlias

from langchain.agents.middleware.summarization import ContextSize, TokenCounter
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from dataagent.core.backends.protocol import BackendProtocol
from dataagent.core.middleware.context._message_eviction import (
    _aoffload_tool_message_content,
    _extract_text_from_message,
    _offload_tool_message_content,
)

Transcript: TypeAlias = list[AnyMessage]
Keep: TypeAlias = ContextSize
Counter: TypeAlias = TokenCounter
Backend: TypeAlias = BackendProtocol
ClipPair: TypeAlias = tuple[Transcript, list[AnyMessage]]
CallMap: TypeAlias = dict[str, dict[str, Any]]

# 种类对不上、或 fraction 没有整数预算时用这个数。不要借 04 里那个常量名。
_CLIP_WHEN_UNSPECIFIED = 5000


def _limit_for(keep: Keep, budget: int | None) -> int:
    """从 ``(种类, 值)`` 得到裁剪阈值。拆包失败就让异常原样冒出去。"""
    kind, value = keep
    if kind == "tokens":
        return int(value)
    if kind == "fraction" and budget is not None:
        return int(budget * value)
    return _CLIP_WHEN_UNSPECIFIED


def _tail_tools(messages: Transcript) -> tuple[int, list[ToolMessage]] | None:
    """末尾连续的 ``ToolMessage``。没有则 ``None``。子类算工具消息。"""
    if not messages:
        return None
    if not isinstance(messages[-1], ToolMessage):
        return None
    start = len(messages)
    while start > 0 and isinstance(messages[start - 1], ToolMessage):
        start -= 1
    return start, messages[start:]  # type: ignore[return-value]


def _index_calls(transcript: Transcript) -> CallMap:
    """``tool_call_id → 工具调用``。后来的同 id 覆盖先来的。假 id 跳过。"""
    found: CallMap = {}
    for turn in transcript:
        if not isinstance(turn, AIMessage):
            continue
        pending = turn.tool_calls or ()
        for call in pending:
            ident = call.get("id")
            if not ident:
                continue
            found[ident] = call
    return found


def _linked_read_path(message: ToolMessage, calls: CallMap) -> str | None:
    """工具调用名恰好是 ``read_file`` 时返回路径。空路径返回 ``None``。

    ``args`` 缺失走落盘。``args`` 是 ``None`` 或不是 dict 时，``.get`` 会抛
    ``AttributeError``，这里不接。
    """
    ident = message.tool_call_id
    if not ident:
        return None
    call = calls.get(ident)
    if not call or call.get("name") != "read_file":
        return None
    args = call.get("args", {})
    raw_path = args.get("file_path")
    if isinstance(raw_path, str) and raw_path:
        return raw_path
    return None


def _window_read_file(message: ToolMessage, path: str) -> ToolMessage:
    """留前 4000 个码点，再接续读通知。图片块不会留在新的字符串内容里。"""
    body = _extract_text_from_message(message)
    head = body[:4_000]
    trailer = (
        f"\n\n[Output was truncated due to context window size limits. "
        f"The full content is at {path}. "
        f"Use read_file with offset and limit parameters to retrieve specific portions. "
        f"For example, to read the first 100 lines, call read_file with file_path='{path}', offset=0, limit=100.]"
    )
    return message.model_copy(update={"content": head + trailer})


def _rewrite_sync(message: ToolMessage, calls: CallMap, backend: Backend, prefix: str) -> ToolMessage | None:
    """同步裁一条。``read_file`` 走切片，其余把抽到的文本交给落盘。"""
    path = _linked_read_path(message, calls)
    if path is not None:
        return _window_read_file(message, path)
    text = _extract_text_from_message(message)
    return _offload_tool_message_content(message, text, backend, prefix)


async def _rewrite_async(message: ToolMessage, calls: CallMap, backend: Backend, prefix: str) -> ToolMessage | None:
    """异步裁一条。切片仍是同步的；只有落盘那一支会 await。"""
    path = _linked_read_path(message, calls)
    if path is not None:
        return _window_read_file(message, path)
    text = _extract_text_from_message(message)
    return await _aoffload_tool_message_content(message, text, backend, prefix)


def _with_fresh_id(message: ToolMessage) -> ToolMessage:
    """裁成功且原来没有 id 时才生成一个。已有 id 原样留下。"""
    if message.id is not None:
        return message
    return message.model_copy(update={"id": str(uuid.uuid4())})


def _pack(original: Transcript, start: int, tail: list[ToolMessage], rewritten: list[ToolMessage | None]) -> ClipPair:
    """组装两条返回列表。一条都没成功时，第一条仍是调用方的那个列表对象。"""
    packed: list[AnyMessage] = []
    changed = False
    for produced, prior in zip(rewritten, tail, strict=True):
        if produced is None:
            packed.append(prior)
            continue
        packed.append(_with_fresh_id(produced))
        changed = True
    if not changed:
        return original, []
    return [*original[:start], *packed], packed


def _gate(messages: Transcript, keep: Keep, budget: int | None, token_counter: Counter) -> tuple[int, list[ToolMessage], CallMap] | None:
    """有尾批且计数达到阈值时，返回起点、尾批和调用索引。计数先于阈值。"""
    located = _tail_tools(messages)
    if located is None:
        return None
    start, tail = located
    counted = token_counter(tail)
    limit = _limit_for(keep, budget)
    if counted < limit:
        return None
    return start, tail, _index_calls(messages)


def _clip_overflow_tail(preserved_messages: Transcript, backend: Backend, *, keep: Keep, max_input_tokens: int | None, token_counter: Counter, large_tool_results_prefix: str) -> ClipPair:
    """同步裁掉末尾超长的工具结果。``keep`` 只能按关键字传。"""
    ready = _gate(preserved_messages, keep, max_input_tokens, token_counter)
    if ready is None:
        return preserved_messages, []
    start, tail, calls = ready
    rewritten = [_rewrite_sync(item, calls, backend, large_tool_results_prefix) for item in tail]
    return _pack(preserved_messages, start, tail, rewritten)


async def _aclip_overflow_tail(preserved_messages: Transcript, backend: Backend, *, keep: Keep, max_input_tokens: int | None, token_counter: Counter, large_tool_results_prefix: str) -> ClipPair:
    """异步入口。尾批里的落盘用 ``asyncio.gather`` 一起发，结果顺序与尾批一致。"""
    ready = _gate(preserved_messages, keep, max_input_tokens, token_counter)
    if ready is None:
        return preserved_messages, []
    start, tail, calls = ready
    jobs = (_rewrite_async(item, calls, backend, large_tool_results_prefix) for item in tail)
    rewritten = await asyncio.gather(*jobs)
    return _pack(preserved_messages, start, tail, list(rewritten))
