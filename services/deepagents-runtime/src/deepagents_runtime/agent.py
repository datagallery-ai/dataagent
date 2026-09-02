from __future__ import annotations

from typing import Any

from deepagents_runtime.config import DEFAULT_SYSTEM_PROMPT, RuntimeSettings
from deepagents_runtime.messages import last_user_text, message_text


def last_human_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        kind = getattr(message, "type", None) or (message.get("role") if isinstance(message, dict) else None)
        if kind in {"human", "user"}:
            return message_text(getattr(message, "content", None) if not isinstance(message, dict) else message.get("content"))
    return last_user_text(
        [{"role": "user", "content": getattr(message, "content", "")} for message in messages]
    )


def ask_user(question: str, options: list[str] | None = None) -> str:
    """Ask the user a clarifying question and wait for their reply."""
    if options:
        return f"Waiting for the user to answer: {question} ({', '.join(options)})"
    return f"Waiting for the user to answer: {question}"


class ScriptedChatModel:
    """Deterministic chat model used when DEEPAGENTS_RUNTIME_MODEL=fake.

    Still goes through create_deep_agent / LangGraph; it only replaces the LLM.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self._bound: Any = None

    @property
    def _llm_type(self) -> str:
        return "deepagents-runtime-scripted"

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
        last = messages[-1] if messages else None
        last_type = getattr(last, "type", None)
        if last_type == "tool":
            return AIMessage(content="已收到你的回复，继续。")
        text = last_human_content(messages)
        lowered = text.lower()
        if "interrupt" in lowered or "ask" in lowered:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "需要我继续吗？", "options": ["继续", "停止"]},
                        "id": "call_ask_scripted",
                        "type": "tool_call",
                    }
                ],
            )
        if "tool" in lowered or "plan" in lowered:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_todos",
                        "args": {"todos": [{"content": "整理问题", "status": "in_progress"}]},
                        "id": "call_todo_scripted",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=f"这是 Deep Agents SDK 的回复：{text or '你好'}")

    def _generate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _agenerate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        messages = input if isinstance(input, list) else [input]
        return self._next_message(messages)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return self.invoke(input, config=config, **kwargs)


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
            return "deepagents-runtime-scripted"

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

    return BoundScriptedChatModel(scripted)


def create_chat_model(settings: RuntimeSettings, *, injected: Any = None) -> Any:
    if injected is not None:
        return _as_langchain_model(injected)
    if settings.fake_model:
        return _wrap_scripted(ScriptedChatModel())
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required unless DEEPAGENTS_RUNTIME_MODEL=fake")
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOpenAI(**kwargs)


def create_runtime_agent(
    settings: RuntimeSettings,
    *,
    model: Any = None,
    checkpointer: Any = None,
    system_prompt: str | None = None,
) -> Any:
    from deepagents import create_deep_agent
    from langchain.tools import tool
    from langgraph.checkpoint.memory import MemorySaver

    resolved_model = create_chat_model(settings, injected=model)
    saver = checkpointer or MemorySaver()
    ask_tool = tool(ask_user)
    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "system_prompt": system_prompt or DEFAULT_SYSTEM_PROMPT,
        "tools": [ask_tool],
        "interrupt_on": {"ask_user": {"allowed_decisions": ["respond", "reject"]}},
        "checkpointer": saver,
    }
    todo_middleware = _todo_middleware()
    if todo_middleware is not None:
        kwargs["middleware"] = [todo_middleware]
    return create_deep_agent(**kwargs)


def _todo_middleware() -> Any | None:
    try:
        from langchain.agents.middleware import TodoListMiddleware

        return TodoListMiddleware()
    except Exception:
        return None
