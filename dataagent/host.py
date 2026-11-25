"""Interfaces for hosts that serve DataAgent to other clients (REST, TUI back ends).

SDK users do not need this module: `prepare_runtime` and `create_agent` cover one
process that owns its Agent. Hosts use these names to reload extensions between
turns, choose a listen address, validate client IDs, and render stable public error codes.
"""

from dataagent.bootstrap import extension_revision, refresh_extensions, server_listen
from dataagent.bootstrap.paths import opaque_id
from dataagent.config.declarations import OPAQUE_ID_PATTERN
from dataagent.config.settings import ServerSettings
from dataagent.diagnostics.logging import (
    configure_logging,
    diagnostic_traceback,
    log_context,
    register_log_secrets,
)
from dataagent.diagnostics.render import ERROR_CODES, error_code, log_event, safe_interrupt_fields

__all__ = [
    "ServerSettings",
    "opaque_id",
    "log_event",
    "ERROR_CODES",
    "OPAQUE_ID_PATTERN",
    "error_code",
    "extension_revision",
    "refresh_extensions",
    "server_listen",
    "configure_logging",
    "diagnostic_traceback",
    "log_context",
    "register_log_secrets",
    "safe_interrupt_fields",
]
