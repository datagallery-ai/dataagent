"""Allocate CLI run numbers across process restarts without overwriting traces."""

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from dataagent.utils.runtime_paths import resolve_flex_context_dir, resolve_flex_session_memory_dir


def reserve_run_id(config: dict, *, user_id: str | None, session_id: str) -> int:
    """Reserve before chat; failed/interrupted turns also consume a number.

    The lock is short-lived and separate from the SDK's full-turn workspace lock.
    Existing Context dumps bootstrap sessions created before this allocator.
    """
    paths = {"config": config, "user_id": user_id, "session_id": session_id}
    memory = resolve_flex_session_memory_dir(**paths)
    memory.mkdir(parents=True, exist_ok=True)
    counter = memory / "cli_runs.json"
    with (memory / "cli_runs.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        persisted = json.loads(counter.read_text()) if counter.exists() else {"next_run_id": 0}
        next_id = persisted["next_run_id"]
        if type(next_id) is not int or next_id < 0:
            raise ValueError("Invalid CLI run sequence; refusing to overwrite existing Context dumps")
        context = resolve_flex_context_dir(**paths)
        for path in context.glob("Run*_Sub0*.json"):
            match = re.fullmatch(r"Run(\d+)_Sub0(?:\.meta)?\.json", path.name)
            if match:
                next_id = max(next_id, int(match[1]) + 1)
        fd, temporary = tempfile.mkstemp(prefix=".cli-runs-", dir=memory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"next_run_id": next_id + 1}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, counter)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return next_id
