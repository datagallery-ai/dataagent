"""Credential-redacting, terminal-safe error rendering used by every layer and host."""

import json
import logging
import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SECRET_FIELD = r"(?:(?:api|access|secret|private)[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|password|passwd|secret|token|authorization|cookie|credentials?|signature)"
_SECRET_NAME = re.compile(_SECRET_FIELD + r"$", re.IGNORECASE)
_QUOTED_SECRET = re.compile(
    r"(\b[A-Za-z0-9_-]{0,64}"
    + _SECRET_FIELD
    + r"""["']?\s*[:=]\s*)("(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*')""",
    re.IGNORECASE,
)
_PLAIN_SECRET = re.compile(
    r"(\b[A-Za-z0-9_-]{0,64}" + _SECRET_FIELD + r"""["']?\s*[:=]\s*["']?)([^\s,"'}]+)""",
    re.IGNORECASE,
)
_URL_SECRET = re.compile(
    r"([?&](?:[A-Za-z0-9_-]{0,64}" + _SECRET_FIELD + r"""|key|sig)=)[^&#\s"']+""",
    re.IGNORECASE,
)

# Public error codes for RUN_ERROR and safe_error. Clients may branch on these values.
ERROR_CODES = frozenset(
    {
        "TIMEOUT",
        "CANCELLED",
        "LOOP_DETECTED",
        "LIMIT_EXCEEDED",
        "INCOMPLETE_STREAM",
        "CONFIG_ERROR",
        "MODEL_ERROR",
        "TOOL_ERROR",
        "INTERNAL_ERROR",
    }
)
_LIMIT_ERRORS = frozenset({"ModelCallLimitExceededError", "ToolCallLimitExceededError"})
_MODEL_MODULES = ("openai", "anthropic")


def is_secret_name(name: str) -> bool:
    """Recognize credential fields without treating token limits or keyboard settings as secrets."""
    return _SECRET_NAME.search(name) is not None


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


def redact_log_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    """Redact diagnostic text without dropping lines or truncating stack traces."""
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", _ANSI.sub("", value))
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        if len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
        else:
            # Allow sentence punctuation while preserving word fragments and path segments.
            value = re.sub(
                r"(^|[^\w./\\-])" + re.escape(secret) + r"(?=$|[^\w./\\-]|\.(?![\w/\\]))",
                r"\1[REDACTED]",
                value,
                flags=re.ASCII,
            )
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"(?i)\b(bearer|basic)\s+[^\s\"'<>]+", r"\1 [REDACTED]", value)
    value = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[REDACTED]@", value)
    value = _QUOTED_SECRET.sub(r'\1"[REDACTED]"', value)
    value = _URL_SECRET.sub(r"\1[REDACTED]", value)
    value = _PLAIN_SECRET.sub(r"\1[REDACTED]", value)
    return value


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
    message = _CONTROL.sub("", redact_log_text(message, secrets))
    return {"code": error_code(error), "message": message[:1500]}


def safe_log_fields(value, secrets: tuple[str, ...] = ()):
    """Redact individual diagnostic fields without truncating the whole event."""
    if isinstance(value, str):
        return safe_error(RuntimeError(value), secrets)["message"] if value else ""
    if isinstance(value, dict):
        return {key: safe_log_fields(item, secrets) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe_log_fields(item, secrets) for item in value]
    return value


def log_event(logger, event: str, *, secrets, level=logging.INFO, **fields) -> None:
    """Keep correlation fields readable by both logging handlers and test/host consumers."""
    payload = safe_log_fields({"event": event, **fields}, secrets)
    logger.log(
        level, "%s", json.dumps(payload, ensure_ascii=False), extra={"dataagent_event": payload}
    )
