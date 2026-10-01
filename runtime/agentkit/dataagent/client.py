"""Thin SDK handle: keep dependencies beside the native graph, never in its state."""

from dataclasses import dataclass, field

from ag_ui_langgraph import LangGraphAgent
from langgraph.graph.state import CompiledStateGraph

from dataagent.agent import build_agent
from dataagent.bootstrap import Runtime
from dataagent.models import ModelContext


class _ContextAgent(LangGraphAgent):
    """Inspect the original graph; supply server-owned context only at invocation."""

    def __init__(self, *, model_context: ModelContext, **kwargs):
        super().__init__(**kwargs)
        self._model_context = model_context

    def get_stream_kwargs(self, *args, **kwargs):
        stream = super().get_stream_kwargs(*args, **kwargs)
        stream["context"] = self._model_context
        return stream


@dataclass(frozen=True)
class Agent:
    """Execution convenience only. Use graph for native schema/state/checkpoint APIs."""

    graph: CompiledStateGraph
    context: ModelContext = field(repr=False)

    def invoke(self, inputs, config=None, **kwargs):
        return self.graph.invoke(inputs, config, context=self.context, **kwargs)

    async def ainvoke(self, inputs, config=None, **kwargs):
        return await self.graph.ainvoke(inputs, config, context=self.context, **kwargs)

    def stream(self, inputs, config=None, **kwargs):
        return self.graph.stream(inputs, config, context=self.context, **kwargs)

    def astream(self, inputs, config=None, **kwargs):
        return self.graph.astream(inputs, config, context=self.context, **kwargs)

    def astream_events(self, inputs, config=None, **kwargs):
        return self.graph.astream_events(inputs, config, context=self.context, **kwargs)

    def as_agui(self, *, name="dataagent", **kwargs) -> LangGraphAgent:
        # AG-UI adapters carry per-run state; never cache/share one across requests.
        return _ContextAgent(graph=self.graph, model_context=self.context, name=name, **kwargs)


def create_agent(runtime: Runtime, **kwargs) -> Agent:
    """Create graph and dependencies once; invocation never reconstructs the clients."""
    context = ModelContext(runtime, model=kwargs.get("model"))
    return Agent(build_agent(runtime, context=context, **kwargs), context)
