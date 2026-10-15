# 扩展声明

产品配置、插件和子代理共用这些形状。模型在 `dataagent.declarations`。未知字段失败。

## 标识

| 用途 | 模式 |
| --- | --- |
| 插件 ID、子代理名、MCP 服务名 | `[a-z][a-z0-9_-]*` |
| 插件内工具 ID | `[a-z][a-z0-9_]*` |
| 保留插件 ID | `user`、`cli` |

根 Agent 名固定为 `dataagent-v2`，不能作为子代理名。

## 插件 `.plugin.json`

`schema_version` 必须是 `2`。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 必填 | 等于目录名 |
| `system_prompt` | 相对路径或省略 | 省略 | UTF-8 Markdown |
| `tools` | 对象 | `{}` | 本地 ID 到 `{ "entrypoint": "file.py:symbol" }` |
| `skills` | 相对路径数组 | `[]` | 每个路径是含 `SKILL.md` 子目录的目录 |
| `subagents` | 相对路径数组 | `[]` | 每个路径是一份子代理 JSON |
| `hooks` | 分组对象或列表 | `[]` | 只作用于根 Agent |

`hook_handlers`，或 Hook 条目里的 `handler` / `events`，会要求迁移到版本 2。

入口路径相对插件根，解析后必须仍在根内。`file.py:symbol` 的 `symbol` 必须是标识符。

## Hook

分组形式的键只能是：

`before_agent`、`after_agent`、`before_model`、`after_model`、`before_tool`、`after_tool`

每条：

| 字段 | 约束 |
| --- | --- |
| `entrypoint` | 非空。`相对.py:函数` |
| `params` | JSON 对象，默认 `{}`。字段名是 `params` |
| `matcher` | 仅工具事件。非空字符串，精确匹配工具全名 |
| `event` | 扁平列表必填。分组里禁止再写 |

声明使用 `event`、`entrypoint`、`params` 和 `matcher`。`command`、`timeout_seconds`、`handler`、`events`、`match` 不能出现在条目里。

函数契约见 [Hook](../extensions/hooks.md)。

## 子代理 JSON

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | 字符串 | 资源 ID |
| `description` | 非空字符串 | 交给 `task` 工具的说明 |
| `system_prompt` | 相对路径 | 读取为提示正文 |
| `tools` | 字符串数组或省略 | 省略继承全部；`[]` 不附加声明工具 |
| `skills` | 字符串数组或省略 | 省略继承；`[]` 不加载 |
| `hooks` | 分组或列表 | 默认空，只作用于该子代理 |

## 产品配置里的 Hook

`dataagent.hooks` 使用同一 Hook 形状。入口相对该配置文件目录。多层配置把条目追加在一起，每条记住自己的目录。

## Skill 元数据

`SKILL.md` 开头是 YAML frontmatter。编译要求 `name` 和 `description` 非空，且文件不超过 10 MiB。

名称长于 64、含非法字符或与目录名不同时，解析器记录警告并仍然装入。描述长于 1024 时截断。这是内置解析器的行为。
