# Capabilities

Web and TUI share the Server's conversations, configuration, and outputs. The table shows what each client can do and where an integration can find the corresponding contract.

| Capability | Web | TUI | Server |
| --- | --- | --- | --- |
| Conversation | Ask from `/data-tasks` | Type in the terminal | `POST /api/v1/runs` |
| Sessions and human feedback | Reopen a session or answer a pending prompt in the workbench | Use `/resume` to reopen a session and answer pending feedback in chat | [Session details](../reference/rest-api.md#session-detail) and [AG-UI human feedback](../reference/events.md#hitl) |
| Outputs | View the current session's outputs | `/outputs` | [List and preview files](../reference/rest-api.md#outputs) |
| Cancel a turn | Stop the active turn | `/stop` calls the cancel endpoint and then closes the local stream | `POST /api/v1/runs/{runId}/cancel` |
| Model and data inputs | Use the Server's configuration and the session's bound input directories | Same as Web | [Configuration](../guides/configuration.md) and [Data inputs](../guides/inputs.md) |
| Skills, plugins, and MCP | Use the Server's loaded extensions | Same as Web | [Extension guides](../guides/integrate/extensions/overview.md) |
| Health and readiness | Check the Server address directly | The source launcher probes the configured Server | `GET /healthz`, `GET /ready` |
| Identity | Displays the Server's local user | Uses the same local user when attached | `GET /api/v1/me` returns the Server's local user, selected by `--user` |

For the machine-readable fields and values, see [health, identity, and capabilities](../reference/rest-api.md#health-and-identity).

The Server has no REST endpoints for datasource registration, knowledge bases, workspace configuration, model profile creation or testing, session renaming, deletion or branching, or a trace DAG. Database questions require an enabled plugin configured for the data source. See [Data inputs](../guides/inputs.md#supported-inputs).
