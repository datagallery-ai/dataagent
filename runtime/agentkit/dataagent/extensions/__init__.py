"""Extension assembly: declarations and host filesystem capabilities → native Deep Agents inputs.

Consumes locations supplied by bootstrap; never discovers Home resources itself.
Importing this package loads model tooling; bootstrap uses the lightweight declarations module.
filesystem_backend assembles file routes, permissions and shell execution separately from the compiler.
tracing binds native LangChain/LangSmith OTEL tracing; trace_exporter writes local OTLP JSON.
"""

from dataagent.extensions.compiler import compile_extensions
from dataagent.extensions.loading import PythonLoader, contained_path, select_plugins

__all__ = [
    "PythonLoader",
    "compile_extensions",
    "contained_path",
    "select_plugins",
]
