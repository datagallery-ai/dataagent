"""Bounded per-session graphs, refreshed atomically before a new run (never mid-run)."""

import asyncio
from collections import OrderedDict

from dataagent import (
    build_agent,
    extension_revision,
    load_mcp_tools,
    refresh_extensions,
    safe_error,
)


class SessionAgents:
    """Only preparation is serialized; active streams keep their own graph reference."""

    def __init__(self, runtime, *, checkpointer, model, mcp_tools, max_cached=32):
        self.runtime = runtime
        self.checkpointer = checkpointer
        self.model = model
        self.mcp_tools = mcp_tools
        self.revision = extension_revision(runtime)
        self.max_cached = max_cached
        self.graphs = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, thread_id: str, *, create: bool):
        async with self.lock:
            candidate = self.runtime
            tools = self.mcp_tools
            changed = create and extension_revision(candidate) != self.revision
            try:
                if changed:
                    candidate = refresh_extensions(candidate)
                revision = extension_revision(candidate) if changed else self.revision
                opened = candidate.paths.open_session(thread_id, create=create)
                if opened is None:
                    return None
                session, workspaces = opened
                key = (thread_id, workspaces)
                if not changed and key in self.graphs:
                    self.graphs.move_to_end(key)
                    return session, self.graphs[key], self.runtime
                if changed and candidate.extensions.mcp_servers != self.runtime.extensions.mcp_servers:
                    tools = await load_mcp_tools(candidate)
                graph = build_agent(
                    candidate, checkpointer=self.checkpointer, model=self.model,
                    session=session, input_workspaces=workspaces, mcp_tools=tools,
                    extension_revision=revision,
                )
                if changed and extension_revision(candidate) != revision:
                    raise ValueError("Extensions changed during loading; retry after installation finishes")
            except Exception as error:
                if not changed:
                    raise
                message = safe_error(error, (*self.runtime.redaction_secrets, *candidate.redaction_secrets))["message"]
                raise ValueError(f"Extension reload failed: {message}") from None
            if changed:
                self.graphs.clear()
                self.runtime, self.mcp_tools, self.revision = candidate, tools, revision
            self.graphs[key] = graph
            while len(self.graphs) > self.max_cached:
                self.graphs.popitem(last=False)
            return session, graph, candidate
