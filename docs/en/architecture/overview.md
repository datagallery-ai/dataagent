# Architecture overview

The product is one local control plane and two presentations. Web and TUI both talk to the same `dataagent serve` process. They do not start separate runtimes.

## One service, two presentations

```text
Browser apps/web
  -> Next same-origin proxy for /api/v1/runs and /api/v1/*
  -> dataagent serve (config.json server.host:server.port, default 127.0.0.1:8790)
  <- apps/tui calls that same process directly
```

| Presentation | Start | Open |
| --- | --- | --- |
| Web | `npm run start` or `npm run start:web` | `http://127.0.0.1:3000/data-tasks` |
| TUI | `npm run start:tui` | The terminal. The TUI starts the control plane when nothing is listening. |

The Python SDK can skip HTTP and prepare the same runtime in process. Web/TUI use `POST /api/v1/runs`; other agents can use A2A 1.0 through `POST /a2a/jsonrpc` on the same backend. A2A returns a Task and final answer rather than the Web/TUI SSE event stream. See the [A2A interface](../reference/rest-api.md#a2a).

## Modules

| Module | Responsibility |
| --- | --- |
| `apps/web` | Browser presentation. Proxies conversation and `/api/v1/*` to the control plane. |
| `apps/tui` | Terminal presentation. Connects to the control plane and uses slash commands for sessions and outputs. |
| `runtime/agentkit/serve` | The only HTTP control plane: health, sessions, the event stream, and A2A. |
| `runtime/agentkit` | Loads configuration, assembles models and plugins, and runs one turn. |
| Home `~/.dataagent` | Configuration, Skills, plugins, and all session state. |
| `dataagent.workspaces` | Read-only input directories bound when the process starts. They are not inside Home. |

The control plane does not expose datasource registration, a knowledge base, cookie login, or a separate configuration REST API.

## Where configuration and data live

What you edit to deploy is `config.json` and `.env` under Home `~/.dataagent`, plus the repository `.env`. The model fields and listen address you must set are in [Configuration](../reference/configuration-api.md).

To let the model read a local directory, use a workspace. There is no datasource file inside Home. Steps are in [Where data comes from](../guides/data-sources.md). New files go to that session's `outputs/`.

A `.dataagent/` directory in the current working directory is not read. REST has no access-token or registration API. A2A JSON-RPC can require a shared Bearer token through `DATAAGENT_A2A_AUTH_TOKEN`; this does not protect REST routes or identify individual callers.
