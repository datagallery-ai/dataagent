"""Thin SDK handle: keep dependencies beside the native graph, never in its state."""

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import AbstractContextManager, aclosing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from ag_ui_langgraph import LangGraphAgent
from langchain_core.runnables.config import merge_configs
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from dataagent.assembly.context import AgentContext
from dataagent.assembly.graph import build_graph, check_agent_configuration
from dataagent.bootstrap import PreparedRuntime
from dataagent.bootstrap.paths import SessionPaths, WorkspaceInput
from dataagent.semantic_client import SemanticServiceClient

# Matches the REST session-handle cache; evicted graphs keep checkpoints in their checkpointer.
_MAX_CACHED_GRAPHS = 32
# Plugin loading edits process-wide sys.path/sys.modules, so builds run one at a time.
_BUILD_LOCK = Lock()


class _ContextAgent(LangGraphAgent):
    """Initialize native AG-UI only after the request selects its session graph."""

    def __init__(self, *, agent, name, config=None, **kwargs):
        self._agent = agent
        self._options = {"name": name, **kwargs}
        self.name, self.description = name, kwargs.get("description")
        self.config = config or {}
        self.graph = self._context = None

    def _initialize(self, graph, context):
        super().__init__(graph=graph, config=self.config, **self._options)
        self._context = context

    def clone(self):
        return type(self)(agent=self._agent, config=dict(self.config), **self._options)

    async def run(self, input):
        with self._agent._claim(input.thread_id):
            graph, context = await asyncio.to_thread(self._agent._resolve, input.thread_id)
            self._initialize(graph, context)
            async with aclosing(super().run(input)) as stream:
                async for event in stream:
                    yield event

    def get_schema_keys(self, config):
        # Explicit inspection selects a thread too; merely constructing an adapter
        # must not open the default session or run plugin factories.
        thread_id = self._agent._thread_id(config)
        if self.graph is None or self._context.session.root.name != thread_id:
            self._initialize(*self._agent._resolve(thread_id))
        return super().get_schema_keys(config)

    def get_stream_kwargs(self, *args, **kwargs):
        stream = super().get_stream_kwargs(*args, **kwargs)
        stream["context"] = self._context
        return stream


@dataclass(frozen=True)
class Agent:
    """Shared SDK services with a fixed native graph/backend per session."""

    context: AgentContext = field(repr=False)
    _graph_options: dict = field(default_factory=dict, repr=False)
    _graphs: OrderedDict[str, tuple[CompiledStateGraph, AgentContext]] = field(
        default_factory=OrderedDict,
        repr=False,
    )
    _owned_semantic_client: SemanticServiceClient | None = field(default=None, repr=False)
    _active: set[str] = field(default_factory=set, repr=False)
    _lock: AbstractContextManager = field(default_factory=Lock, repr=False)

    def _thread_id(self, config):
        configurable = (config or {}).get("configurable") or {}
        return configurable.get("thread_id", self.context.session.root.name)

    @property
    def graph(self) -> CompiledStateGraph:
        """Return the default session's native graph, creating that session if needed."""
        thread_id = self.context.session.root.name
        return (self._cached(thread_id) or self._resolve(thread_id))[0]

    def graph_for(self, thread_id: str) -> CompiledStateGraph:
        """Get an existing session's graph for native state/checkpoint APIs; never create one."""
        return (self._cached(thread_id) or self._resolve(thread_id, create=False))[0]

    def _cached(self, thread_id):
        with self._lock:
            if thread_id in self._graphs:
                self._graphs.move_to_end(thread_id)
            return self._graphs.get(thread_id)

    def _resolve(self, thread_id, output_dir=None, *, create=True):
        # The cache lock never covers file I/O or assembly, so cached sessions and claims
        # never wait for another session's first build; a racing duplicate build is discarded.
        runtime = self.context._runtime
        opened = runtime.paths.open_session(
            thread_id,
            create=create,
            output_dir=output_dir,
            input_workspaces=self._graph_options.get("input_workspaces"),
        )
        if opened is None:
            raise ValueError(f"Session {thread_id!r} does not exist; invoke it first")
        session, workspaces = opened
        cached = self._cached(thread_id)
        if cached is not None:
            return cached
        context = self.context.for_session(session)
        with _BUILD_LOCK:
            graph = build_graph(
                runtime,
                session=session,
                context=context,
                **{**self._graph_options, "input_workspaces": workspaces},
            )
        with self._lock:
            cached = self._graphs.setdefault(thread_id, (graph, context))
            idle = [key for key in self._graphs if key not in self._active and key != thread_id]
            for key in idle[: max(0, len(self._graphs) - _MAX_CACHED_GRAPHS)]:
                del self._graphs[key]
            return cached

    @contextmanager
    def _claim(self, thread_id):
        with self._lock:
            if thread_id in self._active:
                raise ValueError(f"Session {thread_id!r} is already running")
            self._active.add(thread_id)
        try:
            yield
        finally:
            with self._lock:
                self._active.remove(thread_id)

    def _prepare(self, config, output_dir=None):
        """Select the session graph and context without mutating shared dependencies."""
        thread_id = self._thread_id(config)
        graph, context = self._resolve(thread_id, output_dir)
        return (
            graph,
            merge_configs(
                config,
                {
                    "configurable": {"thread_id": thread_id},
                },
            ),
            context,
        )

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

    def invoke(self, inputs, config=None, *, output_dir: str | Path | None = None, **kwargs):
        """Run a task; output_dir is fixed on the session's first invocation."""
        with self._claim(self._thread_id(config)):
            graph, config, context = self._prepare(config, output_dir)
            return graph.invoke(inputs, config, context=context, **kwargs)

    async def ainvoke(self, inputs, config=None, *, output_dir: str | Path | None = None, **kwargs):
        """Run asynchronously with an invocation-local context and session tracing."""
        with self._claim(self._thread_id(config)):
            graph, config, context = await asyncio.to_thread(self._prepare, config, output_dir)
            return await graph.ainvoke(inputs, config, context=context, **kwargs)

    def stream(self, inputs, config=None, *, output_dir: str | Path | None = None, **kwargs):
        """Prepare eagerly; claim on iteration and release on completion or explicit close."""
        with self._claim(self._thread_id(config)):
            graph, config, context = self._prepare(config, output_dir)

        def iterate():
            with self._claim(self._thread_id(config)):
                yield from graph.stream(inputs, config, context=context, **kwargs)

        return iterate()

    async def astream(self, inputs, config=None, *, output_dir: str | Path | None = None, **kwargs):
        """Stream with the same path-binding rules as ainvoke."""
        with self._claim(self._thread_id(config)):
            graph, config, context = await asyncio.to_thread(self._prepare, config, output_dir)
            async with aclosing(graph.astream(inputs, config, context=context, **kwargs)) as stream:
                async for item in stream:
                    yield item

    async def astream_events(
        self, inputs, config=None, *, output_dir: str | Path | None = None, **kwargs
    ):
        """Expose native events with session paths and the caller's callbacks."""
        with self._claim(self._thread_id(config)):
            graph, config, context = await asyncio.to_thread(self._prepare, config, output_dir)
            async with aclosing(
                graph.astream_events(inputs, config, context=context, **kwargs)
            ) as stream:
                async for item in stream:
                    yield item

    def as_agui(self, *, name="dataagent", **kwargs) -> LangGraphAgent:
        """Create a per-request adapter selecting the native graph by input.thread_id."""
        return _ContextAgent(agent=self, name=name, **kwargs)

    def close(self) -> None:
        """Close an automatically created semantic client; borrowed clients stay open."""
        if self._owned_semantic_client is not None:
            self._owned_semantic_client.close()

    async def aclose(self) -> None:
        """Close owned semantic connections outside the event loop."""
        await asyncio.to_thread(self.close)


def create_agent(
    runtime: PreparedRuntime,
    *,
    model=None,
    checkpointer=None,
    session: SessionPaths | None = None,
    input_workspaces: tuple[WorkspaceInput, ...] | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    extension_revision: str | None = None,
    semantic_client: SemanticServiceClient | None = None,
) -> Agent:
    """Create an Agent owning its configured semantic client unless a host supplies one."""
    owned_client = None
    if semantic_client is None and runtime.settings.semantic is not None:
        owned_client = SemanticServiceClient.from_settings(runtime.settings.semantic)
        semantic_client = owned_client
    try:
        # Fail before any session exists; graphs are built per thread on first use.
        check_agent_configuration(
            runtime,
            semantic_client=semantic_client,
            mcp_tools=mcp_tools,
            entry="create_agent",
        )
        if session is not None:
            expected = runtime.paths.for_session(session.root.name)
            if session.root != expected.root:
                raise ValueError("session.root must match this user's Home runtime path")
            if session.traces != expected.traces:
                raise ValueError("session.traces must use the standard Home path")
            session, _ = runtime.paths.open_session(
                session.root.name,
                create=True,
                output_dir=session.outputs,
                input_workspaces=input_workspaces,
            )
        context = AgentContext(
            runtime,
            model=model,
            semantic_client=semantic_client,
            session=session,
        )
        options = {
            "checkpointer": checkpointer,
            "model": model,
            "input_workspaces": input_workspaces,
            "mcp_tools": mcp_tools,
            "extension_revision": extension_revision,
        }
        agent = Agent(
            context,
            _graph_options=options,
            _owned_semantic_client=owned_client,
        )
        if session is not None:
            agent._resolve(session.root.name, output_dir=session.outputs)
        return agent
    except BaseException:
        if owned_client is not None:
            owned_client.close()
        raise
