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


def normalize_usage_metadata(usage: Any) -> dict[str, Any]:
    """补齐 langchain AIMessage 所需的 usage_metadata 必填字段。"""
    usage_dict = cast(dict[str, Any], usage or {}) if isinstance(usage, dict) else {}
    return {
        "input_tokens": int(usage_dict.get("input_tokens") or 0),
        "output_tokens": int(usage_dict.get("output_tokens") or 0),
        "total_tokens": int(usage_dict.get("total_tokens") or 0),
        **usage_dict,
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
        bound_tools: list[Any] | None = None,
    ):
        """
        初始化适配器，持有原始模型实例。

        Args:
            raw_model: 底层 LLM 实例（来自 langchain 或其他 SDK）。
            config: LLMConfig 实例，提供 tool_call_mode 等配置。可选，兼容旧调用。
            bound_tools: structured 模式下绑定的工具列表
            （仅非 :class:`~dataagent.core.managers.llm_manager.llm_client.LLMClient` 路径）。可选。
        """
        self._raw = raw_model
        self._config = config
        self._bound_tools: list[Any] | None = bound_tools

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
    def _tool_call_mode(self) -> str:
        """获取当前 tool_call_mode，兼容无 config 的情况。"""
        return getattr(self._config, "tool_call_mode", "native")

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

    @classmethod
    def _merge_llm_response_pair(
        cls,
        accumulated_resp: LLMResponse,
        chunk_resp: LLMResponse,
    ) -> LLMResponse:
        """将两个已规范化的 LLMResponse 做流式字段合并（content / reasoning / tool_calls）。"""
        usage = _merge_usage_metadata(chunk_resp.usage_metadata, accumulated_resp.usage_metadata)
        reasoning_merged = (accumulated_resp.reasoning_content or "") + (chunk_resp.reasoning_content or "")
        # 流式末尾块常携带完整 tool_calls；与 content 不同，不应做列表拼接（会重复或顺序错乱）。
        merged_tool_calls = (chunk_resp.tool_calls if chunk_resp.tool_calls else accumulated_resp.tool_calls) or []
        merged_invalid = (
            chunk_resp.invalid_tool_calls if chunk_resp.invalid_tool_calls else accumulated_resp.invalid_tool_calls
        ) or []
        return LLMResponse(
            content=accumulated_resp.content + chunk_resp.content,
            usage_metadata=usage,
            reasoning_content=reasoning_merged,
            tool_calls=list(merged_tool_calls),
            invalid_tool_calls=list(merged_invalid),
            raw=chunk_resp.raw,
        )

    @classmethod
    def _merge_stream_output(cls, accumulated: Any, chunk: Any) -> Any:
        """将两侧先规范为 LLMResponse，再合并；避免 LLMResponse 与 LLMClientMessage 等混用导致 + 失败。"""
        if accumulated is None:
            return chunk
        accumulated_resp = accumulated if isinstance(accumulated, LLMResponse) else cls._wrap_output(accumulated)
        chunk_resp = chunk if isinstance(chunk, LLMResponse) else cls._wrap_output(chunk)
        return cls._merge_llm_response_pair(accumulated_resp, chunk_resp)

    def invoke(self, chat_input: Any, **kwargs: Any) -> LLMResponse:
        """规范化输入并同步调用模型。"""
        mode = self._tool_call_mode
        if not self._is_llm_client():
            chat_input, kwargs = self._prepare_tool_call_input(chat_input, kwargs, mode)
        with get_current_collector().measure("llm", self._llm_perf_name, call_mode="invoke") as h:
            resp = self._invoke_inner(chat_input, kwargs, mode)
            self._fill_llm_extra(h, resp)
            return resp

    async def ainvoke(self, chat_input: Any, **kwargs: Any) -> LLMResponse:
        """规范化输入并异步调用模型。"""
        mode = self._tool_call_mode
        if not self._is_llm_client():
            chat_input, kwargs = self._prepare_tool_call_input(chat_input, kwargs, mode)
        with get_current_collector().measure("llm", self._llm_perf_name, call_mode="ainvoke") as h:
            resp = await self._ainvoke_inner(chat_input, kwargs, mode)
            self._fill_llm_extra(h, resp)
            return resp

    async def astream(self, chat_input: Any, **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        """规范化输入并优先使用底层流式能力；不支持时回退到一次性调用。"""
        mode = self._tool_call_mode
        if not self._is_llm_client():
            chat_input, kwargs = self._prepare_tool_call_input(chat_input, kwargs, mode)

        norm_input = self._normalize_input_for_langchain(chat_input)
        raw_fn = getattr(self._raw, "astream", None)

        with get_current_collector().measure("llm", self._llm_perf_name, call_mode="astream") as h:
            if not callable(raw_fn):
                final_resp = await self._ainvoke_without_before_hooks(chat_input, kwargs, mode)
                self._fill_llm_extra(h, final_resp)
                yield LLMStreamChunk(final_response=final_resp, done=True)
                return

            rid = uuid4().hex[:8]
            logger.debug("Start to stream llm rid={}", rid)
            stream = raw_fn(norm_input, **kwargs)
            logger.debug("LLM stream created rid={} stream_type={}", rid, type(stream).__name__)

            accumulated_raw: Any = None
            chunk_count = 0
            if hasattr(stream, "__aiter__"):
                async for out in cast(AsyncIterator[Any], stream):
                    chunk_count += 1
                    accumulated_raw = self._merge_stream_output(accumulated_raw, out)
                    text = self._content_to_text(getattr(out, "content", out))
                    reasoning = self._extract_reasoning_content(out)
                    if text or reasoning:
                        yield LLMStreamChunk(content=text, reasoning_content=reasoning, raw=out)
            else:
                for out in cast(Iterator[Any], stream):
                    chunk_count += 1
                    accumulated_raw = self._merge_stream_output(accumulated_raw, out)
                    text = self._content_to_text(getattr(out, "content", out))
                    reasoning = self._extract_reasoning_content(out)
                    if text or reasoning:
                        yield LLMStreamChunk(content=text, reasoning_content=reasoning, raw=out)

            final_resp = self._finalize_response(accumulated_raw, chat_input, kwargs, mode)
            log_llm_done("LLM stream finished", final_resp, rid=rid)
            self._fill_llm_extra(h, final_resp)
            h["chunk_count"] = chunk_count
            yield LLMStreamChunk(final_response=final_resp, raw=accumulated_raw, done=True)

    def bind_tools(self, tools: Any, **kwargs: Any) -> LangChainChatModelAdapter:
        """
        绑定工具。根据 tool_call_mode 分支：
        - native：直接调用底层 bind_tools。
        - structured：存储工具列表，在 invoke 时注入。
        """
        if self._is_llm_client():
            fn = getattr(self._raw, "bind_tools", None)
            if callable(fn):
                new_raw = fn(tools, **kwargs)
                return LangChainChatModelAdapter(new_raw, self._config)
            return self

        mode = self._tool_call_mode

        if mode == "native":
            fn = getattr(self._raw, "bind_tools", None)
            if callable(fn):
                new_raw = fn(tools, **kwargs)
                return LangChainChatModelAdapter(new_raw, self._config)
            return self
        bound = tools if isinstance(tools, list) else [tools]
        return LangChainChatModelAdapter(self._raw, self._config, bound_tools=bound)

    def normalize_lc_messages_to_openai_dicts(self, chat_input: Any) -> Any:
        """公开入口：将 LangChain messages 转为 OpenAI 风格 dict（供 LLMClient 等外部调用）。"""
        return self._normalize_lc_messages_to_openai_dicts(chat_input)

    def _is_llm_client(self) -> bool:
        try:
            from dataagent.core.managers.llm_manager.llm_client import LLMClient

            return isinstance(self._raw, LLMClient)
        except ImportError:
            return False

    def _invoke_inner(
        self,
        chat_input: Any,
        kwargs: dict[str, Any],
        mode: str,
    ) -> LLMResponse:
        rid = uuid4().hex[:8]
        logger.debug("Start to invoke llm rid={}", rid)
        norm_input = self._normalize_input_for_langchain(chat_input)
        out = self._raw.invoke(norm_input, **kwargs)
        resp = self._wrap_output(out)
        if not self._is_llm_client():
            resp = self._parse_tool_calls_if_needed(resp, mode)
        log_llm_done("LLM invoke finished", resp, rid=rid)
        return resp

    async def _ainvoke_inner(
        self,
        chat_input: Any,
        kwargs: dict[str, Any],
        mode: str,
    ) -> LLMResponse:
        rid = uuid4().hex[:8]
        logger.debug("Start to invoke llm rid={}", rid)
        norm_input = self._normalize_input_for_langchain(chat_input)
        raw_fn = getattr(self._raw, "ainvoke", None)
        if callable(raw_fn):
            out = raw_fn(norm_input, **kwargs)
            if inspect.isawaitable(out):
                out = await out  # type: ignore[misc]
        else:
            out = self._raw.invoke(norm_input, **kwargs)

        resp = self._wrap_output(out)
        if not self._is_llm_client():
            resp = self._parse_tool_calls_if_needed(resp, mode)
        log_llm_done("LLM invoke finished", resp, rid=rid)
        return resp

    async def _ainvoke_without_before_hooks(
        self,
        chat_input: Any,
        kwargs: dict[str, Any],
        mode: str,
    ) -> LLMResponse:
        """在 before hooks 已执行的前提下完成一次性调用。"""
        rid = uuid4().hex[:8]
        logger.debug("Start to invoke llm rid={}", rid)
        norm_input = self._normalize_input_for_langchain(chat_input)
        raw_fn = getattr(self._raw, "ainvoke", None)
        if callable(raw_fn):
            out = raw_fn(norm_input, **kwargs)
            if inspect.isawaitable(out):
                out = await out  # type: ignore[misc]
        else:
            out = self._raw.invoke(norm_input, **kwargs)
        resp = self._finalize_response(out, chat_input, kwargs, mode)
        log_llm_done("LLM invoke finished", resp, rid=rid)
        return resp

    def _finalize_response(
        self,
        out: Any,
        chat_input: Any,
        kwargs: dict[str, Any],
        mode: str,
    ) -> LLMResponse:
        """将底层输出收敛为最终 LLMResponse。"""
        resp = out if isinstance(out, LLMResponse) else self._wrap_output(out)
        if not self._is_llm_client():
            resp = self._parse_tool_calls_if_needed(resp, mode)
        return resp

    # ===== tool_call_mode 处理辅助方法 =====

    def _prepare_tool_call_input(
        self, chat_input: Any, kwargs: dict[str, Any], mode: str
    ) -> tuple[Any, dict[str, Any]]:
        """
        准备阶段：根据 tool_call_mode 注入 prompt 和设置参数。

        仅支持 structured 模式的 tool-call：注入 JSON schema + 设置 response_format。

        Args:
            chat_input: 原始输入消息
            kwargs: 调用参数
            mode: tool_call_mode (native 或 structured)

        Returns:
            (修改后的 chat_input, 修改后的 kwargs)
        """
        # structured 模式：注入 JSON schema + 设置 response_format
        if self._bound_tools and mode == "structured":
            from dataagent.core.managers.llm_manager.tool_prompt_builder import (
                build_tool_calling_prompt,
                convert_tools_to_openai_schema,
                prepend_to_system_message,
            )

            tools_schema = convert_tools_to_openai_schema(self._bound_tools)
            injection = build_tool_calling_prompt(tools_schema)
            chat_input = prepend_to_system_message(chat_input, injection)
            kwargs.setdefault("response_format", {"type": "json_object"})

        return chat_input, kwargs

    def _parse_tool_calls_if_needed(self, resp: LLMResponse, mode: str) -> LLMResponse:
        """
        解析阶段：如果需要，从响应文本中解析 tool_calls。

        仅支持 structured 模式：从响应 JSON 中解析 tool_calls 并清理 content。
        如果底层模型已经通过原生能力返回了 tool_calls，则跳过 structured 解析，
        避免空 content 覆盖已有的 tool_calls。

        Args:
            resp: 原始 LLMResponse
            mode: tool_call_mode (native 或 structured)

        Returns:
            处理后的 LLMResponse（可能包含解析出的 tool_calls）
        """
        # 如果底层模型已返回原生 tool_calls，直接使用，不再走 structured 解析
        if resp.tool_calls:
            return resp

        # structured 模式：从 content 文本中解析 tool_calls 并清理 content
        if self._bound_tools and mode == "structured":
            from dataagent.core.managers.llm_manager.tool_call_parser import parse_tool_calls

            tool_calls, invalid, cleaned_content = parse_tool_calls(resp.content)
            return LLMResponse(
                content=cleaned_content,
                usage_metadata=resp.usage_metadata,
                reasoning_content=resp.reasoning_content,
                tool_calls=tool_calls,
                invalid_tool_calls=invalid,
                raw=resp.raw,
            )

        # native 模式或无工具调用：直接返回
        return resp

    # ===== 输入规范化 =====

    def _normalize_lc_messages_to_openai_dicts(self, chat_input: Any) -> Any:
        """
        raw 模型非 langchain 时，尽量将 langchain messages 转为 OpenAI dict messages。
        非 list / 空 list 时直接返回原输入。原先「首元素为 dict 则整表原样返回」会跳过
        assistant+tool_calls 的 reasoning_content 补全（Thinking 模式 API 必填），已移除。
        """
        if not isinstance(chat_input, list) or not chat_input:
            return chat_input

        def _infer_role(*, msg_type: Any, cls_name: str) -> str:
            if msg_type in {"human", "user"} or cls_name == "HumanMessage":
                return "user"
            if msg_type in {"ai", "assistant"} or cls_name == "AIMessage":
                return "assistant"
            if msg_type == "system" or cls_name == "SystemMessage":
                return "system"
            if msg_type == "tool" or cls_name == "ToolMessage":
                return "tool"
            # 未识别：按 user 兜底
            return "user"

        def _maybe_add_tool_fields(m: dict[str, Any], msg: Any) -> None:
            tool_call_id = getattr(msg, "tool_call_id", None)
            name = getattr(msg, "name", None)
            if tool_call_id is not None:
                m["tool_call_id"] = tool_call_id
            if name is not None:
                m["name"] = name

        def _maybe_add_assistant_tool_calls(m: dict[str, Any], msg: Any) -> None:
            tc = getattr(msg, "tool_calls", None)
            if not tc:
                return
            if isinstance(tc, list):
                converted_tc: list[dict[str, Any]] = []
                for one in tc:
                    conv = self._to_openai_tool_call(one)
                    if conv is not None:
                        converted_tc.append(conv)
                if converted_tc:
                    m["tool_calls"] = converted_tc
                return
            if isinstance(tc, dict):
                conv = self._to_openai_tool_call(tc)
                if conv is not None:
                    m["tool_calls"] = [conv]

        def _ensure_reasoning_for_assistant_tool_calls(m: dict[str, Any], lc_msg: Any | None) -> None:
            """DashScope 等 Thinking 模式：带 tool_calls 的 assistant 必须带 reasoning_content 字段。"""
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                return
            if lc_msg is not None:
                rc = self._extract_reasoning_content(lc_msg)
                m["reasoning_content"] = rc if rc else ""
                return
            if m.get("reasoning_content") is not None:
                return
            m["reasoning_content"] = str(m.get("reasoning") or "")

        converted_msgs: list[dict[str, Any]] = []
        for msg in chat_input:
            # 已经是 dict 的直接透传
            if isinstance(msg, dict):
                m2 = dict(msg)
                _ensure_reasoning_for_assistant_tool_calls(m2, None)
                converted_msgs.append(m2)
                continue

            cls_name = getattr(getattr(msg, "__class__", None), "__name__", "") or ""
            msg_type = getattr(msg, "type", None)  # langchain 常见：human/ai/system/tool
            content = getattr(msg, "content", "")

            role = _infer_role(msg_type=msg_type, cls_name=cls_name)
            m: dict[str, Any] = {"role": role, "content": str(content or "")}

            # tool message 补充字段
            if role == "tool":
                _maybe_add_tool_fields(m, msg)

            # assistant message 若包含 tool_calls，尽量透传（并转换为 OpenAI 兼容格式）
            if role == "assistant":
                _maybe_add_assistant_tool_calls(m, msg)
                _ensure_reasoning_for_assistant_tool_calls(m, msg)

            converted_msgs.append(m)

        return converted_msgs if converted_msgs else chat_input

    def _normalize_input_for_langchain(self, chat_input: Any) -> Any:
        """
        统一输入格式：
        - raw 模型来自 langchain 时：若业务侧传入 OpenAI 风格 dict messages（[{role,content,...}]），转换为 langchain messages。
        - raw 模型非 langchain（如 openjiuwen/self wrapper）时：若业务侧传入 langchain messages（HumanMessage/AIMessage/...），
          转换为 OpenAI 风格 dict messages（[{role,content,...}]），避免底层模型访问 msg.role 时报错。
        - 仅在需要时懒加载 langchain_core.messages，减少非 langgraph 场景的依赖风险。
        """
        raw_mod = getattr(self._raw, "__module__", "") or ""
        if not raw_mod.startswith("langchain"):
            return self._normalize_lc_messages_to_openai_dicts(chat_input)
        return self._normalize_openai_dicts_to_lc_messages(chat_input)
