"""Native LangChain instrumentation → LangSmith OTEL conversion → local OTLP JSON.

When ``DATAAGENT_OTLP_TRACES_ENDPOINT`` (or config ``otlp_endpoint``) is set, ended
spans are also pushed live via OTLP/HTTP in addition to the completion-time file.
Live OTLP export applies the same secret redaction as the local JSON file.
Models inside ordinary tools are omitted from both sinks; agent and subagent
model calls remain visible, as do tool results, errors and total duration.
"""

import logging
import os
from pathlib import Path

from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, RunTree
from langsmith.schemas import LangSmithInfo
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from dataagent.diagnostics import log_event, safe_error
from dataagent.extensions.trace_exporter import LocalFileExporter, RedactingSpanExporter

logger = logging.getLogger(__name__)

# LangSmith's Client.create_run() calls `git describe` via subprocess with inherited
# stdin. A closed or piped stdin makes git block on the asyncio main thread, so the
# run never reaches the model. Setting an explicit revision skips that subprocess.
os.environ.setdefault("LANGCHAIN_REVISION_ID", "local")

# This provider does not consult OTEL_EXPORTER_OTLP_*. Live export has one
# process entry: DATAAGENT_OTLP_TRACES_ENDPOINT, overridden by the explicit argument.
_OTLP_ENDPOINT_ENV = "DATAAGENT_OTLP_TRACES_ENDPOINT"
_OTLP_HEADERS_ENV = "DATAAGENT_OTLP_HEADERS"


def resolve_otlp_endpoint(explicit: str | None = None) -> str | None:
    """Return a non-empty OTLP/HTTP traces endpoint, or None to disable live export.

    The value is the full traces URL. A collector base URL is not rewritten.
    """
    for candidate in (explicit, os.environ.get(_OTLP_ENDPOINT_ENV)):
        if candidate and (value := candidate.strip()):
            return value
    return None


def _parse_otlp_headers(raw: str | None = None) -> dict[str, str] | None:
    text = (raw if raw is not None else os.environ.get(_OTLP_HEADERS_ENV) or "").strip()
    if not text:
        return None
    headers: dict[str, str] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"Invalid {_OTLP_HEADERS_ENV} entry (expected key=value): {part!r}")
        key, value = part.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers or None


def _build_otlp_exporter(endpoint: str, headers: dict[str, str] | None = None) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint, headers=headers)


def _is_agent_run(name, extra, agent_names) -> bool:
    """Recognize declared entries and native Agent roots, not their internal nodes."""
    metadata = (extra or {}).get("metadata") or {}
    return name in agent_names or bool(name and name == metadata.get("lc_agent_name"))


class _LocalOTELClient(Client):
    def __init__(self, *args, agent_names=(), **kwargs):
        self.agent_names = agent_names
        super().__init__(*args, **kwargs)

    def create_run(self, *args, **kwargs):
        super().create_run(*args, **self._with_identity(kwargs["id"], kwargs))

    def update_run(self, run_id, **kwargs):
        super().update_run(run_id, **self._with_identity(run_id, kwargs))

    def _with_identity(self, run_id, kwargs):
        # Keep native run identities: SDK-generated OTel IDs are not RunTree IDs.
        extra = dict(kwargs.get("extra") or {})
        extra["metadata"] = {
            **extra.get("metadata", {}),
            "dataagent_trace_id": str(kwargs["trace_id"]),
            "dataagent_run_id": str(run_id),
            "dataagent_parent_run_id": str(kwargs.get("parent_run_id") or ""),
            "dataagent_dotted_order": kwargs["dotted_order"],
            "dataagent_error": kwargs.get("error") or "",
        }
        result = {**kwargs, "extra": extra}
        if (kwargs.get("run_type") == "chain" and kwargs.get("parent_run_id")
                and not _is_agent_run(kwargs.get("name"), extra, self.agent_names)):
            # Internal middleware requests contain live model/runtime objects,
            # not just this invocation's messages. Keep their timing, not reprs.
            result.update(inputs={}, outputs={})
        return result


class LocalLangChainTracer(LangChainTracer):
    """Reuse native callbacks and SDK conversion, with request-scoped file commits.

    Optional live OTLP/HTTP export: pass ``otlp_endpoint`` or set
    ``DATAAGENT_OTLP_TRACES_ENDPOINT``. Ended spans are pushed immediately via
    ``SimpleSpanProcessor``; the local JSON file is still written on root finish.
    """

    def __init__(
        self,
        directory: Path | None = None,
        secrets: tuple[str, ...] = (),
        agent_names: tuple[str, ...] = (),
        *,
        otlp_endpoint: str | None = None,
        otlp_headers: dict[str, str] | str | None = None,
        **kwargs,
    ):
        if kwargs.get("client") is None:
            self.exporter = LocalFileExporter(directory, secrets)
            self.provider = TracerProvider(
                resource=Resource({"service.name": "dataagent"}), sampler=ALWAYS_ON,
                shutdown_on_exit=False,
            )
            self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
            endpoint = resolve_otlp_endpoint(otlp_endpoint)
            self.otlp_exporter: SpanExporter | None = None
            if endpoint is not None:
                if isinstance(otlp_headers, str):
                    headers = _parse_otlp_headers(otlp_headers)
                elif otlp_headers is not None:
                    headers = dict(otlp_headers)
                else:
                    headers = _parse_otlp_headers()
                try:
                    remote = _build_otlp_exporter(endpoint, headers)
                    self.otlp_exporter = RedactingSpanExporter(remote, secrets)
                    self.provider.add_span_processor(SimpleSpanProcessor(self.otlp_exporter))
                    logger.info("OTLP live export enabled endpoint=%s", endpoint)
                except Exception as error:
                    logger.warning(
                        "OTLP_EXPORTER_INIT_ERROR endpoint=%s message=%s",
                        endpoint,
                        safe_error(error, secrets)["message"],
                    )
                    self.otlp_exporter = None
            kwargs["client"] = _LocalOTELClient(
                agent_names=agent_names,
                api_url="http://localhost", tracing_mode="otel",
                otel_tracer_provider=self.provider, tracing_sampling_rate=1,
                # Preserve SDK operation order even for large concurrent traces.
                info=LangSmithInfo(batch_ingest_config={
                    "size_limit": 100, "scale_up_nthreads_limit": 0,
                    "scale_up_qsize_trigger": 1000, "scale_down_nempty_trigger": 4,
                }),
            )
        else:
            self.otlp_exporter = None
        super().__init__(**kwargs)
        self.directory = directory
        self.secrets = secrets
        self.agent_names = agent_names
        if not hasattr(self, "otlp_exporter"):
            self.otlp_exporter = None

    def copy_with_metadata_defaults(self, *, metadata=None, tags=None):
        clone = super().copy_with_metadata_defaults(metadata=metadata, tags=tags)
        clone.directory = self.directory
        clone.secrets = self.secrets
        clone.agent_names = self.agent_names
        clone.exporter = self.exporter
        clone.provider = self.provider
        clone.otlp_exporter = self.otlp_exporter
        return clone

    def on_llm_new_token(self, token, **kwargs) -> None:
        """Full model messages are exported; per-token events are not retained."""

    def _start_trace(self, run: RunTree) -> None:
        super()._start_trace(run)
        if run.run_type != "llm":
            return
        parent = self.run_map.get(str(run.parent_run_id))
        while parent is not None:
            # Native Agent metadata also covers automatic and nested subagents.
            if parent.run_type == "chain" and _is_agent_run(parent.name, parent.extra, self.agent_names):
                return
            if parent.run_type == "tool":
                # langchain-core 1.6.1 checks this before both POST and PATCH. Keep
                # native bookkeeping, but never export this model to either sink.
                run.extra["__disabled"] = True
                return
            parent = self.run_map.get(str(parent.parent_run_id))

    def _persist_run(self, run: RunTree) -> None:
        # agenerate() can skip on_llm_error when its gather is cancelled. Close
        # existing model runs before the root PATCH; file commit still happens
        # in _update_run_single, after all patches have been flushed.
        if not run.error or run.end_time is None:
            return
        pending = list(run.child_runs)
        while pending:
            child = pending.pop()
            pending.extend(child.child_runs)
            if (child.run_type != "llm" or child.end_time is not None
                    or child.extra.get("__disabled")):
                continue
            try:
                child.end(
                    end_time=run.end_time,
                    error="Model wait interrupted locally when parent execution ended; "
                          "provider outcome unknown.",
                )
                super()._update_run_single(child)
            except Exception as error:
                logger.warning("TRACE_SAVE_ERROR run=%s model_run=%s message=%s", run.id, child.id,
                               safe_error(error, self.secrets)["message"])

    def _update_run_single(self, run: RunTree) -> None:
        super()._update_run_single(run)
        if run.parent_run_id is not None:
            return
        metadata = (run.extra or {}).get("metadata") or {}
        correlation = {
            "request_id": metadata.get("dataagent_request_id"),
            "task_id": metadata.get("dataagent_a2a_task_id"),
            "message_id": metadata.get("dataagent_a2a_message_id"),
            "context_id": metadata.get("thread_id"),
            "root_run_id": metadata.get("dataagent_root_run_id"),
            "native_run_id": str(run.id), "trace_id": str(run.trace_id),
        }
        try:
            if run.extra.get("__disabled"):
                return
            self.client.flush()
            self.provider.force_flush()
            self.exporter.finish(str(run.trace_id), str(run.id))
            if correlation["task_id"]:
                log_event(logger, "a2a.trace.saved", secrets=self.secrets, **correlation,
                          trace_file=str(self.directory / f"{run.id}.json"))
        except Exception as error:
            logger.warning("TRACE_SAVE_ERROR run=%s message=%s", run.id,
                           safe_error(error, self.secrets)["message"])
            if correlation["task_id"]:
                log_event(logger, "a2a.trace.failed", secrets=self.secrets, level=logging.WARNING,
                          **correlation, **safe_error(error, self.secrets))
        finally:
            pending = [run]
            while pending:
                completed = pending.pop()
                self.order_map.pop(completed.id, None)
                self.run_map.pop(str(completed.id), None)
                # SDK 0.12.5 keeps ended spans when POST/PATCH were merged by its
                # worker. Release this root's entries after the queue has drained.
                self.client.otel_exporter._span_info.pop(completed.id)
                pending.extend(completed.child_runs)
