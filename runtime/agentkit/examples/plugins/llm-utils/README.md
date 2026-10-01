# LLM-backed tool example

This optional external plugin summarizes text using the `fast` model alias from
the host's existing `models` configuration. It does not read configuration files,
API keys or environment variables itself, and does not add a tool `model` field.

## Install and configure

Copy this whole directory, including `.plugin.json`, to `<home>/plugins/llm-utils`.
Add `llm-utils` to `plugins.enabled` and add the following entry to `models` in
`<home>/config.json`, preserving your default model and other configured plugins:

```json
{
  "fast": {
    "provider": "openai",
    "name": "your-fast-model",
    "base_url": "https://your-provider.example/v1",
    "api_key": "$env{FAST_MODEL_API_KEY}",
    "max_retries": 2
  }
}
```

Replace the model/URL placeholders and set `FAST_MODEL_API_KEY` in Home `.env`
or the process environment; do not commit credentials. Secondary model environment
references resolve on first use from the startup snapshot, without rereading files.
An unused model with missing environment values does not prevent startup; using it
raises a clear error. Restart after changing model configuration or credentials.
`create_agent` owns the model context and supplies it on every invocation.
Ask the agent to summarize text with `llm-utils__summarize_text`.

## SDK

```python
from dataagent import create_agent, prepare_runtime

runtime = prepare_runtime()
agent = create_agent(runtime)
result = await agent.ainvoke(
    {"messages": [("user", "Use llm-utils__summarize_text to summarize: ...")]},
)
```

No per-call `context` argument is needed, including after rebuilding a graph for
checkpoint restoration.
`agent.graph` is the original compiled graph for checkpoint and schema APIs.
Low-level callers can use `ModelContext(runtime)`, `build_agent(runtime, context=context)`
and native `graph.ainvoke(..., context=context)` instead. No `.bind()` is used.
`get_model()` selects `dataagent.model.default`; `get_model("fast")` selects
`models.fast`. An unknown alias is an error, never a fallback. Clients are native,
unbound chat models, reused within this graph's context. Additional model aliases
are created lazily; separate graph builds have separate caches. There is no
process-global model cache. REST caches the AgentKit handle and uses `agent.as_agui()`;
it does not create, store separately, or pass model context. The adapter reads the
original graph schema and injects the same context only at execution time.

Inside tools, pass `config=runtime.config` when invoking the model to preserve
native callbacks and trace parentage. Synchronous tools may use `model.invoke`.
Use `with_structured_output` or other native model methods as needed. Internal
model calls do not execute Agent model middleware/Hooks or consume its model-node
call limit. The REST whole-run timeout still covers them; SDK hosts own their
overall timeout. Use a SubAgent for a separate governed multi-step reasoning loop.

Python clients are process-local dependencies, not frontend inputs or checkpoint
state. Remote MCP services cannot receive them and must manage their own models.
