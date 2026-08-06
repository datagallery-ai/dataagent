"""Shared dependencies for DataLink REST API routes.

Provides reusable dependency-injection helpers for getting config,
storage, and retrieval instances — same pattern as MCP server.
"""

from datalink.config import DataLinkConfig
from datalink.graph.retrieval import GraphRetrieval
from datalink.graph.storage import GraphStorage

# Global instances — reused across requests (SQLite is single-writer,
# but concurrent reads are safe with WAL mode). Write operations
# (add-table, rebuild, remove-table) close and reset these so the
# next request gets a fresh view.
_storage: GraphStorage | None = None
_retrieval: GraphRetrieval | None = None


def get_config() -> DataLinkConfig:
    """Load and return the current DataLinkConfig."""
    return DataLinkConfig.load()


def get_storage(db_path: str = "") -> GraphStorage:
    """Get or create the global storage instance."""
    global _storage
    if db_path:
        config = get_config()
        config.graph_db_path = db_path
        config.save()
    if _storage is None:
        config = get_config()
        _storage = GraphStorage(config.graph_db_path)
    return _storage


def get_retrieval(db_path: str = "") -> GraphRetrieval:
    """Get or create the global retrieval instance."""
    global _retrieval
    if db_path:
        config = get_config()
        config.graph_db_path = db_path
        config.save()
    if _retrieval is None:
        config = get_config()
        storage = get_storage(config.graph_db_path)
        _retrieval = GraphRetrieval(storage, config)
    return _retrieval


def reset_global_instances() -> None:
    """Reset global storage/retrieval after write operations.

    Called after add-table, rebuild, remove-table so subsequent
    read requests see the updated graph.
    """
    global _storage, _retrieval
    if _storage:
        _storage.close()
    _storage = None
    _retrieval = None
