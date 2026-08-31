# Agent Runtime and AG-UI reference

This reference is for Web, TUI, and other client developers. After reading it, you can construct agent run requests, understand `run_config`, consume AG-UI events, and handle cancel, errors, and restore.

## Runtime entry

```text
POST /api/copilotkit
```

This endpoint starts one agent run and returns an AG-UI event stream. Resource management, file upload, artifact download, and similar actions use `/api/v1/*` REST APIs.

## Request context

Common fields:

| Field | Description |
| --- | --- |
| `threadId` | Session ID. The backend uses it for history, session restore, and artifact archival. |
| `runId` | Single-run ID. Clients use it to cancel, trace, and replay. |
| `messages` | User input for this turn. Do not include credentials. |
| `forwardedProps.run_config` | Resource selection for this run; takes priority over state. |
| `state.run_config` | Run configuration in client state. |

Example:

```json
{
  "threadId": "session-001",
  "runId": "run-001",
  "messages": [
    {
      "role": "user",
      "content": "Compute GMV by channel from the orders table."
    }
  ],
  "forwardedProps": {
    "run_config": {
      "activeDatasourceId": "dtc-growth-demo",
      "enabledDatasourceIds": ["dtc-growth-demo"],
      "activeLlmProfileId": "server-default"
    }
  }
}
```

## `run_config` fields

| Field | Purpose |
| --- | --- |
| `enabledDatasourceIds` | Data sources available for this run. |
| `activeDatasourceId` | Default data source. |
| `enabledKnowledgeIds` | Knowledge bases available for this run. |
| `enabledMcpServerIds` | MCP servers available for this run. |
| `enabledSkillIds` | Skills available for this run. |
| `activeSkillId` | User-specified Skill. |
| `activeLlmProfileId` | Model profile for this run. |
| `skill_mode` | Skill selection mode, e.g. `auto`. |
| `fileIds` | Workspace file IDs. |
| `pinnedPaths` | File or artifact paths pinned in this session. |
| `mentioned` | Resources mentioned via `@`. |

Clients send resource IDs, selections, and references only. The backend validates permissions, state, and capability switches.

## Configuration merge

```text
workspace defaults
  + per-run overrides
  + server policy
  = effective run config
```

- `workspace defaults` come from workspace configuration.
- `per-run overrides` come from input box selection, session resource toggles, and `@` mentions.
- `server policy` is enforced by the backend; clients cannot bypass it.

## Event consumption

Clients render using AG-UI event semantics—no custom SSE/chat protocol required.

| Category | Purpose |
| --- | --- |
| Run state | Run started, completed, canceled, or failed. |
| Text messages | Agent replies. |
| Reasoning / thought | Public reasoning summaries or step descriptions. |
| Tool calls | Schema inspection, SQL queries, file reads, and similar tool invocations. |
| Custom events | Structured data such as artifacts, SQL audit, token usage, workspace metadata. |

Clients should retain `runId`, `threadId`, tool call IDs, and artifact IDs for details, cancel, and restore.

## Cancel, errors, and restore

| Scenario | Client action |
| --- | --- |
| User cancel | Call `POST /api/v1/runs/:runId/cancel`; stop button enters canceling state. |
| Run failure | Show backend error; keep received events and outputs. |
| Network drop | Read session history with `threadId`, then restore UI state. |
| Page refresh | Call session and artifact APIs to rebuild conversation, trace, and outputs. |

The backend persists run events; clients do not need to resend full history on the next request.

## Security boundaries

- Do not put database passwords, model API keys, or MCP tokens in `messages`, `context`, or `forwardedProps`.
- Data source access goes through Data Gateway.
- Files, knowledge bases, Skills, and MCP tools are filtered by backend policy.
- Event streams are for display and replay; they do not carry sensitive plaintext.

## Further reading

- Configure resources: [Configuration API reference](configuration-api.md)
- HTTP endpoints: [REST API reference](rest-api.md)
- System structure: [Architecture overview](../architecture/overview.md)
