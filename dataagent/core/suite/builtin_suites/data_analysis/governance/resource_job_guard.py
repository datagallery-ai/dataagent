from __future__ import annotations

import re
import shlex
from pathlib import PurePath
from typing import Any

from dataagent.governance import GovernanceInvocation

SUBMIT_RESOURCE_JOB_TOOL = "submit_resource_job"
_PYTHON_JOB_TYPES = frozenset({"py", "python", "python_script", "python-script"})
_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "|", "||"})
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh"})
_PYTHON_EXECUTABLE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+(?:\.\d+)*)?)(?:\.exe)?$", re.IGNORECASE)


def data_analysis_forbid_external_python_policy(inv: GovernanceInvocation) -> None:
    """Reject Python execution submitted to an external resource job.

    Local ``bash`` calls are intentionally outside this policy. The policy only
    protects the boundary where a command or script artifact is handed to a
    resource backend such as the ClickHouse MCP service.
    """
    if str(inv.tool_name or "").strip() != SUBMIT_RESOURCE_JOB_TOOL:
        return None
    if str(getattr(inv.runtime, "hierarchy", "") or "").upper() != "SUB":
        return None

    args = inv.tool_args if isinstance(inv.tool_args, dict) else {}
    reason = _python_submission_reason(args)
    if reason is None:
        return None
    raise ValueError(
        "DataAnalysis forbids executing Python through submit_resource_job "
        f"({reason}). Use ClickHouse SQL with task_type='sql_query', or run approved "
        "local Python operators through bash."
    )


def _python_submission_reason(args: dict[str, Any]) -> str | None:
    job_type = str(args.get("task_type") or "").strip().lower().replace("-", "_")
    if job_type in _PYTHON_JOB_TYPES:
        return f"resource job type is `{job_type}`"

    if _script_artifact_is_python(args.get("script_artifact")):
        return "script_artifact is a Python script"

    command = str(args.get("command") or "").strip()
    if command and _command_executes_python(command):
        return "command invokes Python"
    return None


def _script_artifact_is_python(raw_artifact: Any) -> bool:
    if not isinstance(raw_artifact, dict):
        return False
    for key in ("type", "language"):
        value = str(raw_artifact.get(key) or "").strip().lower().replace("-", "_")
        if value in _PYTHON_JOB_TYPES:
            return True
    for key in ("path", "name", "uri"):
        value = str(raw_artifact.get(key) or "").strip()
        if _is_python_script_path(value):
            return True
    return False


def _command_executes_python(command: str) -> bool:
    tokens = _shell_tokens(command)
    if tokens is None:
        return _looks_like_python_command(command)
    return _tokens_execute_python(tokens)


def _shell_tokens(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _tokens_execute_python(tokens: list[str]) -> bool:
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SHELL_CONTROL_TOKENS:
            if _segment_executes_python(segment):
                return True
            segment = []
            continue
        segment.append(token)
    return False


def _segment_executes_python(tokens: list[str]) -> bool:
    if not tokens:
        return False
    executable_index = _executable_index(tokens)
    if executable_index is None:
        return False
    executable = tokens[executable_index]
    if _is_python_executable(executable) or _is_python_script_path(executable):
        return True

    base_name = PurePath(executable).name.lower()
    if base_name == "uv" and executable_index + 2 < len(tokens) and tokens[executable_index + 1] == "run":
        return _is_python_executable(tokens[executable_index + 2])
    if base_name in _SHELL_WRAPPERS:
        return _shell_wrapper_executes_python(tokens[executable_index + 1 :])
    return False


def _executable_index(tokens: list[str]) -> int | None:
    index = 0
    while index < len(tokens) and _is_environment_assignment(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None
    if PurePath(tokens[index]).name.lower() == "env":
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token.startswith("-") or _is_environment_assignment(token):
                index += 1
                continue
            break
    if index < len(tokens) and PurePath(tokens[index]).name.lower() == "command":
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
    return index if index < len(tokens) else None


def _shell_wrapper_executes_python(arguments: list[str]) -> bool:
    for index, argument in enumerate(arguments):
        if argument == "-c" and index + 1 < len(arguments):
            return _command_executes_python(arguments[index + 1])
    return False


def _is_environment_assignment(token: str) -> bool:
    name, separator, _ = str(token or "").partition("=")
    return bool(separator and name and name.replace("_", "a").isalnum() and not name[0].isdigit())


def _is_python_executable(token: str) -> bool:
    return bool(_PYTHON_EXECUTABLE.fullmatch(PurePath(str(token or "")).name))


def _is_python_script_path(value: str) -> bool:
    candidate = str(value or "").split("?", 1)[0].split("#", 1)[0]
    return PurePath(candidate).suffix.lower() == ".py"


def _looks_like_python_command(command: str) -> bool:
    """Conservative fallback for malformed shell input.

    The fallback intentionally only checks command-leading executable forms, so
    SQL text such as ``SELECT 'python'`` remains valid.
    """
    leading = command.lstrip()
    return bool(
        re.match(
            r"(?i)^(?:(?:env|command)\s+)*(?:[./\w-]+/)?(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+(?:\.\d+)*)?)(?:\.exe)?(?:\s|$)",
            leading,
        )
        or re.match(r"(?i)^(?:\.?/?[^\s]+\.py)(?:\s|$)", leading)
    )
