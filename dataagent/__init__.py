"""Public SDK for preparing a runtime, creating an Agent and invoking it.

The client owns session graphs. The assembly package builds them. Bootstrap
uses config to prepare the runtime, and diagnostics handles errors and logging.
Hosts use dataagent.host and raw graph callers use dataagent.lowlevel.
Model tooling is loaded when its SDK names are first accessed.
"""

from dataagent.bootstrap import LaunchOptions, PreparedRuntime, prepare_runtime
from dataagent.bootstrap.config_files import ConfigurationLoadError
from dataagent.config.strict_yaml import read_yaml
from dataagent.diagnostics.render import safe_error

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
    "HITLContext",
    "HITLHandler",
    "HITLState",
    "InteractionRequest",
    "ResumeDecision",
    "TriggerDecision",
    "PreparedRuntime",
    "SemanticServiceClient",
    "SubagentServices",
    "collect_model_stream",
    "create_agent",
    "load_mcp_tools",
    "prepare_runtime",
    "safe_error",
    "read_yaml",
]

_LAZY = {
    "HITLContext": ("dataagent.extensions.hitl", "HITLContext"),
    "HITLHandler": ("dataagent.extensions.hitl", "HITLHandler"),
    "HITLState": ("dataagent.extensions.hitl", "HITLState"),
    "InteractionRequest": ("dataagent.extensions.hitl", "InteractionRequest"),
    "ResumeDecision": ("dataagent.extensions.hitl", "ResumeDecision"),
    "TriggerDecision": ("dataagent.extensions.hitl", "TriggerDecision"),
    "SemanticServiceClient": ("dataagent.semantic_client", "SemanticServiceClient"),
    "DataAgentError": ("dataagent.diagnostics.errors", "DataAgentError"),
    "LoopDetectedError": ("dataagent.extensions.loop_detection", "LoopDetectedError"),
    "create_agent": ("dataagent.client", "create_agent"),
    "Agent": ("dataagent.client", "Agent"),
    "AgentContext": ("dataagent.assembly.context", "AgentContext"),
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
