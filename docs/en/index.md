# User Guide

DataFoundry is an AI workbench for data analysis. It brings natural-language questions, data source management, read-only SQL execution, analysis traceability, and result delivery into one workflow.

<div class="df-grid" markdown>

<div class="df-card" markdown>
<p class="df-card-title">Product overview</p>
Understand where DataFoundry fits and which problems it addresses.

[Read overview](overview.md)
</div>

<div class="df-card" markdown>
<p class="df-card-title">Quick start</p>
Start the local workbench and run a first analysis against the built-in demo data source.

[Start locally](quick-start.md)
</div>

<div class="df-card" markdown>
<p class="df-card-title">DTC growth case</p>
Review a realistic growth spike with schema inspection, read-only SQL, and traceable evidence.

[Open example](examples/dtc-growth-demo.md)
</div>

</div>

## Recommended path

1. Read [Product overview](overview.md) to confirm the problem space and capability boundaries.
2. Follow [Quick start](quick-start.md): on Ubuntu / Debian prefer `./deploy.sh`; on Windows / macOS and other hosts use manual npm (`npm install` → configure → `build` / `start`). Then create a model profile in the Web UI and run the built-in DTC Growth Review.
3. Read [Capabilities](capabilities.md) to see coverage across Web, TUI, and backend API.
4. Choose [Web workbench guide](guides/web-workbench.md) or [TUI guide](guides/tui.md) based on your entry point.
5. When you need your own data, read [Data sources guide](guides/data-sources.md).

## Documentation scope

These docs are for product trials, customer demos, open-source visitors, and integration developers. If you are extending or integrating DataFoundry, start with `Reference` and `Developer Guide`.
