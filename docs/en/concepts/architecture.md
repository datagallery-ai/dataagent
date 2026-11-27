# Architecture overview

DataAgent runs a local control plane. The Web workbench and terminal client share its conversation runtime and session data.

## How clients connect

| Client | Connection |
| --- | --- |
| Web | Next.js proxies same-origin `/api/v1/*` requests to the control plane. |
| TUI | Connects directly to the control plane. The source TUI can start a local backend when needed. |
| Python SDK | Prepares the runtime and invokes the Agent in the caller's Python process. |
| Remote agents | Discover the Agent Card and use A2A JSON-RPC to send messages and query Tasks. |

Web and TUI both send conversation requests to `POST /api/v1/runs` and consume the AG-UI event stream. A2A has a separate Task protocol on the same control plane. [REST API](../reference/rest-api.md) · [AG-UI integration](../reference/agent-runtime.md) · [A2A interface](../reference/a2a.md) · [Python SDK](../guides/integrate/sdk.md)

## Where configuration and data live

The control plane reads its model, plugins, and default input directories from configuration. DataAgent Home holds local configuration, extensions, and runtime state. A new session binds its input directories and output location, which it keeps when resumed. Outputs are stored under Home by default or in a configured location. [Configuration](../guides/configuration.md) · [Input directories](../guides/inputs.md) · [Sessions and outputs](../guides/sessions-and-outputs.md)

For connection, credential, and file-permission boundaries, see the [security notes](security.md). For source modules and assembly order, see the [developer architecture](../../development/architecture/overview.md).
