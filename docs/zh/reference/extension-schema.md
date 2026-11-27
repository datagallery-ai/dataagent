# 扩展声明

产品配置、插件和子代理共用这些形状。模型在 `dataagent.config.declarations`。未知字段失败。

## 标识 { #extension-identifiers }

| 用途 | 模式 |
| --- | --- |
| 插件 ID、插件内工具 ID、子代理名、MCP 服务名、`dataagent.entry`、A2A 远端 `id` | `[a-z][a-z0-9_-]{0,63}`（`dataagent.config.declarations.NAME_PATTERN`） |
| 用户、会话、输入目录 `name` | `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`（`OPAQUE_ID_PATTERN`） |

工具名统一用双下划线 `__` 分隔命名空间：

| 来源 | 模型看到的工具名 | 例子 |
| --- | --- | --- |
| 插件工具 | `<插件 ID>__<工具 ID>` | `common__summarize_numbers` |
| A2A 远端 | `a2a__<id>` | `a2a__finance` |
| MCP | `mcp__<scope>__<服务名>__<MCP 工具名>`；Home 的 scope 是 `user`，插件的是插件目录名 | `mcp__user__math__add` |
| 内置 | 无前缀 | `read_file`、`task` |

插件 ID 保留 `user`、`cli`。根 Agent 名固定为 `dataagent`，不能作为子代理名。

## 插件 `.plugin.yaml` { #plugin-manifest }

`schema_version` 默认 `3`。显式填写时也必须为 `3`。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 必填 | 等于目录名 |
| `requires` | 插件 ID 数组 | `[]` | 自动启用本地传递依赖，依赖先加载 |
| `params` | 对象或省略 | `{}` | 当前插件的业务参数默认值；低于 defaults_file 和产品 plugins.config 的优先级 |
| `defaults_file` | 相对路径或省略 | 省略 | 只含 plugins.config 的 YAML 默认片段 |
| `system_prompt_file` | 相对路径或省略 | 省略 | UTF-8 Markdown |
| `tools` | 对象 | `{}` | 本地 ID 到 `{ "entrypoint": "file.py:symbol", "scope": "all" }` |
| `skills` | 相对路径数组 | `[]` | 每个路径是含 `SKILL.md` 子目录的目录 |
| `subagents` | 相对路径数组 | `[]` | 每个路径是一份子代理 YAML |
| `hooks` | 分组对象或列表 | `[]` | [根 Agent Hook](#agent-hooks) |
| `hitl` | 对象或省略 | 省略时不启用 | [人工交互](#hitl-configuration) |

资源路径相对插件根，解析后必须仍在根内。`file.py:symbol` 需要指向 Python 文件，`symbol` 必须是标识符。
工具 `scope` 默认 `all`；`subagents` 只保留在子代理可显式引用的注册表中，不加入根 Agent 的工具列表或子代理继承的工具列表。

## Hook { #agent-hooks }

分组形式的键只能是：

`before_agent`、`after_agent`、`before_model`、`after_model`、`before_tool`、`after_tool`

每条：

| 字段 | 约束 |
| --- | --- |
| `entrypoint` | 非空。`相对.py:函数` |
| `params` | JSON 对象，默认 `{}` |
| `matcher` | 仅工具事件。非空字符串，精确匹配工具全名 |
| `event` | 扁平列表必填。分组里禁止再写 |

`matcher` 省略时匹配该 Agent 的全部工具。插件和子代理 Hook 的 `entrypoint` 相对插件根，产品配置中的入口相对该配置文件目录。解析后的路径都必须留在对应根内。

状态更新和工具返回值见 [Hook 函数签名](../guides/integrate/extensions/hooks.md#hook-signatures)。

## 子代理 YAML { #subagent-declaration }

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | `1` | 默认 `1`。其他值加载失败 |
| `kind` | `standard` 或 `compiled` | 默认 `standard`。`compiled` 表示由 Python 工厂提供整张图 |
| `name` | 字符串 | 资源 ID |
| `description` | 非空字符串 | 交给 `task` 工具的说明。和 `description_file` 必须二选一 |
| `description_file` | 相对路径 | 读取为 `description` 的正文 |
| `system_prompt_file` | 相对路径 | 读取为提示正文。`standard` 必填，`compiled` 禁止 |
| `entrypoint` | `file.py:symbol` | `compiled` 必填，指向图工厂。`standard` 不用 |
| `tools` | 字符串数组或省略 | 省略继承父 Agent 的工具，`[]` 不附加声明工具 |
| `model` | 模型别名或省略 | 省略使用父 Agent 的模型 |
| `temperature` | 0 到 2 的数或省略 | 单独覆盖该子代理的模型温度 |
| `skills` | 字符串数组或省略 | 省略继承；`[]` 不加载 |
| `hooks` | 分组或列表 | 默认空，只作用于该子代理 |
| `filesystem_tools` | 原生文件工具名数组或省略 | 省略沿用框架默认；必须含 `read_file`，例如 `["ls", "read_file"]` |

`filesystem_tools` 通过原生 FilesystemMiddleware 设置该子代理的文件工具集合，独立于插件工具 `tools`。
`compiled` 子代理自行管理执行图。配置 `system_prompt_file`、`tools`、`skills`、非空 `hooks`、`filesystem_tools`、`model` 或 `temperature` 会被拒绝。图工厂与入口示例见[子代理指南](../guides/integrate/extensions/subagents.md)。

## 产品配置里的 Hook

`dataagent.hooks` 使用同一 Hook 形状。入口相对该配置文件目录。多层配置把条目追加在一起，每条记住自己的目录。

## 人工交互配置 { #hitl-configuration }

插件清单显式提供 `hitl` 时，`enabled` 默认 `true`，可用 `false` 禁用该插件的人工交互资源。设置为 `false` 时仍校验声明结构。

| 字段 | 类型与约束 |
| --- | --- |
| `enabled` | 布尔值，默认 `true` |
| `instructions` | 相对插件根的非空 Markdown 文件路径，选填 |
| `policies` | 策略数组，默认 `[]` |

每条策略使用该来源内唯一的 `id`，格式与插件 ID 一致。可填写 `version`（默认为字符串 `"1"`）、`at`（使用 [Hook 阶段](#agent-hooks)，默认 `before_tool`）、`handler`（相对插件根的 `file.py:ClassName`）、`params`（JSON 对象，默认 `{}`）。`matcher` 仅适用于 `before_tool` 和 `after_tool`，精确匹配注册后的工具名。处理器示例与恢复流程见 [HITL 指南](../guides/integrate/extensions/hitl.md)。

## Skill 元数据 { #skill-metadata }

`SKILL.md` 开头使用 YAML frontmatter，必须能解析为映射，且提供非空 `name` 和 `description`。解析器检查解码后的整个文件，最多接受 10 × 2²⁰ 个字符。

名称建议使用小写字母、数字和连字符，最长 64 个字符，并与目录名一致。连字符不置于首尾，也不连续使用。不符合时解析器记录警告并继续装入。描述超过 1024 个字符时截断。可选字段包括 `license`、`compatibility`、`metadata` 和 `allowed-tools`。

## 编译工作流 Hook { #compiled-workflow-hook }

`plugins.config.<id>.run_hooks` 可声明 `before_run` 和 `after_run`，值为条目数组。键名和 Agent Hook 的 `hooks` 分开。
每条仅含 `entrypoint` 和可选的 `params` 对象。
入口相对声明它的插件根或用户配置目录，即使它配置的是另一个插件也不会改变基准目录。
宿主通过 `services.run_hooks` 向编译子代理提供已绑定的处理器；工作流负责在自己的边界调用。
当前内置消费者是 NL2SQL。插件没有编译子代理却写了 `run_hooks` 时，装配失败，不会静默忽略。

函数签名为 `handle(value, runtime, *, params)`，支持 `def` 和 `async def`。每次都深拷贝传入的值与参数；返回 `None` 保留原值。
`before_run` 返回的字典合并进当次输入；`after_run` 返回完整公开结果字典。
前置按声明顺序，后置逆序；后置不得删除已有的非空 error。
NL2SQL 前置允许 question、schema、schema_str、joins、evidence、sql_rules、few_shot_examples，
显式空字段也保留，不被 Perceptor 默认加载覆盖。后置在图返回时执行，异常中止不执行。
