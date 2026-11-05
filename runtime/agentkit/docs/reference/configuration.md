# 配置字段

这是 `config.json` 的字段全表，给改配置和写插件的人。操作步骤在 [配置操作](../guides/configuration.md)。对外产品站只保留部署时要改的文件和最小模型示例，不重复这份表。

文件是标准 JSON。未知字段、重复键、错误类型和非有限数值都会失败。`schema_version` 必须是 `3`。省略时按 `3` 校验。

相对路径以该配置文件所在目录为基准，并解析 `~/`。

## 顶层

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | `3` | `3` | 唯一接受的产品配置版本 |
| `dataagent` | 对象 | 见下表 | 模型选择、限额、Hook、只读输入、入口 |
| `models` | 对象 | 必填 | 模型 ID 到连接。至少要能解析 `dataagent.model.default` |
| `plugins` | 对象 | `enabled: []`，`paths: []` | 启用列表和额外插件目录 |
| `server` | 对象 | 见下表 | 唯一的监听地址。网页和部署读同一字段 |
| `semantic` | 对象或省略 | 省略 | 省略时不创建语义客户端 |

## `dataagent`

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `model.default` | 字符串 | `"primary"` | 必须是 `models` 的键 |
| `limits.timeout_seconds` | 数 | `180` | 大于 0。REST 整轮时限，也是模型请求超时 |
| `limits.model_calls_per_agent` | 整数 | `128` | 大于 0。每个 Agent 每次调用的模型循环上限 |
| `limits.tool_calls_per_agent` | 整数 | `128` | 大于 0。每个 Agent 每次调用的工具次数上限 |
| `hooks` | 分组对象或列表 | `[]` | 见 [扩展声明](extension-schema.md)。跨配置层追加 |
| `workspaces` | 数组 | `[]` | 只读输入。空数组不创建目录 |
| `output_root` | 路径或 `null` | `null` | 新会话的产物根目录，只接受绝对路径或 `~/` 开头的路径。会话产物写入 `<output_root>/<user>/<thread>/`；`null` 时根目录为 `<home>/outputs`。已有会话保留创建时的位置。见 [会话与产物](../guides/sessions-and-outputs.md#产物目录的位置) |
| `entry` | 字符串或省略 | 省略 | `[a-z][a-z0-9_-]*`。已启用插件里某个编译型子代理的 `name`。省略则装配 Deep Agents 根。见 [入口模式](../extensions/subagents.md#入口模式) |

`workspaces[]`：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `name` | 字符串 | `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`，列表内唯一 |
| `path` | 字符串 | 必须是已存在且可读的目录，解析后唯一 |
| `read_only` | `true` | 只能是 `true` |

`entry` 由高层替换；省略则继承低层。改它要重启进程，`refresh_extensions` 不更新这个字段。`/healthz` 返回这个配置值，未设置时为 `null`；错误的名字不会让它失败。`POST /dataagent/stream` 第一次装配图时返回 HTTP 409。设了 `entry` 之后，`dataagent.hooks`、`model_calls_per_agent`、`tool_calls_per_agent` 和 `workspaces` 不装进入口图，装配不因此失败。`limits.timeout_seconds` 仍然是整轮时限。

## `models.<id>`

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `provider` | `"openai"` | `"openai"` | OpenAI 兼容服务使用同一取值 |
| `name` | 字符串 | 必填 | 非空。服务商模型名。可用 `$env{...}` |
| `base_url` | 字符串 | 必填 | 非空。API 根地址 |
| `api_key` | 字符串 | 必填 | 非空。建议 `$env{LLM_API_KEY}` |
| `max_retries` | 整数 | `2` | 大于等于 0。HTTP 重试次数，首次请求另计。`0` 关闭 |
| `temperature` | 数或省略 | 省略 | 0 到 2。省略时用服务商默认值 |
| `max_tokens` | 整数或省略 | 省略 | 大于 0。省略时用服务商默认值 |
| `extra_body` | 对象 | `{}` | 原样传给 Chat Completions 的 `extra_body` |

没有 `response_format`。模型固定走 Chat Completions（`use_responses_api=False`）。`temperature`、`max_tokens` 和 `extra_body` 只在写了的时候传给客户端。

启动只展开 `model.default` 指向的那一组环境引用。其他模型保留原文，到 `get_model` 时再用启动快照展开。`$env{...}` 只从一份环境文件取值：`--env-file`，否则 Home `.env`。`--config` 不改变这个选择。进程里的非空值覆盖这份文件。

## `plugins`

| 字段 | 类型 | 默认 | 合并 |
| --- | --- | --- | --- |
| `enabled` | 字符串数组 | `[]` | 高层整表替换。ID 不能重复。`[]` 清空低层 |
| `paths` | 字符串数组 | `[]` | 高层整表替换。每项是插件自身目录 |
| `config` | 对象 | `{}` | 按插件 ID 合并。键是插件 ID，值由该插件自己校验 |

启用 ID 必须正好匹配一个候选目录，且目录名等于 `.plugin.json` 的 `id`。

## `server`

`npm run start`、网页转发和无参数 TUI 在启动时读所选产品文件的 `server`：传了 `--config` 用那份文件，否则用 Home `config.json`。`./deploy.sh start` 也在启动当时解析同一地址，把主机名写入 `storage/run/deployment.json` 的 `listen.apiHost`，把端口写入 `ports.api`。之后的 `./deploy.sh tui`、`status`、`doctor` 使用这份地址。首次部署在 Home 没有 `config.json` 时，用 `dataagent/bootstrap/templates/config.json` 创建完整配置，再写入所选 `host` 和 `port`。已有文件只改这两个字段。语法无法解析时不覆盖原文件。改了监听地址后，重新执行 `./deploy.sh start` 或 `restart` 才会记下新地址。

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `host` | 字符串 | `"127.0.0.1"` | 绑定地址，例如 `127.0.0.1` 或 `0.0.0.0` |
| `port` | 整数 | `8790` | 1 到 65535。占用则启动失败 |

## `a2a`

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `agents` | 数组 | `[]` | 高层整表替换。`id` 不能重复 |

`agents[]`：

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `id` | 字符串 | 必填 | `[A-Za-z][A-Za-z0-9_-]{0,63}`。工具名是 `a2a__<id>` |
| `url` | 字符串 | 必填 | 以 `http://` 或 `https://` 开头的服务根地址 |
| `description` | 字符串 | `""` | 工具说明。空字符串用默认说明 |
| `auth_token` | 字符串或省略 | 省略 | Bearer 令牌。进入错误信息脱敏 |
| `timeout_seconds` | 数或省略 | 省略 | 大于 0。省略时用 `dataagent.limits.timeout_seconds` |

远端对话按本地 `thread_id` 分开。声明和调用约定见 [A2A](a2a.md)。

## `semantic`

省略整个对象表示不接入。

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `base_url` | 字符串 | 必填 | 去空白后非空。客户端规范到 `/api/semantic/v1` |
| `username` | 字符串或省略 | 省略 | 与 `password` 成对。最短 1 |
| `password` | 字符串或省略 | 省略 | 与 `username` 成对 |
| `timeout` | 数 | `30` | 大于 0 的有限数 |
| `tls.verify_server` | 布尔 | `false` | `false` 且无客户端证书、无 cipher 时不校验服务端证书 |
| `tls.ca_file` | 路径或省略 | 省略 | 仅在 `verify_server` 为 true 时作为 CA 文件 |
| `tls.client_cert` | 路径或省略 | 省略 | 与 `client_key` 成对 |
| `tls.client_key` | 路径或省略 | 省略 | 与 `client_cert` 成对 |
| `tls.ciphers` | 字符串或省略 | 省略 | 非空时传给 SSL `set_ciphers` |

## 合并

产品文件二选一：未传 `--config` 时用 Home `config.json`，传了就只用那份文件。插件 `plugins.config` 在选中的产品文件下面。

- 对象按字段递归合并，包括每个模型 ID 和 `plugins.config.<id>`。没写的字段保留插件默认值；插件也没写时用程序默认值。
- `dataagent.hooks`，以及 `plugins.config.<id>.hooks.before_run` / `after_run`，按层追加。`[]` 或空事件组只表示这一层不追加。
- 其他数组和标量由选中文件整段替换，包括 `dataagent.entry`、`plugins.enabled`、`plugins.paths`。`[]` 清空。

启动目录中的 `config.json` 不参与。
