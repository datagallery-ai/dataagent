# 如何选择扩展

先选最小的一种。声明字段在 [扩展声明](../../../reference/extension-schema.md)。

| 你要增加的东西 | 用 | 最小落点 |
| --- | --- | --- |
| 一段模型按需阅读的操作说明 | [Skill](skills.md) | `<home>/skills/<name>/SKILL.md` |
| 一个可调用的 Python 函数 | [插件里的工具](tools.md) | `plugins/<id>/.plugin.yaml` 的 `tools` |
| 在模型或工具前后检查、改状态或拒绝 | [Hook](hooks.md) | Home 配置的 `dataagent.hooks`，或插件 `hooks` |
| 一组说明、工具和 Hook，可单独开关 | [插件](plugins.md) | `<home>/plugins/<id>/` |
| 一次有自己提示和工具范围的子任务 | [子代理](subagents.md) | 插件清单的 `subagents` 引用的 YAML |
| 已有的外部工具服务 | [MCP](mcp.md) | `<home>/.mcp.yaml` 或已启用插件的 `.mcp.yaml` |
| 在模型或工具执行期间请求人工反馈 | [人工交互](hitl.md) | 插件清单的 `hitl` 字段 |
| 查询企业语义元数据 | [语义客户端](semantic-service.md) | 产品配置 `semantic`，在工具里用 `runtime.context.semantic_client` |

## 组合方式

- 独立 Skill 不需要插件。Home 的 `skills/` 会和已启用插件的 Skill 一起编译。
- 工具、子代理和插件级 Hook 必须写在 `.plugin.yaml`。只把文件放进目录不会注册。
- 根 Agent 的 Hook 不会自动套到子代理。子代理在自己的 YAML 里声明。
- `plugins.enabled: []` 只是不启用插件。独立 Skill 和 Home Hook 仍然生效。
- 内置工具由运行时提供。插件、MCP 和 A2A 工具的注册名见[扩展声明](../../../reference/extension-schema.md#extension-identifiers)。

## 示例

| 示例 | 路径 |
| --- | --- |
| 带工具、Skill、子代理和 Hook 的插件 | [examples/plugins/common](../../../../../examples/plugins/common/README.md) |
| 在工具里调用另一个模型 | [examples/plugins/llm-utils](../../../../../examples/plugins/llm-utils/README.md) |
| 两个个人 Hook | [examples/hooks](../../../../../examples/hooks/check_input.py) |
| 本地 stdio MCP | [examples/mcp](../../../../../examples/mcp/README.md) |

安装和更新步骤见[插件指南](plugins.md)。
