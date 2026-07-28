# Configuration API reference

This reference is for developers managing workspace resources. After reading it, you can create data sources, models, knowledge bases, MCP servers, Skills, and files, and attach them to agent runs.

Default service address:

```text
http://127.0.0.1:8787
```

## Design boundaries

DataFoundry separates resource management from agent runs:

| Type | Entry | Purpose |
| --- | --- | --- |
| Configuration REST API | `/api/v1/*` | Create, test, update, delete resources. |
| Agent run | `/api/copilotkit` | Start one data analysis run. |

Resources are written to the workspace first, then selected for a run through `run_config`.

## Three-layer configuration model

```text
effectiveRunConfig = merge(workspaceDefaults, perRunOverrides, serverPolicy)
```

| Layer | Source | Description |
| --- | --- | --- |
| `workspaceDefaults` | Workspace configuration | Default resources in the workspace. |
| `perRunOverrides` | Current run | Resources chosen in the input box, session toggles, or `@` mentions. |
| `serverPolicy` | Backend | Permissions, security policy, and capability switches. |

The backend merges these into an immutable snapshot before handing off to Agent Runtime.

## Auth

Configuration API calls and AG-UI runs must share the same password session (`df_session` cookie). Unsafe methods also need `X-CSRF-Token` from the `df_csrf` cookie:

```text
REST /api/v1/*           -> Cookie session + X-CSRF-Token (unsafe methods)
CopilotKit /api/copilotkit -> Cookie session + X-CSRF-Token (unsafe methods)
```

This keeps workspace defaults, server sessions, file assets, artifacts, SQL audit, and run history in one user scope. Web v1 does not expose workspace switching.

## Common resource fields

| Field | Description |
| --- | --- |
| `id` | Stable resource ID. |
| `name` | Display name. |
| `description` | Resource description. |
| `defaultEnabled` | Whether new runs use this resource by default. |
| `builtin` | Whether this is a built-in resource. |
| `revision` | Optimistic concurrency version. |
| `createdAt` / `updatedAt` | Audit timestamps. |

Updates may send `revision` or `If-Match`. Conflicts return `REVISION_CONFLICT`.

## Credential principles

- Credentials are submitted only when creating or updating resources.
- Read APIs do not return plaintext passwords, tokens, or full connection strings.
- Read responses return `secretRef`, `hasSecret`, or equivalent markers only.
- Frontend and TUI must not put credentials in AG-UI `messages`, `context`, or `forwardedProps`.
- To clear credentials on a resource, use `clearCredentials: true`.

## Minimum create fields

### Data source

```json
{
  "id": "sales-pg",
  "name": "Sales PostgreSQL",
  "type": "postgresql",
  "config": {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "sales",
    "username": "readonly",
    "password": "replace-with-your-key"
  }
}
```

Field shapes for each type come from `GET /api/v1/datasource-types`. See [Supported data sources](supported-datasources.md).

### Model profile

```json
{
  "id": "qwen",
  "name": "Qwen",
  "provider": "openai-compatible",
  "model": "qwen-plus",
  "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "apiKey": "replace-with-your-key"
}
```

The server default model can also be configured through `.env` without creating a model resource.

### Knowledge base

```json
{
  "id": "metrics-docs",
  "name": "Metrics Docs",
  "description": "Metric definition documentation"
}
```

After creation you can upload files, import file assets, search, and rebuild indexes.

### MCP server

```json
{
  "id": "local-tools",
  "name": "Local Tools",
  "transport": "streamable-http",
  "serverUrl": "http://127.0.0.1:3333/mcp"
}
```

For stdio or authenticated remote services, submit fields according to backend capability switches.

### Skill

Skills upload as `multipart/form-data` packages. After upload you can validate, replace, and filter them in runs.

### Files

`POST /api/v1/files` accepts batch upload via `multipart/form-data`. Returned file IDs can go into `run_config.fileIds`.

## Test actions

| Resource | Test endpoint |
| --- | --- |
| Data source | `POST /api/v1/datasources/:id/test` |
| Model | `POST /api/v1/model-profiles/:id/test` |
| Knowledge base | `POST /api/v1/knowledge-bases/:id/test` |
| MCP server | `POST /api/v1/mcp-servers/:id/test` |
| Skill | `POST /api/v1/skills/:id/test` |

Examples:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/datasources/sales-pg/test
curl -X POST http://127.0.0.1:8787/api/v1/model-profiles/qwen/test
curl -X POST http://127.0.0.1:8787/api/v1/mcp-servers/local-tools/test
```

Test responses should return status, latency, and diagnostics—not plaintext credentials.

## Connecting to agent runs

After resources exist, select them for a run through `run_config`:

```json
{
  "forwardedProps": {
    "run_config": {
      "activeDatasourceId": "sales-pg",
      "enabledDatasourceIds": ["sales-pg"],
      "activeLlmProfileId": "server-default",
      "enabledKnowledgeIds": ["metrics-docs"],
      "enabledMcpServerIds": ["local-tools"],
      "skill_mode": "auto",
      "fileIds": ["file-ref-1"]
    }
  }
}
```

Clients send resource IDs and selection only. The backend validates existence, enablement, and run eligibility.

## Concurrency and idempotency

- `PATCH` uses `revision` or `If-Match` to avoid overwriting concurrent updates.
- Async actions such as schema fetch, knowledge reindex, and artifact export may send `Idempotency-Key`.
- Async actions return a job; poll with `GET /api/v1/jobs/:id`.

## Further reading

- Endpoint overview: [REST API reference](rest-api.md)
- Agent runs: [Agent Runtime and AG-UI reference](agent-runtime.md)
- Data source connection: [Data sources guide](../guides/data-sources.md)
