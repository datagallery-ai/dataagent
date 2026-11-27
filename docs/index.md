---
hide:
  - navigation
  - toc
  - edit
---

<div class="df-home" markdown>

<section class="df-home-hero" markdown>

# DataAgent

A local Data Agent workbench for multi-step analysis. Use Web or TUI to ask questions, connect configured plugins and semantic services, and review conversation history, outputs, and traces.

</section>

<figure class="df-home-shot" markdown>
![DataAgent web workbench](assets/readme/gui-demo.gif){ .df-shot }
<figcaption>Web workbench, terminal workflow, and backend runtime share one governed task model.</figcaption>
</figure>

<section class="df-proof-grid" markdown>

<div class="df-proof" markdown>
### Semantic Accuracy
Schema, metrics, field relationships, and datasource context are resolved before SQL is written, reducing guessed tables, wrong joins, and definition drift.
</div>

<div class="df-proof" markdown>
### Data access and execution
Input workspaces give the Agent read-only guidance. File tools and shell run with the Server process's operating-system permissions. NL2SQL safety rules depend on plugin configuration. See [security](en/concepts/security.md).
</div>

<div class="df-proof" markdown>
### Data-task Runtime
Multi-step analysis converges through schema inspection, tool policy, event streams, artifacts, and replayable history across Web, TUI, and API.
</div>

</section>

<section class="df-runtime-preview" markdown>

<div class="df-runtime-copy" markdown>
## One runtime, three entry points

Use the Web workbench for visual analysis review, the TUI for SSH and terminal workflows, and the API / AG-UI runtime when you need to embed the same governed task model into your own product.

[TUI guide](en/guides/tui.md) · [Watch terminal demo](assets/readme/tui-demo.gif)
</div>

<a class="df-tui-preview" href="assets/readme/tui-demo.gif" aria-label="Watch DataAgent TUI demo">
  <img src="assets/readme/tui-demo.gif" alt="DataAgent TUI demo" loading="lazy">
</a>

</section>

## Start with the right path

<div class="df-grid df-route-grid" markdown>

<div class="df-card" markdown>
### Use and Integration
Deploy DataAgent, use Web or TUI, or integrate through APIs, the Python SDK, and extensions.

[Open use and integration docs](en/index.md){ .df-card-action }
</div>

<div class="df-card" markdown>
### Developer Docs
Set up a source checkout, understand the architecture, run tests, or build a release.

[Open developer docs](development/index.md){ .df-card-action }
</div>

</div>

</div>
