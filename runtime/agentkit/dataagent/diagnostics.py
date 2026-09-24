"""Credential-redacting, terminal-safe error rendering used by every layer and host."""

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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
    return {"code": type(error).__name__, "message": message[:1500]}
