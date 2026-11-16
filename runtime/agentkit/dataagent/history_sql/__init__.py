"""Offline history-SQL corpus preparation; retrieval belongs to the de-agent plugin."""

from dataagent.history_sql.config import corpus_path, enabled

__all__ = ["add_arguments", "corpus_path", "enabled", "run"]


def __getattr__(name):
    # The CLI imports bootstrap and the semantic client; load it only for `dataagent history-sql`.
    if name in {"add_arguments", "run"}:
        from dataagent.history_sql import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
