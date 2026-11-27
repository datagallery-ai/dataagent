# Extension schema

Product settings, plugins, and subagents share these declarations. Models live in `dataagent.config.declarations`. Unknown fields are rejected.

<a name="extension-identifiers"></a>

## Identifiers

| Use | Pattern |
| --- | --- |
| Plugin and tool IDs, subagent and MCP service names, `dataagent.entry`, A2A remote ID | `[a-z][a-z0-9_-]{0,63}` |
| User ID, session ID, input directory `name` | `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` |

| Source | Model-facing name | Example |
| --- | --- | --- |
| Plugin | `<plugin-id>__<tool-id>` | `common__summarize_numbers` |
| A2A | `a2a__<id>` | `a2a__finance` |
| MCP | `mcp__<scope>__<service>__<tool>` with Home scope `user` or plugin directory | `mcp__user__math__add` |
| Built-in | Unprefixed | `read_file`, `task` |

`user` and `cli` are reserved plugin IDs. Root agent name `dataagent` is reserved for the root.

<a name="plugin-manifest"></a>

## Plugin `.plugin.yaml`

`schema_version` defaults to 3 and only accepts 3.

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | Required | Directory name |
| `requires` | `[]` | Local transitive dependencies, compiled first |
| `params` | `{}` | Plugin parameter defaults below `defaults_file` and product `plugins.config` |
| `defaults_file` | Omitted | Relative YAML containing only `plugins.config` |
| `system_prompt_file` | Omitted | Relative UTF-8 Markdown path |
| `tools` | `{}` | Local ID to `{"entrypoint":"file.py:symbol","scope":"all"}` |
| `skills` | `[]` | Relative paths to directories containing Skill subdirectories |
| `subagents` | `[]` | Relative subagent YAML paths |
| `hooks` | `[]` | Grouped or flat [root hook declarations](#agent-hooks) |
| `hitl` | Omitted | Optional [human feedback](#hitl-configuration) |

Paths resolve within the plugin root. The entrypoint points to a Python file and the symbol must be a valid identifier. Tool scope defaults to `all`. Scope `subagents` keeps the tool in the registry for explicit subagent references, separate from root and inherited lists.

<a name="agent-hooks"></a>

## Hooks

Grouped keys are `before_agent`, `after_agent`, `before_model`, `after_model`, `before_tool`, and `after_tool`.

| Field | Constraint |
| --- | --- |
| `entrypoint` | Nonempty `relative.py:function` |
| `params` | JSON object, default `{}` |
| `matcher` | Tool events only, nonempty exact registered name. Omit to match all tools for the agent |
| `event` | Required in flat lists and forbidden inside groups |

Plugin and subagent hook paths resolve from the plugin root. Product hook paths resolve from the declaring file's directory. Both must remain in that root. Multi-layer product settings append hooks while retaining each declaration's directory. See [signatures](../guides/integrate/extensions/hooks.md#hook-signatures).

<a name="subagent-declaration"></a>

## Subagent YAML

| Field | Constraint |
| --- | --- |
| `schema_version` | 1 only, default 1 |
| `kind` | `standard` (default) or `compiled` |
| `name` | Resource ID |
| `description`, `description_file` | Exactly one. Nonempty inline text or relative description file for the task tool |
| `system_prompt_file` | Relative prompt file, required for standard and forbidden for compiled |
| `entrypoint` | `file.py:symbol` factory for compiled |
| `tools` | Optional list. Omit to inherit, `[]` attaches no declared tools |
| `model` | Optional alias, otherwise inherit |
| `temperature` | Optional number from 0 to 2 |
| `skills` | Optional paths. Omit to inherit, `[]` loads none |
| `hooks` | Grouped or flat, default empty, scoped to this subagent |
| `filesystem_tools` | Optional list of native filesystem tools including `read_file`, e.g. `["ls","read_file"]` |

Native FilesystemMiddleware uses `filesystem_tools` independently of plugin `tools`. A compiled subagent owns its graph. Its declaration rejects `system_prompt_file`, `tools`, `skills`, nonempty hooks, `filesystem_tools`, `model`, and `temperature`. See [subagents](../guides/integrate/extensions/subagents.md).

<a name="hitl-configuration"></a>

## Human feedback configuration

When `hitl` is present, `enabled` defaults to `true`. Setting it false disables the plugin's HITL resources but still validates the structure. `instructions` is an optional relative path to nonempty Markdown. `policies` defaults to `[]`.

Each policy has a source-unique `id` using the plugin pattern. Optional fields are `version` (default string `"1"`), `at` (one of the [hook phases](#agent-hooks), default `before_tool`), plugin-relative `handler: file.py:ClassName`, and JSON `params` (default `{}`). `matcher` is only for before/after-tool and matches the exact registered name. See [HITL guide](../guides/integrate/extensions/hitl.md).

<a name="skill-metadata"></a>

## Skill metadata

`SKILL.md` starts with YAML frontmatter mapping with nonempty `name` and `description`. The decoded file limit is 10 × 2²⁰ characters. Names should match their directory and use up to 64 lowercase letters, digits, and hyphens, without leading, trailing, or repeated hyphens. Invalid forms warn but still load. Descriptions truncate at 1,024 characters. Optional fields include `license`, `compatibility`, `metadata`, and `allowed-tools`.

<a name="compiled-workflow-hook"></a>

## Compiled workflow hooks

`plugins.config.<id>.run_hooks` accepts `before_run` and `after_run` arrays independently of agent hooks. Entries contain `entrypoint` and optional `params`. Paths are relative to the declaring plugin or user file even when configuring another plugin. Compiled subagents receive the registered handlers in `services.run_hooks` and call them at their own workflow boundaries. NL2SQL is the built-in consumer. Declaring hooks for a plugin without a compiled subagent fails assembly.

Use `handle(value, runtime, *, params)` with `def` or `async def`. Each call receives deep copies. `None` retains the input. Before-run dicts merge into input. After-run returns the complete public result and must retain a nonempty error. Before hooks run forward, after hooks in reverse. NL2SQL accepts before-run keys `question`, `schema`, `schema_str`, `joins`, `evidence`, `sql_rules`, and `few_shot_examples`. Explicitly empty values survive Perceptor defaults. After hooks run when the graph returns, excluding exceptions.
