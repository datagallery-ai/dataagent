"""A2A service hosted by DataAgent's REST application or CLI."""

from dataagent.server.a2a.server import create_a2a_server, run_a2a_server

__all__ = ["create_a2a_server", "run_a2a_server"]
