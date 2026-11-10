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
| `loop_detection.enabled` | 布尔 | `true` | 检测模型输出复读和无进展工具循环。复读命中后终止本轮；工具循环第一次命中先提示模型换方法，第二次命中再终止 |
| `loop_detection.ignored_tools` | 字符串数组 | `[]` | 不参与工具循环检测的完整工具名，适合有意轮询的工具 |
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

`entry` 由高层替换；省略则继承低层。改它要重启进程，`refresh_extensions` 不更新这个字段。`/healthz` 返回这个配置值，未设置时为 `null`；错误的名字不会让它失败。`POST /dataagent/stream` 第一次装配图时返回 HTTP 409。设了 `entry` 之后，`dataagent.hooks`、`model_calls_per_agent`、`tool_calls_per_agent`、`loop_detection.ignored_tools` 和 `workspaces` 不装进入口图，装配不因此失败。`limits.timeout_seconds` 仍然是整轮时限。

### 复读与循环检测

检测只使用本地规则，不产生额外模型调用，不改变模型的流式设置。实现位于 `dataagent/extensions/loop_detection.py`，与 `extensions/tracing.py` 同层，只依赖 LangChain 公开接口。两类检测装在不同的位置：

- **工具循环**由 `LoopDetectionMiddleware` 检查。`agent.py` 的 `build_native_middleware()` 把它装进主代理和标准子代理，位置在调用次数上限之后、插件 Hook 之前。中间件钩子有准确的轮边界，命中时模型 span 已经结束，不会在追踪里留下未闭合的记录。
- **文本复读**由 `LoopDetectionCallback` 检查。`build_agent()` 和 `build_entry_graph()` 通过 `with_config(callbacks=...)` 安装，LangChain 将其传递给内部模型和子代理调用。只有流式回调能在整段回复被消费完之前停下退化的解码，所以这部分留在回调上。

具体规则：

- **流式文本**：在最近 5,000 个字符中检查 50 字符片段。相同片段密集出现十次且间隔内容也重复时停止。文本和服务商公开的推理内容分开统计，工具参数 JSON 不作为文本检查。代码围栏内以及表格、列表、标题、引用、分隔线等本就会重复的 Markdown 结构行都跳过，避免正常输出触发。只有确认块级标记后才跳过结构行；粗体、斜体和行内代码前缀不豁免正文。围栏按 CommonMark 的行首、同字符和长度规则判断，反引号围栏的信息字符串不能含反引号，结束行在围栏后只能有空格或制表符。支持 LF、CRLF 和 CR 换行，包括跨流式分片的换行。非流式回复中同一通道的所有内容块都会检查；已在流式阶段检查过的通道不重复检查。非流式模型只能在回复完成后检查。
- **工具循环**：以"一次助手回复"为一步，检查本轮最近 25 步。长度为 1–5 的步骤序列连续重复五遍，且工具名、规范化参数、结果和状态都相同时停止。并行调用属于同一步，不会放大计数；结果按所属回复匹配，复用的 call ID 和并行结果不会串。结果有变化时继续，轮询工具可加入 `ignored_tools`。
- **工具循环重试**：本轮第一次命中时不终止。中间件追加一条 `name` 为 `loop_detection` 的用户消息，告诉模型同样的调用已重复五遍、结果相同，请换个方法或直接用已有信息回答，然后继续本轮。提示之前的步骤不再计数，提示之后再出现一次完整的循环才终止，所以每轮最多重试一次。提示消息保存在会话历史中。重试的模型调用计入 `model_calls_per_agent` 和 `limits.timeout_seconds`。

配置示例：

```json
{
  "dataagent": {
    "loop_detection": {
      "enabled": true,
      "ignored_tools": ["jobs__get_status"]
    }
  }
}
```

轮边界由 `before_agent` 在每次调用开始时记录，所以其他中间件在轮中插入的消息（摘要、评分复议、媒体占位）不会重置计数，但一次新的用户请求会。复读命中或工具循环第二次命中后，SDK 抛出 `dataagent.LoopDetectedError`，REST 发出 `RUN_ERROR`，错误代码为 `LoopDetectedError`。不会自动重放任务或已完成的工具操作。可查看已完成步骤后发送新请求。异常中不包含原始文本或参数。修改检测配置需重启或重新创建 SDK 实例。

中间件自己发起的模型调用（上下文摘要、Rubric 评分）不是 Agent 的输出，不参与文本检测。

这些检查是启发式保护，有明确的覆盖边界：

- 设了 `dataagent.entry` 时，编译型入口自带图，不经过 `build_native_middleware`，因此**只有文本复读检测**，没有工具循环检测和工具循环重试。该入口的轮数约束仍由它自身的实现和 `limits.timeout_seconds` 承担。入口内部即使捕获异常并返回普通回复（如 NL2SQL），`build_entry_graph()` 也会在入口返回后重新抛出本次调用中记录的 `LoopDetectedError`。
- 通过 `task` 委派的编译型子代理也只有文本复读检测。它与编译型入口使用相同的异常传播保护：内部即使把循环异常转成普通回复，宿主仍会抛出 `LoopDetectedError` 并终止本轮，父代理不会继续调用模型。
- 没有插件声明 `general-purpose` 时，核心自动创建的通用子代理不继承自定义中间件，因此**没有工具循环检测**，只有文本复读检测。需要时由插件显式声明 `general-purpose` 子代理。
- 文本复读不重试。HTTP 接口始终流式输出，重试时前端已经收到了复读文本，AG-UI 没有作废已发文本的事件。临时性模型错误（429、5xx、超时、断连）由模型 SDK 按 `models.<id>.max_retries` 重试，不另加中间件重试。
- Markdown 结构行豁免意味着整段由相同表格行或列表项组成的复读不会被文本检测拦住。这是为避免误伤正常的数据表输出而刻意保留的取舍，由工具循环检测和 `model_calls_per_agent`、`tool_calls_per_agent` 兜底。
- 上下文摘要重写历史后，记录的轮起点可能超出重写后的长度，此时检查范围偏短，只会漏报，不会把上一轮的调用算进本轮。
- 插件自行屏蔽回调的模型调用不在覆盖范围内；不公开的内部推理不可检查。客户端中止流不保证服务商立即停止计算，仍需保留 token、轮数和超时限制。

`enabled: false` 同时关闭两类检测，用于有意生成大量重复文本或有意重复调用工具的任务。

算法参考 [Gemini CLI LoopDetectionService](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/loopDetectionService.ts)，按 dataagent 的原生中间件、回调和错误处理方式独立实现。

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
