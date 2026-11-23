"""History corpus paths shared by the offline downloader and de-agent hooks."""

import os
from pathlib import Path


def corpus_path(options: dict, home: Path) -> Path:
    """Allow a pinned evaluation snapshot without coupling it to session outputs."""
    value = os.environ.get("DATAAGENT_HISTORY_SQL_CORPUS") or options.get("corpus_path")
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("history_sql.corpus_path must be an absolute path")
        return path.resolve()
    return (home / "history_sql").resolve()


def enabled(options: dict) -> bool:
    value = options.get("enabled", False)
    if not isinstance(value, bool):
        raise ValueError("history_sql.enabled must be a boolean")
    return value
