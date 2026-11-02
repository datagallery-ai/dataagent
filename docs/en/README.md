# Documentation

This is the product guide: how to deploy, how to use Web and TUI, and what the external HTTP API accepts. The runtime field table, plugins, and SDK are not in this tree.

The product is one local control plane plus two presentations, Web and TUI. Both connect to the same `dataagent serve`. English and Chinese are both maintained.

## Start here

| Goal | Recommended reading |
| --- | --- |
| Understand product positioning and use cases | [Product overview](overview.md) |
| Run a local demo end to end | [Quick start](quick-start.md) |
| See what Web, TUI, and API support | [Capabilities](capabilities.md) |
| Use the graphical workbench | [Web workbench guide](guides/web-workbench.md) |
| Use the terminal interface | [TUI guide](guides/tui.md) |
| Give the model a local directory | [Where data comes from](guides/data-sources.md) |
| See which inputs you can configure | [Supported inputs](reference/supported-datasources.md) |
| Learn the API and configuration | [REST API](reference/rest-api.md), [Configuration](reference/configuration-api.md), and [Agent Runtime](reference/agent-runtime.md) |
| Understand system structure | [Architecture overview](architecture/overview.md) |
| Review security boundaries | [Security](security.md) |

## Recommended path

For a first trial, read in this order:

1. Read [Product overview](overview.md) to confirm there is one control plane and two presentations.
2. Follow [Quick start](quick-start.md) to set the model key, then ask from the Web UI or the TUI.
3. Read [Capabilities](capabilities.md) to separate routes the control plane serves from leftover UI that returns 404.
4. Choose [Web workbench guide](guides/web-workbench.md) or [TUI guide](guides/tui.md).
5. To let the model read a local directory, read [Where data comes from](guides/data-sources.md).

## Documentation scope

Start a trial with `overview.md`, `quick-start.md`, `capabilities.md`, and `guides/`. Read `reference/` when you call HTTP. This tree does not document plugin manifests, hooks, or bootstrap order.
