# Configuration guide

Configure a new instance or change an existing one. Edit the model and workspace settings on the server. See the [configuration reference](../reference/configuration.md) for fields, defaults, and merge rules.

Examples include the [minimal template](../../../config.example.yaml), [product settings with two hooks](../../../examples/config.yaml), and [nl2sql as the entry graph](../../../examples/nl2sql-entry.yaml). See the [examples README](../../../examples/README.md) for the files these configurations use.

<a id="select-the-configuration-files"></a>

<a name="files-to-edit"></a>

## Select the configuration files

| File | Role |
| --- | --- |
| `<home>/config.yaml` | Product configuration. Home defaults to `~/.dataagent`. |
| `<home>/.env` | Model name, base URL, and key. |
| Repository `.env` | Source launchers such as `npm run start` and `./deploy.sh` load this file. |

Set `DATAAGENT_HOME` in the launch environment to change Home. Use `--config` and `--env-file` to select the YAML and environment files independently. For an existing control plane, edit the configuration on that server. See [environment precedence](../reference/cli-and-environment.md#environment-variables).

<a name="minimum-model"></a>

## Configure the first model

The minimal YAML below uses the default model ID, `primary`. Put the three `LLM_*` values in your selected environment file.

```yaml
schema_version: 4
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
```

See [listen settings](../reference/configuration.md#server-settings) if you need a different address. Restart the control plane and [verify a model response](../quick-start.md#run-your-first-question).

<a name="precedence"></a>

## Precedence

The [merge reference](../reference/configuration.md#configuration-merge) defines the load order and how objects, lists, and hooks combine.

Suppose an enabled plugin's `defaults_file` supplies:

```yaml
plugins:
  config:
    demo:
      limit: 100
      format:
        header: true
        delimiter: ','
      tags:
      - default
```

The selected product YAML supplies:

```yaml
plugins:
  config:
    demo:
      limit: 20
      format:
        delimiter: ;
      tags: []
```

The plugin receives:

```json
{"limit":20,"format":{"header":true,"delimiter":";"},"tags":[]}
```

These fragments illustrate merging. A runnable configuration also needs a model and an enabled plugin. Adding `plugins.config.demo` alone does not enable it.

For relative paths, check whether the product YAML, manifest, or plugin interprets the value. See [configuration paths](../reference/configuration.md#configuration-merge). Implementation and tests are in [startup](../../../dataagent/bootstrap/startup.py), [field merging](../../../dataagent/bootstrap/config_files.py), [dependency resolution](../../../dataagent/config/plugins.py), and [configuration tests](../../../tests/test_plugin_configuration.py). For manifest settings, see [plugins](integrate/extensions/plugins.md).

Configuration errors identify the selected environment file. Variables for unused models can remain unset. A non-default model resolves them on its first `get_model("alias")` call, which fails if a required variable is missing. See [model fields](../reference/configuration.md#model-settings).

Restart after changing the model, service address, port, Home, input directories, or `dataagent.entry`. Plugin, Skill, Hook, and MCP declarations can refresh before the next turn, as described in the [extension lifecycle](../../development/architecture/extension-lifecycle.md). `refresh_extensions` does not change the entry.

## Common changes

**Change the default model.** Add an ID under `models`, set `dataagent.model` to it, then restart. There is no automatic fallback.

**Limit execution.** Adjust timeouts, model calls, and tool calls in `dataagent.limits`. See [run limits](../reference/configuration.md#dataagent). SDK hosts set their own whole-turn timeout.

**Add read-only inputs.** Add existing directories to `dataagent.input_workspaces` as described in [data inputs](inputs.md).

**Set the output root.** Add:

```yaml
dataagent:
  output_root: ~/reports
```

New sessions write to `<output_root>/<user>/<thread>/`. For example, Alice's `task-a` writes to `~/reports/alice/task-a/`. Omit the setting or use `null` to put it under `<home>/outputs/`.

- Use an absolute path or one starting with `~/`.
- The resolved directory is saved in the `session_bindings` table of `sessions.sqlite`. Settings changes affect new sessions.
- Creating a session fails if the derived directory already contains files without a recorded owner.
- The SDK can override `output_root` with a final `output_dir` for one task. See [SDK task outputs](integrate/sdk.md#task-outputs).

See [sessions and outputs](sessions-and-outputs.md) for identity, ownership, exclusivity, and common errors.

**Enable a plugin.** Install it in `<home>/plugins/<id>/` or list the plugin directory in `plugins.paths`. Then add its ID to `plugins.enabled`. See [plugins](integrate/extensions/plugins.md).

**Connect a semantic service.** Add a `semantic` object. See [semantic service](integrate/extensions/semantic-service.md) for client use, authentication, and TLS. Without the object, the runtime creates no client.

**Select a compiled entry graph.** Follow [entry mode](integrate/extensions/subagents.md#entry-mode) to set `dataagent.entry` and verify which graph runs.

## Settings in other files

- Declare MCP connections in the Home or enabled plugin's `.mcp.yaml`.
- Declare tools, Skills, and subagents in the manifest. Place standalone Skills in `<home>/skills/<name>/SKILL.md`.
- For TUI display settings and logs, see [environment variables](../reference/cli-and-environment.md#environment-variables).

Keep secrets in `.env` or the process environment and out of version control.
