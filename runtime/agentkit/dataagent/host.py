"""Interfaces for hosts that serve DataAgent to other clients (REST, TUI back ends).

SDK users do not need this module: `prepare_runtime` and `create_agent` cover one
process that owns its Agent. Hosts use these names to reload extensions between
turns, choose a listen address, validate client IDs, and render stable public error codes.
"""

from dataagent.bootstrap import extension_revision, refresh_extensions, server_listen
from dataagent.config_migration import add_arguments as add_config_arguments
from dataagent.config_migration import run as run_config_command
from dataagent.declarations import OPAQUE_ID_PATTERN
from dataagent.diagnostics import ERROR_CODES, error_code
from dataagent.logging import (
    configure_logging,
    diagnostic_traceback,
    log_context,
    register_log_secrets,
)

__all__ = [
    "ERROR_CODES",
    "add_config_arguments",
    "run_config_command",
    "OPAQUE_ID_PATTERN",
    "error_code",
    "extension_revision",
    "refresh_extensions",
    "server_listen",
    "configure_logging",
    "diagnostic_traceback",
    "log_context",
    "register_log_secrets",
]
