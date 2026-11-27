# Semantic service client

Connect DataAgent to a deployed semantic service. The repository supplies the client. When configured, REST creates one client for the process lifespan and shares it through each `AgentContext`. If the SDK does not receive a client, `create_agent` creates and closes its own.

## Configuration

```yaml
semantic:
  base_url: https://semantic.example.com
  username: $env{SEMANTIC_USERNAME}
  password: $env{SEMANTIC_PASSWORD}
  timeout_seconds: 30
  semantic_scenario: business_twin
  tls:
    verify_server: true
    ca_file: certs/ca.pem
    client_cert: certs/client.pem
    client_key: certs/client.key
```

Set `base_url` to the service root. Requests use `/api/semantic/v1/...`. Supply Basic credentials through the environment. See [semantic configuration](../../../reference/configuration.md#semantic).

## TLS

`verify_server` defaults to `false`. With no client certificate or ciphers, this keeps verification disabled. When `true`, the default SSL context is used, or the CA from `ca_file` when set. Provide `client_cert` and `client_key` together. Nonempty `ciphers` is passed to `set_ciphers`.

## Call from a tool

```python
from langchain.tools import ToolRuntime
from dataagent import AgentContext

def lookup(query: str, runtime: ToolRuntime[AgentContext]) -> str:
    """Retrieve semantic metadata for a natural-language query."""
    client = runtime.context.semantic_client
    if client is None:
        raise RuntimeError("semantic service is not configured")
    payload = client.semantic_retrieve(query)
    return str(payload)
```

The client uses synchronous `httpx.Client`. Offload calls from async tools to a thread. Close waits for synchronous requests in flight.

| Method | Route suffix or purpose |
| --- | --- |
| `semantic_retrieve` | `semantic/retrieve` |
| `get_table_list` | `advanced-search/table-list` |
| `get_table_columns_info` | `advanced-search/table-columns-info` |
| `get_columns_sample_values` | `advanced-search/column-value-info` |
| `semantic_search_columns` | `advanced-search/semantic-search-columns` |
| `get_joinable_tables` | `advanced-search/joinable-tables` |
| `get_table_relations_path` | `advanced-search/table-relations-path` |
| `vector_search_table_desc` | `advanced-search/vector-search-table-desc` |
| `vector_search_column_value` | `advanced-search/vector-search-column-value` |
| `sql_few_shots` | `advanced-search/sql-few-shots` |
| `search_fulltext`, `search_basic`, `search_dsl` | Corresponding search endpoints |
| `get_entity_by_guid`, `get_entity_by_unique_attribute` | Entity lookup |
| `get_retrieval_table_schema` | Table schema |
| `search_entities(type_name, *, limit=200, offset=0)` | GET `search/basic`, paginated by type |
| `list_retrieval_tables` | `retrieval/tables` |
| `hybrid_table_columns(tables)` | `hybrid/table-columns`, physical columns in bulk |

`semantic_scenario` or `scenario=` sets `X-Semantic-Scenario` on requests except health. `with_scenario(scenario)` returns a view using the same connection with another header value.

Use `db.table.column` identifiers. `sample_count` ranges from 0 to 100 and is keyword-only. Positional arguments go from broad to narrow. Use keyword `limit` for counts, sent as `topK` or `limit` depending on endpoint. `max_depth` is also keyword-only.

If you create a client with `SemanticServiceClient.from_settings(runtime.settings.semantic)`, you own its `close()`. Direct construction defaults to `verify=True`. `from_settings` uses the TLS configuration. Diagnostics redact Basic credentials and their Base64 representation.
