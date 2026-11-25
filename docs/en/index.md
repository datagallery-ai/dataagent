# User Guide

This is one local control plane. Web and TUI are its two presentations.

<div class="df-grid" markdown>

<div class="df-card" markdown>
<p class="df-card-title">Product overview</p>
Understand where DataAgent fits and which problems it addresses.

[Read overview](overview.md)
</div>

<div class="df-card" markdown>
<p class="df-card-title">Quick start</p>
Set the model key, then ask the first question in the Web UI or the TUI.

[Start locally](quick-start.md)
</div>

</div>

## Recommended path

1. Read [Product overview](overview.md) to confirm the problem space and capability boundaries.
2. Follow [Quick start](quick-start.md): on Linux (including openEuler) prefer `./deploy.sh`; on Windows / macOS use manual npm (`npm install` + `uv sync` → configure → `build` / `start`). Put the model key in `.env`, then ask from `/data-tasks` or the TUI.
3. Read [Capabilities](capabilities.md) for the routes Web, TUI, and the control plane actually dataagent.server.
4. Choose [Web workbench guide](guides/web-workbench.md) or [TUI guide](guides/tui.md).
5. To let the model read a local directory, read [Where data comes from](guides/data-sources.md).

## Documentation scope

These docs are for product trials, customer demos, open-source visitors, and integration developers. If you are extending or integrating DataAgent, start with `Reference` and `Developer Guide`.
