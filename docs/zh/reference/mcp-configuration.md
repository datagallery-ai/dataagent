# MCP 配置参考

DataAgent 从 DataAgent Home 和已启用插件根目录中的 `.mcp.yaml` 读取宿主 MCP 配置。文件使用严格 YAML 1.2。使用步骤见 [MCP 接入指南](../guides/integrate/extensions/mcp.md)，三种传输的配置模板见 [mcp.full.yaml](../../../examples/mcp/mcp.full.yaml)。

## 顶层 { #top-level}

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mcpServers` | 对象 | 必填。键是服务名，匹配 `[a-z][a-z0-9_-]{0,63}`，值是下列一种连接。用 `{}` 表示空配置 |

顶层只接受 `mcpServers`。可删除其中的某个条目，或禁用携带该配置的插件，以停用对应服务。产品 `config.yaml` 和插件清单 `.plugin.yaml` 各自使用自己的字段声明。

## stdio { #stdio}

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `type` | `"stdio"` | `"stdio"` | 可省略 |
| `command` | 非空字符串 | 必填 | 程序名或可执行文件路径 |
| `args` | 字符串数组 | `[]` | 每个参数独立一项，原样传给进程 |
| `env` | 字符串到字符串 | `{}` | 传给子进程的额外环境变量 |

以 `.mcp.yaml` 所在目录作为子进程的工作目录。含 `/` 的相对 `command` 路径按该目录解析，程序名由启动环境查找。程序和参数通过进程接口直接传递，使用单独字段表达可执行文件及每项参数。

## HTTP 与 SSE { #remote-transports}

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `type` | `"http"` 或 `"sse"` | 必填 | `http` 使用 Streamable HTTP，`sse` 使用 SSE |
| `url` | HTTP(S) URL | 必填 | 服务提供的完整 MCP 端点 |
| `headers` | 字符串到字符串 | `{}` | 显式声明服务要求的请求头，包括认证头 |

每种连接只校验其表格中的字段。未知字段和跨传输混用的字段会被拒绝。

## 环境与路径 { #environment-and-paths}

`$env{NAME}` 在读取 `.mcp.yaml` 后、校验字段前展开，可用于程序路径、子进程环境、请求头等值。缺少引用变量会导致运行时准备失败。环境来源与产品设置一致，由启动进程的非空值覆盖所选环境文件，后者默认为 Home `.env`，也可以用 `--env-file` 指定。插件旁的 `.env` 不会自动加载。详见 [环境变量参考](cli-and-environment.md#environment-variables)。

`mcp.full.yaml` 是模板。将选中的服务配置合并到实际的 `.mcp.yaml`，并使用可达端点和匹配的凭据后，再启动服务。

## 工具命名与发现失败 { #tool-naming}

服务 ID 为 `mcp__<scope>__<server>`，Home 的 scope 是 `user`，插件采用插件目录名。相同服务 ID 出现多次会使运行时准备失败。

工具名为 `<服务 ID>__<MCP 工具名>`，例如 `mcp__user__math__add`。适配器调用服务时继续使用其原始工具名。发现后的完整名称必须匹配 `[A-Za-z0-9_-]{1,64}`。重复 MCP 工具名会导致发现失败，与其他已注册的工具重名则会导致编译失败。

一次工具发现的总时限取 20 秒与 `dataagent.limits.timeout_seconds` 中的较小值。调用工具时，适配器也使用该配置字段设置连接传输的等待时限。

配置或发现错误的排查方式见 [MCP 接入指南](../guides/integrate/extensions/mcp.md#discovery-and-refresh)。
