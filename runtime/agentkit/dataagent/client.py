"""Thin SDK handle: keep dependencies beside the native graph, never in its state."""

import asyncio
from dataclasses import dataclass, field

from ag_ui_langgraph import LangGraphAgent
from langgraph.graph.state import CompiledStateGraph

from dataagent.agent import build_agent
from dataagent.bootstrap import Runtime
from dataagent.models import ModelContext
from dataagent.semantic_client import SemanticServiceClient


class _ContextAgent(LangGraphAgent):
    """Inspect the original graph; supply server-owned context only at invocation."""

    def __init__(self, *, model_context: ModelContext, **kwargs):
        super().__init__(**kwargs)
        self._model_context = model_context

    def get_stream_kwargs(self, *args, **kwargs):
        """Inject the private native context only when the adapter invokes the graph."""
        stream = super().get_stream_kwargs(*args, **kwargs)
        stream["context"] = self._model_context
        return stream


@dataclass(frozen=True)
class Agent:
    """Native execution handle that closes only the semantic client it created."""

    graph: CompiledStateGraph
    context: ModelContext = field(repr=False)
    _owned_semantic_client: SemanticServiceClient | None = field(default=None, repr=False)

    def __enter__(self) -> "Agent":
        """Enter an SDK scope owning its automatically created semantic client."""
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Close owned resources after synchronous execution."""
        self.close()

    async def __aenter__(self) -> "Agent":
        """Enter an asynchronous SDK resource scope."""
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Wait for semantic HTTP workers without blocking the event loop."""
        await self.aclose()

    def invoke(self, inputs, config=None, **kwargs):
        """Invoke the native graph with this handle's context."""
        return self.graph.invoke(inputs, config, context=self.context, **kwargs)

    async def ainvoke(self, inputs, config=None, **kwargs):
        """Invoke the native graph asynchronously with this handle's context."""
        return await self.graph.ainvoke(inputs, config, context=self.context, **kwargs)

    def stream(self, inputs, config=None, **kwargs):
        """Stream the native graph with this handle's context."""
        return self.graph.stream(inputs, config, context=self.context, **kwargs)

    def astream(self, inputs, config=None, **kwargs):
        """Return the native asynchronous stream with this handle's context."""
        return self.graph.astream(inputs, config, context=self.context, **kwargs)

    def astream_events(self, inputs, config=None, **kwargs):
        """Return native execution events with this handle's context."""
        return self.graph.astream_events(inputs, config, context=self.context, **kwargs)

    def as_agui(self, *, name="dataagent", **kwargs) -> LangGraphAgent:
        """Create a per-run AG-UI adapter using the same private context."""
        # AG-UI adapters carry per-run state; never cache/share one across requests.
        return _ContextAgent(graph=self.graph, model_context=self.context, name=name, **kwargs)

    def close(self) -> None:
        """Close an automatically created semantic client; borrowed clients stay open."""
        if self._owned_semantic_client is not None:
            self._owned_semantic_client.close()

    async def aclose(self) -> None:
        """Close owned semantic connections outside the event loop."""
        await asyncio.to_thread(self.close)


def create_agent(
    runtime: Runtime, *, semantic_client: SemanticServiceClient | None = None, **kwargs
) -> Agent:
    """Create an Agent owning its configured semantic client unless a host supplies one."""
    owned_client = None
    if semantic_client is None and runtime.settings.semantic is not None:
        owned_client = SemanticServiceClient.from_settings(runtime.settings.semantic)
        semantic_client = owned_client
    try:
        context = ModelContext(runtime, model=kwargs.get("model"), semantic_client=semantic_client)
        graph = build_agent(runtime, context=context, **kwargs)
        return Agent(graph, context, _owned_semantic_client=owned_client)
    except BaseException:
        if owned_client is not None:
            owned_client.close()
        raise
