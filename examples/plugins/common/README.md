# Common：外部插件示例

这是内部验证用插件，不随 wheel 分发，也不默认启用。
将整个目录（包含隐藏的 `.plugin.yaml` 和 `.mcp.yaml`）复制到 `<home>/plugins/common`，
然后在 `<home>/config.yaml` 的 `plugins.enabled` 中加入 `"common"`；无需配置 `plugins.paths`。
随 `plugins.enabled` 中的 `common` 启用，不需要另外注册 SubAgent。

| 组件 | 职责 |
| --- | --- |
| `subagents/general-purpose.yaml` | 通用委派 Agent，名称保持 `general-purpose`，覆盖 Deep Agents 自动生成的同名 Agent |
| `prompts/general-purpose.md` | 子 Agent 的通用任务流程、文件路径约定和结果要求 |
| `prompts/agent.md` | 给根 Agent 追加的插件使用说明 |
| `tools/statistics.py` | 无副作用的 `common__summarize_numbers`，返回 count/sum/mean/min/max |
| `skills/number-summary/` | 显式数字列表的统计流程 |
| `skills/tabular-inspection/` | CSV/TSV 结构与质量检查流程，复用原生文件工具和 shell，不新增依赖 |
| `hooks/audit.py` | 六类生命周期审计；根与子 Agent 分别声明，不隐式继承 |
| `.mcp.yaml` | 插件 MCP 配置入口，当前为空，不连接外部服务 |

统计能力由根 Agent 或通用子 Agent 使用。
通用子 Agent 省略 `tools`，由 Deep Agents 继承根的显式工具（包括 MCP 工具）；
省略 `skills` 时由 Compiler 传入根已编译的全部 Skill 来源（包括 Home Skills）。
任意声明式子 Agent 都遵循相同规则：显式列表限定来源，`[]` 表示不接入这些工具或 Skills。
文件工具等 Deep Agents 自带工具不由 `tools: []` 关闭。

`assembly/graph.py` 仅对编译得到的子 Agent 添加独立的模型/工具调用限额和错误处理，
不硬编码任何业务子 Agent。启用 common 时，这些策略同样覆盖 `general-purpose`。
禁用 common 后，不加载其提示词、审计与 Skill；Deep Agents 仍可能自动创建原生
`general-purpose`，该原生默认子 Agent 不携带这里声明的审计或 DataAgent 对声明式子 Agent
添加的限额。本次未修改上游默认行为，也未新增模型 profile 全局注册。

## MCP

随附的 [`.mcp.yaml`](.mcp.yaml) 声明空的服务集合。添加名为 `analytics` 的服务后，它的 `query` 工具会注册为 `mcp__common__analytics__query`。配置与刷新规则见 [MCP 接入指南](../../../docs/zh/guides/integrate/extensions/mcp.md)。
