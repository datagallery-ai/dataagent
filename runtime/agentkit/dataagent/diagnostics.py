"""Credential-redacting, terminal-safe error rendering used by every layer and host."""

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Public error codes for RUN_ERROR and safe_error. Clients may branch on these values.
ERROR_CODES = frozenset({
    "TIMEOUT",
    "CANCELLED",
    "LOOP_DETECTED",
    "LIMIT_EXCEEDED",
    "INCOMPLETE_STREAM",
    "CONFIG_ERROR",
    "MODEL_ERROR",
    "TOOL_ERROR",
    "INTERNAL_ERROR",
})
_LIMIT_ERRORS = frozenset({"ModelCallLimitExceededError", "ToolCallLimitExceededError"})
_MODEL_MODULES = ("openai", "anthropic")


def _own_code(error: BaseException) -> str | None:
    declared = getattr(error, "error_code", None)
    if isinstance(declared, str) and declared in ERROR_CODES:
        return declared
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if type(error).__name__ in _LIMIT_ERRORS:
        return "LIMIT_EXCEEDED"
    if type(error).__module__.split(".", 1)[0] in _MODEL_MODULES:
        # Model SDK timeouts do not subclass the builtin TimeoutError.
        return "TIMEOUT" if type(error).__name__ == "APITimeoutError" else "MODEL_ERROR"
    return None


def error_code(error: BaseException) -> str:
    """Map an exception chain to one public error code; unknown errors are INTERNAL_ERROR."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited and len(visited) < 8:
        visited.add(id(current))
        code = _own_code(current)
        if code is not None:
            return code
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return "INTERNAL_ERROR"


def safe_error(error: BaseException, secrets: tuple[str, ...] = ()) -> dict[str, str]:
    """Render an exception chain without credentials or terminal control sequences.

    Paths and extension names flow into messages verbatim, so escape sequences are
    stripped as well as secrets: a directory name must not be able to repaint a terminal.
    """
    message = str(error) or type(error).__name__
    current = error
    visited = {id(current)}
    while len(visited) < 8:
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        if cause is None or id(cause) in visited:
            break
        visited.add(id(cause))
        # Empty transport errors still have useful types (e.g. ReadError).
        message += f" <- {type(cause).__name__}" + (f": {cause}" if str(cause) else "")
        current = cause
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", message)
    message = _CONTROL.sub("", _ANSI.sub("", message))
    return {"code": error_code(error), "message": message[:1500]}
