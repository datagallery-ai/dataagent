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
"""基于 litellm 的 chat client — DataAgent 主架构唯一 LLM 底层实现。

- **Flex**：:mod:`dataagent.core.flex.flex_runtime_from_config` 解析 ``env.llm_configs``；
  :func:`llm_adapter_from_env_cfg` 组装 ``LangChainChatModelAdapter``（内部建 ``LLMClient``）。
- **LLMManager**：:meth:`~dataagent.core.managers.llm_manager.llm_manager.LLMManager.create_llm`
  经 :meth:`LLMClient.from_llm_config` 构造同一 ``LLMClient``。

重试：litellm ``num_retries=0`` + ``retry_policy`` 白名单（429/Timeout）；
DataAgent 对 5xx/``APIConnectionError``/Timeout（含 ``httpx.TimeoutException``）重试，
``astream`` 读流阶段与建流共用同一 ``num_retries`` 计数。失败统一映射为 :class:`LLMCallError`。
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

import httpx
from litellm.exceptions import (
    APIConnectionError,
    APIResponseValidationError,
    AuthenticationError,
    BadGatewayError,
    BadRequestError,
    ContentPolicyViolationError,
    ContextWindowExceededError,
    InternalServerError,
    InvalidRequestError,
    MidStreamFallbackError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from loguru import logger

from dataagent.core.managers.llm_manager.llm_config import LLMConfig
from dataagent.utils.constants import DEFAULT_LLM_MAX_RETRIES, DEFAULT_LLM_RETRY_POLICY


class LLMErrorCategory(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    CONTEXT_WINDOW = "context_window"
    CONTENT_POLICY = "content_policy"
    BAD_REQUEST = "bad_request"
    SERVER_ERROR = "server_error"
    RESPONSE_INVALID = "response_invalid"
    STREAM_INTERRUPTED = "stream_interrupted"
    UNKNOWN = "unknown"


class LLMCallError(Exception):
    """单一 LLM 失败出口；可直接 raise/except，无需再剥 litellm 类型。"""

    def __init__(
        self,
        category: LLMErrorCategory,
        message: str,
        *,
        app_recoverable: bool = False,
        model: str | None = None,
        request_url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.app_recoverable = app_recoverable
        self.model = model
        self.request_url = request_url
        self.status_code = status_code

    def __str__(self) -> str:
        label = f"[{self.status_code} {self.category}]" if self.status_code is not None else f"[{self.category}]"
        meta: list[str] = []
        if self.model:
            meta.append(f"model={self.model}")
        if self.request_url:
            meta.append(f"url={self.request_url}")
        suffix = f" ({' '.join(meta)})" if meta else ""
        return f"{label} {self.message}{suffix}"


def map_litellm_exception(
    exc: BaseException,
    *,
    model: str | None = None,
    api_base: str | None = None,
) -> LLMCallError:
    """将 litellm（或其它）异常映射为 ``LLMCallError``；已是 ``LLMCallError`` 则原样返回。"""
    if isinstance(exc, LLMCallError):
        return exc

    message = str(getattr(exc, "message", None) or exc)
    resolved_model = model or getattr(exc, "model", None)
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = None
    request_url = f"{api_base.rstrip('/')}/chat/completions" if api_base else None

    category: LLMErrorCategory
    app_recoverable = False

    if isinstance(exc, RateLimitError):
        category = LLMErrorCategory.RATE_LIMIT
    elif isinstance(exc, (Timeout, httpx.TimeoutException)):
        category = LLMErrorCategory.TIMEOUT
    elif isinstance(exc, APIConnectionError):
        category = LLMErrorCategory.CONNECTION
    elif isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        category = LLMErrorCategory.AUTH
    elif isinstance(exc, NotFoundError):
        category = LLMErrorCategory.NOT_FOUND
    elif isinstance(exc, ContextWindowExceededError):
        category = LLMErrorCategory.CONTEXT_WINDOW
        app_recoverable = True
    elif isinstance(exc, ContentPolicyViolationError):
        category = LLMErrorCategory.CONTENT_POLICY
    elif isinstance(exc, (BadRequestError, InvalidRequestError)):
        category = LLMErrorCategory.BAD_REQUEST
    elif isinstance(exc, (InternalServerError, ServiceUnavailableError, BadGatewayError)):
        category = LLMErrorCategory.SERVER_ERROR
    elif isinstance(exc, APIResponseValidationError):
        category = LLMErrorCategory.RESPONSE_INVALID
    elif isinstance(exc, MidStreamFallbackError):
        category = LLMErrorCategory.STREAM_INTERRUPTED
    else:
        category = LLMErrorCategory.UNKNOWN

    return LLMCallError(
        category,
        message,
        app_recoverable=app_recoverable,
        model=str(resolved_model) if resolved_model else None,
        request_url=request_url,
        status_code=status_code,
    )


def _normalize_litellm_retry_kwargs(call_kw: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """策略 D：litellm 全局不重试，429/Timeout 走 policy；返回 DataAgent 5xx 薄层次数。"""
    call_kw.pop("max_retries", None)
    max_attempts = int(call_kw.pop("num_retries", DEFAULT_LLM_MAX_RETRIES))
    call_kw.pop("retry_policy", None)
    call_kw["num_retries"] = 0
    policy = dict(DEFAULT_LLM_RETRY_POLICY)
    policy["RateLimitErrorRetries"] = max_attempts
    policy["TimeoutErrorRetries"] = max_attempts
    call_kw["retry_policy"] = policy
    return call_kw, max_attempts


_DATAAGENT_TRANSIENT_EXCEPTIONS = (
    InternalServerError,
    ServiceUnavailableError,
    BadGatewayError,
    APIConnectionError,
)


def _should_dataagent_retry(exc: BaseException, *, attempt: int, max_attempts: int) -> bool:
    """策略 D：DataAgent 薄层是否在抛出 ``LLMCallError`` 前继续重试。

    与 ``_normalize_litellm_retry_kwargs`` 返回的 ``max_attempts`` 共用计数；
    覆盖 litellm ``TimeoutErrorRetries`` 未识别的 ``httpx.TimeoutException``。
    """
    if attempt >= max_attempts:
        return False
    if isinstance(exc, _DATAAGENT_TRANSIENT_EXCEPTIONS):
        return True
    if isinstance(exc, Timeout):
        return True
    return isinstance(exc, httpx.TimeoutException)


def _call_with_transient_retry(
    operation,
    *,
    max_attempts: int,
    model: str,
    api_base: str,
) -> Any:
    for attempt in range(max_attempts + 1):
        try:
            return operation()
        except Exception as e:
            if _should_dataagent_retry(e, attempt=attempt, max_attempts=max_attempts):
                continue
            raise map_litellm_exception(e, model=model, api_base=api_base) from e
    raise RuntimeError("Unexpected error in transient retry loop")  # pragma: no cover


async def _acall_with_transient_retry(
    operation,
    *,
    max_attempts: int,
    model: str,
    api_base: str,
) -> Any:
    for attempt in range(max_attempts + 1):
        try:
            return await operation()
        except Exception as e:
            if _should_dataagent_retry(e, attempt=attempt, max_attempts=max_attempts):
                continue
            raise map_litellm_exception(e, model=model, api_base=api_base) from e
    raise RuntimeError("Unexpected error in transient retry loop")  # pragma: no cover


def _llm_config_for_adapter(cfg: dict[str, Any], logical_name: str) -> LLMConfig:
    """供 ``LangChainChatModelAdapter`` 使用；适配器读取 logical name 等元数据。"""
    return LLMConfig(
        name=logical_name,
        provider="openai",
        model_type="chat",
        section=logical_name,
        params={},
    )


@dataclass
class LLMClientMessage:
    """单次模型输出，字段对齐 adapter :meth:`LangChainChatModelAdapter._wrap_output`。"""

    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    invalid_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_blocks: list[dict[str, Any]] | None = None
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


@lru_cache(maxsize=1)
def _litellm():
    import litellm

    litellm.ssl_verify = False
    litellm.modify_params = True
    litellm.suppress_debug_info = True

    return litellm


class LLMClient:
    """litellm 直连；工具调用走 OpenAI native tools API。"""

    def __init__(
        self,
        *,
        model: str,
        api_base: str,
        api_key: str,
        tools: list[Any] | None = None,
        **litellm_kwargs: Any,
    ) -> None:
        """初始化 LLMClient 实例"""
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._tools: list[Any] = list(tools) if tools else []
        self._extra = litellm_kwargs

    @staticmethod
    def _is_likely_incomplete_json(raw_args: Any, error: json.JSONDecodeError) -> bool:
        """判断流式参数解析失败是否更像截断/半包，而非完整但非法 JSON。"""
        if not isinstance(raw_args, str):
            return False
        stripped = raw_args.strip()
        if not stripped:
            return False
        if stripped in {"{", "["}:
            return True
        opens = stripped.count("{") + stripped.count("[")
        closes = stripped.count("}") + stripped.count("]")
        if opens > closes:
            return True
        if stripped.endswith((":", ",", '"', "\\")):
            return True
        return error.pos >= max(len(stripped) - 2, 0)

    @staticmethod
    def _finalize_stream_tool_calls_for_lc(
        by_index: dict[int, dict[str, str]],
        finish_reason: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将累积的 OpenAI 流式分片转为 LangChain AIMessage 可用的 tool_calls / invalid_tool_calls。"""
        out: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        finish_reason = finish_reason or ""
        for idx in sorted(by_index.keys()):
            part = by_index[idx]
            name = (part.get("name") or "").strip()
            if not name:
                logger.warning(
                    "stream.tool_call.missing_name index={} has_id={} args_len={}",
                    idx,
                    bool(part.get("id")),
                    len(str(part.get("arguments") or "")),
                )
                invalid.append(
                    {
                        "id": str(part.get("id") or "").strip() or f"call_stream_{idx}",
                        "name": "unknown",
                        "args": part.get("arguments") or "",
                        "error": "Streamed tool call is missing a function name",
                    }
                )
                continue
            raw_args = part.get("arguments") or "{}"
            tid = str(part.get("id") or "").strip() or f"call_stream_{idx}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError as e:
                truncated = finish_reason == "length"
                filtered = finish_reason == "content_filter"
                incomplete = truncated or LLMClient._is_likely_incomplete_json(raw_args, e)
                log_fn = logger.debug if incomplete else logger.warning
                log_fn(
                    "stream.tool_call.args_json_decode_failed index={} error_type={}"
                    " args_len={} has_name={} has_id={} suspected_incomplete={} finish_reason={}",
                    idx,
                    type(e).__name__,
                    len(raw_args) if isinstance(raw_args, str) else 0,
                    bool(name),
                    bool(part.get("id")),
                    incomplete,
                    finish_reason,
                )
                invalid.append(
                    {
                        "id": tid,
                        "name": name,
                        "args": raw_args,
                        "error": (
                            "Streamed tool arguments were truncated by the model output limit"
                            if truncated
                            else (
                                "Streamed tool arguments were blocked or truncated by the content filter"
                                if filtered
                                else (
                                    "Incomplete streamed tool arguments JSON"
                                    if incomplete
                                    else "Invalid streamed tool arguments JSON"
                                )
                            )
                        ),
                    }
                )
                continue
            except Exception as e:
                logger.exception(
                    "stream.tool_call.args_parse_failed index={} error_type={} raw_args_type={} has_name={} has_id={}",
                    idx,
                    type(e).__name__,
                    type(raw_args).__name__,
                    bool(name),
                    bool(part.get("id")),
                )
                invalid.append(
                    {
                        "id": tid,
                        "name": name,
                        "args": raw_args,
                        "error": f"Failed to parse streamed tool arguments: {type(e).__name__}",
                    }
                )
                continue
            if not isinstance(args, dict):
                logger.warning(
                    "stream.tool_call.args_not_object index={} args_type={} has_name={} has_id={}",
                    idx,
                    type(args).__name__,
                    bool(name),
                    bool(part.get("id")),
                )
                invalid.append(
                    {
                        "id": tid,
                        "name": name,
                        "args": args,
                        "error": "Streamed tool arguments must decode to object",
                    }
                )
                continue
            out.append({"id": tid, "name": name, "args": args, "type": "tool_call"})
        return out, invalid

    @staticmethod
    def _tool_calls_to_dicts(tool_calls: list[Any]) -> list[dict[str, Any]]:
        """将 tool_calls 对象列表转为 dict 格式，便于下游处理"""
        result: list[dict[str, Any]] = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                result.append(tc)
                continue
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn is not None else getattr(tc, "name", None)
            args = getattr(fn, "arguments", None) if fn is not None else None
            tid = getattr(tc, "id", "") or ""
            if name:
                result.append(
                    {
                        "id": str(tid),
                        "type": "function",
                        "function": {"name": str(name), "arguments": str(args or "")},
                    }
                )
        return result

    @staticmethod
    def _tools_to_openai(tools: list[Any]) -> list[dict[str, Any]]:
        """将自定义工具列表转换为 OpenAI/LLM 能用的工具描述字典格式"""
        out: list[dict[str, Any]] = []
        for t in tools:
            if isinstance(t, dict) and (t.get("type") == "function" or "function" in t):
                out.append(t)
                continue
            name = getattr(t, "name", None) or getattr(t, "__name__", None)
            if not name:
                continue
            desc = str(getattr(t, "description", None) or "")
            params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            args_schema = getattr(t, "args_schema", None)
            if args_schema is not None:
                try:
                    params = args_schema.model_json_schema()  # type: ignore[attr-defined]
                except Exception:
                    with contextlib.suppress(Exception):
                        params = args_schema.schema()  # type: ignore[attr-defined]
            out.append(
                {
                    "type": "function",
                    "function": {"name": str(name), "description": desc, "parameters": params},
                }
            )
        return out

    @staticmethod
    def _lc_messages_to_dicts(messages: list[Any]) -> list[dict[str, Any]]:
        """将 LangChain Message 对象列表转换为 dict 格式消息"""
        from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter

        converted = LangChainChatModelAdapter.messages_to_openai_dicts(messages)
        return converted if isinstance(converted, list) else messages

    @staticmethod
    def _stream_chunk_to_dict(chunk: Any) -> dict[str, Any]:
        """将 litellm/OpenAI 流式 chunk 转为 dict，便于解析 choices/delta。

        优先 ``dict`` / ``Mapping``；否则调用 Pydantic 的 ``model_dump`` 或 v1 ``dict``。
        无法识别时打 warning 并返回空 dict（流式不中断），不在此处吞掉 ``model_dump`` 等真实异常。
        """
        if chunk is None:
            return {}
        if isinstance(chunk, dict):
            return chunk
        if isinstance(chunk, Mapping):
            return dict(chunk)
        model_dump = getattr(chunk, "model_dump", None)
        if callable(model_dump):
            out = model_dump()
            if isinstance(out, dict):
                return out
            if isinstance(out, Mapping):
                return dict(out)
            logger.warning(
                "stream.chunk.model_dump_non_dict type={}",
                type(out).__name__,
            )
            return {}
        dict_method = getattr(chunk, "dict", None)
        if callable(dict_method):
            out = dict_method()
            if isinstance(out, dict):
                return out
            if isinstance(out, Mapping):
                return dict(out)
            logger.warning(
                "stream.chunk.dict_non_dict type={}",
                type(out).__name__,
            )
            return {}
        logger.warning(
            "stream.chunk.convert_failed type={}",
            type(chunk).__name__,
        )
        return {}

    @classmethod
    def from_llm_config(cls, config: LLMConfig) -> LLMClient:
        """由 :class:`LLMConfig` 构造 ``LLMClient``（非 Flex 路径的工厂入口）。

        约定：

        * ``model`` 取自 ``config.client_params()["model"]``，必填。
        * ``api_base`` / ``api_key`` 优先取自 ``client_params()`` 中的 ``base_url`` /
          ``api_key``；未配置时再读环境变量 ``{PROVIDER}_BASE_URL`` /
          ``{PROVIDER}_API_KEY``（``PROVIDER`` 为 ``config.provider`` 的大写形式）。
        * ``client_params()`` 中除上述显式参数外，全部作为 litellm 透传 kwargs；
          ``custom_llm_provider`` 默认置为 ``"openai"``（沿用旧 ``OpenAIProvider`` 语义）。

        与 :meth:`from_env_cfg` 的区别仅在于配置形态：
        前者接 ``LLMConfig``（YAML ``MODEL.<name>`` 嵌套 ``params``），后者接 Flex
        :data:`env.llm_configs` 的扁平 dict。两者构造目标都是同一个 ``LLMClient``。
        """
        params = dict(config.client_params() or {})
        model = params.pop("model", None)
        if not model:
            raise ValueError(f"Missing model for '{config.name}'.")

        provider_env = (config.provider or "").upper()
        if not provider_env:
            raise ValueError(f"Missing provider for '{config.name}'.")

        param_base_url = params.pop("base_url", None) or params.pop("api_base", None)
        if param_base_url and str(param_base_url).strip():
            api_base = str(param_base_url).strip()
        else:
            api_base = os.getenv(f"{provider_env}_BASE_URL")
        if not api_base:
            raise ValueError(
                f"Missing URL for '{config.name}'. "
                f"Set MODEL.{config.name}.params.base_url or {provider_env}_BASE_URL in .env."
            )

        param_api_key = params.pop("api_key", None)
        if param_api_key and str(param_api_key).strip():
            api_key = str(param_api_key).strip()
        else:
            api_key = os.getenv(f"{provider_env}_API_KEY")
        if not api_key:
            raise ValueError(
                f"Missing API key for '{config.name}'. "
                f"Set MODEL.{config.name}.params.api_key or {provider_env}_API_KEY in .env."
            )

        params.setdefault("custom_llm_provider", "openai")

        return cls(
            model=str(model),
            api_base=str(api_base),
            api_key=str(api_key),
            **params,
        )

    @classmethod
    def from_env_cfg(cls, cfg: Mapping[str, Any]) -> LLMClient:
        """由 ``env.llm_configs`` 扁平项构造 litellm 客户端。"""
        model = cfg.get("model")
        if not model:
            raise ValueError("LLM config must include top-level 'model'")
        if not cfg.get("api_base") or not cfg.get("api_key"):
            raise ValueError("LLM config must include api_base and api_key")
        extra = {k: v for k, v in cfg.items() if k not in ("model", "api_base", "api_key")}
        extra.setdefault("custom_llm_provider", "openai")
        return cls(
            model=str(model),
            api_base=str(cfg["api_base"]),
            api_key=str(cfg["api_key"]),
            **extra,
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> LLMClient:
        """绑定工具信息，并返回包含该工具的新 LLMClient 实例"""
        bound = tools if isinstance(tools, list) else [tools]
        return LLMClient(
            model=self._model,
            api_base=self._api_base,
            api_key=self._api_key,
            tools=bound,
            **{**self._extra, **kwargs},
        )

    def invoke(self, messages: list[Any], **kwargs: Any) -> LLMClientMessage:
        """以同步方式调用模型生成回复"""
        litellm = _litellm()
        msgs, call_kw = self._prepare_messages_and_kwargs(messages, kwargs)
        call_kw, max_attempts = _normalize_litellm_retry_kwargs(call_kw)
        logger.debug(
            "invoke.request model={} params={}",
            self._model,
            call_kw,
        )

        def _call_litellm():
            resp = litellm.completion(
                model=self._model,
                messages=msgs,
                api_base=self._api_base,
                api_key=self._api_key,
                **call_kw,
            )
            return self._wrap_response(resp)

        return _call_with_transient_retry(
            _call_litellm,
            max_attempts=max_attempts,
            model=self._model,
            api_base=self._api_base,
        )

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> LLMClientMessage:
        """以异步方式调用模型生成回复"""
        litellm = _litellm()
        msgs, call_kw = self._prepare_messages_and_kwargs(messages, kwargs)
        call_kw, max_attempts = _normalize_litellm_retry_kwargs(call_kw)
        logger.debug(
            "ainvoke.request model={} params={}",
            self._model,
            call_kw,
        )

        async def _call_litellm():
            resp = await litellm.acompletion(
                model=self._model,
                messages=msgs,
                api_base=self._api_base,
                api_key=self._api_key,
                **call_kw,
            )
            return self._wrap_response(resp)

        return await _acall_with_transient_retry(
            _call_litellm,
            max_attempts=max_attempts,
            model=self._model,
            api_base=self._api_base,
        )

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[LLMClientMessage]:
        """以异步方式流式调用模型，逐步产生消息块"""
        litellm = _litellm()
        msgs, call_kw = self._prepare_messages_and_kwargs(messages, kwargs)
        call_kw, max_attempts = _normalize_litellm_retry_kwargs(call_kw)
        call_kw = {**call_kw, "stream": True}
        stream_options = dict(call_kw.get("stream_options") or {})
        stream_options.setdefault("include_usage", True)
        call_kw["stream_options"] = stream_options

        async def _get_stream():
            return await litellm.acompletion(
                model=self._model,
                messages=msgs,
                api_base=self._api_base,
                api_key=self._api_key,
                **call_kw,
            )

        # OpenAI 流式 tool_calls 按 index 分片出现在 delta 中；建流 + 读流共用薄层重试。
        for attempt in range(max_attempts + 1):
            by_index: dict[int, dict[str, str]] = {}
            stream_finish_reason = ""

            try:
                stream = await _get_stream()
                if hasattr(stream, "__aiter__"):
                    async for chunk in stream:
                        msg, finish_reason = self._stream_chunk_message(chunk, by_index)
                        if finish_reason:
                            stream_finish_reason = finish_reason
                        yield msg
                else:
                    for chunk in stream:
                        msg, finish_reason = self._stream_chunk_message(chunk, by_index)
                        if finish_reason:
                            stream_finish_reason = finish_reason
                        yield msg
            except Exception as e:
                if _should_dataagent_retry(e, attempt=attempt, max_attempts=max_attempts):
                    logger.warning(
                        "astream.dataagent_retry attempt={}/{} model={} error_type={}",
                        attempt + 1,
                        max_attempts,
                        self._model,
                        type(e).__name__,
                    )
                    continue
                raise map_litellm_exception(e, model=self._model, api_base=self._api_base) from e

            final_tc, final_invalid = self._finalize_stream_tool_calls_for_lc(by_index, stream_finish_reason)
            if final_tc or final_invalid:
                yield LLMClientMessage(
                    content="",
                    reasoning_content="",
                    tool_calls=final_tc,
                    invalid_tool_calls=final_invalid,
                    raw=None,
                )
            return

    def _prepare_messages_and_kwargs(
        self, messages: list[Any], kwargs: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """根据当前工具及参数构建最终消息和请求参数，供 litellm 使用（输入已由 adapter 规范化）。"""
        call_kw = {**self._extra, **kwargs}
        timeout = call_kw.pop("timeout", None)
        if timeout is not None:
            call_kw.setdefault("request_timeout", timeout)

        msgs: list[Any] = messages
        if self._tools:
            openai_tools = self._tools_to_openai(self._tools)
            if openai_tools:
                call_kw["tools"] = openai_tools

        if msgs and not isinstance(msgs[0], dict):
            msgs = self._lc_messages_to_dicts(msgs)

        return msgs, call_kw

    def _stream_chunk_message(
        self,
        chunk: Any,
        tool_buf: dict[int, dict[str, str]],
    ) -> tuple[LLMClientMessage, str | None]:
        """Build one streaming chunk message and optional finish_reason from tool-call deltas.

        Args:
            chunk: Raw LiteLLM stream chunk.
            tool_buf: Per-attempt accumulator for tool-call fragments keyed by index.

        Returns:
            ``(message, finish_reason)`` where ``finish_reason`` is set when the stream ends.
        """
        finish_reason = self._feed_stream_tool_call_deltas(chunk, tool_buf)
        wrapped = self._wrap_stream_chunk(chunk)
        return (
            LLMClientMessage(
                content=wrapped.content,
                reasoning_content=wrapped.reasoning_content,
                tool_calls=[],
                invalid_tool_calls=[],
                usage_metadata=wrapped.usage_metadata,
                raw=wrapped.raw,
            ),
            finish_reason or None,
        )

    def _feed_stream_tool_call_deltas(
        self,
        chunk: Any,
        by_index: dict[int, dict[str, str]],
    ) -> str:
        """合并流式 delta.tool_calls（含 index 分片）及末包 message.tool_calls。"""
        d = self._stream_chunk_to_dict(chunk)
        choices = d.get("choices") or []
        if not choices:
            return ""
        c0 = choices[0]
        if not isinstance(c0, dict):
            c0 = self._stream_chunk_to_dict(c0)
        finish_reason = c0.get("finish_reason")
        choice_idx = int(c0.get("index", 0) or 0)
        delta = c0.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}
        raw_delta_tool_calls = delta.get("tool_calls") or []
        if not isinstance(raw_delta_tool_calls, list):
            raw_delta_tool_calls = []
        if finish_reason:
            logger.debug("stream.finish_reason index={} reason={}", choice_idx, finish_reason)
        for tc in raw_delta_tool_calls:
            if not isinstance(tc, dict):
                logger.warning("stream.tool_call.delta_non_dict raw={}", tc)
                continue
            idx = int(tc.get("index", 0))
            if idx not in by_index:
                by_index[idx] = {"id": "", "name": "", "arguments": ""}
            tid = tc.get("id")
            if tid:
                by_index[idx]["id"] = str(tid)
            fn = tc.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    by_index[idx]["name"] = str(fn["name"])
                if fn.get("arguments"):
                    fragment = str(fn["arguments"])
                    by_index[idx]["arguments"] += fragment
        msg = c0.get("message")
        if isinstance(msg, dict):
            mtc = msg.get("tool_calls")
            if isinstance(mtc, list):
                for i, tc in enumerate(mtc):
                    if not isinstance(tc, dict):
                        logger.warning("stream.tool_call.message_non_dict raw={}", tc)
                        continue
                    idx = int(tc.get("index", i))
                    if idx not in by_index:
                        by_index[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.get("id"):
                        by_index[idx]["id"] = str(tc["id"])
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            by_index[idx]["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            by_index[idx]["arguments"] = str(fn["arguments"])
                            logger.debug(
                                "stream.tool_call.message_overwrite index={} id={} name={} args_len={}",
                                idx,
                                by_index[idx]["id"],
                                by_index[idx]["name"],
                                len(by_index[idx]["arguments"]),
                            )
        return str(finish_reason or "")

    def _wrap_response(self, resp: Any) -> LLMClientMessage:
        msg = resp.choices[0].message
        content = getattr(msg, "content", None) or ""
        if not isinstance(content, str):
            content = str(content)
        rc = getattr(msg, "reasoning_content", None) or ""
        tool_calls = list(getattr(msg, "tool_calls", None) or [])
        thinking = getattr(msg, "thinking_blocks", None)
        usage = getattr(resp, "usage", None)
        usage_dict: dict[str, Any] = {}
        if usage is not None:
            usage_dict = {
                "input_tokens": int(getattr(usage, "prompt_tokens", None) or 0),
                "output_tokens": int(getattr(usage, "completion_tokens", None) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", None) or 0),
            }
            _extract_detail_tokens(usage, usage_dict)
        return LLMClientMessage(
            content=content,
            reasoning_content=str(rc) if rc else "",
            tool_calls=self._tool_calls_to_dicts(tool_calls),
            thinking_blocks=list(thinking) if thinking else None,
            usage_metadata=usage_dict,
            raw=resp,
        )

    def _wrap_stream_chunk(self, chunk: Any) -> LLMClientMessage:
        chunk_dict = chunk if isinstance(chunk, dict) else self._stream_chunk_to_dict(chunk)
        choices = chunk_dict.get("choices") or []
        usage = chunk_dict.get("usage") or {}
        usage_dict = {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        if usage:
            _extract_detail_tokens_from_dict(usage, usage_dict)
        if not choices:
            return LLMClientMessage(content="", usage_metadata=usage_dict, raw=chunk_dict)
        delta = choices[0].get("delta") or {}
        content = delta.get("content") or ""
        rc = delta.get("reasoning_content") or ""
        return LLMClientMessage(
            content=str(content) if content else "",
            reasoning_content=str(rc) if rc else "",
            usage_metadata=usage_dict,
            raw=chunk_dict,
        )


def _safe_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _safe_assign(obj: Any, attr: str, target: dict[str, Any], target_key: str) -> None:
    """Safely read *attr* from *obj* and store the int in *target[target_key]*."""
    target[target_key] = _safe_int(getattr(obj, attr, None))


def _extract_detail_tokens(usage: Any, target: dict[str, Any]) -> None:
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details:
        target["input_cache_read_tokens"] = _safe_int(getattr(prompt_details, "cached_tokens", None))
        target["input_cache_creation_tokens"] = _safe_int(
            getattr(prompt_details, "cache_creation_tokens", None)
            or getattr(prompt_details, "cache_creation_input_tokens", None)
        )
    else:
        _safe_assign(usage, "cache_read_input_tokens", target, "input_cache_read_tokens")
        _safe_assign(usage, "cache_creation_input_tokens", target, "input_cache_creation_tokens")

    completion_details = getattr(usage, "completion_tokens_details", None)
    if completion_details:
        target["output_reasoning_tokens"] = _safe_int(getattr(completion_details, "reasoning_tokens", None))
    else:
        _safe_assign(usage, "reasoning_tokens", target, "output_reasoning_tokens")


def _extract_detail_tokens_from_dict(usage: dict, target: dict[str, Any]) -> None:
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        target["input_cache_read_tokens"] = _safe_int(prompt_details.get("cached_tokens"))
        target["input_cache_creation_tokens"] = _safe_int(
            prompt_details.get("cache_creation_tokens")
            or prompt_details.get("cache_creation_input_tokens")
        )
    else:
        target["input_cache_read_tokens"] = _safe_int(usage.get("cache_read_input_tokens"))
        target["input_cache_creation_tokens"] = _safe_int(usage.get("cache_creation_input_tokens"))

    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        target["output_reasoning_tokens"] = _safe_int(completion_details.get("reasoning_tokens"))
    else:
        target["output_reasoning_tokens"] = _safe_int(usage.get("reasoning_tokens"))


def llm_adapter_from_env_cfg(cfg: dict[str, Any], logical_name: str) -> Any:
    """``env.llm_configs[logical_name]`` → ``LangChainChatModelAdapter``（供 ``Runtime.llm`` 懒加载缓存）。"""
    from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter

    return LangChainChatModelAdapter(
        LLMClient.from_env_cfg(cfg),
        _llm_config_for_adapter(cfg, logical_name),
    )
