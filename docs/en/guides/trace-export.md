# Export traces to OpenTelemetry

DataAgent writes a local OTLP JSON file when an agent run ends. With a receiver URL configured, DataAgent also sends Protobuf spans over OTLP/HTTP as they end. The local trace file is still written. This exporter sends run traces. Logs are written to local log files.

If you already have an OTLP/HTTP collector or observability backend, go straight to [Configure DataAgent](#configure-dataagent). For a local smoke check, start a collector first.

<a name="local-collector"></a>

## Start a local collector

Create `otel-collector.yaml`. This configuration enables an HTTP receiver and a trace pipeline. It prints the received spans to the collector output. Use a synthetic question for the smoke check because detailed output includes span attributes with application content.

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
```

Run the collector from the same directory. The published port is bound to loopback.

```bash
docker run --rm --name dataagent-otel-collector \
  -p 127.0.0.1:4318:4318 \
  -v "$PWD/otel-collector.yaml:/etc/otelcol/config.yaml:ro" \
  otel/opentelemetry-collector:0.166.0
```

See the [OpenTelemetry Collector documentation](https://opentelemetry.io/docs/collector/install/docker/) for other installation and deployment options.

<a name="configure-dataagent"></a>

## Configure DataAgent

Set the full trace receiver URL in the **DataAgent backend process environment**, then start or restart the backend. This example assumes DataAgent and the collector above run on the same host.

```bash
export DATAAGENT_OTLP_TRACES_ENDPOINT='http://127.0.0.1:4318/v1/traces'
dataagent serve
```

For source deployments you can put the settings in the repository root `.env` read by `npm run start` or `./deploy.sh`. For a standalone server, set them in the process environment. The backend's Home `.env` and `dataagent serve --env-file` do not set these process variables. If Web or TUI connects to an existing server, configure that server. See [Command-line options and environment variables](../reference/cli-and-environment.md).

DataAgent uses the URL as given, so include `/v1/traces` or the full path specified by your receiver. Collector HTTP commonly listens on `4318`, while `4317` is typically used for gRPC. Setting `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` alone does not enable export.

When your receiver requires authentication, use `DATAAGENT_OTLP_HEADERS` to supply one or more comma-separated `key=value` headers. For a receiver that expects a Bearer token, use the following format.

```bash
export DATAAGENT_OTLP_HEADERS='authorization=Bearer replace-with-your-token'
```

<a name="verify"></a>

## Verify and troubleshoot

1. Send a synthetic question and let the response finish. In the collector output or receiver UI, find the `service.name=dataagent` resource and confirm the run's spans arrived.
2. Find the run's local JSON file under `<home>/runtime/users/<user>/sessions/<thread>/traces/`. Match the `langsmith.metadata.dataagent_trace_id` span attribute across both sinks. The local file reconciles trace IDs and parent links using native run IDs, so the receiver's raw OTLP IDs can differ.
3. If you have only a local file, check the backend process environment, receiver URL, protocol, credentials, and network. `OTLP live export enabled` in the backend log confirms exporter initialization. Verify span arrival separately. For `OTLP_EXPORTER_INIT_ERROR`, check the exporter configuration. Correct malformed headers to use comma-separated `key=value` entries and try again.

<a name="platform-receivers"></a>

## Choose an evaluation platform receiver

| Platform | Ingest and verify |
| --- | --- |
| Arize Phoenix | A common self-hosted HTTP Protobuf endpoint is `http://127.0.0.1:6006/v1/traces`. Supply `authorization` if the deployment requires it. You can specify an ingestion project with `x-project-name`. [Receiver guide](https://arize.com/docs/phoenix/deployment/authentication) |
| Langfuse | Use `/api/public/otel/v1/traces` on the correct region or self-hosted instance. It accepts HTTP Protobuf and OTLP JSON. Use Basic authentication with the project public and secret keys. Follow the platform guide for the `x-langfuse-ingestion-version: 4` header on its v4 ingestion path. [Receiver guide](https://langfuse.com/integrations/native/opentelemetry) |
| Opik | Use the full HTTP traces URL for your instance. The cloud example is `https://www.comet.com/opik/api/v1/private/otel/v1/traces`. Set `Authorization`, `projectName`, and `Comet-Workspace` as required by your instance. Check the root span and mapped input and output in the project. [Receiver guide](https://www.comet.com/docs/opik/integrations/opentelemetry) |

For a Langfuse Cloud project using v4 ingestion, load its public and secret keys into the environment and set the following variables. Use the project host for other regions or self-hosted instances.

```bash
langfuse_basic=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 | tr -d '\r\n')
export DATAAGENT_OTLP_TRACES_ENDPOINT='https://cloud.langfuse.com/api/public/otel/v1/traces'
export DATAAGENT_OTLP_HEADERS="Authorization=Basic ${langfuse_basic},x-langfuse-ingestion-version=4"
```

<a name="import-json"></a>

## Import a historical trace into a receiver

To preserve spans, parent relationships, and timestamps, send the entire file to an **OTLP/HTTP JSON** receiver. The following example uses a [Collector with OTLP HTTP enabled](#local-collector), which can forward to a platform that accepts only Protobuf.

```bash
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary '@/path/to/run.json' \
  http://127.0.0.1:4318/v1/traces
```

Check that the response reports no rejected spans. Then look for the file's `traceId` and original time range downstream. A successful Collector response confirms receipt at the entry point. Verify forwarding or downstream storage separately. For a binary Protobuf-only endpoint such as Phoenix's HTTP receiver, use a Collector or convert the encoding. [Collector receiver](https://github.com/open-telemetry/opentelemetry-collector/blob/main/receiver/otlpreceiver/README.md#writing-with-httpjson) · [OTLP JSON specification](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding) · [Phoenix ingestion implementation](https://github.com/Arize-ai/phoenix/blob/main/src/phoenix/server/api/routers/v1/traces.py)

Langfuse accepts OTLP JSON directly. Replace the receiver URL with the project's `/api/public/otel/v1/traces` URL and add Basic authentication and any applicable ingestion-version header from the table above. For Opik, use the Collector forwarding pattern below with the Opik URL and headers. [Langfuse JSON ingestion example](https://langfuse.com/docs/observability/get-started)

For Phoenix, add `otlp_http/phoenix` under the existing Collector `exporters` and include it in `service.pipelines.traces.exporters`. Use an address reachable from the Collector. Set headers on that exporter when the destination requires authentication.

```yaml
exporters:
  otlp_http/phoenix:
    traces_endpoint: https://phoenix.example.com/v1/traces
    encoding: proto
service:
  pipelines:
    traces:
      exporters: [debug, otlp_http/phoenix]
```

[Collector HTTP exporter configuration](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)

Confirm the destination project, credentials, and retention before importing. Test one trace without sensitive data first. For a bulk import, deduplicate by file and by `traceId` and `spanId`. Take particular care with runs that were already sent live so they do not become a second set of evaluation samples. Importing sends recorded telemetry without rerunning the Agent or its tools.

To extract samples, map fields, or save scores, see [Evaluate runs with traces](trace-evaluation.md).
