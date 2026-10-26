"""Task store for the A2A server.

The default store is in memory, matching the previous Ferry server. A durable
store can replace it by passing another ``TaskStore`` into ``create_a2a_server``.
"""

from a2a.server.tasks import InMemoryTaskStore


def create_task_store() -> InMemoryTaskStore:
    """Create the in-memory task store used by a single server process."""
    return InMemoryTaskStore()
