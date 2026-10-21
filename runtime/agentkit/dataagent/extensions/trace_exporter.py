"""Write completed OpenTelemetry traces as OTLP JSON, without a collector."""

import base64
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from threading import Lock
from uuid import UUID

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        return re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, secrets) for item in value)
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


def _redact_attributes(attributes, secrets: tuple[str, ...]):
    if not attributes:
        return attributes
    return {_redact(key, secrets): _redact(item, secrets) for key, item in attributes.items()}


class _RedactedEvent:
    """Minimal Event surface for OTLP encoding with redacted fields."""

    __slots__ = ("name", "timestamp", "attributes", "dropped_attributes")

    def __init__(self, event, secrets: tuple[str, ...]):
        self.name = _redact(event.name, secrets)
        self.timestamp = event.timestamp
        self.attributes = _redact_attributes(event.attributes, secrets)
        self.dropped_attributes = event.dropped_attributes


class _RedactedLink:
    """Minimal Link surface for OTLP encoding with redacted attributes."""

    __slots__ = ("context", "attributes", "dropped_attributes")

    def __init__(self, link, secrets: tuple[str, ...]):
        self.context = link.context
        self.attributes = _redact_attributes(link.attributes, secrets)
        self.dropped_attributes = link.dropped_attributes


class _RedactedReadableSpan:
    """Proxy ReadableSpan so live OTLP export applies the same redaction as finish()."""

    __slots__ = ("_span", "_secrets")

    def __init__(self, span, secrets: tuple[str, ...]):
        object.__setattr__(self, "_span", span)
        object.__setattr__(self, "_secrets", secrets)

    def __getattr__(self, name):
        return getattr(self._span, name)

    def get_span_context(self):
        return self._span.get_span_context()

    @property
    def name(self):
        return _redact(self._span.name, self._secrets)

    @property
    def attributes(self):
        return _redact_attributes(self._span.attributes, self._secrets)

    @property
    def events(self):
        events = self._span.events
        if not events:
            return events
        return tuple(_RedactedEvent(event, self._secrets) for event in events)

    @property
    def links(self):
        links = self._span.links
        if not links:
            return links
        return tuple(_RedactedLink(link, self._secrets) for link in links)

    @property
    def status(self):
        from opentelemetry.trace import Status

        status = self._span.status
        if status is None or not status.description:
            return status
        return Status(
            status_code=status.status_code,
            description=_redact(status.description, self._secrets),
        )


class RedactingSpanExporter(SpanExporter):
    """Apply LocalFileExporter redaction rules before forwarding to another exporter."""

    def __init__(self, wrapped: SpanExporter, secrets: tuple[str, ...] = ()):
        self._wrapped = wrapped
        self.secrets = secrets

    def export(self, spans):
        return self._wrapped.export(
            tuple(_RedactedReadableSpan(span, self.secrets) for span in spans)
        )

    def shutdown(self):
        return self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self._wrapped.force_flush(timeout_millis)


class LocalFileExporter(SpanExporter):
    """Buffer ended spans; the root callback flushes the SDK before calling finish.

    force_flush cannot finish files: another request may still be running on this
    same graph. Only finish(trace_id, ...) commits that invocation's spans.
    """

    def __init__(self, directory: Path, secrets: tuple[str, ...] = ()):
        self.directory = directory
        self.secrets = secrets
        self._spans = defaultdict(list)
        self._lock = Lock()

    def export(self, spans):
        with self._lock:
            for span in spans:
                trace_id = span.attributes["langsmith.metadata.dataagent_trace_id"]
                self._spans[trace_id].append(span)
        return SpanExportResult.SUCCESS

    def finish(self, trace_id: str, run_id: str):
        with self._lock:
            spans = self._spans.pop(trace_id, [])
        if not spans:
            raise ValueError("No completed spans were exported for the trace")
        data = MessageToDict(encode_spans(spans), use_integers_for_enums=True)
        records = [span for resource in data["resourceSpans"]
                   for scope in resource["scopeSpans"] for span in scope["spans"]]
        # OTLP JSON deviates from protobuf JSON: IDs are hex, not base64. Keep
        # attribute bytesValue fields in base64, as required by OTLP.
        for span in records:
            for record in (span, *span.get("links", [])):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if record.get(key):
                        record[key] = base64.b64decode(record[key]).hex()
        # SDK 0.12.5 can use a random trace ID / synthetic root parent. Reconcile
        # native run identities so batching and ambient OTel context cannot split
        # one invocation or leave children pointing at nonexistent parents.
        attrs = [{item["key"]: item["value"].get("stringValue")
                  for item in span.get("attributes", [])} for span in records]
        run_ids = {attr["langsmith.metadata.dataagent_run_id"]: span["spanId"]
                   for span, attr in zip(records, attrs)}
        for span, attr in zip(records, attrs):
            span["traceId"] = UUID(trace_id).hex
            parent = attr["langsmith.metadata.dataagent_parent_run_id"]
            if parent not in run_ids:
                # enabled="local" can omit @traceable middleware spans. Link to
                # the closest exported ancestor from the native dotted order.
                ancestors = attr["langsmith.metadata.dataagent_dotted_order"].split(".")[:-1]
                parent = next((part[-36:] for part in reversed(ancestors)
                               if part[-36:] in run_ids), None)
            if parent in run_ids:
                span["parentSpanId"] = run_ids[parent]
            else:
                span.pop("parentSpanId", None)
            # SDK 0.12.5 sets OK when a span starts; OTel then disallows changing
            # that status to ERROR on PATCH. Keep the actual final run error.
            if error := attr.get("langsmith.metadata.dataagent_error"):
                span["status"] = {"code": 2, "message": error}
        self._write(self.directory / f"{run_id}.json", _redact(data, self.secrets))

    @staticmethod
    def _write(path: Path, data: dict):
        # Publish only complete JSON; hard-link creation refuses overwrites.
        # mkstemp uses 0600, and the temporary file lives on the same filesystem.
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".trace-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.link(temporary, path)
        finally:
            os.unlink(temporary)

    def shutdown(self):
        with self._lock:
            self._spans.clear()
