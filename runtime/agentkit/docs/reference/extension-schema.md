# 扩展声明

产品配置、插件和子代理共用这些形状。模型在 `dataagent.declarations`。未知字段失败。

## 标识

| 用途 | 模式 |
| --- | --- |
| 插件 ID、子代理名、MCP 服务名 | `[a-z][a-z0-9_-]*` |
| 插件内工具 ID | `[a-z][a-z0-9_]*` |
| 保留插件 ID | `user`、`cli` |

根 Agent 名固定为 `dataagent`，不能作为子代理名。

## 插件 `.plugin.json`

`schema_version` 必须是 `2`。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 必填 | 等于目录名 |
| `requires` | 插件 ID 数组 | `[]` | 自动启用本地传递依赖，依赖先加载 |
| `config` | 相对路径或省略 | 省略 | 只含 plugins.config 的 JSON 默认片段 |
| `system_prompt` | 相对路径或省略 | 省略 | UTF-8 Markdown |
| `tools` | 对象 | `{}` | 本地 ID 到 `{ "entrypoint": "file.py:symbol", "scope": "all" }` |
| `skills` | 相对路径数组 | `[]` | 每个路径是含 `SKILL.md` 子目录的目录 |
| `subagents` | 相对路径数组 | `[]` | 每个路径是一份子代理 JSON |
| `hooks` | 分组对象或列表 | `[]` | 只作用于根 Agent |

入口路径相对插件根，解析后必须仍在根内。`file.py:symbol` 的 `symbol` 必须是标识符。
工具 `scope` 默认 `all`；`subagents` 只保留在子代理可引用的注册表中，不加入根 Agent 工具列表。

工具可声明 `enabled_when: "hitl.enabled"`，按该插件配置中的路径决定是否装配；仅精确值 `true` 启用。
生命周期 hook 函数可通过 `handler.state_schema` 声明原生状态 schema，编译器传给 LangChain
middleware 装饰器，使工具返回的额外状态能正常 checkpoint。未声明的 hook 行为不变。

## Hook

分组形式的键只能是：

`before_agent`、`after_agent`、`before_model`、`after_model`、`before_tool`、`after_tool`

每条：

| 字段 | 约束 |
| --- | --- |
| `entrypoint` | 非空。`相对.py:函数` |
| `params` | JSON 对象，默认 `{}` |
| `matcher` | 仅工具事件。非空字符串，精确匹配工具全名 |
| `event` | 扁平列表必填。分组里禁止再写 |

声明使用 `event`、`entrypoint`、`params` 和 `matcher`。

函数契约见 [Hook](../extensions/hooks.md)。

## 子代理 JSON

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | `standard` 或 `compiled` | 默认 `standard`。`compiled` 表示由 Python 工厂提供整张图 |
| `name` | 字符串 | 资源 ID |
| `description` | 非空字符串 | 交给 `task` 工具的说明。和 `description_file` 必须二选一 |
| `description_file` | 相对路径 | 读取为 `description` 的正文 |
| `system_prompt` | 相对路径 | 读取为提示正文。`standard` 必填，`compiled` 禁止 |
| `entrypoint` | `file.py:symbol` | `compiled` 必填，指向图工厂。`standard` 不用 |
| `tools` | 字符串数组或省略 | 省略继承全部；`[]` 不附加声明工具 |
| `skills` | 字符串数组或省略 | 省略继承；`[]` 不加载 |
| `hooks` | 分组或列表 | 默认空，只作用于该子代理 |
| `filesystem_tools` | 原生文件工具名数组或省略 | 省略沿用框架默认；必须含 `read_file`，例如 `["ls", "read_file"]` |

`filesystem_tools` 通过原生 FilesystemMiddleware 设置该子代理的文件工具集合，独立于插件工具 `tools`。
`compiled` 子代理自行管理执行图，不接受 `system_prompt`、`tools`、`skills`、`hooks` 和 `filesystem_tools`。

## 产品配置里的 Hook

`dataagent.hooks` 使用同一 Hook 形状。入口相对该配置文件目录。多层配置把条目追加在一起，每条记住自己的目录。

## Skill 元数据

`SKILL.md` 开头是 YAML frontmatter。编译要求 `name` 和 `description` 非空，且文件不超过 10 MiB。

名称长于 64、含非法字符或与目录名不同时，解析器记录警告并仍然装入。描述长于 1024 时截断。这是内置解析器的行为。

## 编译工作流 Hook

`plugins.config.<id>.hooks` 可声明 `before_run` 和 `after_run`，值为条目数组。
每条仅含 `entrypoint` 和可选的 `params` 对象。
入口相对声明它的插件根或用户配置目录，即使它配置的是另一个插件也不会改变基准目录。
宿主通过 `services.run_hooks` 向编译子代理提供已绑定的处理器；工作流负责在自己的边界调用。
当前内置消费者是 NL2SQL，不会自动把这些 Hook 应用到任意标准子 Agent。

函数签名 `def/async def handle(value, runtime, *, params)`，可返回 None。
`before_run` 返回的字典合并进当次输入；`after_run` 返回完整公开结果字典。
前置按声明顺序，后置逆序；后置不得删除已有的非空 error。
NL2SQL 前置允许 question、schema、schema_str、joins、evidence、sql_rules、few_shot_examples，
显式空字段也保留，不被 Perceptor 默认加载覆盖。后置在图返回时执行，异常中止不执行。
