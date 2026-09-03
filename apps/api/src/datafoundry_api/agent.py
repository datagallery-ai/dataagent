from __future__ import annotations

from typing import Any

from datafoundry_api.settings import Settings

DEFAULT_SYSTEM_PROMPT = (
    "You are DataFoundry's assistant. "
    "Data warehouse tools, knowledge retrieval, and skill packages are not connected in this runtime version. "
    "You may converse, use built-in planning/todo/filesystem tools, and ask the user questions when you need confirmation."
)


def last_human_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        kind = getattr(message, "type", None) or (message.get("role") if isinstance(message, dict) else None)
        if kind in {"human", "user"}:
            content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
            return content if isinstance(content, str) else str(content or "")
    return ""


class ScriptedChatModel:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self._bound: Any = None

    @property
    def _llm_type(self) -> str:
        return "deepagents-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        clone = ScriptedChatModel(self._responses)
        clone._index = self._index
        clone._bound = tools
        return clone

    def _next_message(self, messages: list[Any]) -> Any:
        from langchain_core.messages import AIMessage

        if self._responses:
            message = self._responses[min(self._index, len(self._responses) - 1)]
            self._index += 1
            return message
        text = last_human_content(messages)
        return AIMessage(content=f"这是 Deep Agents SDK 的回复：{text or '你好'}")

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        messages = input if isinstance(input, list) else [input]
        return self._next_message(messages)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return self.invoke(input, config=config, **kwargs)


def create_chat_model(settings: Settings, *, injected: Any = None) -> Any:
    if injected is not None:
        return _as_langchain_model(injected)
    if settings.fake_model:
        return _wrap_scripted(ScriptedChatModel())
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required unless DEEPAGENTS_RUNTIME_MODEL=fake")
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {"model": settings.llm_model, "api_key": settings.llm_api_key}
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOpenAI(**kwargs)


def create_runtime_agent(settings: Settings, *, model: Any = None, checkpointer: Any = None) -> Any:
    from deepagents import create_deep_agent
    from deepagents_runtime import (
        extra_backend,
        extra_interrupt_on,
        extra_middleware,
        extra_skills,
        extra_subagents,
        extra_system_prompt,
        extra_tools,
    )

    return create_deep_agent(
        model=create_chat_model(settings, injected=model),
        system_prompt=extra_system_prompt(DEFAULT_SYSTEM_PROMPT),
        tools=extra_tools(),
        middleware=extra_middleware(),
        subagents=extra_subagents(),
        skills=extra_skills(),
        backend=extra_backend(),
        interrupt_on=extra_interrupt_on(),
        checkpointer=checkpointer,
        name="dataFoundry",
    )


def _as_langchain_model(model: Any) -> Any:
    from langchain_core.language_models.chat_models import BaseChatModel

    if isinstance(model, BaseChatModel):
        return model
    if isinstance(model, ScriptedChatModel):
        return _wrap_scripted(model)
    return model


def _wrap_scripted(scripted: ScriptedChatModel) -> Any:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import PrivateAttr

    class BoundScriptedChatModel(BaseChatModel):
        _inner: ScriptedChatModel = PrivateAttr()

        def __init__(self, inner: ScriptedChatModel) -> None:
            super().__init__()
            self._inner = inner

        @property
        def _llm_type(self) -> str:
            return "deepagents-scripted"

        def bind_tools(self, tools: Any, **kwargs: Any) -> BoundScriptedChatModel:
            return BoundScriptedChatModel(self._inner.bind_tools(tools, **kwargs))

        def _generate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
            message = self._inner._next_message(messages)
            if not isinstance(message, AIMessage):
                message = AIMessage(content=str(message))
            return ChatResult(generations=[ChatGeneration(message=message)])

        async def _agenerate(
            self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> ChatResult:
            return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        def _stream(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any):
            from langchain_core.messages import AIMessageChunk
            from langchain_core.outputs import ChatGenerationChunk

            message = self._inner._next_message(messages)
            text = message.content if isinstance(getattr(message, "content", None), str) else str(message)
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=text, id=getattr(message, "id", None)))
            if run_manager:
                run_manager.on_llm_new_token(text, chunk=chunk)
            yield chunk

        async def _astream(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any):
            for chunk in self._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
                yield chunk

    return BoundScriptedChatModel(scripted)
