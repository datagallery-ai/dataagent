# Where data comes from

This page is only the procedure for giving the model a local directory. The input list is [Supported inputs](../reference/supported-datasources.md).

The control plane has no datasource registration API. Web and TUI cannot create, test, or switch a datasource resource.

## Read-only directories

To let the model read local files, list the directories as workspaces. Each directory must exist before startup.

```json
{
  "dataagent": {
    "workspaces": [
      {"name": "input", "path": "/data/reports", "read_only": true}
    ]
  }
}
```

You can also repeat `--workspace` when starting the control plane, or set `DATAAGENT_WORKSPACE`. The command line wins over the other two.

`read_only` can only be `true`. A new session writes the list into the `session_bindings` table in `sessions.sqlite` once. Files the agent creates go to that session's output directory, `<home>/outputs/<user>/<thread>/` by default, not into the workspace.

The system prompt tells the model to treat workspaces as read-only. That does not change directory permissions.

## Plugins

Skills, plugins, and MCP are files under Home, not resources created over REST. See [Configuration](../reference/configuration-api.md).

Questions against a database are not built into the control plane. That behavior exists only after the matching plugin is enabled and its `plugins.config` is set.
