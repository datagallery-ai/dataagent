# Evaluate runs with traces

A DataAgent trace captures execution steps, timing, and available inputs and outputs. You can [export ended spans live](trace-export.md) or use [the OTLP JSON in DataAgent Home](../../development/architecture/execution-traces.md#local-files) to evaluate earlier runs. Define what to evaluate, which fields to use, and how to score them. Ingesting spans makes the run available for inspection. Scoring runs in a configured evaluator or your own script.

## Extract a sample from a local file

Find `runtime/users/<user>/sessions/<thread>/traces/<native_run_id>.json` in the backend or SDK Home. Use `traceId` and `spanId` to identify a sample and `parentSpanId` to find its parent step. Use the root span for an end-to-end evaluation, or select child spans with `langsmith.span.kind=llm` to inspect individual model calls.

This Python example prints the root span's identifiers, status, duration, and available input and output. Replace `/path/to/run.json` with an actual trace file. Review the extracted fields locally if they may contain sensitive data.

```bash
python3 - /path/to/run.json <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

def structured_value(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value

for resource in data["resourceSpans"]:
    for scope in resource.get("scopeSpans", []):
        for span in scope.get("spans", []):
            if span.get("parentSpanId"):
                continue
            attrs = {item["key"]: item["value"] for item in span.get("attributes", [])}
            prompt = attrs.get("gen_ai.prompt", {}).get("stringValue")
            completion = attrs.get("gen_ai.completion", {}).get("stringValue")
            record = {
                "trace_id": span["traceId"],
                "span_id": span["spanId"],
                "name": span["name"],
                "status_code": span.get("status", {}).get("code", 0),
                "duration_ms": (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1_000_000,
                "input": structured_value(prompt),
                "output": structured_value(completion),
            }
            print(json.dumps(record, ensure_ascii=False))
PY
```

Use the JSON line to build an evaluation sample. Extract the user question and final answer from the actual `input` and `output` structures, and join expected outcomes or reference answers from your test set by case ID. For the [SQLite tutorial](sqlite-tutorial.md), use the trace to inspect routing, status, and latency, then check the generated CSV for the three expected totals. SQL text and a successful tool call alone cannot establish the query result. Store datasets and scores separately and retain the Trace ID or Span ID for investigation.

Span attributes come from the runtime's LangChain/LangSmith conversion. Extract inputs, outputs, model names, and token counts when available. Leave values absent when the call was interrupted, omitted, or did not provide them. Inspect one real run to find where the user question, answer, model call, and tool calls appear before choosing the scope of evaluation. Review what can be shared if samples contain prompts, conversation history, tool results, or business data. See [trace data boundaries](../../development/architecture/execution-traces.md#trace-data-boundaries).

## Connect an evaluation platform

After a trace arrives through the [receiver you configured](trace-export.md#platform-receivers), inspect an actual ingested record to map its fields. Model, input, output, and usage views may require OpenInference, OpenTelemetry GenAI, or vendor attributes.

| Platform | Evaluate and write results |
| --- | --- |
| Arize Phoenix | Query with `client.spans.get_spans_dataframe`, map fields to evaluator columns, and write annotations by Span ID with `client.spans.log_span_annotations_dataframe`. [Evaluate traces](https://arize.com/docs/phoenix/tracing/how-to-tracing/feedback-and-annotations/evaluating-phoenix-traces) |
| Langfuse | Map inputs, outputs, and context in an LLM-as-a-Judge evaluator, or compute scores in your script and write them by Trace ID. To score one observation, also provide its Observation ID. [Scoring API](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk) |
| Opik | Configure a trace or span evaluation rule, select its inputs, and inspect the feedback score. To evaluate an existing trace, select it in Traces and run an evaluation rule. [Evaluation rules](https://www.comet.com/docs/opik/production/online-evaluation/rules) |

When writing scores, use the IDs observed in the evaluation platform or match IDs from the local file to the imported record. To correlate a live trace with an imported file for the same run, use native identifiers such as `langsmith.metadata.dataagent_run_id`. OTLP IDs and parent relationships may differ, so the filename cannot reliably identify a live trace by its OTLP Trace ID.

## Import a historical trace into a receiver

See [import a historical trace](trace-export.md#import-json) for the file upload command, HTTP/JSON compatibility, and Collector forwarding.
