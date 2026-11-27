# Choose an extension

Choose the smallest extension that covers your task. See the [extension schema](../../../reference/extension-schema.md) for declaration fields.

| What you want to add | Use | Where it goes |
| --- | --- | --- |
| Instructions the model reads on demand | [Skill](skills.md) | `<home>/skills/<name>/SKILL.md` |
| A callable Python function | [Plugin tool](tools.md) | `tools` in `plugins/<id>/.plugin.yaml` |
| A check, state update, or rejection around a model or tool call | [Hook](hooks.md) | Home `dataagent.hooks` or plugin `hooks` |
| A group of instructions, tools, and hooks that can be enabled together | [Plugin](plugins.md) | `<home>/plugins/<id>/` |
| A focused task with its own prompt and tool scope | [Subagent](subagents.md) | YAML referenced by the manifest's `subagents` |
| An existing external tool server | [MCP](mcp.md) | `.mcp.yaml` in Home or an enabled plugin |
| A human feedback request during model or tool execution | [Human feedback](hitl.md) | Manifest's `hitl` field |
| Enterprise semantic metadata | [Semantic client](semantic-service.md) | Product setting `semantic`, exposed as `runtime.context.semantic_client` |

## Combine extensions

- Home Skills compile alongside Skills from enabled plugins. Standalone Skills can run without a plugin.
- Register tools, subagents, and plugin hooks in `.plugin.yaml`.
- Subagents declare their own hooks. Root hooks apply to the root.
- `plugins.enabled: []` disables plugins. Standalone Skills and Home hooks still apply.
- The runtime supplies built-in tools. See [registered names](../../../reference/extension-schema.md#extension-identifiers) for plugin, MCP, and A2A tools.

## Examples

| Example | Location |
| --- | --- |
| Plugin with tools, Skills, a subagent, and hooks | [examples/plugins/common](../../../../../examples/plugins/common/README.md) |
| Calling a different model from a tool | [examples/plugins/llm-utils](../../../../../examples/plugins/llm-utils/README.md) |
| Two Home hooks | [examples/hooks](../../../../../examples/hooks/check_input.py) |
| Local stdio MCP | [examples/mcp](../../../../../examples/mcp/README.md) |

See the [plugin guide](plugins.md) for installation and refresh.
