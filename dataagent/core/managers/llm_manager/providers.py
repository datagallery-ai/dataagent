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
import os
from abc import ABC, abstractmethod
from typing import Any, Literal

from loguru import logger

from dataagent.core.managers.llm_manager.llm_config import LLMConfig


class BaseLLMProvider(ABC):
    """LLM提供商基类"""

    @abstractmethod
    def create_llm(self, config: LLMConfig) -> Any:
        """创建LLM实例"""
        pass


class OpenAIProvider(BaseLLMProvider):
    """OpenAI 兼容协议 Provider（统一入口，所有平台共用）

    provider 字段作为平台标识，用于拼接环境变量名查找 base_url 和 api_key：
      - {PROVIDER}_BASE_URL  （如 DEEPSEEK_BASE_URL）
      - {PROVIDER}_API_KEY   （如 DEEPSEEK_API_KEY）

    优先级：
      base_url:  {PROVIDER}_BASE_URL (env)
      api_key:  {PROVIDER}_API_KEY > OPENAI_API_KEY
    """

    def create_llm(self, config: LLMConfig) -> Any:
        """构造 OpenAI 兼容的 LLM 客户端。

        当前实现统一通过 :meth:`LLMClient.from_llm_config` 构造，跟 Flex 路径
        （``runtime.llm`` → ``llm_adapter_from_env_cfg``）共用同一份解析逻辑。
        """
        import litellm

        from dataagent.core.managers.llm_manager.llm_client import LLMClient

        litellm.ssl_verify = False
        return LLMClient.from_llm_config(config)


# ===== openjiuwen native provider (参考 dataagent_jiuwen) =====

_DROP_REQUEST_KWARGS: set[str] = {
    # 这些是"客户端级参数"，不应进入 openai.chat.completions.create(**params)
    # 某些 openjiuwen 版本会把 kwargs 直接塞进请求 params，导致 OpenAI SDK 报错
    "max_retries",
    "api_key",
}


def _sanitize_request_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    if not params:
        return {}
    return {k: v for k, v in params.items() if k not in _DROP_REQUEST_KWARGS}


class OpenJiuWenChatLLMResponse:
    """兼容 DataAgent 旧代码：模仿 langchain 的 response 属性形态。"""

    def __init__(self, *, content: str, tool_calls: list[dict] | None = None, usage_metadata: dict | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.invalid_tool_calls = []
        self.usage_metadata = usage_metadata or {}


class OpenJiuWenChatLLM:
    """
    openjiuwen 原生 LLM wrapper：提供 invoke(messages)->response，形态对齐 langchain。
    底层实际调用 openjiuwen 的 BaseModelClient（ModelFactory 获取）。
    """

    def __init__(self, client: Any, model: str, *, tools: list[dict] | None = None, default_params: dict | None = None):
        self._client = client
        self._model = model
        self._tools = tools or []
        self._default_params = _sanitize_request_kwargs(default_params or {})

    def bind_tools(self, tools: Any, **kwargs: Any) -> "OpenJiuWenChatLLM":
        """
        兼容 langchain ChatModel.bind_tools(tools)：
        - Flex 的 Planner 等节点会传入 langchain_core.tools.StructuredTool 列表
        - openjiuwen client 侧期望的是 OpenAI function tools schema
        因此这里将 tools 转为 [{type:'function', function:{name,description,parameters}}] 并返回一个新实例。
        """

        def _tool_to_openai_schema(t: Any) -> dict[str, Any] | None:
            # 已经是 OpenAI tools 形态
            if isinstance(t, dict) and ("function" in t or t.get("type") == "function"):
                return t
            name = getattr(t, "name", None) or getattr(t, "__name__", None)
            if not name:
                return None
            desc = getattr(t, "description", None) or ""
            params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            args_schema = getattr(t, "args_schema", None)
            if args_schema is not None:
                try:
                    # pydantic v2
                    params = args_schema.model_json_schema()  # type: ignore[attr-defined]
                except Exception:
                    try:
                        # pydantic v1
                        params = args_schema.schema()  # type: ignore[attr-defined]
                    except Exception:
                        params = {"type": "object", "properties": {}, "required": []}
            return {
                "type": "function",
                "function": {
                    "name": str(name),
                    "description": str(desc or ""),
                    "parameters": params,
                },
            }

        tool_list: list[Any]
        if tools is None:
            tool_list = []
        elif isinstance(tools, list):
            tool_list = tools
        else:
            tool_list = [tools]

        converted: list[dict[str, Any]] = []
        for t in tool_list:
            schema = _tool_to_openai_schema(t)
            if schema is not None:
                converted.append(schema)

        # 合并 kwargs 允许覆盖/追加默认参数（保持 langchain 行为风格）
        new_defaults = {**self._default_params, **_sanitize_request_kwargs(dict(kwargs or {}))}
        return OpenJiuWenChatLLM(self._client, self._model, tools=converted, default_params=new_defaults)

    def invoke(self, messages: Any, **kwargs: Any) -> OpenJiuWenChatLLMResponse:
        """
        同步调用 OpenJiuWen 模型。

        处理流程：
        1. 清洗参数并调用底层客户端。
        2. 兼容处理 content 为列表（多模态/多段）或非字符串的情况。
        3. 若 content 为空，尝试回退到 raw/reason 字段。
        4. 解析 tool_calls 和 usage_metadata。
        """
        params = _sanitize_request_kwargs({**self._default_params, **kwargs})
        ai_msg = self._client.invoke(model_name=self._model, messages=messages, tools=self._tools, **params)

        content = ai_msg.content if hasattr(ai_msg, "content") else str(ai_msg)
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
            content = "".join(parts)
        elif not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        if not str(content).strip():
            raw_content = getattr(ai_msg, "raw_content", None)
            reason_content = getattr(ai_msg, "reason_content", None)
            if isinstance(raw_content, str) and raw_content.strip():
                content = raw_content
            elif isinstance(reason_content, str) and reason_content.strip():
                content = reason_content

        tool_calls: list[dict[str, Any]] = []
        for tc in getattr(ai_msg, "tool_calls", None) or []:
            args_raw = getattr(tc, "arguments", "") if tc is not None else ""
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) and args_raw else args_raw
            except Exception:
                args = args_raw
            tool_calls.append({"id": getattr(tc, "id", ""), "name": getattr(tc, "name", ""), "args": args})

        usage_dump: dict[str, Any] = {}
        try:
            usage_dump = ai_msg.model_dump().get("usage_metadata", {})  # type: ignore[attr-defined]
        except Exception:
            usage_dump = {}
        total_tokens = usage_dump.get("total_tokens")
        if total_tokens is None:
            total_tokens = usage_dump.get("total_latency", 0)

        try:
            total_tokens_int = int(float(total_tokens or 0))
        except Exception:
            total_tokens_int = 0

        return OpenJiuWenChatLLMResponse(
            content=str(content),
            tool_calls=tool_calls,
            # langchain 的 AIMessage.usage_metadata 在新版本里要求 input_tokens/output_tokens 字段
            usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": total_tokens_int},
        )

    async def ainvoke(self, messages: Any, **kwargs: Any) -> OpenJiuWenChatLLMResponse:
        """
        异步兼容接口：Flex 节点会 await llm.ainvoke(...)
        openjiuwen 的 BaseModelClient 在当前实现里是同步 invoke，因此这里直接复用同步逻辑。
        """
        return self.invoke(messages, **kwargs)


class OpenJiuWenProvider(BaseLLMProvider):
    """
    openjiuwen 原生 provider：
    - 通过 ModelFactory 获取 BaseModelClient（OpenAI 兼容接口）
    - 返回 OpenJiuWenChatLLM（invoke 形态对齐 langchain）
    """

    def create_llm(self, config: LLMConfig) -> Any:
        try:
            from openjiuwen.core.utils.llm.model_utils.model_factory import ModelFactory

            # type: ignore[import-not-found]
        except Exception as e:
            raise ImportError("openjiuwen is required for OpenJiuWenProvider") from e
        ssl_verify = os.getenv("LLM_SSL_VERIFY", "").strip().lower()
        ssl_cert = os.getenv("LLM_SSL_CERT")
        # 有些环境会把 LLM_SSL_CERT 配成空字符串；对 openjiuwen 来说等同于"未提供证书"
        ssl_cert = ssl_cert if (isinstance(ssl_cert, str) and ssl_cert.strip()) else None
        # openjiuwen 默认"严格校验"会要求提供证书；而多数内网/自签场景并不会配 LLM_SSL_CERT。
        # 因此：只要没提供证书，就主动关闭校验，避免 [188005] 报错阻塞业务。
        if not ssl_cert and ssl_verify != "false":
            logger.warning(
                "LLM_SSL_CERT is not set; forcing LLM_SSL_VERIFY=false to avoid openjiuwen SSL cert error. "
                "If you need SSL verification, please set LLM_SSL_CERT (and optionally SAFE_CERT_DIR)."
            )
            os.environ["LLM_SSL_VERIFY"] = "false"

        llm_params = config.client_params()
        provider = config.provider.upper()

        # --- base_url ---
        # 优先级: params.base_url (YAML) > {PROVIDER}_BASE_URL (env)
        api_base = llm_params.get("base_url") or llm_params.get("api_base") or llm_params.get("api_base_url")
        if not api_base:
            api_base = os.getenv(f"{provider}_BASE_URL")

        model = llm_params.get("model")
        if not api_base or not model:
            raise ValueError(f"{config.name}: missing required params base_url/model for openjiuwen client")

        # --- api_key ---
        # 优先级: {PROVIDER}_API_KEY > OPENAI_API_KEY
        api_key = llm_params.get("api_key")
        if not api_key:
            api_key = os.getenv(f"{provider}_API_KEY")
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY")

        model_provider = llm_params.get("model_provider", "openai")
        client = ModelFactory().get_model(
            model_provider=str(model_provider),
            api_key=api_key,
            api_base=api_base,
            timeout=llm_params.get("timeout", 60),
            temperature=llm_params.get("temperature"),
            top_p=llm_params.get("top_p"),
        )

        default_params = {
            k: v
            for k, v in llm_params.items()
            if k not in {"base_url", "api_base", "api_base_url", "model", "max_retries", "model_provider"}
        }
        return OpenJiuWenChatLLM(client, str(model), default_params=default_params)


# ===== Provider 路由 =====
#
# provider 字段仅表达"平台名"（deepseek / bailian / openai / local 等），
# 用于拼接环境变量名查找 base_url 和 api_key。
# SDK 的选择由 AGENT_CONFIG.backend 决定：
# - backend=langgraph：统一使用 OpenAIProvider（ChatOpenAI）
# - backend=openjiuwen：使用 OpenJiuWenProvider（ModelFactory）
#

_openai_provider = OpenAIProvider()
_openjiuwen_provider = OpenJiuWenProvider()


def get_provider(provider: str, backend: Literal["langgraph", "openjiuwen"] = "langgraph") -> BaseLLMProvider:
    """获取 LLM provider

    provider 参数仅作为平台标识（如 deepseek、bailian、openai），
    不再用于查表路由。SDK 选择完全由 backend 决定。
    """
    if backend == "openjiuwen":
        return _openjiuwen_provider
    return _openai_provider
