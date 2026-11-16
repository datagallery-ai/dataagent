"""DataAgent core. Install in its own environment, not alongside the legacy package.

Layout, by phase:

    declarations.py extension schemas (pure Pydantic)
    settings.py     effective product configuration
    strict_json.py  duplicate-key-rejecting JSON reader
    diagnostics.py  credential-redacting, terminal-safe error rendering
    bootstrap/      settings, paths, extension locations and report -> PreparedRuntime; no LangChain
    extensions/     plugin and standalone declarations -> native Deep Agents kwargs
    prompts/        host templates + session paths + extension instructions -> prompt text
    agent.py        PreparedRuntime + compiled extensions + host policies -> Deep Agents graph
    models.py       Configured model clients exposed through native ToolRuntime.context
    client.py       Graph + dependencies; SDK invocation and AG-UI adapter convenience
    host.py         interfaces for serving hosts: extension refresh, listen address, error codes
    lowlevel.py     raw graph assembly (build_graph)

The public SDK is `prepare_runtime` -> `create_agent` -> `Agent`. Hosts also use
`dataagent.host`; raw LangGraph callers use `dataagent.lowlevel`. Hooks receive native objects.
Names that need LangChain are imported lazily so that
`import dataagent` stays light.
"""

from dataagent.bootstrap import LaunchOptions, PreparedRuntime, prepare_runtime
from dataagent.bootstrap.config_files import ConfigurationLoadError
from dataagent.diagnostics import safe_error

__version__ = "0.1.0"

# Public SDK: prepare a PreparedRuntime, create an Agent, call it. Host interfaces live in
# dataagent.host, raw graph assembly in dataagent.lowlevel.
__all__ = [
    "Agent",
    "ConfigurationLoadError",
    "DataAgentError",
    "LaunchOptions",
    "LoopDetectedError",
    "AgentContext",
    "PreparedRuntime",
    "SemanticServiceClient",
    "SubagentServices",
    "collect_model_stream",
    "create_agent",
    "load_mcp_tools",
    "prepare_runtime",
    "safe_error",
]

_LAZY = {
    "SemanticServiceClient": ("dataagent.semantic_client", "SemanticServiceClient"),
    "DataAgentError": ("dataagent.errors", "DataAgentError"),
    "LoopDetectedError": ("dataagent.extensions.loop_detection", "LoopDetectedError"),
    "create_agent": ("dataagent.client", "create_agent"),
    "Agent": ("dataagent.client", "Agent"),
    "AgentContext": ("dataagent.models", "AgentContext"),
    "SubagentServices": ("dataagent.extensions.subagents", "SubagentServices"),
    "collect_model_stream": ("dataagent.model_stream", "collect_model_stream"),
    "load_mcp_tools": ("dataagent.extensions.mcp", "load_mcp_tools"),
}


def __getattr__(name):
    if name in _LAZY:
        from importlib import import_module

        module, attribute = _LAZY[name]
        return getattr(import_module(module), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
