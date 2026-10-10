"""Suite-specific safeguards for the shared file-writing tool."""

import fnmatch
from pathlib import Path

from dataagent.actions.tools.hooks.base import ToolHookInvocation, ToolPreHookOutcome


def require_nl2sql_for_new_insert(inv: ToolHookInvocation) -> ToolPreHookOutcome:
    """Require the NL2SQL subagent to create an INSERT script before manual edits."""
    path = str(inv.tool_args.get("path") or "").strip()
    if fnmatch.fnmatchcase(Path(path).name, "insert_*.sql") and not Path(path).is_file():
        raise ValueError(
            "insert_*.sql files must be created by the nl2sql subagent first. "
            "Once created, they can be modified with write_file or edit_file."
        )
    return ToolPreHookOutcome()
