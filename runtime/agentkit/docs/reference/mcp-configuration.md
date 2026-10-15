# MCP 配置

文件名固定为 `.mcp.json`。只从 Home 和已启用插件的根目录读取。标准 JSON，支持 `$env{NAME}`。

操作步骤见 [MCP](../extensions/mcp.md)。覆盖三种传输的模板见 [examples/mcp/mcp.full.json](../../examples/mcp/mcp.full.json)。使用前替换其中的示例地址。

## 顶层

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mcpServers` | 对象 | 必填。键是服务名，匹配 `[a-z][a-z0-9_-]*` |

没有其他顶层字段。

## stdio

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `type` | `"stdio"` | `"stdio"` | 可省略 |
| `command` | 非空字符串 | 必填 | 含 `/` 的相对路径相对 `.mcp.json` 目录 |
| `args` | 字符串数组 | `[]` | 原样传递 |
| `env` | 字符串到字符串 | `{}` | 该进程的环境。值里的 `$env{NAME}` 先展开 |

内部连接还固定 `cwd` 为该文件所在目录，`transport` 为 `stdio`。

## http 与 sse

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | `"http"` 或 `"sse"` | 必填，用来区分远程传输 |
| `url` | HTTP(S) URL | 必填 |
| `headers` | 字符串到字符串 | 默认 `{}`。可含 `$env{NAME}` |

`http` 在运行时写成 `streamable_http`。`sse` 保持 `sse`。

## 命名和失败

服务 ID 是 `mcp__<scope>__<server>`。Home 的 scope 是 `user`。插件的 scope 是插件目录名。

同一 ID 出现两次则准备失败。工具进入注册表时使用适配器给出的命名空间名称。与已有工具重名则编译失败。

缺少被引用的环境变量时，读取该文件失败，REST 不会进入就绪状态。

每次工具调用建立一次连接。stdio 进程不在多次调用之间保持。认证写在 `headers` 或 `env` 中。
