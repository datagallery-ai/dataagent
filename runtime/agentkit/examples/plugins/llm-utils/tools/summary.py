"""A tool using a host-configured model alias, without loading configuration or keys."""

from langchain.tools import ToolRuntime

from dataagent import ModelContext


async def summarize_text(text: str, runtime: ToolRuntime[ModelContext]) -> str:
    """Summarize the supplied text using the configured fast model."""
    model = runtime.context.get_model("fast")
    response = await model.ainvoke(
        [("system", "Summarize the supplied text accurately and concisely."), ("human", text)],
        config=runtime.config,
    )
    return response.text
