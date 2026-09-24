"""DataAgent V2 core. Install in its own environment, not alongside the legacy package.

Layout, by phase:

    declarations.py extension schemas (pure Pydantic)
    settings.py     effective product configuration
    strict_json.py  duplicate-key-rejecting JSON reader
    diagnostics.py  credential-redacting, terminal-safe error rendering
    bootstrap/      settings, paths, extension locations and report -> Runtime; no LangChain
    extensions/     plugin and standalone declarations -> native Deep Agents kwargs
    prompts/        host templates + session paths + extension instructions -> prompt text
    agent.py        Runtime + compiled extensions + host policies -> Deep Agents graph

Hosts (REST, SDK) prepare/build native graphs; `extension_revision` and `refresh_extensions`
support next-turn refresh without changing host identity. Hooks receive native objects.
Names that need LangChain are imported lazily so that
`import dataagent` stays light.
"""

from dataagent.bootstrap import (
    LaunchOptions,
    Runtime,
    extension_revision,
    prepare_runtime,
    refresh_extensions,
)
from dataagent.diagnostics import safe_error

__version__ = "0.1.0"

__all__ = [
    "LaunchOptions",
    "Runtime",
    "build_agent",
    "load_extensions",
    "load_mcp_tools",
    "prepare_runtime",
    "refresh_extensions",
    "extension_revision",
    "safe_error",
]

_LAZY = {
    "build_agent": ("dataagent.agent", "build_agent"),
    "load_extensions": ("dataagent.agent", "load_extensions"),
    "load_mcp_tools": ("dataagent.extensions.mcp", "load_mcp_tools"),
}


def __getattr__(name):
    if name in {"deny", "HookDecision"}:
        raise AttributeError(
            f"{name} was removed: Python hooks reject execution by raising an exception"
        )
    if name in _LAZY:
        from importlib import import_module

        module, attribute = _LAZY[name]
        return getattr(import_module(module), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
