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
"""自动摘要，以及让模型自己调用的 compact_conversation。

切点和摘要正文交给 LangChain 的摘要中间件。这里负责把被切掉的历史追加到后端、
把内联 data URL 换成路径、把摘要收进私有事件。原始 messages 不删。
"""

from __future__ import annotations

import asyncio as aio
import base64 as b64
import hashlib as hashes
import inspect as sigmod
import logging as logmod
import mimetypes as mimes
import urllib.parse as urlparse
import uuid as ids
import warnings as warnmod
from collections.abc import Mapping as MapType
from datetime import UTC as utc_zone
from datetime import datetime as clock
from typing import Annotated, Any, ClassVar, NotRequired

import langchain.agents.middleware.summarization as lc_summary
import langchain.agents.middleware.types as lc_types
import langchain.tools as lc_tools
import langchain_core.exceptions as lc_errors
import langchain_core.messages as lc_messages
import langchain_core.messages.utils as lc_token_utils
import langgraph.types as lg_types
import pydantic
import typing_extensions

from ..backends.composite import CompositeBackend
from ._utils import append_to_system_message
from .context._overflow_clip import _aclip_overflow_tail, _clip_overflow_tail

_MEDIA_REFERENCE_SUMMARY_PROMPT = """<media_reference_information>
Conversation history may include XML media reference tags, for example:
<image url=\"/conversation_history/media/{{hash}}.png\" />
These tags mean the original message included media that was preserved at the referenced backend path.
Treat the tag and path as part of the conversation context. Do not infer visual details that are not available from surrounding text.
When the media could be important for future context, preserve the media reference in your summary.
The model consuming the summary can call `read_file` on the referenced path if it needs to inspect the media.
</media_reference_information>"""

_PROMPT_ANCHOR = "\n<messages>\n"
DEEPAGENTS_DEFAULT_SUMMARY_PROMPT = lc_summary.DEFAULT_SUMMARY_PROMPT.replace(
    _PROMPT_ANCHOR,
    f"\n{_MEDIA_REFERENCE_SUMMARY_PROMPT}\n\n<messages>\n",
    1,
)

_OFFLOAD_FAILED_PLACEHOLDER = '<image error="failed_to_offload" />'
_ARG_CLIP_SUFFIX = "...(argument truncated)"
_PLAIN_SUMMARY_HEAD = "Here is a summary of the conversation to date:\n\n"
_HISTORY_REMOVED = "`history_path_prefix` was removed in deepagents 0.7. Configure `CompositeBackend.artifacts_root` instead."
_FACTORY_MODEL_TYPE = "`create_summarization_middleware` expects `model` to be a `BaseChatModel` instance."
_RECOVERY_EXHAUSTED = "Model input still does not fit after recovery; reduce input, tools, or configured output tokens."
_STILL_OVER_BUDGET = (
    "Context remains above the input budget after compaction; reduce input, tools, or configured output tokens."
)
_OFFLOAD_LOST = "Offloading conversation history to backend failed during summarization. Older messages will not be recoverable."
_MEDIA_PARTIAL_TAIL = (
    " media block(s) could not be offloaded and appear as failed placeholders in the saved "
    "history; the original media is not recoverable."
)
_COMPACT_TOOL_DESCRIPTION = (
    "Compact the conversation by summarizing older messages "
    "into a concise summary. Use this proactively when the "
    "conversation is getting long to free up context window "
    "space. Use it when moving on to a completely new, unrelated "
    "task, or after finishing synthesis or extraction when the "
    "previous working context is no longer needed. This tool "
    "takes no arguments."
)
_NOTHING_TO_COMPACT = "Nothing to compact yet \u2014 conversation is within the token budget."
_BAD_EVENT_KEYS = "Malformed _summarization_event (missing keys): %s"
_CUTOFF_PAST_END = "Summarization cutoff_index %d exceeds message count %d; remaining slice will be empty"
_BAD_CUTOFF = "Malformed _summarization_event: missing cutoff_index"
_DECODE_FAILED = "Failed to decode data: content block (%s): %s"
_UPLOAD_FAILED = "Failed to upload media %s to backend: %s"
_UPLOAD_RAISED = "Failed to upload media %s to backend: %s: %s"
_READ_HISTORY_FAILED = "Exception reading existing history from %s (treating as new file): %s: %s"
_BACKEND_RETURNED_NONE = "backend returned None"
_WRITE_HISTORY_FAILED = "Failed to offload conversation history to %s (%d messages): %s"
_WRITE_HISTORY_RAISED = "Exception offloading conversation history to %s (%d messages): %s: %s"
_WRITE_HISTORY_OK = "Offloaded %d messages to %s"
_COMPACT_FAILED_LOG = "compact_conversation tool failed"
_MISSING_UPLOAD = "missing_upload_response"
_OVERFLOW_MARKERS = ("context_length_exceeded", "contextwindowexceedederror", "maximum context length", "exceeds the context window", "exceeds the available context size", "context window exceeded", "context limit exceeded", "input tokens exceed the configured limit", "prompt is too long")
_OVERFLOW_STATUS = frozenset({400, 413, 422})
_HEAVY_ARG_TOOLS = frozenset({"write_file", "edit_file"})
_TYPED_MEDIA = frozenset({"image", "audio", "video"})
_OUTPUT_LIMIT_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

SUMMARIZATION_EVENT_KEY = "_summarization_event"
SUMMARIZATION_SESSION_ID_KEY = "_summarization_session_id"

logger = logmod.getLogger(__name__)


class CompactConversationSchema(pydantic.BaseModel):
    """compact_conversation 不读参数。这个模型留着，但不接到工具上。"""


class SummarizationEvent(typing_extensions.TypedDict):
    """一次折叠：切点、摘要消息、历史文件路径。"""

    cutoff_index: int
    summary_message: lc_messages.HumanMessage
    file_path: str | None


class TriggerClause(typing_extensions.TypedDict, total=False):
    """一个 AND 子句。列表里的多个子句之间是 OR。"""

    tokens: int
    messages: int
    fraction: float


class TruncateArgsSettings(typing_extensions.TypedDict, total=False):
    """老消息里 write_file / edit_file 参数的截断设置。"""

    trigger: lc_summary.ContextSize | None
    keep: lc_summary.ContextSize
    max_length: int
    truncation_text: str


class SummarizationState(lc_types.AgentState):
    """在 AgentState 上加两条私有字段。"""

    _summarization_event: Annotated[NotRequired[SummarizationEvent | None], lc_types.PrivateStateAttr]
    _summarization_session_id: Annotated[NotRequired[str | None], lc_types.PrivateStateAttr]


class SummarizationDefaults(typing_extensions.TypedDict):
    """按模型画像算出来的三组默认值。"""

    trigger: lc_summary.ContextSize
    keep: lc_summary.ContextSize
    truncate_args_settings: TruncateArgsSettings


def _token_counter_accepts_tools(counter: lc_summary.TokenCounter) -> bool | None:
    """签名里有 tools 或 **kwargs 就返回 True。看不了签名返回 None。"""
    try:
        signature = sigmod.signature(counter)
    except (TypeError, ValueError):
        return None
    for item in signature.parameters.values():
        if item.kind is sigmod.Parameter.VAR_KEYWORD:
            return True
        if item.name == "tools" and item.kind in {sigmod.Parameter.POSITIONAL_OR_KEYWORD, sigmod.Parameter.KEYWORD_ONLY}:
            return True
    return False


def compute_summarization_defaults(model: Any) -> SummarizationDefaults:
    """有整数 max_input_tokens 就用比例，否则用固定条数。bool 也算整数。"""
    # 四个条件各读一次 model.profile。第一次是带整数上限的 dict、第二次是 None 时走固定条数。
    profile_ready = model.profile is not None and isinstance(model.profile, dict)
    key_ready = profile_ready and "max_input_tokens" in model.profile
    has_limit = key_ready and isinstance(model.profile["max_input_tokens"], int)
    if has_limit:
        fraction_trigger = ("fraction", 0.85)
        fraction_keep = ("fraction", 0.10)
        return {
            "trigger": fraction_trigger,
            "keep": fraction_keep,
            "truncate_args_settings": {"trigger": fraction_trigger, "keep": fraction_keep},
        }
    fixed_trigger = ("tokens", 170000)
    fixed_keep = ("messages", 6)
    arg_trigger = ("messages", 20)
    return {
        "trigger": fixed_trigger,
        "keep": fixed_keep,
        "truncate_args_settings": {"trigger": arg_trigger, "keep": arg_trigger},
    }


def _is_data_url(url: str) -> bool:
    """只认小写 data: 前缀。"""
    return url.startswith("data:")


def _extract_data_url(block: Any) -> str | None:
    """从三种块形状里拿出 data URL。空 base64 当没有。"""
    if isinstance(block, dict) is False:
        return None
    encoded = block.get("base64")
    if encoded:
        kind = block.get("mime_type") or "application/octet-stream"
        return f"data:{kind};base64,{encoded}"
    location = block.get("url", "")
    if isinstance(location, str) and _is_data_url(location):
        return location
    nested = block.get("image_url")
    if not isinstance(nested, dict):
        return None
    inner_url = nested.get("url", "")
    if isinstance(inner_url, str) and _is_data_url(inner_url):
        return inner_url
    return None


def _decode_data_url(data_url: str) -> tuple[bytes, str, str] | None:
    """解出字节、扩展名和 mime。失败记一条 warning，返回 None。"""
    try:
        header, payload = data_url.split(",", 1)
        mime = header.split(":")[1].split(";")[0] if ":" in header else "application/octet-stream"
        ext = (mimes.guess_extension(mime) or ".bin").lstrip(".")
        encoded = "base64" in header.lower().split(";")
        raw = b64.b64decode(payload) if encoded else urlparse.unquote_to_bytes(payload)
    except Exception as exc:
        logger.warning(_DECODE_FAILED, type(exc).__name__, exc)
        return None
    return raw, ext, mime


def _media_reference_block(path: str, mime: str) -> dict[str, Any]:
    """image / audio / video 用类型块，其余写成 file 文本。"""
    major = mime.split("/", 1)[0]
    if major in _TYPED_MEDIA:
        return {"type": major, "url": path}
    return {"type": "text", "text": f'<file url="{path}" />'}


def _rewrite_data_url_blocks(
    messages: list[lc_messages.AnyMessage],
    path_map: dict[str, str],
) -> tuple[list[lc_messages.AnyMessage], int]:
    """总是新建列表。没有 data URL 的消息保持原对象。"""
    rewritten: list[lc_messages.AnyMessage] = []
    failed_blocks = 0
    for message in messages:
        blocks: list[Any] = []
        touched = False
        for block in message.content_blocks:
            data_url = _extract_data_url(block)
            if data_url is None:
                blocks.append(block)
                continue
            touched = True
            decoded = _decode_data_url(data_url)
            if decoded is not None:
                raw, _ext, mime = decoded
                digest = hashes.sha256(raw).hexdigest()[:16]
                if digest in path_map:
                    blocks.append(_media_reference_block(path_map[digest], mime))
                    continue
            failed_blocks += 1
            blocks.append({"type": "text", "text": _OFFLOAD_FAILED_PLACEHOLDER})
        if touched:
            copied = message.model_copy()
            copied.content = blocks
            rewritten.append(copied)
        else:
            rewritten.append(message)
    return rewritten, failed_blocks


def _upload_response_error(responses: list[Any]) -> str | None:
    """空响应是 missing_upload_response。error 为 None 才算成功。"""
    if not responses:
        return _MISSING_UPLOAD
    error = responses[0].error
    if error is None:
        return None
    return str(error)


def _is_context_overflow(exc: Exception) -> bool:
    """ContextOverflowError 直接算。其它只认 400/413/422 加标记子串。"""
    if isinstance(exc, lc_errors.ContextOverflowError):
        return True
    if getattr(exc, "status_code", None) not in _OVERFLOW_STATUS:
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


class _DeepAgentsSummarizationMiddleware(lc_types.AgentMiddleware):
    """带后端落盘的摘要中间件。公开名字是 SummarizationMiddleware。"""

    trace_policy = lc_types.TracePolicy(process_inputs=lc_types.omit_payload)
    state_schema = SummarizationState
    serialized_name: ClassVar[str] = "SummarizationMiddleware"

    @property
    def name(self) -> str:
        """精确类型才叫 SummarizationMiddleware。子类用自己的类名。"""
        exact_type = type(self) is _DeepAgentsSummarizationMiddleware
        if exact_type:
            return "SummarizationMiddleware"
        return type(self).__name__

    def __init__(
        self,
        model: str | Any,
        *,
        backend: Any,
        trigger: Any = None,
        keep: lc_summary.ContextSize = ("messages", lc_summary._DEFAULT_MESSAGES_TO_KEEP),
        token_counter: lc_summary.TokenCounter = lc_token_utils.count_tokens_approximately,
        summary_prompt: str = DEEPAGENTS_DEFAULT_SUMMARY_PROMPT,
        trim_tokens_to_summarize: int | None = lc_summary._DEFAULT_TRIM_TOKEN_LIMIT,
        truncate_args_settings: TruncateArgsSettings | None = None,
        **deprecated_kwargs: Any,
    ) -> None:
        """backend 必填。history_path_prefix 在交给 LangChain 之前就拒绝。"""
        if "history_path_prefix" in deprecated_kwargs:
            raise TypeError(_HISTORY_REMOVED)
        self._lc_helper = lc_summary.SummarizationMiddleware(model=model, trigger=trigger, keep=keep, token_counter=token_counter, summary_prompt=summary_prompt, trim_tokens_to_summarize=trim_tokens_to_summarize, **deprecated_kwargs)
        self._counter_accepts_tools = _token_counter_accepts_tools(self.token_counter)
        self._backend = backend
        root = backend.artifacts_root if isinstance(backend, CompositeBackend) else "/"
        trimmed = root.rstrip("/")
        self._history_path_prefix = f"{trimmed}/conversation_history"
        self._large_tool_results_prefix = f"{trimmed}/large_tool_results"
        self._media_prefix = f"{self._history_path_prefix}/media"
        if truncate_args_settings is None:
            self._truncate_args_trigger = None
            self._truncate_args_keep: lc_summary.ContextSize = ("messages", 20)
            self._max_arg_length = 2000
            self._truncation_text = _ARG_CLIP_SUFFIX
        else:
            self._truncate_args_trigger = truncate_args_settings.get("trigger")
            self._truncate_args_keep = truncate_args_settings.get("keep", ("messages", 20))
            self._max_arg_length = truncate_args_settings.get("max_length", 2000)
            self._truncation_text = truncate_args_settings.get("truncation_text", _ARG_CLIP_SUFFIX)

    @property
    def model(self) -> Any:
        """摘要用的模型，来自 LangChain helper。"""
        return self._lc_helper.model

    @property
    def token_counter(self) -> lc_summary.TokenCounter:
        """计数器。默认实现可能已经被 helper 包成 partial。"""
        return self._lc_helper.token_counter

    def _get_profile_limits(self) -> int | None:
        return self._lc_helper._get_profile_limits()

    def _should_summarize(self, messages: list[lc_messages.AnyMessage], total_tokens: int) -> bool:
        return self._lc_helper._should_summarize(messages, total_tokens)

    def _determine_cutoff_index(self, messages: list[lc_messages.AnyMessage]) -> int:
        return self._lc_helper._determine_cutoff_index(messages)

    def _partition_messages(
        self,
        conversation_messages: list[lc_messages.AnyMessage],
        cutoff_index: int,
    ) -> tuple[list[lc_messages.AnyMessage], list[lc_messages.AnyMessage]]:
        return self._lc_helper._partition_messages(conversation_messages, cutoff_index)

    def _create_summary(self, messages_to_summarize: list[lc_messages.AnyMessage]) -> str:
        return self._lc_helper._create_summary(messages_to_summarize)

    async def _acreate_summary(self, messages_to_summarize: list[lc_messages.AnyMessage]) -> str:
        return await self._lc_helper._acreate_summary(messages_to_summarize)

    def _get_session_id(self, state: MapType[str, Any]) -> str:
        """非空字符串沿用。空字符串和别的类型都新开一个 session_ 加 32 位十六进制。"""
        found = state.get(SUMMARIZATION_SESSION_ID_KEY)
        if isinstance(found, str) and found:
            return found
        return "session_" + ids.uuid4().hex

    def _get_history_path(self, session_id: str) -> str:
        return f"{self._history_path_prefix}/{session_id}.md"

    def _is_summary_message(self, msg: lc_messages.AnyMessage) -> bool:
        if not isinstance(msg, lc_messages.HumanMessage):
            return False
        return msg.additional_kwargs.get("lc_source") == "summarization"

    def _filter_summary_messages(self, messages: list[lc_messages.AnyMessage]) -> list[lc_messages.AnyMessage]:
        return [msg for msg in messages if not self._is_summary_message(msg)]

    def _build_new_messages_with_path(self, summary: str, file_path: str | None) -> list[lc_messages.AnyMessage]:
        """file_path 只要不是 None 就写入路径，包括空字符串。"""
        if file_path is not None:
            content = f"You are in the middle of a conversation that has been summarized.\n\nThe full conversation history has been saved to {file_path} should you need to refer back to it for details.\n\nA condensed summary follows:\n\n<summary>\n{summary}\n</summary>"
        else:
            content = f"{_PLAIN_SUMMARY_HEAD}{summary}"
        return [lc_messages.HumanMessage(content=content, additional_kwargs={"lc_source": "summarization"})]

    def _get_effective_messages(self, request: lc_types.ModelRequest) -> list[lc_messages.AnyMessage]:
        event = request.state.get(SUMMARIZATION_EVENT_KEY)
        return self._apply_event_to_messages(request.messages, event)

    @staticmethod
    def _apply_event_to_messages(
        messages: list[lc_messages.AnyMessage],
        event: SummarizationEvent | None,
    ) -> list[lc_messages.AnyMessage]:
        """event 为 None 时复制列表。切点超过长度只留摘要。负数切片照 Python 的规则。"""
        if event is None:
            return list(messages)
        try:
            summary_message = event["summary_message"]
            boundary = event["cutoff_index"]
        except (KeyError, TypeError) as exc:
            logger.warning(_BAD_EVENT_KEYS, exc)
            return list(messages)
        if boundary > len(messages):
            logger.warning(_CUTOFF_PAST_END, boundary, len(messages))
            return [summary_message]
        visible = [summary_message]
        visible.extend(messages[boundary:])
        return visible

    @staticmethod
    def _compute_state_cutoff(event: SummarizationEvent | None, effective_cutoff: int) -> int:
        """bool 也是 int。True 当 1，False 当 0。"""
        if event is None:
            return effective_cutoff
        prior = event.get("cutoff_index")
        if not isinstance(prior, int):
            logger.warning(_BAD_CUTOFF)
            return effective_cutoff
        return prior + effective_cutoff - 1

    def _should_truncate_args(self, messages: list[lc_messages.AnyMessage], total_tokens: int) -> bool:
        if self._truncate_args_trigger is None:
            return False
        kind, value = self._truncate_args_trigger
        if kind == "messages":
            return len(messages) >= value
        if kind == "tokens":
            return total_tokens >= value
        if kind == "fraction":
            limit = self._get_profile_limits()
            if limit is None:
                return False
            threshold = int(limit * value)
            if threshold <= 0:
                threshold = 1
            return total_tokens >= threshold
        return False

    def _determine_truncate_cutoff_index(self, messages: list[lc_messages.AnyMessage]) -> int:
        kind, value = self._truncate_args_keep
        if kind == "messages":
            if len(messages) <= value:
                return len(messages)
            return int(len(messages) - value)
        if kind in {"tokens", "fraction"}:
            if kind == "fraction":
                limit = self._get_profile_limits()
                if limit is None:
                    keep_count = 20
                    if len(messages) <= keep_count:
                        return len(messages)
                    return len(messages) - keep_count
                target = int(limit * value)
            else:
                target = int(value)
            if target <= 0:
                target = 1
            kept = 0
            index = len(messages)
            while index > 0:
                index -= 1
                weight = self._lc_helper._partial_token_counter([messages[index]])
                if kept + weight > target:
                    return index + 1
                kept += weight
            return 0
        return len(messages)

    def _truncate_tool_call(self, tool_call: Any) -> Any:
        args = tool_call.get("args", {})
        trimmed: dict[str, Any] = {}
        touched = False
        for key, value in args.items():
            if isinstance(value, str) and len(value) > self._max_arg_length:
                trimmed[key] = value[:20] + self._truncation_text
                touched = True
            else:
                trimmed[key] = value
        if touched:
            return {**tool_call, "args": trimmed}
        return tool_call

    def _count_tokens(
        self,
        messages: list[lc_messages.AnyMessage],
        system_message: lc_messages.SystemMessage | None,
        tools: list[Any] | None,
    ) -> int:
        """True / False / None 三条路。None 才吞 TypeError。"""
        if system_message is not None:
            counted: list[Any] = [system_message, *messages]
        else:
            counted = messages
        if self._counter_accepts_tools is True:
            return self.token_counter(counted, tools=tools)
        if self._counter_accepts_tools is False:
            return self.token_counter(counted)
        try:
            return self.token_counter(counted, tools=tools)
        except TypeError:
            return self.token_counter(counted)

    def _truncate_args(
        self,
        messages: list[lc_messages.AnyMessage],
        total_tokens: int,
    ) -> tuple[list[lc_messages.AnyMessage], bool]:
        if not self._should_truncate_args(messages, total_tokens):
            return messages, False
        boundary = self._determine_truncate_cutoff_index(messages)
        if boundary >= len(messages):
            return messages, False
        produced: list[lc_messages.AnyMessage] = []
        modified = False
        for index, message in enumerate(messages):
            eligible = index < boundary and isinstance(message, lc_messages.AIMessage) and message.tool_calls
            if not eligible:
                produced.append(message)
                continue
            calls = []
            message_changed = False
            for tool_call in message.tool_calls:
                if tool_call["name"] in _HEAVY_ARG_TOOLS:
                    updated = self._truncate_tool_call(tool_call)
                    if updated != tool_call:
                        message_changed = True
                    calls.append(updated)
                else:
                    calls.append(tool_call)
            if message_changed:
                copied = message.model_copy()
                copied.tool_calls = calls
                produced.append(copied)
                modified = True
            else:
                produced.append(message)
        return produced, modified

    def _offload_inline_media(
        self,
        backend: Any,
        messages: list[lc_messages.AnyMessage],
    ) -> tuple[list[lc_messages.AnyMessage], int]:
        saw, path_map = _upload_unique_media(self, backend, messages)
        if not saw:
            return messages, 0
        return _rewrite_data_url_blocks(messages, path_map)

    async def _aoffload_inline_media(
        self,
        backend: Any,
        messages: list[lc_messages.AnyMessage],
    ) -> tuple[list[lc_messages.AnyMessage], int]:
        saw, path_map = await _aupload_unique_media(self, backend, messages)
        if not saw:
            return messages, 0
        return _rewrite_data_url_blocks(messages, path_map)

    def _offload_to_backend(
        self,
        backend: Any,
        messages: list[lc_messages.AnyMessage],
        session_id: str,
    ) -> str | None:
        path, kept, section = self._history_section(messages, session_id)
        existing = _read_existing(backend.download_files, path)
        return _commit_history(backend, path, existing, section, len(kept))

    async def _aoffload_to_backend(
        self,
        backend: Any,
        messages: list[lc_messages.AnyMessage],
        session_id: str,
    ) -> str | None:
        path, kept, section = self._history_section(messages, session_id)
        existing = await _aread_existing(backend, path)
        return await _acommit_history(backend, path, existing, section, len(kept))

    def _history_section(
        self,
        messages: list[lc_messages.AnyMessage],
        session_id: str,
    ) -> tuple[str, list[lc_messages.AnyMessage], str]:
        path = self._get_history_path(session_id)
        kept = self._filter_summary_messages(messages)
        stamp = clock.now(utc_zone).isoformat()
        body = lc_messages.get_buffer_string(kept, format="xml")
        section = f"## Summarized at {stamp}\n\n{body}\n\n"
        return path, kept, section

    @staticmethod
    def _with_replacements(response: lc_types.ModelResponse, replacements: list[lc_messages.AnyMessage]) -> Any:
        if replacements:
            return lc_types.ExtendedModelResponse(
                model_response=response,
                command=lg_types.Command(update={"messages": replacements}),
            )
        return response

    @staticmethod
    def _raise_recovery_exhausted(error: Exception) -> None:
        raise lc_errors.ContextOverflowError(_RECOVERY_EXHAUSTED) from error

    def _input_budget(self, request: lc_types.ModelRequest) -> int | None:
        profile = request.model.profile
        advertised = profile.get("max_input_tokens") if isinstance(profile, dict) else None
        if isinstance(advertised, bool) or not isinstance(advertised, int):
            return None
        reserved = 0
        settings = request.model_settings
        chat = request.model
        for field in _OUTPUT_LIMIT_KEYS:
            candidate = settings.get(field, getattr(chat, field, None))
            if isinstance(candidate, bool) or not isinstance(candidate, int):
                continue
            if candidate > reserved:
                reserved = candidate
        headroom = int(advertised * 0.95) - reserved
        if headroom > 0:
            return headroom
        return 0

    def _over_budget(self, request: lc_types.ModelRequest, total_tokens: int | None = None) -> bool:
        budget = self._input_budget(request)
        if budget is None:
            return False
        if total_tokens is not None:
            count = total_tokens
        else:
            count = self._count_tokens(request.messages, request.system_message, request.tools)
        return count > budget

    def _check_reduction(
        self,
        original: lc_types.ModelRequest,
        reduced: lc_types.ModelRequest,
        error: Exception | None,
    ) -> None:
        count = self._count_tokens(reduced.messages, reduced.system_message, reduced.tools)
        original_count = self._count_tokens(original.messages, original.system_message, original.tools)
        if error is not None and count >= original_count:
            self._raise_recovery_exhausted(error)
        budget = self._input_budget(reduced)
        if budget is not None and count > budget:
            raise lc_errors.ContextOverflowError(_STILL_OVER_BUDGET)

    def _call_with_budget(
        self,
        request: lc_types.ModelRequest,
        handler: Any,
        *,
        error: Exception | None = None,
        rejected: lc_types.ModelRequest | None = None,
    ) -> tuple[lc_types.ModelResponse, list[lc_messages.AnyMessage]]:
        baseline = rejected if rejected is not None else request
        swapped: list[lc_messages.AnyMessage] = []
        if error is not None or self._over_budget(request):
            clipped, swapped = _clip_overflow_tail(request.messages, self._backend, keep=("tokens", 1), max_input_tokens=self._get_profile_limits(), token_counter=self.token_counter, large_tool_results_prefix=self._large_tool_results_prefix)
            request = request.override(messages=clipped)
            self._check_reduction(baseline, request, error)
        try:
            outcome = handler(request)
        except Exception as exc:
            if _is_context_overflow(exc) is False:
                raise
            if error is not None or swapped:
                self._raise_recovery_exhausted(exc)
            return self._call_with_budget(request, handler, error=exc)
        return outcome, swapped

    async def _acall_with_budget(
        self,
        request: lc_types.ModelRequest,
        handler: Any,
        *,
        error: Exception | None = None,
        rejected: lc_types.ModelRequest | None = None,
    ) -> tuple[lc_types.ModelResponse, list[lc_messages.AnyMessage]]:
        baseline = rejected if rejected is not None else request
        swapped: list[lc_messages.AnyMessage] = []
        if error is not None or self._over_budget(request):
            clipped, swapped = await _aclip_overflow_tail(request.messages, self._backend, keep=("tokens", 1), max_input_tokens=self._get_profile_limits(), token_counter=self.token_counter, large_tool_results_prefix=self._large_tool_results_prefix)
            request = request.override(messages=clipped)
            self._check_reduction(baseline, request, error)
        try:
            outcome = await handler(request)
        except Exception as exc:
            if _is_context_overflow(exc) is False:
                raise
            if error is not None or swapped:
                self._raise_recovery_exhausted(exc)
            return await self._acall_with_budget(request, handler, error=exc)
        return outcome, swapped

    def wrap_model_call(self, request: lc_types.ModelRequest, handler: Any) -> Any:
        """先折叠旧事件，再截参数，够了才摘要。钩子参数名是 request 和 handler。"""
        effective = self._get_effective_messages(request)
        total_tokens = self._count_tokens(effective, request.system_message, request.tools)
        truncated, changed = self._truncate_args(effective, total_tokens)
        if changed:
            total_tokens = self._count_tokens(truncated, request.system_message, request.tools)
        should = self._should_summarize(truncated, total_tokens) or self._over_budget(request, total_tokens)
        overflow_error: Exception | None = None
        if not should:
            try:
                return handler(request.override(messages=truncated))
            except Exception as exc:
                if not _is_context_overflow(exc):
                    raise
                overflow_error = exc
        boundary = self._determine_cutoff_index(truncated)
        if boundary <= 0:
            response, replacements = self._call_with_budget(
                request.override(messages=truncated),
                handler,
                error=overflow_error,
            )
            return self._with_replacements(response, replacements)
        to_summarize, preserved = self._partition_messages(truncated, boundary)
        prepared, failed_media = self._offload_inline_media(self._backend, to_summarize)
        session_id = self._get_session_id(request.state)
        file_path = self._offload_to_backend(self._backend, prepared, session_id)
        _announce_offload(file_path, failed_media)
        summary = self._create_summary(prepared)
        new_messages = self._build_new_messages_with_path(summary, file_path)
        previous = request.state.get(SUMMARIZATION_EVENT_KEY)
        state_cutoff = self._compute_state_cutoff(previous, boundary)
        new_event: SummarizationEvent = {
            "cutoff_index": state_cutoff,
            "summary_message": new_messages[0],
            "file_path": file_path,
        }
        modified = request.override(messages=[*new_messages, *preserved])
        response, tail = self._call_with_budget(
            modified,
            handler,
            error=overflow_error,
            rejected=request.override(messages=truncated),
        )
        update: dict[str, Any] = {
            SUMMARIZATION_EVENT_KEY: new_event,
            SUMMARIZATION_SESSION_ID_KEY: session_id,
        }
        if tail:
            update["messages"] = list(tail)
        return lc_types.ExtendedModelResponse(model_response=response, command=lg_types.Command(update=update))

    async def awrap_model_call(self, request: lc_types.ModelRequest, handler: Any) -> Any:
        """和同步版相同，但落盘和摘要一起跑。"""
        effective = self._get_effective_messages(request)
        total_tokens = self._count_tokens(effective, request.system_message, request.tools)
        truncated, changed = self._truncate_args(effective, total_tokens)
        if changed:
            total_tokens = self._count_tokens(truncated, request.system_message, request.tools)
        should = self._should_summarize(truncated, total_tokens) or self._over_budget(request, total_tokens)
        overflow_error: Exception | None = None
        if not should:
            try:
                return await handler(request.override(messages=truncated))
            except Exception as exc:
                if not _is_context_overflow(exc):
                    raise
                overflow_error = exc
        boundary = self._determine_cutoff_index(truncated)
        if boundary <= 0:
            response, replacements = await self._acall_with_budget(
                request.override(messages=truncated),
                handler,
                error=overflow_error,
            )
            return self._with_replacements(response, replacements)
        to_summarize, preserved = self._partition_messages(truncated, boundary)
        prepared, failed_media = await self._aoffload_inline_media(self._backend, to_summarize)
        session_id = self._get_session_id(request.state)
        file_path, summary = await aio.gather(
            self._aoffload_to_backend(self._backend, prepared, session_id),
            self._acreate_summary(prepared),
        )
        _announce_offload(file_path, failed_media)
        new_messages = self._build_new_messages_with_path(summary, file_path)
        previous = request.state.get(SUMMARIZATION_EVENT_KEY)
        state_cutoff = self._compute_state_cutoff(previous, boundary)
        new_event: SummarizationEvent = {
            "cutoff_index": state_cutoff,
            "summary_message": new_messages[0],
            "file_path": file_path,
        }
        modified = request.override(messages=[*new_messages, *preserved])
        response, tail = await self._acall_with_budget(
            modified,
            handler,
            error=overflow_error,
            rejected=request.override(messages=truncated),
        )
        update: dict[str, Any] = {
            SUMMARIZATION_EVENT_KEY: new_event,
            SUMMARIZATION_SESSION_ID_KEY: session_id,
        }
        if tail:
            update["messages"] = list(tail)
        return lc_types.ExtendedModelResponse(model_response=response, command=lg_types.Command(update=update))


SummarizationMiddleware = _DeepAgentsSummarizationMiddleware


def create_summarization_middleware(
    model: Any,
    backend: Any,
    *,
    summary_prompt: str = DEEPAGENTS_DEFAULT_SUMMARY_PROMPT,
    trim_tokens_to_summarize: int | None = None,
    token_counter: lc_summary.TokenCounter = lc_token_utils.count_tokens_approximately,
) -> _DeepAgentsSummarizationMiddleware:
    """model 必须已经是 BaseChatModel。trim 默认 None，不是 4000。"""
    from langchain.chat_models import BaseChatModel as RuntimeBaseChatModel

    if not isinstance(model, RuntimeBaseChatModel):
        raise TypeError(_FACTORY_MODEL_TYPE)
    chosen = compute_summarization_defaults(model)
    return SummarizationMiddleware(model, backend=backend, trigger=chosen["trigger"], keep=chosen["keep"], token_counter=token_counter, summary_prompt=summary_prompt, trim_tokens_to_summarize=trim_tokens_to_summarize, truncate_args_settings=chosen["truncate_args_settings"])


def create_summarization_tool_middleware(
    model: str | Any,
    backend: Any,
    *,
    system_prompt: str | None = None,
) -> SummarizationToolMiddleware:
    """每次调用都 import resolve_model，然后才看是不是字符串。"""
    from dataagent.core._models import resolve_model

    if isinstance(model, str):
        model = resolve_model(model)
    engine = create_summarization_middleware(model, backend)
    return SummarizationToolMiddleware(engine, system_prompt=system_prompt)


class SummarizationToolMiddleware(lc_types.AgentMiddleware):
    """只在 compact_conversation 被调用时折叠。自己不按阈值自动跑。"""

    trace_policy = lc_types.TracePolicy(process_inputs=lc_types.omit_payload)
    state_schema = SummarizationState

    def __init__(self, summarization: _DeepAgentsSummarizationMiddleware, *, system_prompt: str | None = None) -> None:
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError(f"system_prompt must be str or None, got {type(system_prompt).__name__}")
        self._summarization = summarization
        self.system_prompt = system_prompt
        self.tools = [self._create_compact_tool()]

    def _create_compact_tool(self) -> Any:
        from langchain_core.tools import StructuredTool

        owner = self

        def sync_compact(runtime: lc_tools.ToolRuntime) -> lg_types.Command:
            return owner._run_compact(runtime)

        async def async_compact(runtime: lc_tools.ToolRuntime) -> lg_types.Command:
            return await owner._arun_compact(runtime)

        return StructuredTool.from_function(
            name="compact_conversation",
            description=_COMPACT_TOOL_DESCRIPTION,
            func=sync_compact,
            coroutine=async_compact,
        )

    def _build_compact_result(
        self,
        runtime: Any,
        to_summarize: list[lc_messages.AnyMessage],
        summary: str,
        file_path: str | None,
        event: SummarizationEvent | None,
        cutoff: int,
        session_id: str,
    ) -> lg_types.Command:
        engine = self._summarization
        summary_message = engine._build_new_messages_with_path(summary, file_path)[0]
        state_cutoff = engine._compute_state_cutoff(event, cutoff)
        new_event: SummarizationEvent = {
            "cutoff_index": state_cutoff,
            "summary_message": summary_message,
            "file_path": file_path,
        }
        note = f"Conversation compacted. Summarized {len(to_summarize)} messages into a concise summary."
        return lg_types.Command(
            update={
                SUMMARIZATION_EVENT_KEY: new_event,
                SUMMARIZATION_SESSION_ID_KEY: session_id,
                "messages": [lc_messages.ToolMessage(content=note, tool_call_id=runtime.tool_call_id)],
            }
        )

    @staticmethod
    def _nothing_to_compact(tool_call_id: str) -> lg_types.Command:
        return lg_types.Command(
            update={
                "messages": [lc_messages.ToolMessage(content=_NOTHING_TO_COMPACT, tool_call_id=tool_call_id)],
            }
        )

    @staticmethod
    def _compact_error(tool_call_id: str, exc: BaseException) -> lg_types.Command:
        text = f"Compaction failed: an error occurred while generating the summary ({type(exc).__name__}: {exc}). The conversation has not been compacted — no messages were summarized or removed."
        return lg_types.Command(
            update={"messages": [lc_messages.ToolMessage(content=text, tool_call_id=tool_call_id)]}
        )

    @staticmethod
    def _compact_threshold(value: float) -> int:
        return max(1, int(value * 0.5))

    @staticmethod
    def _compact_trigger_clause(condition: object) -> MapType[str, float]:
        if isinstance(condition, MapType):
            return condition
        kind, value = condition  # type: ignore[misc]
        return {kind: value}

    def _is_compaction_clause_met(self, clause: MapType[str, float], messages: list[lc_messages.AnyMessage]) -> bool:
        helper = self._summarization._lc_helper
        for kind, value in clause.items():
            if kind == "messages" and len(messages) < self._compact_threshold(value):
                return False
            if kind == "tokens" and not helper._should_summarize_based_on_reported_tokens(
                messages, self._compact_threshold(value)
            ):
                return False
            if kind == "fraction":
                limit = helper._get_profile_limits()
                if limit is None:
                    return False
                threshold = self._compact_threshold(limit * value)
                if not helper._should_summarize_based_on_reported_tokens(messages, threshold):
                    return False
            if kind not in {"messages", "tokens", "fraction"}:
                return False
        return True

    def _is_eligible_for_compaction(self, messages: list[lc_messages.AnyMessage]) -> bool:
        clauses = self._summarization._lc_helper._trigger_clauses
        if not clauses:
            return False
        return any(self._is_compaction_clause_met(self._compact_trigger_clause(clause), messages) for clause in clauses)

    def _run_compact(self, runtime: Any) -> lg_types.Command:
        engine = self._summarization
        tool_call_id = runtime.tool_call_id or ""
        messages = runtime.state.get("messages", [])
        event = runtime.state.get(SUMMARIZATION_EVENT_KEY)
        effective = engine._apply_event_to_messages(messages, event)
        if not self._is_eligible_for_compaction(effective):
            return self._nothing_to_compact(tool_call_id)
        cutoff = engine._determine_cutoff_index(effective)
        if cutoff == 0:
            return self._nothing_to_compact(tool_call_id)
        session_id = engine._get_session_id(runtime.state)
        try:
            to_summarize, _preserved = engine._partition_messages(effective, cutoff)
            summary = engine._create_summary(to_summarize)
            file_path = engine._offload_to_backend(engine._backend, to_summarize, session_id)
        except Exception as exc:
            logger.exception(_COMPACT_FAILED_LOG)
            return self._compact_error(tool_call_id, exc)
        return self._build_compact_result(runtime, to_summarize, summary, file_path, event, cutoff, session_id)

    async def _arun_compact(self, runtime: Any) -> lg_types.Command:
        engine = self._summarization
        tool_call_id = runtime.tool_call_id or ""
        messages = runtime.state.get("messages", [])
        event = runtime.state.get(SUMMARIZATION_EVENT_KEY)
        effective = engine._apply_event_to_messages(messages, event)
        if not self._is_eligible_for_compaction(effective):
            return self._nothing_to_compact(tool_call_id)
        cutoff = engine._determine_cutoff_index(effective)
        if cutoff == 0:
            return self._nothing_to_compact(tool_call_id)
        session_id = engine._get_session_id(runtime.state)
        try:
            to_summarize, _preserved = engine._partition_messages(effective, cutoff)
            summary = await engine._acreate_summary(to_summarize)
            file_path = await engine._aoffload_to_backend(engine._backend, to_summarize, session_id)
        except Exception as exc:
            logger.exception(_COMPACT_FAILED_LOG)
            return self._compact_error(tool_call_id, exc)
        return self._build_compact_result(runtime, to_summarize, summary, file_path, event, cutoff, session_id)

    def wrap_model_call(self, request: lc_types.ModelRequest, handler: Any) -> lc_types.ModelResponse:
        if self.system_prompt is None:
            return handler(request)
        updated = append_to_system_message(request.system_message, self.system_prompt)
        return handler(request.override(system_message=updated))

    async def awrap_model_call(self, request: lc_types.ModelRequest, handler: Any) -> lc_types.ModelResponse:
        if self.system_prompt is None:
            return await handler(request)
        updated = append_to_system_message(request.system_message, self.system_prompt)
        return await handler(request.override(system_message=updated))


def _announce_offload(file_path: str | None, failed_media: int) -> None:
    """警告必须从 wrap_model_call 的直接调用栈发出，stacklevel 才是 2。"""
    if file_path is None:
        logger.error(_OFFLOAD_LOST)
        warnmod.warn(_OFFLOAD_LOST, stacklevel=3)
        return
    if failed_media:
        notice = f"Conversation history was offloaded to {file_path}, but {failed_media}{_MEDIA_PARTIAL_TAIL}"
        logger.warning(notice)
        warnmod.warn(notice, stacklevel=3)


def _upload_unique_media(owner: _DeepAgentsSummarizationMiddleware, backend: Any, messages: list[Any]) -> tuple[bool, dict[str, str]]:
    """同一轮块循环里，解码成功立刻 upload_files，不要先把全部 data URL 解完。"""
    saw = False
    path_map: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        for block in message.content_blocks:
            data_url = _extract_data_url(block)
            if data_url is None:
                continue
            saw = True
            decoded = _decode_data_url(data_url)
            if decoded is None:
                continue
            raw, ext, _mime = decoded
            digest = hashes.sha256(raw).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            img_path = f"{owner._media_prefix}/{digest}.{ext}"
            try:
                responses = backend.upload_files([(img_path, raw)])
                if problem := _upload_response_error(responses):
                    logger.warning(_UPLOAD_FAILED, img_path, problem)
                    continue
                path_map[digest] = img_path
            except Exception as exc:
                logger.warning(_UPLOAD_RAISED, img_path, type(exc).__name__, exc)
    return saw, path_map


async def _aupload_unique_media(
    owner: _DeepAgentsSummarizationMiddleware,
    backend: Any,
    messages: list[Any],
) -> tuple[bool, dict[str, str]]:
    """异步版也是解一条传一条。aupload_files 紧跟在这次解码后面。"""
    saw = False
    path_map: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        for block in message.content_blocks:
            data_url = _extract_data_url(block)
            if data_url is None:
                continue
            saw = True
            decoded = _decode_data_url(data_url)
            if decoded is None:
                continue
            raw, ext, _mime = decoded
            digest = hashes.sha256(raw).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            img_path = f"{owner._media_prefix}/{digest}.{ext}"
            try:
                responses = await backend.aupload_files([(img_path, raw)])
                if problem := _upload_response_error(responses):
                    logger.warning(_UPLOAD_FAILED, img_path, problem)
                    continue
                path_map[digest] = img_path
            except Exception as exc:
                logger.warning(_UPLOAD_RAISED, img_path, type(exc).__name__, exc)
    return saw, path_map


def _read_existing(download, path: str) -> str:
    try:
        responses = download([path])
        if responses and responses[0].content is not None and responses[0].error is None:
            return responses[0].content.decode("utf-8")
    except Exception as exc:
        logger.debug(_READ_HISTORY_FAILED, path, type(exc).__name__, exc)
    return ""


async def _aread_existing(backend: Any, path: str) -> str:
    try:
        responses = await backend.adownload_files([path])
        if responses and responses[0].content is not None and responses[0].error is None:
            return responses[0].content.decode("utf-8")
    except Exception as exc:
        logger.debug(_READ_HISTORY_FAILED, path, type(exc).__name__, exc)
    return ""


def _commit_history(backend: Any, path: str, existing: str, section: str, count: int) -> str | None:
    combined = existing + section
    try:
        result = backend.edit(path, existing, combined) if existing else backend.write(path, combined)
        if result is None or result.error:
            reason = result.error if result else _BACKEND_RETURNED_NONE
            logger.warning(_WRITE_HISTORY_FAILED, path, count, reason)
            return None
    except Exception as exc:
        logger.warning(_WRITE_HISTORY_RAISED, path, count, type(exc).__name__, exc)
        return None
    logger.debug(_WRITE_HISTORY_OK, count, path)
    return path


async def _acommit_history(backend: Any, path: str, existing: str, section: str, count: int) -> str | None:
    combined = existing + section
    try:
        result = await backend.aedit(path, existing, combined) if existing else await backend.awrite(path, combined)
        if result is None or result.error:
            reason = result.error if result else _BACKEND_RETURNED_NONE
            logger.warning(_WRITE_HISTORY_FAILED, path, count, reason)
            return None
    except Exception as exc:
        logger.warning(_WRITE_HISTORY_RAISED, path, count, type(exc).__name__, exc)
        return None
    logger.debug(_WRITE_HISTORY_OK, count, path)
    return path
