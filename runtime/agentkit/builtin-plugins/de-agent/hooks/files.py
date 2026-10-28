"""Return DDL diagnostics through the native filesystem tool result."""

import json
from dataclasses import replace
from pathlib import Path

from langchain_core.messages import ToolMessage
from langgraph.types import Command
from utils.checks import _validate_ddl_comprehensive, validate_sql_syntax
from utils.evidence import output_file


def check_ddl(request, result, *, params):
    """Validate written CREATE artifacts without losing native Command state updates."""
    if isinstance(result, ToolMessage) and result.status == "error":
        return None
    value = request.tool_call.get("args", {}).get("file_path", "")
    if not value or not Path(value).name.startswith("create_") or Path(value).suffix != ".sql":
        return None
    path = output_file(request.runtime.context, value)
    if not path.name.startswith("create_") or path.suffix != ".sql" or not path.is_file():
        return None
    errors = _validate_ddl_comprehensive(path)
    if not errors:
        return None
    message = ToolMessage(
        content=json.dumps(
            {"file_written": True, "path": str(path), "ddl_valid": False, "errors": errors}
        ),
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )
    if isinstance(result, Command):
        update = dict(result.update or {})
        messages = update.get("messages", [])
        update["messages"] = [
            message
            if isinstance(item, ToolMessage) and item.tool_call_id == message.tool_call_id
            else item
            for item in messages
        ]
        return replace(result, update=update)
    return message


def guard_insert(request, *, params):
    """Let the NL2SQL result Hook create the first INSERT artifact; allow subsequent edits."""
    value = request.tool_call.get("args", {}).get("file_path", "")
    if not value or not Path(value).name.startswith("insert_") or Path(value).suffix != ".sql":
        return None
    path = output_file(request.runtime.context, value)
    if path.exists():
        return None
    return ToolMessage(
        content="Create the initial INSERT via task(subagent_type='nl2sql'); its result Hook saves the file.",
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )


def check_insert_syntax(request, result, *, params):
    """Report syntax errors after INSERT writes/edits without rolling back the file."""
    if isinstance(result, ToolMessage) and result.status == "error":
        return None
    value = request.tool_call.get("args", {}).get("file_path", "")
    if not value or not Path(value).name.startswith("insert_") or Path(value).suffix != ".sql":
        return None
    path = output_file(request.runtime.context, value)
    if not path.is_file():
        return None
    errors = validate_sql_syntax(path.read_text(encoding="utf-8"))
    if not errors:
        return None
    message = ToolMessage(
        content=json.dumps(
            {"file_written": True, "path": str(path), "sql_valid": False, "errors": errors},
            ensure_ascii=False,
        ),
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )
    if isinstance(result, Command):
        update = dict(result.update or {})
        messages = update.get("messages", [])
        update["messages"] = [
            message
            if isinstance(item, ToolMessage) and item.tool_call_id == message.tool_call_id
            else item
            for item in messages
        ]
        return replace(result, update=update)
    return message
