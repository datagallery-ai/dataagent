"""Read committed IR fields from this native session only."""

from pathlib import Path

from .runtime import ir_state
from .session_store import IRSessionStore


def load_committed_fields(context):
    if context is None:
        return []
    if not callable(getattr(context, "get_plugin_state", None)):
        return []
    session = ir_state(context).get("session") or {}
    snapshot = session.get("snapshot")
    if snapshot is None:
        root = getattr(getattr(context, "session", None), "root", None)
        if root is None:
            return []
        root = Path(root).resolve()
        snapshot = IRSessionStore(root, root.parent.parent.name, root.name).load()
    if not snapshot or snapshot.get("status") != "committed":
        return []
    return snapshot.get("fields", [])
