# Semantic client

`dataagent/semantic_client/` contains the built-in semantic-service HTTP client. It has no plugin manifest or registration requirement. The package exports `SemanticServiceClient` and `normalize_semantic_base_url`; `DataAgentError` lives in `dataagent/errors.py`.

## Agent configuration

Add an optional `semantic` section alongside `models` and `plugins` in Agent JSON:

```json
{
  "semantic": {
    "base_url": "$env{SEMANTIC_BASE_URL}",
    "username": "$env{SEMANTIC_USERNAME}",
    "password": "$env{SEMANTIC_PASSWORD}",
    "timeout": 30,
    "tls": {
      "verify_server": false
    }
  }
}
```

Omit both credentials for an unauthenticated service. Credentials must be supplied together. Omit `semantic`, or set it to `null`, to run without a semantic client. A configured section requires a nonblank `base_url` and a positive finite timeout.

The normal Home-to-explicit-file merge and `$env{NAME}` expansion apply. Fields merge independently, including credentials and TLS fields. TLS paths are relative to the configuration file that supplied that field, not the process working directory.

TLS supports `verify_server`, `ca_file`, `client_cert`, `client_key`, and `ciphers`. Certificate and key must be supplied together. `verify_server: false` preserves the legacy semantic client's default. For server verification set it to `true`, with an optional CA file; supply certificate and key for client authentication. No `DATAAGENT_OUTBOUND_*` process variables are read or written.

## Host ownership

The REST host creates one optional client for its lifespan, lends it to each Agent's native `ModelContext`, and closes it at shutdown. Root and child tools and Hooks use that context. Sessions retain separate model caches while sharing the semantic HTTP pool; graph eviction and plugin refresh do not close the pool. Semantic configuration changes take effect after restarting the host, like model configuration.

The SDK owns an explicit resource scope:

```python
from pathlib import Path

from dataagent import LaunchOptions, create_agent, prepare_runtime

runtime = prepare_runtime(LaunchOptions(config=Path("config.json")))
with create_agent(runtime) as agent:
    result = agent.invoke({"messages": [{"role": "user", "content": "分析订单数据"}]})
```

`create_agent` creates the configured client once and injects it through the same native context used by PR 473 for models. For asynchronous use, `async with create_agent(...)` closes outside the event loop. Explicit `agent.close()` and `await agent.aclose()` are also supported. Graph assembly failures close newly created clients before propagating the error. If MCP is configured, discover MCP tools with `load_mcp_tools` and pass them to `create_agent`, as before.

A host can pass `create_agent(runtime, semantic_client=client)` to lend an existing client. Closing that Agent never closes the borrowed client: its original owner must do so after all consumers finish. The low-level `build_agent` API requires a `ModelContext(runtime, semantic_client=client)` when semantic configuration is present; pass the same context to native graph invocation. It does not create or own a hidden HTTP pool.

Direct Python callers can use `with SemanticServiceClient(base_url, auth=..., timeout=..., verify=...) as client`. Construction makes no semantic API calls. `close()` rejects new requests, waits for active synchronous requests, and closes the HTTP pool. The client does not implement automatic retries.

## Native context access

Use `runtime.context.semantic_client` in state Hooks and local Python tools. Before/after-tool Hooks use `request.runtime.context.semantic_client`. Hook signatures and JSON declarations remain unchanged; no `dependencies` argument or semantic-client plugin registration is required. Clients are private context attributes, outside messages, checkpoints and model-facing schemas. JSON `params` still receive a per-call copy.

```python
import asyncio


async def handle(state, runtime, *, params):
    """Read semantic context using the host-owned client."""
    client = runtime.context.semantic_client
    if client is None:
        raise ValueError("This Hook requires semantic configuration")
    result = await asyncio.to_thread(client.semantic_retrieve, params.get("query", ""))
    # Translate the result into the component's intended native state update.
    return None
```

Local tools can declare `runtime: ToolRuntime[ModelContext]` and read both `runtime.context.get_model(...)` and `runtime.context.semantic_client`. Native context propagation carries these references to child Agents as well. Synchronous clients must be offloaded when called from an async Hook/tool. A timeout or cancellation of the awaiting coroutine does not cancel the underlying synchronous HTTP request; resource shutdown waits for it to finish.

## Migrated API

All eighteen legacy business methods are retained:

- Metadata: `get_table_list`, `get_table_columns_info`, `get_columns_sample_values`.
- Semantic recall: `semantic_retrieve`, `semantic_search_columns`, `vector_search_column_value`, `vector_search_table_desc`, `sql_few_shots`.
- Relationships: `get_table_relations_path`, `get_joinable_tables`.
- Search: `search_fulltext`, `search_basic`, `search_dsl`.
- Entities: `get_entity_by_unique_attribute`, `get_entity_by_guid`.
- Retrieval: `list_retrieval_tables`, `get_retrieval_table_schema`, `hybrid_table_columns`.

Generic `get` and `post`, API-prefix normalization, repeated query parameters, response validation, and audit logging are retained. Defaults remain 25 tables, 25 columns, and 2000 joinable results.

`from_settings(SemanticSettings)` replaces the old `from_config(ConfigManager)`. Malformed responses raise structured `DataAgentError` with `source`, `component`, `fact`, and `trace_id`. Timeout, connection, and HTTP status failures retain their original `httpx` exception types. Audit messages redact upstream secrets; host diagnostics also redact configured credentials.

Workspace caches, semantic-context formatting, and `.metric_dir` artifacts belong to consumers and are not part of this HTTP client.

## Validation

```bash
uv run pytest tests/test_semantic_client.py tests/test_semantic_configuration.py -q
DATAAGENT_SEMANTIC_LIVE_ENV=/absolute/path/to/.env \
  uv run pytest tests/test_semantic_live.py -q
```

The plugin-refresh test uses real DeepSeek credentials (`DEEPSEEK_BASE_URL`, `DEEPSEEK_API_KEY`) and an actual local HTTP contract fixture. It checks two Agent runs separated by a plugin refresh, semantic context reaching the model, SSE completion, shared client identity, and shutdown. This fixture does not certify the deployed semantic-service backend. Override `DATAAGENT_SEMANTIC_LIVE_MODEL` if the gateway uses a model name other than `deepseek-chat`.

To check a deployed backend, opt into the read-only `/api/semantic/v1/types/typedefs` smoke test:

```bash
DATAAGENT_SEMANTIC_LIVE_ENV=/absolute/path/to/.env \
DATAAGENT_SEMANTIC_LIVE_URL=http://semantic-host:port \
  uv run pytest tests/test_semantic_live.py -k deployed -q
```

The independent-Hook test writes an Agent JSON with top-level `semantic` settings and an empty plugin list. Its `before_model` Hook reads typedefs through `runtime.context.semantic_client`; real DeepSeek must return the observed type counts and sample names.

The delegation tests run through both the SDK handle and REST host. Real DeepSeek delegates to a child Agent, whose local Python tool reads live typedefs. Root and child state Hooks, before/after-tool Hooks and the tool must observe the identical context and client. The tests also check SDK/REST shutdown, SSE completion, model access through the same context and a verification code generated only inside the actual tool call. Configurations, observations and model answers are saved under the pytest temporary directory. Only `types/typedefs` is covered against the deployed backend.

HTTPX honors process proxy settings. If the backend requires a direct connection, add its host to `NO_PROXY` (and `no_proxy` if configured), preserving existing entries. This is a launch environment setting; no proxy credentials belong in the Agent JSON.
