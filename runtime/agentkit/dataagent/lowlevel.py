"""Low-level graph assembly. Use `dataagent.create_agent` unless you need the raw graph.

`build_graph` returns one native LangGraph `CompiledStateGraph` bound to one session.
It does not own a semantic client, cache graphs per thread, or manage output
directories; the caller supplies the context on every invocation.
"""

from dataagent.agent import build_graph

__all__ = ["build_graph"]
