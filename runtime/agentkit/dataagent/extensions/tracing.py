"""Native LangChain instrumentation → LangSmith OTEL conversion → local OTLP JSON."""

import logging
from pathlib import Path

from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, RunTree
from langsmith.schemas import LangSmithInfo
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from dataagent.diagnostics import safe_error
from dataagent.extensions.trace_exporter import LocalFileExporter

logger = logging.getLogger(__name__)


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
                and kwargs.get("name") not in self.agent_names):
            # Internal middleware requests contain live model/runtime objects,
            # not just this invocation's messages. Keep their timing, not reprs.
            result.update(inputs={}, outputs={})
        return result


class LocalLangChainTracer(LangChainTracer):
    """Reuse native callbacks and SDK conversion, with request-scoped file commits."""

    def __init__(self, directory: Path | None = None, secrets: tuple[str, ...] = (),
                 agent_names: tuple[str, ...] = (), **kwargs):
        if kwargs.get("client") is None:
            self.exporter = LocalFileExporter(directory, secrets)
            self.provider = TracerProvider(
                resource=Resource({"service.name": "dataagent"}), sampler=ALWAYS_ON,
                shutdown_on_exit=False,
            )
            self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
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
        super().__init__(**kwargs)
        self.directory = directory
        self.secrets = secrets
        self.agent_names = agent_names

    def copy_with_metadata_defaults(self, *, metadata=None, tags=None):
        clone = super().copy_with_metadata_defaults(metadata=metadata, tags=tags)
        clone.directory = self.directory
        clone.secrets = self.secrets
        clone.agent_names = self.agent_names
        clone.exporter = self.exporter
        clone.provider = self.provider
        return clone

    def on_llm_new_token(self, token, **kwargs) -> None:
        """Full model messages are exported; per-token events are not retained."""

    def _persist_run(self, run: RunTree) -> None:
        # BaseTracer calls this BEFORE the native root PATCH callback. Wait until
        # _update_run_single below to flush, rather than writing an incomplete file.
        pass

    def _update_run_single(self, run: RunTree) -> None:
        super()._update_run_single(run)
        if run.parent_run_id is not None:
            return
        try:
            if run.extra.get("__disabled"):
                return
            self.client.flush()
            self.provider.force_flush()
            self.exporter.finish(str(run.trace_id), str(run.id))
        except Exception as error:
            logger.warning("TRACE_SAVE_ERROR run=%s message=%s", run.id,
                           safe_error(error, self.secrets)["message"])
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
