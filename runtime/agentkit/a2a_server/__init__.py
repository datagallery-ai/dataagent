"""A2A 1.0 client and server for one AgentKit runtime.

Call another agent with `A2ACaller`. Hosts expose this runtime with
`create_a2a_server` / `run_a2a_server` (or `dataagent serve-a2a`).
"""

from a2a_server.client import A2ACaller, A2ACallError, A2AResult
from a2a_server.server import create_a2a_server, run_a2a_server

__all__ = [
    "A2ACallError",
    "A2ACaller",
    "A2AResult",
    "create_a2a_server",
    "run_a2a_server",
]
