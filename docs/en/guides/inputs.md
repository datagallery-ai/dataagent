# Where data comes from

Use a workspace to give DataAgent a local directory, or configure an extension for another kind of input.

<a id="plugins"></a>

## Supported inputs { #supported-inputs }

| Input | Configuration |
| --- | --- |
| Local directory | `dataagent.input_workspaces` in the selected YAML |
| Skill | `<home>/skills/<name>/SKILL.md` |
| Plugin | `<home>/plugins/<id>/` or `plugins.paths`, then `plugins.enabled` |
| MCP | `.mcp.yaml` in Home or at the root of an enabled plugin |
| Semantic service | The `semantic` object in `config.yaml`. If omitted, no client is created |

Querying a database, warehouse, or spreadsheet requires an enabled plugin with the data declared in that plugin's own configuration. The control plane has no datasource registry or `GET /api/v1/datasource-types` endpoint. Web and TUI use the server configuration to access data.

<a id="read-only-directories"></a>

## Read-only workspaces { #read-only-workspaces }

To let the model use local files, write their directory into the selected YAML. Each directory must already exist and be readable and traversable by the server process.

```yaml
dataagent:
  input_workspaces:
  - name: input
    path: /data/reports
    read_only: true
```

Use `--config` to select a YAML file when starting Server. Relative paths are resolved from that file's directory. Workspace names are logical labels and must be unique. `read_only` accepts only `true`.

Restart the server after editing the list, then start a new session with the new inputs. Existing sessions resume with the list they were created with. Generated files belong in the session's output directory, `<home>/outputs/<user>/<thread>/` by default. For output and restoration details, see [Sessions and outputs](../guides/sessions-and-outputs.md).

The system prompt asks the model to treat workspaces as read-only. File and shell tools still have the operating system permissions of the server process. See [Security](../concepts/security.md).
