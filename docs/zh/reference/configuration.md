# 配置字段

这里列出 `config.yaml` 的字段、默认值和合并规则。首次设置模型请看[配置速查](../guides/configuration.md)，完整示例见[配置操作](../guides/configuration.md)。

文件使用 YAML 1.2，支持注释和多行字符串。未知字段、重复键、锚点、别名、合并键、显式标签、错误类型和非有限数值都会导致加载失败。`schema_version` 必须是 `4`。省略时按 `4` 校验。

相对路径以该配置文件所在目录为基准，并解析 `~/`。例外：`dataagent.output_root` 只接受绝对路径或 `~/` 开头的路径。

## 顶层

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | `4` | `4` | 唯一接受的产品配置版本 |
| `dataagent` | 对象 | 见下表 | 模型选择、限额、Hook、只读输入、入口 |
| `models` | 对象 | 必填 | 模型 ID 到连接。至少要能解析 `dataagent.model` |
| `plugins` | 对象 | `enabled: []`，`paths: []` | 启用列表和额外插件目录 |
| `server` | 对象 | 见下表 | 后端监听地址。`dataagent serve`、`dataagent serve-a2a`（可用 `--host`、`--port` 覆盖）、网页和部署读同一字段 |
| `semantic` | 对象或省略 | 省略 | 省略时不创建语义客户端 |
| `a2a` | 对象或省略 | 省略 | 本服务的 Agent Card 和本进程要调用的远端 Agent。见下文 `a2a` 一节 |

## `dataagent`

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `model` | 字符串 | `"primary"` | 必须是 `models` 的键 |
| `limits.timeout_seconds` | 数 | `180` | 大于 0。用于 REST 的 Agent 事件迭代时限、A2A 的 Agent `ainvoke` 时限、模型请求超时、shell 命令超时、MCP 读超时、MCP 工具发现时限（取它和 20 秒中的较小值），以及远端 A2A 调用的默认超时。A2A 排队、会话准备和 Checkpoint 保存不在 `ainvoke` 的时限内，不是客户端请求总耗时上限 |
| `limits.model_calls_per_agent` | 整数 | `128` | 大于 0。每个 Agent 每次调用的模型循环上限 |
| `limits.tool_calls_per_agent` | 整数 | `128` | 大于 0。每个 Agent 每次调用的工具次数上限 |
| `loop_detection.enabled` | 布尔 | `true` | 检测模型输出复读和无进展工具循环。复读命中后终止本轮；工具循环第一次命中先提示模型换方法，第二次命中再终止 |
| `loop_detection.ignored_tools` | 字符串数组 | `[]` | 不参与工具循环检测的完整工具名，适合有意轮询的工具 |
| `hooks` | 分组对象或列表 | `[]` | 见 [扩展声明](extension-schema.md)。跨配置层追加 |
| `input_workspaces` | 数组 | `[]` | 只读输入。空数组不创建目录 |
| `output_root` | 路径或 `null` | `null` | 新会话的产物根目录，只接受绝对路径或 `~/` 开头的路径。会话产物写入 `<output_root>/<user>/<thread>/`；`null` 时根目录为 `<home>/outputs`。已有会话保留创建时的位置。见 [会话与产物](../guides/sessions-and-outputs.md#output-directory) |
| `entry` | 字符串或省略 | 省略 | `[a-z][a-z0-9_-]{0,63}`。已启用插件里某个编译型子代理的 `name`。省略则装配 Deep Agents 根。见 [入口模式](../guides/integrate/extensions/subagents.md#entry-mode) |

已有 YAML 使用 `dataagent.workspaces` 时，将字段名改为 `dataagent.input_workspaces`。

`input_workspaces[]`：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `name` | 字符串 | `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`，列表内唯一 |
| `path` | 字符串 | 必须是已存在且可读的目录，解析后唯一 |
| `read_only` | `true` | 只能是 `true` |

`entry` 由高层替换；省略则继承低层。改它要重启进程，扩展刷新不更新这个字段。`/healthz` 返回这个配置值，未设置时为 `null`；错误的名字不会让它失败。`POST /api/v1/runs` 第一次装配图时返回 HTTP 409。设了 `entry` 之后，`dataagent.hooks`、`model_calls_per_agent`、`tool_calls_per_agent`、`loop_detection.ignored_tools` 和 `input_workspaces` 不装进入口图，装配不因此失败。`limits.timeout_seconds` 仍约束宿主执行阶段，边界见上表。

### 复读与循环检测

默认启用本地复读和工具循环检测。初次命中工具循环时给模型一次换方法的机会，再次命中才终止。可用 `ignored_tools` 排除预期内的轮询。编译型入口、编译型子代理和没有显式覆盖的默认 `general-purpose` 只做文本检测。

```yaml
dataagent:
  loop_detection:
    enabled: true
    ignored_tools:
    - jobs__get_status
```

设 `enabled: false` 可关闭两类检测。修改配置后重启服务或重新创建 SDK 实例。文本复读或第二次工具循环终止时，SDK 抛出 `LoopDetectedError`，REST 流返回 `LOOP_DETECTED`。已完成操作不会重放。触发算法和装配位置见[执行实现](../../development/architecture/execution.md#loop-detection)。

<a name="model-settings"></a>

## `models.<id>`

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `provider` | `"openai"` | `"openai"` | OpenAI 兼容服务使用同一取值 |
| `name` | 字符串 | 必填 | 非空。服务商模型名。可用 `$env{...}` |
| `base_url` | 字符串 | 必填 | 非空。填写服务商要求的 API 根地址，通常以 `/v1` 结尾 |
| `api_key` | 字符串 | 必填 | 非空。建议 `$env{LLM_API_KEY}` |
| `max_retries` | 整数 | `2` | 大于等于 0。HTTP 重试次数，首次请求另计。`0` 关闭 |
| `temperature` | 数或省略 | 省略 | 0 到 2。省略时用服务商默认值 |
| `max_tokens` | 整数或省略 | 省略 | 大于 0。省略时用服务商默认值 |
| `extra_body` | 对象 | `{}` | 原样传给 Chat Completions 的 `extra_body` |

没有 `response_format`。模型固定走 Chat Completions（`use_responses_api=False`）。`temperature`、`max_tokens` 和 `extra_body` 只在写了的时候传给客户端。

启动只展开 `model` 指向的那一组环境引用。其他模型保留原文，到 `get_model` 时再用启动快照展开。取值顺序见[环境变量](cli-and-environment.md#environment-variables)。

## `plugins`

| 字段 | 类型 | 默认 | 合并 |
| --- | --- | --- | --- |
| `enabled` | 字符串数组 | `[]` | 高层整表替换。ID 不能重复。`[]` 清空低层 |
| `paths` | 字符串数组 | `[]` | 高层整表替换。每项是插件自身目录 |
| `config` | 对象 | `{}` | 按插件 ID 合并。键是插件 ID，值由该插件自己校验 |

启用 ID 必须正好匹配一个候选目录，且目录名等于 `.plugin.yaml` 的 `id`。

<a name="server-settings"></a>

## `server`

`npm run start`、网页转发和无参数 TUI 在启动时读所选产品文件的 `server`：传了 `--config` 用那份文件，否则用 Home `config.yaml`。`./deploy.sh start` 也在启动当时解析同一地址，把主机名写入 `storage/run/deployment.json` 的 `listen.apiHost`，把端口写入 `ports.api`。之后的 `./deploy.sh tui`、`status`、`doctor` 使用这份地址。首次部署在 Home 没有 `config.yaml` 时，用 `dataagent/bootstrap/templates/config.yaml` 创建完整配置，再写入所选 `host` 和 `port`。已有文件只改这两个字段。语法无法解析时不覆盖原文件。改了监听地址后，重新执行 `./deploy.sh start` 或 `restart` 才会记下新地址。

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `host` | 字符串 | `"127.0.0.1"` | 绑定地址，例如 `127.0.0.1` 或 `0.0.0.0` |
| `port` | 整数 | `8790` | 1 到 65535。占用则启动失败 |

## `a2a`

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `card.name` | 字符串或省略 | `DataAgent` | 本服务 Agent Card 的名称，设置时不能是空白 |
| `card.description` | 字符串或省略 | `DataAgent` | 本服务 Agent Card 的用途说明，设置时不能是空白 |
| `remote_agents` | 数组 | `[]` | 高层整表替换。`id` 不能重复；只配置出站调用，不控制入站 A2A 路由 |

`remote_agents[]`：

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `id` | 字符串 | 必填 | `[a-z][a-z0-9_-]{0,63}`。工具名是 `a2a__<id>` |
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
| `timeout_seconds` | 数 | `30` | 大于 0 的有限数 |
| `semantic_scenario` | 字符串或省略 | 省略 | 语义服务侧配置的场景名。客户端把它作为 `X-Semantic-Scenario` 头标在除 health 外的每个请求上；省略时不带该头，服务端按默认场景路由 |
| `tls.verify_server` | 布尔 | `false` | `false` 且无客户端证书、无 cipher 时不校验服务端证书 |
| `tls.ca_file` | 路径或省略 | 省略 | 仅在 `verify_server` 为 true 时作为 CA 文件 |
| `tls.client_cert` | 路径或省略 | 省略 | 与 `client_key` 成对 |
| `tls.client_key` | 路径或省略 | 省略 | 与 `client_cert` 成对 |
| `tls.ciphers` | 字符串或省略 | 省略 | 非空时传给 SSL `set_ciphers` |

<a name="configuration-merge"></a>

## 合并

产品 YAML 只选一份。传入 `--config` 时读取指定文件，否则读取 Home 的 `config.yaml`。启动目录中的 `config.yaml` 和 `.dataagent/` 不参与。环境文件独立选择，见[环境变量](cli-and-environment.md#environment-variables)。

1. 读取所选产品文件，以默认值补齐初步的 `plugins.enabled`、`plugins.paths`。
2. 按启用顺序查找内置、Home 和显式路径中的插件，并递归解析 `requires`。依赖先加载，每个 ID 只处理一次。同一实际目录去重，同一启用 ID 对应多个实际目录时报错。
3. 对加载的插件依次读取清单 `params` 和 `defaults_file`。清单参数只贡献当前插件的业务参数，可用 `$env{NAME}`。默认文件只贡献 `plugins.config`。最后叠加所选产品文件。
4. 程序字段默认值只补缺省项；把业务参数交给插件工厂后，插件参数模型还会补齐未传的业务字段。例如当前内置 nl2sql 未声明 `defaults_file`，默认业务值来自参数模型。

| 值 | 合并规则 |
| --- | --- |
| 对象，包括 `models` 和 `plugins.config` | 递归按字段合并。省略字段时继承；`{}` 不清空既有对象 |
| 普通列表和标量 | 高层值替换。普通列表用 `[]` 清空 |
| `dataagent.hooks`、`plugins.config.<id>.run_hooks.before_run` 和 `after_run` | 按来源顺序追加，保留重复注册。空列表或空事件组不会清除既有注册 |
| `null` | 表示显式值，仅在允许 `null` 的字段使用 |

产品配置中的 `plugins.paths` 和输入目录相对所选 YAML。插件清单资源，以及清单 `params.run_hooks` 和 `defaults_file` 里的运行 Hook 路径，相对插件根目录。清单资源不能越过插件根。普通业务参数里的路径字符串由插件解释。`output_root` 只接受绝对路径或以 `~/` 开头的路径。

[配置操作](../guides/configuration.md#优先级)展示了插件默认参数与产品配置叠加后的结果。
