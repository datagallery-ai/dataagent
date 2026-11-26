# Architecture overview

The product is one local control plane and two presentations. Web and TUI both talk to the same `dataagent serve` process. They do not start separate runtimes.

## One service, two presentations

```text
Browser ui/web
  -> Next same-origin proxy for /api/v1/runs and /api/v1/*
  -> dataagent serve (config.yaml server.host:server.port, default 127.0.0.1:8790)
  <- ui/tui calls that same process directly
```

| Presentation | Start | Open |
| --- | --- | --- |
| Web | `npm run start` or `npm run start:web` | `http://127.0.0.1:3000/data-tasks` |
| TUI | `npm run start:tui` | The terminal. The TUI starts the control plane when nothing is listening. |

The Python SDK can skip HTTP and prepare the same runtime in process. Web/TUI use `POST /api/v1/runs`; other agents can use A2A 1.0 through `POST /a2a/jsonrpc` on the same backend. A2A returns a Task and final answer rather than the Web/TUI SSE event stream. See the [A2A interface](../reference/rest-api.md#a2a).

## Modules

| Module | Responsibility |
| --- | --- |
| `ui/web` | Browser presentation. Proxies conversation and `/api/v1/*` to the control plane. |
| `ui/tui` | Terminal presentation. Connects to the control plane and uses slash commands for sessions and outputs. |
| `dataagent/server/` | Serves health checks, sessions and the AG-UI event stream. The A2A service lives in `dataagent/server/a2a/`. |
| `dataagent/` | Exposes the Python SDK. `bootstrap/` prepares the runtime and `assembly/graph.py` builds the agent graph. |
| `dataagent/assembly/prompts/` | Renders the host templates for the main agent, subagents and filesystem instructions. Plugins keep their own prompts in their plugin directories. |
| Home `~/.dataagent` | Configuration, Skills, plugins, and all session state. |
| `dataagent.input_workspaces` | Read-only input directories bound when the process starts. They are not inside Home. |

The control plane does not expose datasource registration, a knowledge base, cookie login, or a separate configuration REST API.

## Where configuration and data live

What you edit to deploy is `config.yaml` and `.env` under Home `~/.dataagent`, plus the repository `.env`. The model fields and listen address you must set are in [Configuration](../reference/configuration-api.md).

To let the model read a local directory, use a workspace. There is no datasource file inside Home. Steps are in [Where data comes from](../guides/data-sources.md). New files go to that session's `outputs/`.

A `.dataagent/` directory in the current working directory is not read. REST has no access-token or registration API. A2A JSON-RPC can require a shared Bearer token through `DATAAGENT_A2A_AUTH_TOKEN`; this does not protect REST routes or identify individual callers.
