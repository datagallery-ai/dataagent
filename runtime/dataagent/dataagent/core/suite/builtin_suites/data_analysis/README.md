# Data Analysis Suite

This is the native 8045 Suite migrated from the 5519 `data_analysis` plugin.
The 8045 Suite configuration is the only runtime source of truth; there is no
`plugin.yaml` compatibility layer and no `submit_resource_job` metadata post-hook.

Install the optional operators and activate the Suite in the main Agent config:

```bash
pip install 'dataagent[data-analysis]'
```

```yaml
AGENT_CONFIG:
  subagent_output_sharing: true

SUITE:
  include:
    - data_analysis
```

The Suite subagents use the same default `MODEL.chat_model` provider and model as
the main Agent examples (`bailian` / `deepseek-v4-flash`), so they reuse
`BAILIAN_BASE_URL` and `BAILIAN_API_KEY`; no Data Analysis-specific model
environment variables are needed. The sampling step uses resource id
`sql_warehouse`; its default MCP endpoint is `http://127.0.0.1:8766/mcp`.
Other pipeline stages retain their existing resource contracts until they are
generalized separately. Semantic retrieval defaults to
`http://localhost:31000/api/semantic`.

Sampling scenes live in `config/scenes/<scene_name>.yaml`; the filename stem is
the semantic scene name. Warehouse bindings live in `config/data_source.md`.
Explanatory copies are in `docs/scene.md` and
`docs/data_source.md`. The sampling skill runs six steps (plan, pool,
train/test, projection, stats, finalize) and compiles warehouse-neutral logical
SQL using the workspace binding.

Completed Job artifacts are published to the parent workspace's read-only
`subagent_output/` area. The workflow stages only `data_refs` that resolve to
local files in the active workspace; other non-empty data-source references are
passed through to subagents. Subagents discover published local inputs and
upstream artifacts through the shared output index.
