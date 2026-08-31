# DataFoundry Documentation

DataFoundry is an AI workbench for data analysis. It brings natural-language questions, data source management, read-only SQL execution, analysis traceability, and result delivery into a single workflow—so you can move through exploratory data analysis faster.

These docs are for product trials, customer demos, open-source visitors, and integration developers. Public documentation is available in English and Chinese.

## Start here

| Goal | Recommended reading |
| --- | --- |
| Understand product positioning and use cases | [Product overview](overview.md) |
| Run a local demo end to end | [Quick start](quick-start.md) |
| See what Web, TUI, and API support | [Capabilities](capabilities.md) |
| Use the graphical workbench | [Web workbench guide](guides/web-workbench.md) |
| Use the terminal interface | [TUI guide](guides/tui.md) |
| Connect your own data sources | [Data sources guide](guides/data-sources.md) |
| Browse supported data sources | [Supported data sources](reference/supported-datasources.md) |
| Learn about APIs and integration | [REST API reference](reference/rest-api.md), [Configuration API reference](reference/configuration-api.md), and [Agent Runtime reference](reference/agent-runtime.md) |
| Understand system structure | [Architecture overview](architecture/overview.md) |
| Review security boundaries | [Security](security.md) |

## Recommended path

For a first trial, read in this order:

1. Read [Product overview](overview.md) to confirm the problem space and capability boundaries.
2. Follow [Quick start](quick-start.md) to configure a model API key and run your first question against the built-in DTC Growth Review data source.
3. Read [Capabilities](capabilities.md) to see coverage across Web, TUI, and backend API.
4. Choose [Web workbench guide](guides/web-workbench.md) or [TUI guide](guides/tui.md) based on your entry point.
5. When you need your own data, read [Data sources guide](guides/data-sources.md).

## Documentation scope

This directory focuses on the public reading experience. It does not include project management notes, implementation plans, AI collaboration logs, historical refactor logs, or source-sensitive early discussions. Public docs describe only capabilities confirmed by current code, configuration, scripts, and local README files.

If you are extending or integrating the product, start with `reference/` and `architecture/`. If you are just trying the product, `overview.md`, `quick-start.md`, `capabilities.md`, and `guides/` cover the main paths.
