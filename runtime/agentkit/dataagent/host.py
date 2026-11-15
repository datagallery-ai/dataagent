"""Interfaces for hosts that serve DataAgent to other clients (REST, TUI back ends).

SDK users do not need this module: `prepare_runtime` and `create_agent` cover one
process that owns its Agent. Hosts use these names to reload extensions between
turns, choose a listen address, and render stable public error codes.
"""

from dataagent.bootstrap import extension_revision, refresh_extensions, server_listen
from dataagent.diagnostics import ERROR_CODES, error_code

__all__ = [
    "ERROR_CODES",
    "error_code",
    "extension_revision",
    "refresh_extensions",
    "server_listen",
]
