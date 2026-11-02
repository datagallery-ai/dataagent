"""Collect streamed model text for callers that require a complete SQL or JSON response."""

from dataagent.core.managers.llm_manager.adapters import ChatModel


async def collect_llm_stream(llm: ChatModel, prompts: list[dict[str, str]], **kwargs) -> str:
    """Consume all chunks, prefer the normalized final response, and propagate stream failures.

    ``kwargs`` are forwarded to the underlying model call (e.g. ``enable_thinking=False``),
    so a single call can override the model slot's own request options.
    """
    parts: list[str] = []
    final_content: str | None = None
    async for chunk in llm.astream(prompts, **kwargs):
        if chunk.final_response is not None:
            final_content = chunk.final_response.content
        elif chunk.content:
            parts.append(chunk.content)
    return final_content if final_content is not None else "".join(parts)
