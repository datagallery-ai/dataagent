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

import inspect
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from loguru import logger

from dataagent.core.utils.performance import get_current_collector, summarize_llm_usage


def log_llm_done(phase: str, resp: LLMResponse, *, rid: str | None = None) -> None:
    """单次 invoke / 流式结束时的汇总日志（避免在 _wrap_output 内对每个 chunk 打 debug）。

    与同一调用里 ``Start ... rid=`` 成对传入 ``rid`` 即可在日志里对齐 start/finish。
    """
    rid_seg = f" rid={rid}" if rid else ""
    logger.debug(
        f"{phase}{rid_seg}: reasoning_len={len(resp.reasoning_content or '')} "
        f"content_len={len(resp.content or '')} tool_calls={len(resp.tool_calls or [])} "
        f"invalid_tool_calls={len(resp.invalid_tool_calls or [])}"
    )


@dataclass(frozen=True)
class LLMResponse:
    """
    统一的 LLM 返回结构（尽量兼容 langchain 的 AIMessage 使用方式）。

    兼容点：
    - 业务侧常用：resp.content / resp.usage_metadata
    - raw 保留底层对象，便于排障或兼容极少数高级用法
    """

    content: str
    usage_metadata: dict[str, Any]
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    invalid_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None


@dataclass(frozen=True)
class LLMStreamChunk:
    """统一的流式输出块。"""

    content: str = ""
    reasoning_content: str = ""
    raw: Any = None
    final_response: LLMResponse | None = None
    done: bool = False


@dataclass
class _StreamAccum:
    """流式 merge 内部状态：用 list 累积文本，避免 str 逐片相加的 O(n²) 拷贝。"""

    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    invalid_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None

    def append_chunk(self, chunk_resp: LLMResponse) -> None:
        """合并单个流式 chunk 的文本与元数据。"""
        self.content_parts.append(chunk_resp.content)
        if chunk_resp.reasoning_content:
            self.reasoning_parts.append(chunk_resp.reasoning_content)
        self.usage_metadata = _merge_usage_metadata(chunk_resp.usage_metadata, self.usage_metadata)
        if chunk_resp.tool_calls:
            self.tool_calls = list(chunk_resp.tool_calls)
        if chunk_resp.invalid_tool_calls:
            self.invalid_tool_calls = list(chunk_resp.invalid_tool_calls)
        self.raw = chunk_resp.raw

    def to_llm_response(self) -> LLMResponse:
        """将累积状态化为最终 LLMResponse。"""
        return LLMResponse(
            content="".join(self.content_parts),
            usage_metadata=self.usage_metadata,
            reasoning_content="".join(self.reasoning_parts),
            tool_calls=list(self.tool_calls),
            invalid_tool_calls=list(self.invalid_tool_calls),
            raw=self.raw,
        )


def coerce_chat_input_to_messages(chat_input: Any) -> Any:
    """将裸 str 转为 OpenAI 风格 user message，兼容 LangChain 旧式 llm.invoke(\"...\") 调用。"""
    if isinstance(chat_input, str):
        return [{"role": "user", "content": chat_input}]
    return chat_input


@runtime_checkable
class ChatModel(Protocol):
    """DataAgent 侧统一 ChatModel 协议（对外不暴露具体 SDK）。"""

    def invoke(self, chat_input: Any, **kwargs: Any) -> LLMResponse:
        """同步调用模型接口。"""
        ...

    async def ainvoke(self, chat_input: Any, **kwargs: Any) -> LLMResponse:
        """异步调用模型接口。"""
        ...

    def astream(self, chat_input: Any, **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        """异步流式调用模型接口。"""
        ...

    def bind_tools(self, tools: Any, **kwargs: Any) -> ChatModel:
        """绑定工具到模型。"""
        ...


def _int_or_zero(val: Any) -> int:
    """Coerce ``val`` to int, falling back to 0 on failure."""
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage_metadata(usage: Any) -> dict[str, Any]:
    """补齐 langchain AIMessage 所需的 usage_metadata 必填字段，保留 cache/reasoning 子字段。

    Handles both:
    - OpenAI/DeepSeek: ``input_cache_read_tokens`` / ``input_cache_creation_tokens`` /
      ``output_reasoning_tokens`` at root (from ``_extract_detail_tokens``)
    - Anthropic fallback: ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` /
      ``reasoning_tokens`` at root (when extraction did not rename them)
    """
    usage_dict = cast(dict[str, Any], usage or {}) if isinstance(usage, dict) else {}
    return {
        "input_tokens": _int_or_zero(usage_dict.get("input_tokens")),
        "output_tokens": _int_or_zero(usage_dict.get("output_tokens")),
        "total_tokens": _int_or_zero(usage_dict.get("total_tokens")),
        "input_cache_read_tokens": _int_or_zero(
            usage_dict.get("input_cache_read_tokens") or usage_dict.get("cache_read_input_tokens")
        ),
        "input_cache_creation_tokens": _int_or_zero(
            usage_dict.get("input_cache_creation_tokens") or usage_dict.get("cache_creation_input_tokens")
        ),
        "output_reasoning_tokens": _int_or_zero(
            usage_dict.get("output_reasoning_tokens") or usage_dict.get("reasoning_tokens")
        ),
    }


def _usage_has_tokens(usage: dict[str, Any]) -> bool:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        try:
            if int(usage.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _merge_usage_metadata(preferred: Any, fallback: Any) -> dict[str, Any]:
    preferred_usage = normalize_usage_metadata(preferred)
    fallback_usage = normalize_usage_metadata(fallback)
    if _usage_has_tokens(preferred_usage) or not _usage_has_tokens(fallback_usage):
        return preferred_usage
    return fallback_usage


class LangChainChatModelAdapter:
    """
    将 langchain ChatModel 适配为 DataAgent 统一 ChatModel。

    设计目标：
    - 对业务侧保持“看起来像 langchain”的 API：invoke/ainvoke，返回有 .content/.usage_metadata
    - 通过 __getattr__ 透传底层对象能力，降低迁移风险
    """

    def __init__(
        self,
        raw_model: Any,
        config: Any = None,
    ):
        """
        初始化适配器，持有原始模型实例。

        Args:
            raw_model: 底层 LLM 实例（来自 langchain 或其他 SDK）。
            config: LLMConfig 实例，提供 logical name 等元数据。可选，兼容旧调用。
        """
        self._raw = raw_model
        self._config = config

    def __call__(self, *args: Any, **kwargs: Any) -> LLMResponse:
        """允许直接调用实例，内部转发给 invoke。"""
        # 兼容少数旧代码：llm([HumanMessage(...)]) 这样的调用方式
        return self.invoke(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        """代理访问原始模型的属性和方法。"""
        # 透传底层模型属性/方法（如 model_name、bind_tools 等）
        return getattr(self._raw, item)

    @property
    def raw(self) -> Any:
        """返回被包装的原始模型对象。"""
        return self._raw

    @property
    def _llm_perf_name(self) -> str:
        """Name used for performance events: logical:model when both are known."""
        logical = (
            getattr(self._config, "logical_name", None)
            or getattr(self._config, "name", None)
            or getattr(self._config, "section", None)
        )
        model = (
            getattr(self._raw, "model_name", None)
            or getattr(self._raw, "model", None)
            or getattr(self._raw, "_model", None)
        )
        if logical and model and str(logical) != str(model):
            return f"{logical}:{model}"
        return str(logical or model or self._raw.__class__.__name__)

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """兼容字符串、多段内容块以及其他可序列化对象。"""
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if "text" in item:
                        parts.append(str(item.get("text") or ""))
                    elif "content" in item:
                        parts.append(str(item.get("content") or ""))
                    else:
                        parts.append(json.dumps(item, ensure_ascii=False))
                else:
                    parts.append(str(item))
            return "".join(parts)
        if isinstance(content, dict):
            if "text" in content:
                return str(content.get("text") or "")
            if "content" in content:
                return str(content.get("content") or "")
            return json.dumps(content, ensure_ascii=False)
        return str(content)

    @staticmethod
    def _extract_reasoning_content(out: Any) -> str:
        """从 AIMessage / 类似对象上取出 reasoning（含 additional_kwargs）。"""
        try:
            rc = getattr(out, "reasoning_content", None)
            if rc is not None and str(rc).strip():
                return str(rc)
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            additional_kwargs = cast(dict[str, Any], getattr(out, "additional_kwargs", None) or {})
            reasoning_raw = additional_kwargs.get("reasoning_content") or additional_kwargs.get("reasoning")
            if reasoning_raw is not None:
                return str(reasoning_raw)
        except (AttributeError, TypeError, ValueError):
            pass
        return ""

    @staticmethod
    def _to_openai_tool_call(tc: Any) -> dict[str, Any] | None:
        # 允许已是 OpenAI 形态
        if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
            return tc
        if not isinstance(tc, dict):
            return None
        tc_id = tc.get("id") or tc.get("tool_call_id") or ""
        name = tc.get("name")
        args = tc.get("args")
        # 兼容部分实现把 function/arguments 放在其他字段
        func = tc.get("function") if isinstance(tc.get("function"), dict) else None
        if name is None and func:
            name = func.get("name")
        if args is None and func:
            args = func.get("arguments")
        if args is None:
            args = tc.get("arguments")
        args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args or "")
        if not name:
            return None
        return {"id": str(tc_id), "type": "function", "function": {"name": str(name), "arguments": args_str}}

    @staticmethod
    def _normalize_openai_dicts_to_lc_messages(chat_input: Any) -> Any:
        """
        raw 模型来自 langchain 时：将 OpenAI dict messages 转为 langchain messages。
        保持与旧逻辑一致：输入不是 list/为空/首元素不符合 dict+role 时直接返回原输入。
        """
        if not isinstance(chat_input, list) or not chat_input:
            return chat_input
        first = chat_input[0]
        if not isinstance(first, dict) or "role" not in first:
            return chat_input

        try:
            from langchain_core.messages import (  # type: ignore[import-not-found]
                AIMessage,
                BaseMessage,
                HumanMessage,
                SystemMessage,
                ToolMessage,
            )
        except Exception as e:
            raise ImportError(
                "收到 dict messages 输入，但当前环境未安装 langchain_core；"
                "请在 backend=langgraph 时安装 langchain 依赖。"
            ) from e

        converted_lc: list[BaseMessage] = []
        for msg in chat_input:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "user":
                converted_lc.append(HumanMessage(content=str(content)))
            elif role == "system":
                converted_lc.append(SystemMessage(content=str(content)))
            elif role == "assistant":
                tc = msg.get("tool_calls", None)
                if tc:
                    converted_lc.append(AIMessage(content=str(content), tool_calls=tc))
                else:
                    converted_lc.append(AIMessage(content=str(content)))
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id")
                name = msg.get("name", None)
                kwargs2: dict[str, Any] = {"content": str(content), "tool_call_id": tool_call_id}
                if name is not None:
                    kwargs2["name"] = name
                converted_lc.append(ToolMessage(**kwargs2))
            else:
                converted_lc.append(HumanMessage(content=str(content)))

        return converted_lc if converted_lc else chat_input

    @staticmethod
    def _fill_llm_extra(handle: Any, resp: LLMResponse) -> None:
        usage = summarize_llm_usage(resp.usage_metadata)
        handle.update(
            usage,
            tool_call_count=len(resp.tool_calls or []),
            invalid_tool_call_count=len(resp.invalid_tool_calls or []),
            content_len=len(resp.content or ""),
            reasoning_len=len(resp.reasoning_content or ""),
        )

    @staticmethod
    def _infer_role(*, msg_type: Any, cls_name: str) -> str:
        """Infer OpenAI role from a langchain message's type/cls_name (default user)."""
        if msg_type in {"human", "user"} or cls_name == "HumanMessage":
            return "user"
        if msg_type in {"ai", "assistant"} or cls_name == "AIMessage":
            return "assistant"
        if msg_type == "system" or cls_name == "SystemMessage":
            return "system"
        if msg_type == "tool" or cls_name == "ToolMessage":
            return "tool"
        return "user"

    @staticmethod
    def _maybe_add_tool_fields(m: dict[str, Any], msg: Any) -> None:
        """Copy tool_call_id / name from a langchain ToolMessage into the dict form."""
        tool_call_id = getattr(msg, "tool_call_id", None)
        name = getattr(msg, "name", None)
        if tool_call_id is not None:
            m["tool_call_id"] = tool_call_id
        if name is not None:
            m["name"] = name

    @classmethod
    def messages_to_openai_dicts(cls, chat_input: Any) -> Any:
        """非 langchain 后端：LangChain Message / dict 列表 → OpenAI dict messages。"""
        return cls._normalize_lc_messages_to_openai_dicts(chat_input)

    @classmethod
    def _normalize_lc_messages_to_openai_dicts(cls, chat_input: Any) -> Any:
        """
        raw 模型非 langchain 时，尽量将 langchain messages 转为 OpenAI dict messages。
        非 list / 空 list 时直接返回原输入。原先「首元素为 dict 则整表原样返回」会跳过
        assistant+tool_calls 的 reasoning_content 补全（Thinking 模式 API 必填），已移除。
        """
        if not isinstance(chat_input, list) or not chat_input:
            return chat_input

        converted_msgs: list[dict[str, Any]] = []
        for msg in chat_input:
            if isinstance(msg, dict):
                m2 = dict(msg)
                cls._ensure_reasoning_for_assistant_tool_calls(m2, None)
                converted_msgs.append(m2)
                continue
            converted_msgs.append(cls._normalize_single_lc_message(msg))

        return converted_msgs if converted_msgs else chat_input

    @classmethod
    def _maybe_add_assistant_tool_calls(cls, m: dict[str, Any], msg: Any) -> None:
        """Convert and attach langchain tool_calls (list or single dict) to OpenAI form."""
        tc = getattr(msg, "tool_calls", None)
        if not tc:
            return
        if isinstance(tc, list):
            converted_tc: list[dict[str, Any]] = []
            for one in tc:
                conv = cls._to_openai_tool_call(one)
                if conv is not None:
                    converted_tc.append(conv)
            if converted_tc:
                m["tool_calls"] = converted_tc
            return
        if isinstance(tc, dict):
            conv = cls._to_openai_tool_call(tc)
            if conv is not None:
                m["tool_calls"] = [conv]

    @classmethod
    def _ensure_reasoning_for_assistant_tool_calls(cls, m: dict[str, Any], lc_msg: Any | None) -> None:
        """DashScope 等 Thinking 模式：带 tool_calls 的 assistant 必须带 reasoning_content 字段。"""
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            return
        if lc_msg is not None:
            rc = cls._extract_reasoning_content(lc_msg)
            m["reasoning_content"] = rc if rc else ""
            return
        if m.get("reasoning_content") is not None:
            return
        m["reasoning_content"] = str(m.get("reasoning") or "")

    @classmethod
    def _normalize_single_lc_message(cls, msg: Any) -> dict[str, Any]:
        """Normalize one langchain Message object into an OpenAI dict message."""
        cls_name = getattr(getattr(msg, "__class__", None), "__name__", "") or ""
        msg_type = getattr(msg, "type", None)
        content = getattr(msg, "content", "")
        role = cls._infer_role(msg_type=msg_type, cls_name=cls_name)
        if isinstance(content, list):
            m: dict[str, Any] = {"role": role, "content": content}
        else:
            m: dict[str, Any] = {"role": role, "content": str(content or "")}
        if role == "tool":
            cls._maybe_add_tool_fields(m, msg)
        if role == "assistant":
            cls._maybe_add_assistant_tool_calls(m, msg)
            cls._ensure_reasoning_for_assistant_tool_calls(m, msg)
        return m

    @classmethod
    def _wrap_output(cls, out: Any) -> LLMResponse:
        """解析模型输出并封装为统一响应结构。"""
        # langchain 通常返回 AIMessage，具备 content / usage_metadata
        content = ""
        usage: dict[str, Any] = {}
        tool_calls: list[dict[str, Any]] = []
        invalid_tool_calls: list[dict[str, Any]] = []
        try:
            content = cls._content_to_text(getattr(out, "content", ""))
        except Exception:
            content = cls._content_to_text(out)
        try:
            usage = normalize_usage_metadata(getattr(out, "usage_metadata", None) or {})
        except Exception:
            usage = normalize_usage_metadata({})

        # 如果未来直接收到带顶层 usage 的原始 stream chunk，也能被统一收敛成 metrics。
        if not any(usage.values()) and isinstance(out, dict):
            raw_usage = out.get("usage") or {}
            usage = normalize_usage_metadata(
                {
                    "input_tokens": raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0)),
                    "output_tokens": raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0)),
                    "total_tokens": raw_usage.get("total_tokens", 0),
                }
            )
        reasoning_content = cls._extract_reasoning_content(out)
        try:
            tool_calls = cast(list[dict[str, Any]], getattr(out, "tool_calls", None) or [])
        except Exception:
            tool_calls = []
        try:
            invalid_tool_calls = cast(list[dict[str, Any]], getattr(out, "invalid_tool_calls", None) or [])
        except Exception:
            invalid_tool_calls = []
        return LLMResponse(
            content=content,
            usage_metadata=usage,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls or [],
            invalid_tool_calls=invalid_tool_calls or [],
            raw=out,
        )

    def invoke(self, chat_input: Any, **kwargs: Any) -> LLMResponse:
        """规范化输入并同步调用模型。"""
        with get_current_collector().measure("llm", self._llm_perf_name, call_mode="invoke") as h:
            resp = self._invoke_inner(chat_input, kwargs)
            self._fill_llm_extra(h, resp)
            return resp

    async def ainvoke(self, chat_input: Any, **kwargs: Any) -> LLMResponse:
        """规范化输入并异步调用模型。"""
        with get_current_collector().measure("llm", self._llm_perf_name, call_mode="ainvoke") as h:
            norm_input = self._normalize_input_for_langchain(chat_input)
            resp = await self._ainvoke_normalized(norm_input, kwargs)
            self._fill_llm_extra(h, resp)
            return resp

    async def astream(self, chat_input: Any, **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        """规范化输入并优先使用底层流式能力；不支持时回退到一次性调用。"""
        norm_input = self._normalize_input_for_langchain(chat_input)
        raw_fn = getattr(self._raw, "astream", None)

        with get_current_collector().measure("llm", self._llm_perf_name, call_mode="astream") as h:
            if not callable(raw_fn):
                final_resp = await self._ainvoke_normalized(norm_input, kwargs)
                self._fill_llm_extra(h, final_resp)
                yield LLMStreamChunk(final_response=final_resp, done=True)
                return

            rid = uuid4().hex[:8]
            logger.debug("Start to stream llm rid={}", rid)
            stream = raw_fn(norm_input, **kwargs)
            logger.debug("LLM stream created rid={} stream_type={}", rid, type(stream).__name__)

            accumulated: _StreamAccum | None = None
            chunk_count = 0
            if hasattr(stream, "__aiter__"):
                async for out in cast(AsyncIterator[Any], stream):
                    chunk_count += 1
                    accumulated, stream_chunk = self._stream_out_step(accumulated, out)
                    if stream_chunk is not None:
                        yield stream_chunk
            else:
                for out in cast(Iterator[Any], stream):
                    chunk_count += 1
                    accumulated, stream_chunk = self._stream_out_step(accumulated, out)
                    if stream_chunk is not None:
                        yield stream_chunk

            final_resp = accumulated.to_llm_response() if accumulated is not None else self._wrap_output(None)
            log_llm_done("LLM stream finished", final_resp, rid=rid)
            self._fill_llm_extra(h, final_resp)
            h["chunk_count"] = chunk_count
            yield LLMStreamChunk(final_response=final_resp, raw=final_resp.raw, done=True)

    def bind_tools(self, tools: Any, **kwargs: Any) -> LangChainChatModelAdapter:
        """绑定工具，委托底层 bind_tools。"""
        fn = getattr(self._raw, "bind_tools", None)
        if callable(fn):
            new_raw = fn(tools, **kwargs)
            return LangChainChatModelAdapter(new_raw, self._config)
        return self

    def _stream_out_step(
        self, accumulated: _StreamAccum | None, out: Any
    ) -> tuple[_StreamAccum, LLMStreamChunk | None]:
        chunk_resp = out if isinstance(out, LLMResponse) else self._wrap_output(out)
        if accumulated is None:
            accumulated = _StreamAccum()
        accumulated.append_chunk(chunk_resp)
        if not chunk_resp.content and not chunk_resp.reasoning_content:
            return accumulated, None
        return accumulated, LLMStreamChunk(
            content=chunk_resp.content,
            reasoning_content=chunk_resp.reasoning_content or "",
            raw=out,
        )

    def _invoke_inner(
        self,
        chat_input: Any,
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        rid = uuid4().hex[:8]
        logger.debug("Start to invoke llm rid={}", rid)
        norm_input = self._normalize_input_for_langchain(chat_input)
        out = self._raw.invoke(norm_input, **kwargs)
        resp = self._wrap_output(out)
        log_llm_done("LLM invoke finished", resp, rid=rid)
        return resp

    async def _ainvoke_normalized(
        self,
        norm_input: Any,
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        rid = uuid4().hex[:8]
        logger.debug("Start to invoke llm rid={}", rid)
        raw_fn = getattr(self._raw, "ainvoke", None)
        if callable(raw_fn):
            out = raw_fn(norm_input, **kwargs)
            if inspect.isawaitable(out):
                out = await out  # type: ignore[misc]
        else:
            out = self._raw.invoke(norm_input, **kwargs)

        resp = self._wrap_output(out)
        log_llm_done("LLM invoke finished", resp, rid=rid)
        return resp

    def _normalize_input_for_langchain(self, chat_input: Any) -> Any:
        """
        统一输入格式：
        - raw 模型来自 langchain 时：OpenAI dict messages 转为 langchain messages。
        - raw 模型非 langchain 时：langchain messages 转为 OpenAI dict messages。
        """
        chat_input = coerce_chat_input_to_messages(chat_input)
        raw_mod = getattr(self._raw, "__module__", "") or ""
        if not raw_mod.startswith("langchain"):
            return type(self)._normalize_lc_messages_to_openai_dicts(chat_input)
        return self._normalize_openai_dicts_to_lc_messages(chat_input)
