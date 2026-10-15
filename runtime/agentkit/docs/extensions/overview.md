# 如何选择扩展

先选最小的一种。声明字段在 [扩展声明](../reference/extension-schema.md)。

| 你要增加的东西 | 用 | 最小落点 |
| --- | --- | --- |
| 一段模型按需阅读的操作说明 | Skill | `<home>/skills/<name>/SKILL.md` |
| 一个可调用的 Python 函数 | 插件里的工具 | `plugins/<id>/.plugin.json` 的 `tools` |
| 在模型或工具前后检查、改状态或拒绝 | Hook | Home 配置的 `dataagent.hooks`，或插件 `hooks` |
| 一组说明、工具和 Hook，可单独开关 | 插件 | `<home>/plugins/<id>/` |
| 一次有自己提示和工具范围的子任务 | 子代理 | 插件 `subagents` 指向的 JSON |
| 已有的外部工具服务 | MCP | `<home>/.mcp.json` 或已启用插件的 `.mcp.json` |
| 查询企业语义元数据 | 语义客户端 | 产品配置 `semantic`，在工具里用 `runtime.context.semantic_client` |

## 组合方式

- 独立 Skill 不需要插件。Home 的 `skills/` 会和已启用插件的 Skill 一起编译。
- 工具、子代理和插件级 Hook 必须写在 `.plugin.json`。只把文件放进目录不会注册。
- 根 Agent 的 Hook 不会自动套到子代理。子代理在自己的 JSON 里声明。
- `plugins.enabled: []` 只是不启用插件。独立 Skill 和 Home Hook 仍然生效。
- 内置文件工具 `ls`、`read_file`、`write_file`、`edit_file`、`delete`、`glob`、`grep`、`task`、`write_todos` 由运行时提供。插件工具名是 `<plugin-id>__<local-id>`。MCP 工具名是 `mcp__<scope>__<server>__<tool>` 这一类带命名空间的名字。

## 示例

| 示例 | 路径 |
| --- | --- |
| 带工具、Skill、子代理和 Hook 的插件 | [examples/plugins/common](../../examples/plugins/common/README.md) |
| 在工具里调用另一个模型 | [examples/plugins/llm-utils](../../examples/plugins/llm-utils/README.md) |
| 两个个人 Hook | [examples/hooks](../../examples/hooks/check_input.py) |
| 本地 stdio MCP | [examples/mcp](../../examples/mcp/README.md) |

`builtin-plugins/attribution-analysis` 只有说明，没有 `.plugin.json`，不能启用。

改完扩展后，REST 和 TUI 的下一轮请求会重新编译。模型和服务配置仍要重启。见 [插件](plugins.md)。
