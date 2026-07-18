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
environment variables are needed. ClickHouse-backed steps expect the resource MCP
endpoint at `http://127.0.0.1:8766/mcp`; semantic retrieval defaults to
`http://localhost:31000/api/semantic`.

Completed Job artifacts are published to the parent workspace's read-only
`subagent_output/` area. The workflow stages local `data_refs` there and later
subagents discover both inputs and upstream artifacts through `manifest.json`.
