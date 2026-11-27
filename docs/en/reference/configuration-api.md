# Configuration quick reference

Find the files to edit and the minimum model settings for your first deployment.

The control plane has no configuration REST API. The Web UI cannot change the model or the workspaces.

<a name="files-to-edit"></a>

## Files to edit

| File | Role |
| --- | --- |
| `<home>/config.yaml` | Personal configuration. Home defaults to `~/.dataagent`. |
| `<home>/.env` | Model name, base URL, and key. |
| Repository `.env` | Source launchers such as `npm run start` and `./deploy.sh` load this file. |

Set `DATAAGENT_HOME` in the launch environment to select a different Home. Use `--config` and `--env-file` to select the YAML and environment files independently. See [environment precedence](../reference/cli-and-environment.md#environment-variables).

## Minimum model

```yaml
schema_version: 4
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
```

Fill in the three `LLM_*` values in the selected environment file. To change the listen address, update the `server` section in the same YAML. See [listen settings](../reference/configuration.md#server-settings) for defaults and other options.

Restart the control plane after changing the model, port, or Home.

## Read-only directories

Set existing input directories in the YAML field `dataagent.input_workspaces`. Steps are in [Where data comes from](../guides/inputs.md).

## More configuration topics

- [Configuration guide](../guides/configuration.md) covers switching models, run limits, outputs, and plugins.
- [Configuration fields](../reference/configuration.md) lists types, defaults, and merge rules.
- [Command-line options and environment variables](../reference/cli-and-environment.md) lists startup options.
