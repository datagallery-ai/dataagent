# Product overview

This is one local control plane and two presentations: Web in the browser, and TUI in the terminal. Both connect to the same `dataagent serve`.

## What you can do

After configuring a model, ask in the browser or the terminal. The Server uses the selected configuration and enabled extensions, and streams its reply. To supply local files, set up [data inputs](../guides/inputs.md). See [sessions and outputs](../guides/sessions-and-outputs.md) to find the results.

## Entries

| Entry | Fit | Notes |
| --- | --- | --- |
| Web | Conversation and sessions in a browser | `http://127.0.0.1:3000/data-tasks`. There is no registration. |
| TUI | SSH and terminals | `npm run start:tui`. It starts the control plane when nothing is listening. |

The backend defaults to `http://127.0.0.1:8790`. Integrators call that control plane directly.

## Boundaries

- Configuration is files, not Web forms.
- There is no datasource registration API and no knowledge-base API.
- Database questions depend on an enabled plugin configured for that data source.
- The default Server address is loopback. See [security](security.md) for authentication and file access.

## Next

- Run it locally: [Quick start](../quick-start.md)
- What each presentation can do: [Capabilities](capabilities.md)
- Browser: [Web workbench guide](../guides/web.md)
- Terminal: [TUI guide](../guides/tui.md)
- Configuration and workspaces: [Configuration](../reference/configuration-api.md)
- HTTP: [REST API](../reference/rest-api.md), [Agent Runtime](../reference/agent-runtime.md)
- Structure: [Architecture overview](architecture.md)
