# Configuration fields

This reference covers `config.yaml` fields, defaults, and merge rules. See the [quick reference](../guides/configuration.md) for your first model and the [configuration guide](../guides/configuration.md) for examples.

Use strict YAML 1.2. Comments and multiline strings are allowed. Unknown fields, duplicate keys, anchors, aliases, merge keys, explicit tags, invalid types, and nonfinite numbers are rejected. `schema_version` defaults to 4 and only accepts 4. Relative paths resolve from the declaring file's directory and `~/` expands. `dataagent.output_root` only accepts absolute or `~/` paths.

## Top level

| Field | Default | Meaning |
| --- | --- | --- |
| `schema_version` | 4 | Product schema version |
| `dataagent` | See below | Model, limits, hooks, inputs, and entry |
| `models` | Required | Model ID to connection mapping. Must contain the selected model |
| `plugins` | `enabled: []`, `paths: []` | Enabled IDs and extra candidate paths |
| `server` | See below | Listen settings for both servers, Web, and deploy. Standalone A2A may override them |
| `semantic` | Omitted | Optional semantic client |
| `a2a` | Omitted | Agent Card and outbound agents |

<a name="dataagent"></a>

## `dataagent`

| Field | Default | Constraint |
| --- | --- | --- |
| `model` | `"primary"` | Key in `models` |
| `limits.timeout_seconds` | 180 | Positive number. Bounds REST event iteration, A2A agent `ainvoke`, model and shell calls, and MCP reads. MCP discovery uses the smaller of this and 20 seconds. It is also the outbound A2A default. A2A queueing, session preparation, and checkpoint settlement occur outside the agent deadline |
| `limits.model_calls_per_agent` | 128 | Positive integer, model loop limit per agent per invocation |
| `limits.tool_calls_per_agent` | 128 | Positive integer, tool call limit per agent per invocation |
| `loop_detection.enabled` | `true` | Detect repeated text and stagnant tool loops. Text aborts immediately, tool repeats are allowed one correction |
| `loop_detection.ignored_tools` | `[]` | Full registered names to exclude from tool-loop detection |
| `hooks` | `[]` | Grouped or flat [declarations](extension-schema.md), appended across layers |
| `input_workspaces` | `[]` | Read-only inputs. Empty does not create directories |
| `output_root` | `null` | Absolute or `~/` output root for new sessions. The default is `<home>/outputs`. Adds `<user>/<thread>/`. Existing sessions keep their directory. See [outputs](../guides/sessions-and-outputs.md#output-directory) |
| `entry` | Omitted | Name matching `[a-z][a-z0-9_-]{0,63}` of an enabled compiled subagent. Otherwise use the Deep Agents root. See [entry mode](../guides/integrate/extensions/subagents.md#entry-mode) |

Rename older `dataagent.workspaces` to `dataagent.input_workspaces`. Each item has a unique `name` matching `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`, a unique resolved `path` to a readable existing directory, and `read_only: true`.

Higher layers replace `entry` when present. Restart after a change. Extension refresh retains it. `/healthz` reports the value or `null`; a bad name fails first graph assembly with HTTP 409. Entry wrappers omit product hooks, model/tool call limits, ignored-tool middleware, and input workspaces. The host timeout still applies.

### Repetition and loop detection

Local repetition and tool-loop detection are enabled by default. On the first tool-loop hit, the model gets one chance to change its approach. A second hit stops the run. Use `ignored_tools` for intentional polling. Compiled entry graphs, compiled subagents, and the built-in `general-purpose` without a plugin override only use text detection.

```yaml
dataagent:
  loop_detection:
    enabled: true
    ignored_tools:
    - jobs__get_status
```

Set `enabled: false` to disable both checks. Restart the server or recreate the SDK agent after changing settings. A text hit or a second tool-loop hit raises `LoopDetectedError` in the SDK and sends `LOOP_DETECTED` in the REST stream. Completed operations are not replayed. See the [execution implementation](../../development/architecture/execution.md#loop-detection) for thresholds and middleware placement.

<a name="model-settings"></a>

## `models.<id>`

| Field | Default | Constraint |
| --- | --- | --- |
| `provider` | `"openai"` | For OpenAI-compatible providers |
| `name` | Required | Nonempty provider model name. May use `$env{...}` |
| `base_url` | Required | Nonempty API root, usually ending in `/v1` |
| `api_key` | Required | Nonempty, preferably `$env{LLM_API_KEY}` |
| `max_retries` | 2 | Nonnegative HTTP retries in addition to the first request. 0 disables retries |
| `temperature` | Omitted | Number from 0 to 2 |
| `max_tokens` | Omitted | Positive integer |
| `extra_body` | `{}` | Passed to Chat Completions |

Models use Chat Completions with `use_responses_api=False`. There is no `response_format` field. Optional sampling, token, and extra body settings are passed only when configured. Startup expands the default model's environment values. Other models use the startup environment snapshot on first `get_model`. See [environment precedence](cli-and-environment.md#environment-variables).

## `plugins`

`enabled` and `paths` are list replacements and default to `[]`. Enabled IDs are unique. Each path names one plugin directory. `config` defaults to `{}` and merges by plugin ID. The plugin validates its own values. An enabled ID matches exactly one candidate whose directory name agrees with `.plugin.yaml`.

<a name="server-settings"></a>

## `server`

The npm launcher, Web proxy, and TUI without arguments read the explicit YAML or Home YAML. At startup, `./deploy.sh start` records the resolved hostname as `listen.apiHost` and port as `ports.api` in `storage/run/deployment.json`. Later `tui`, `status`, and `doctor` use that address. First deploy creates Home YAML from the template and sets the selected host and port. Existing files only receive those two changes. Invalid YAML is not overwritten. Run start or restart to record subsequent address changes.

| Field | Default | Constraint |
| --- | --- | --- |
| `host` | `"127.0.0.1"` | Bind address, such as `127.0.0.1` or `0.0.0.0` |
| `port` | 8790 | Integer from 1 to 65535. Occupied ports fail startup |

## `a2a`

`card.name` and `card.description` default to `DataAgent` and must be nonblank when present. `remote_agents` defaults to `[]` and is replaced as a whole. Unique IDs register outbound tools and do not control inbound routes.

| Remote field | Default | Constraint |
| --- | --- | --- |
| `id` | Required | `[a-z][a-z0-9_-]{0,63}`; tool name is `a2a__<id>` |
| `url` | Required | Service root beginning with HTTP or HTTPS |
| `description` | `""` | Empty uses the default tool description |
| `auth_token` | Omitted | Bearer token, redacted from errors |
| `timeout_seconds` | Host timeout | Positive when set |

Remote contexts are separated by local thread. See [A2A](a2a.md).

## `semantic`

Omit the object to disable the client.

| Field | Default | Constraint |
| --- | --- | --- |
| `base_url` | Required | Nonblank; normalized to `/api/semantic/v1` |
| `username`, `password` | Omitted | Nonempty pair |
| `timeout_seconds` | 30 | Positive finite number |
| `semantic_scenario` | Omitted | Scenario name sent as `X-Semantic-Scenario` except on health requests |
| `tls.verify_server` | `false` | With no client certificate or cipher configuration, false disables server certificate verification |
| `tls.ca_file` | Omitted | CA file when verification is enabled |
| `tls.client_cert`, `tls.client_key` | Omitted | Must be provided together |
| `tls.ciphers` | Omitted | Nonempty value passed to SSL `set_ciphers` |

<a name="configuration-merge"></a>

## Merging

Select one product file, explicit `--config` or Home `config.yaml`. Startup cwd files and `.dataagent/` do not participate. The [environment file](cli-and-environment.md#environment-variables) is selected separately.

1. Read the selected file and defaults for preliminary `plugins.enabled` and `plugins.paths`.
2. Resolve built-in, Home, and explicit candidates in enabled order. Follow `requires` recursively and load dependencies first, once per ID. Deduplicate real directories and reject distinct candidates for one enabled ID.
3. In load order, apply each manifest's `params` and `defaults_file`. Manifest params apply only to that plugin and can use `$env{NAME}`. Defaults files contribute only `plugins.config`. Overlay the product file last.
4. Fill omitted program defaults. Plugin factories separately fill omitted business parameters with their models. Built-in nl2sql, for example, obtains its defaults from its parameter model.

| Value | Merge rule |
| --- | --- |
| Objects, including `models` and `plugins.config` | Recursive field merge. Omission inherits and `{}` does not clear |
| Ordinary lists and scalars | The higher layer replaces. `[]` clears lists |
| Agent hooks and `run_hooks.before_run` / `after_run` | Append in source order, including duplicates. Empty lists or groups do not clear |
| `null` | An explicit value where allowed |

Product plugin paths and inputs resolve from selected YAML. Manifest resources and workflow hooks declared in manifest params or defaults resolve from the plugin root. Manifest resources must remain inside that root. Plugins interpret ordinary business path strings. `output_root` requires an absolute or `~/` path. See the [worked merge example](../guides/configuration.md#precedence).
