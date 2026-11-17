"""Standard Python logging configuration shared by backend hosts and plugins."""

import json
import logging
import os
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dataagent.diagnostics import is_secret_name, redact_log_text, safe_log_fields

_secrets: set[str] = set()
log_context: ContextVar[dict] = ContextVar("dataagent_log_context", default={})


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        return os.fdopen(fd, self.mode, encoding="utf-8")

    def handleError(self, record):
        # logging's default fallback prints the original, potentially secret-bearing record.
        if not getattr(self, "_warned", False):
            self._warned = True
            try:
                sys.stderr.write("DataAgent: unable to write backend.log\n")
            except OSError:
                pass


def register_log_secrets(values):
    # Keep prior credentials while in-flight runs continue on their original configuration.
    _secrets.update(value for value in values if isinstance(value, str) and value)


def diagnostic_traceback(error: BaseException, secrets=()) -> str:
    return redact_log_text("".join(traceback.format_exception(error)), (*_secrets, *secrets))


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname, "component": record.name,
            **{key: value for key, value in log_context.get().items() if not key.startswith("_")},
            "message": record.getMessage(),
        }
        for key in ("threadId", "runId", "requestId", "stage", "code", "stack"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["stack"] = self.formatException(record.exc_info)
        # Redact before encoding so newlines/quotes in configured secrets still match.
        data = {key: redact_log_text(str(value), tuple(_secrets)) for key, value in data.items()}
        if hasattr(record, "dataagent_event"):
            event = safe_log_fields(record.dataagent_event, tuple(_secrets))
            data.update(event)
            data["message"] = event.get("message", event["event"])
        return json.dumps(data, ensure_ascii=False)


def _file_handler(destination):
    path = Path(destination)
    for directory in (path.parent.parent, path.parent):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    handler = PrivateRotatingFileHandler(path, maxBytes=5_000_000,
                                         backupCount=3, encoding="utf-8")
    path.chmod(0o600)
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(runtime=None):
    register_log_secrets(value for key, value in os.environ.items()
                         if is_secret_name(key))
    if runtime is not None:
        register_log_secrets(runtime.redaction_secrets)
        state = runtime.paths.state_dir
    else:
        raw = os.environ.get("DATAAGENT_HOME", "").strip() or "~/.dataagent"
        state = Path(raw).expanduser() / "runtime"
    formatter = JsonFormatter()
    handlers = []
    file_failed = False
    if os.environ.get("DATAAGENT_LOG_CAPTURE") != "backend":
        try:
            handlers.append(_file_handler(str((state / "logs/backend.log").resolve())))
        except OSError:
            file_failed = True
    stderr = logging.StreamHandler(sys.stderr)
    handlers.append(stderr)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    if file_failed:
        logging.getLogger("dataagent.startup").warning(
            "Unable to initialize backend.log; using stderr", extra={"stage": "logging"},
        )
    if runtime is not None:
        logging.getLogger("dataagent.startup").info("Enabled plugins and sources: %s", runtime.report.plugin_origins)
