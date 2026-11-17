# Product overview

This is one local control plane and two presentations: Web in the browser, and TUI in the terminal. Both connect to the same `dataagent serve`.

## What you can do

After the model is configured, ask in the browser or the terminal. The runtime loads the model, plugins, Skills, and MCP from Home `config.json`, streams the reply, and keeps the session and outputs under `~/.dataagent`.

If read-only workspaces are configured, the model can read files in those directories. New files go to that session's `outputs/`.

## Entries

| Entry | Fit | Notes |
| --- | --- | --- |
| Web | Conversation and sessions in a browser | `http://127.0.0.1:3000/data-tasks`. There is no registration. |
| TUI | SSH and terminals | `npm run start:tui`. It starts the control plane when nothing is listening. |

The backend defaults to `http://127.0.0.1:8790`. Integrators call that control plane directly.

## Boundaries

- Configuration is files, not Web forms.
- There is no datasource registration API and no knowledge-base API.
- Workspaces are read-only directories bound at startup. A conversation request cannot change them.
- The default listen address is loopback. REST does not check a token; A2A JSON-RPC can require a shared Bearer token, which does not protect REST.
- Database questions depend on an enabled plugin. They are not built into the control plane.

## Next

- Run it locally: [Quick start](quick-start.md)
- What each presentation can do: [Capabilities](capabilities.md)
- Browser: [Web workbench guide](guides/web-workbench.md)
- Terminal: [TUI guide](guides/tui.md)
- Configuration and workspaces: [Configuration](reference/configuration-api.md)
- HTTP: [REST API](reference/rest-api.md), [Agent Runtime](reference/agent-runtime.md)
- Structure: [Architecture overview](architecture/overview.md)
