# Configuration

This page is for deployers and people trying the product. It lists the files to edit and the minimum model settings. The full field table, merge rules, and plugin parameters are not part of this site.

The control plane has no configuration REST API. The Web UI cannot change the model or the workspaces.

## Files to edit

| File | Role |
| --- | --- |
| `<home>/config.json` | Personal configuration. Home defaults to `~/.dataagent`. |
| `<home>/.env` | Model name, base URL, and key. |
| Repository `.env` | `npm run start` and `./deploy.sh` load it into the process environment. Process environment wins over Home `.env`. |

`DATAAGENT_HOME` is read only from the process environment. A `config.json` in the launch directory is not merged. A `.dataagent/` directory in the current working directory is not read.

## Minimum model

```json
{
  "schema_version": 3,
  "models": {
    "primary": {
      "provider": "openai",
      "name": "$env{LLM_MODEL}",
      "base_url": "$env{LLM_BASE_URL}",
      "api_key": "$env{LLM_API_KEY}"
    }
  }
}
```

`provider` is `openai`. Compatible servers use the same value. The listen address is `server.host` and `server.port` in the same file, default `127.0.0.1` and `8790`. Web and TUI both read those two fields.

Restart the control plane after changing the model, port, or Home.

## Read-only directories

A workspace is not a subdirectory of Home, and there is no configuration route for it. Put an existing directory in `dataagent.workspaces`, or pass `--workspace` at startup. Steps are in [Where data comes from](../guides/data-sources.md).
