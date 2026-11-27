# 子代理

子代理是插件清单 `subagents` 引用的一份 YAML。模型通过内置 `task` 工具把子任务交给它。

示例：[general-purpose.yaml](../../../../../examples/plugins/common/subagents/general-purpose.yaml)。

## 最小声明

```yaml
schema_version: 1
name: general-purpose
description: Handle focused research and file inspection.
system_prompt_file: prompts/general-purpose.md
```

描述可直接填写或从文件读取。提示使用相对插件根的 `system_prompt_file`。名称、路径和字段限制见[子代理声明](../../../reference/extension-schema.md#subagent-declaration)。

## 继承

| 字段 | 省略时 | 写出来时 |
| --- | --- | --- |
| `tools` | 继承父 Agent 的工具 | 只使用列出的工具。短名补成 `<plugin-id>__<短名>`；已含 `__` 的名字原样解析。可显式引用 `scope: subagents` 的工具。未知或重复引用会使编译失败 |
| `skills` | 继承根 Agent 的 Skill 来源 | 按路径重新编译。`[]` 表示不加载 |
| `hooks` | 没有 Hook | 只作用于这个子代理，语法与根 Hook 相同 |

`tools: []` 表示不附加声明工具。省略和空列表是两种结果。

标准子代理默认使用父 Agent 的模型。可用 `model` 选取配置中的模型别名，并用 `temperature` 单独覆盖温度。它有自己的系统提示，并附上文件系统说明。

## 编译型子代理

`kind: compiled` 引用 Python 图工厂，例如 [nl2sql](../../../../../dataagent/plugins/nl2sql/subagents/nl2sql.yaml)。编译型子代理自行设置提示、模型和工具，字段限制见[子代理声明](../../../reference/extension-schema.md#subagent-declaration)。

签名是 `factory(params, services)`。`params` 是 `plugins.config.<plugin-id>`（不含 `run_hooks`）。`services` 的类型是 `dataagent.SubagentServices`，只有四个字段：`get_model`（按别名取模型）、`semantic_client`（未配置语义服务时为空）、`outputs_dir`（当前会话的 `outputs/`，没有会话时为空；工厂可以在这里写主代理能读取的结果文件）、`run_hooks`（本插件的编译工作流 Hook，见 [扩展声明](../../../reference/extension-schema.md#compiled-workflow-hook)）。

工厂返回以下之一：

- langchain `Runnable`
- `{"runnable": Runnable, "description": "..."}`：`description` 追加到声明的描述之后，用于告诉主代理运行期事实，例如已绑定的数据源。不要写入密码、主机、文件路径等连接参数

其他返回值或多余的键会使编译失败。

## 执行限制

`dataagent.limits` 对根 Agent 和各标准子代理每次调用单独计算。编译型子代理在自己的执行图中管理模型和工具调用限额。

- `model_calls_per_agent` 默认 128。这是该 Agent 的模型循环上限，不是整棵子树的预算，也不把一次 HTTP 重试算成多次循环。
- `tool_calls_per_agent` 默认 128。超限抛错。新一轮重新计数。

根 Agent 会装上待办列表中间件。子代理不装。

未声明名为 `general-purpose` 的子代理时，运行时会加入内置的 general-purpose。它使用父级工具和 Skill。插件中为其他子代理声明的 Hook 不会套用到它。要为这个名字配置 Hook，请在插件中声明同名子代理。标准子代理的限额仍读取 `dataagent.limits`。

服务端 AG-UI 适配器使用 `subagent_visibility="attributed"`，子代理步骤归属到当前根运行，客户端可展示其过程；它们不会变成独立的用户回合。A2A 通过 Task 提供最终结果，可等待结束或先返回任务号再查询，不提供这条 AG-UI 事件流。

## 入口模式 { #entry-mode }

`dataagent.entry` 设成某个编译型子代理的 `name` 时，装配不再创建 Deep Agents 根图，只把这个编译型入口包进同一层壳。每个编译型入口走同一层壳，宿主不按入口名重建插件图。

工厂返回已可运行的 Runnable。它从状态读入 `messages`，将要写入 Checkpoint 的消息放入返回值的 `messages`。插件内部执行由 Runnable 管理。宿主封装和事件转发的实现见[执行过程](../../../../development/architecture/execution.md)。

```yaml
dataagent:
  entry: nl2sql
```

名字必须匹配 `[a-z][a-z0-9_-]{0,63}`，并且是已启用插件里 `kind` 为 `compiled` 的子代理。标准子代理不能当入口。找不到时，错误列出当前可用的编译型子代理名。

限制：

- 只接受带 `runnable` 的编译型子代理。
- `dataagent.hooks`、`limits.model_calls_per_agent` / `limits.tool_calls_per_agent`、`input_workspaces`，以及插件里的 Skill、工具（含 MCP）、`system_prompt`、其他子代理和根中间件，仍按原路径编译或留在配置里，不装进入口图，也不因此报错。`limits.timeout_seconds` 仍约束宿主执行阶段；A2A 排队等时限边界见 [配置参考](../../../reference/configuration.md#dataagent)。
- 不要用同一个 `thread_id` 在 Deep Agents 根和入口图之间恢复。两边的 Checkpoint 状态形状不同，根图还有 `messages` 以外的通道。
- 只保证异步调用（`ainvoke`、`astream`）。nl2sql 这类只实现了 async 的 Runnable，同步 `invoke` / `stream` 可能失败。
- 名字在插件工厂之前对照声明核对。写错的名字不会被某个工厂的参数错误盖住。MCP 连接和全部插件工厂仍会执行；名字对上之后，工厂失败仍使这次装配失败。REST 在初始化对话引擎时发现 MCP 工具，发现失败时引擎不就绪。
- 错误的入口名不会让 `/healthz` 失败。响应里的 `entry` 是配置值，未设置时为 `null`。`POST /api/v1/runs` 第一次装配图时返回 HTTP 409。
- 修改 `entry` 要重启。扩展刷新会重读插件和 Hook，但保留原来的 `entry`。
