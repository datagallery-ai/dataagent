# 配置字段

这里列出 `config.yaml` 的字段、默认值和合并规则。首次设置模型请看[配置速查](configuration-api.md)，完整示例见[配置操作](../guides/configuration.md)。

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

检测只使用本地规则，不产生额外模型调用，不改变模型的流式设置。实现位于 `dataagent/extensions/loop_detection.py`，与 `extensions/tracing.py` 同层，只依赖 LangChain 公开接口。两类检测装在不同的位置：

- **工具循环**由 `LoopDetectionMiddleware` 检查。`assembly/graph.py` 的 `build_native_middleware()` 把它装进主代理和标准子代理，位置在调用次数上限之后、插件 Hook 之前。中间件钩子有准确的轮边界，命中时模型 span 已经结束，不会在追踪里留下未闭合的记录。
- **文本复读**由 `LoopDetectionCallback` 检查。装配图时通过 `with_config(callbacks=...)` 安装，LangChain 将其传递给内部模型和子代理调用。只有流式回调能在整段回复被消费完之前停下退化的解码，所以这部分留在回调上。

具体规则：

- **流式文本**：在最近 5,000 个字符中检查 50 字符片段。相同片段密集出现十次且间隔内容也重复时停止。文本和服务商公开的推理内容分开统计，工具参数 JSON 不作为文本检查。代码围栏内以及表格、列表、标题、引用、分隔线等本就会重复的 Markdown 结构行都跳过，避免正常输出触发。只有确认块级标记后才跳过结构行；粗体、斜体和行内代码前缀不豁免正文。围栏按 CommonMark 的行首、同字符和长度规则判断，反引号围栏的信息字符串不能含反引号，结束行在围栏后只能有空格或制表符。支持 LF、CRLF 和 CR 换行，包括跨流式分片的换行。非流式回复中同一通道的所有内容块都会检查；已在流式阶段检查过的通道不重复检查。非流式模型只能在回复完成后检查。
- **工具循环**：以"一次助手回复"为一步，检查本轮最近 25 步。长度为 1–5 的步骤序列连续重复五遍，且工具名、规范化参数、结果和状态都相同时停止。并行调用属于同一步，不会放大计数；结果按所属回复匹配，复用的 call ID 和并行结果不会串。结果有变化时继续，轮询工具可加入 `ignored_tools`。
- **工具循环重试**：本轮第一次命中时不终止。中间件追加一条 `name` 为 `loop_detection` 的用户消息，告诉模型同样的调用已重复五遍、结果相同，请换个方法或直接用已有信息回答，然后继续本轮。提示之前的步骤不再计数，提示之后再出现一次完整的循环才终止，所以每轮最多重试一次。提示消息保存在会话历史中。重试的模型调用计入 `model_calls_per_agent` 和 `limits.timeout_seconds`。

配置示例：

```yaml
dataagent:
  loop_detection:
    enabled: true
    ignored_tools:
    - jobs__get_status
```

轮边界由 `before_agent` 在每次调用开始时记录，所以其他中间件在轮中插入的消息（摘要、评分复议、用于引用媒体的消息）不会重置计数，但一次新的用户请求会。复读命中或工具循环第二次命中后，SDK 抛出 `dataagent.LoopDetectedError`，REST 发出 `RUN_ERROR`，错误代码为 `LOOP_DETECTED`。不会自动重放任务或已完成的工具操作。可查看已完成步骤后发送新请求。异常中不包含原始文本或参数。修改检测配置需重启或重新创建 SDK 实例。

中间件自己发起的模型调用（上下文摘要、Rubric 评分）不是 Agent 的输出，不参与文本检测。

这些检查是启发式保护，有明确的覆盖边界：

- 设了 `dataagent.entry` 时，编译型入口自带图，不经过 `build_native_middleware`，因此**只有文本复读检测**，没有工具循环检测和工具循环重试。该入口的轮数约束仍由它自身的实现和 `limits.timeout_seconds` 承担。入口内部即使捕获异常并返回普通回复（如 NL2SQL），`build_entry_graph()` 也会在入口返回后重新抛出本次调用中记录的 `LoopDetectedError`。
- 通过 `task` 委派的编译型子代理也只有文本复读检测。它与编译型入口使用相同的异常传播保护：内部即使把循环异常转成普通回复，宿主仍会抛出 `LoopDetectedError` 并终止本轮，父代理不会继续调用模型。
- 没有插件声明 `general-purpose` 时，核心自动创建的通用子代理不继承自定义中间件，因此**没有工具循环检测**，只有文本复读检测。需要时由插件显式声明 `general-purpose` 子代理。
- 文本复读不重试。NL2SQL 节点和 UDN 场景包的 JSON 重试入口直接传播 `LoopDetectedError`，不会将其作为格式错误重试；validator 或 selector 命中后停止后续流程，不生成最终 CSV。HTTP 接口始终流式输出，重试时前端已经收到了复读文本，AG-UI 没有作废已发文本的事件。临时性模型错误（429、5xx、超时、断连）由模型 SDK 按 `models.<id>.max_retries` 重试，不另加中间件重试。
- Markdown 结构行豁免意味着整段由相同表格行或列表项组成的复读不会被文本检测拦住。这是为避免误伤正常的数据表输出而刻意保留的取舍，由工具循环检测和 `model_calls_per_agent`、`tool_calls_per_agent` 兜底。
- 上下文摘要重写历史后，记录的轮起点可能超出重写后的长度，此时检查范围偏短，只会漏报，不会把上一轮的调用算进本轮。
- 插件自行屏蔽回调的模型调用不在覆盖范围内；不公开的内部推理不可检查。客户端中止流不保证服务商立即停止计算，仍需保留 token、轮数和超时限制。

`enabled: false` 同时关闭两类检测，用于有意生成大量重复文本或有意重复调用工具的任务。

算法参考 [Gemini CLI LoopDetectionService](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/loopDetectionService.ts)，按 dataagent 的原生中间件、回调和错误处理方式独立实现。

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
