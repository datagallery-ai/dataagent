from pathlib import Path

from conftest import ScriptedModel, call
from deepagents import create_deep_agent
from deepagents.middleware import FilesystemMiddleware, SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from dataagent.extensions.filesystem_backend import build_filesystem_backend


def backend_for(runtime):
    return build_filesystem_backend(runtime, runtime.paths.for_session("context-test"))


async def test_native_summarization_offloads_inside_session_and_continues(runtime):
    backend = backend_for(runtime)
    artifacts = runtime.paths.for_session("context-test").artifacts
    model = ScriptedModel(responses=[AIMessage(content="The earlier messages discussed apples."), AIMessage(content="Continuing from the summary.")])
    graph = create_deep_agent(
        model=model, backend=backend, checkpointer=InMemorySaver(),
        middleware=[SummarizationMiddleware(model=model, backend=backend, trigger=("messages", 4), keep=("messages", 2))],
    )
    result = await graph.ainvoke({"messages": [
        HumanMessage(content="I like apples"), AIMessage(content="Noted"),
        HumanMessage(content="Remember apples"), AIMessage(content="Remembered"),
        HumanMessage(content="Continue"),
    ]}, {"configurable": {"thread_id": "summary-test"}})
    assert result["messages"][-1].content == "Continuing from the summary."
    files = list((artifacts / "conversation_history").rglob("*"))
    saved = [path for path in files if path.is_file()]
    assert saved
    assert any("apples" in path.read_text() for path in saved)
    assert all(path.resolve().is_relative_to(artifacts) for path in saved)
    assert any("apples" in str(message.content) for message in model.requests[-1])


async def test_native_large_tool_result_offload_is_readable(runtime):
    @tool
    def big_result() -> str:
        """Return a long deterministic tool result."""
        return "statistics evidence " * 1000

    backend = backend_for(runtime)
    artifacts = runtime.paths.for_session("context-test").artifacts
    model = ScriptedModel(responses=[call("big_result", {}, id="large-result"), AIMessage(content="Saved the evidence.")])
    graph = create_deep_agent(
        model=model, backend=backend, tools=[big_result],
        middleware=[FilesystemMiddleware(backend=backend, tool_token_limit_before_evict=20)],
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="Get evidence")]})
    files = [path for path in (artifacts / "large_tool_results").rglob("*") if path.is_file()]
    assert files and all(path.resolve().is_relative_to(artifacts) for path in files)
    assert any("statistics evidence" in path.read_text() for path in files)
    tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert str(artifacts) in str(tool_message.content)
    reader = ScriptedModel(responses=[
        call("read_file", {"file_path": str(files[0])}), AIMessage(content="Read saved evidence."),
    ])
    read_result = await create_deep_agent(model=reader, backend=backend).ainvoke({
        "messages": [HumanMessage(content="Read the saved tool output")],
    })
    restored = next(message for message in read_result["messages"] if isinstance(message, ToolMessage))
    assert "statistics evidence" in restored.content
    assert Path(files[0]).is_absolute()
