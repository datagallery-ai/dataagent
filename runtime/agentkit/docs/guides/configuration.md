# 配置操作

这份说明帮助你改到一份可运行的配置。字段、类型、默认值和校验规则只维护在 [配置参考](../reference/configuration.md)。

示例文件：

- 最小模板：[config.example.json](../../config.example.json)
- 覆盖当前产品字段并带两个 Hook 的示例：[examples/config.json](../../examples/config.json)
- nl2sql 作为整张图：[examples/nl2sql-entry.json](../../examples/nl2sql-entry.json)
- 示例字段怎么配合文件：[examples/README.md](../../examples/README.md)

配置是标准 JSON。不支持注释、尾逗号和重复键。

## 你要改的两个文件

| 文件 | 作用 |
| --- | --- |
| `<home>/config.json` | 个人默认配置。Home 默认是 `~/.dataagent` |
| `<home>/.env` | 模型名、地址、密钥，以及其他 `$env{NAME}` 引用 |

用进程环境变量 `DATAAGENT_HOME` 指定 Home。写在后端所选的 Home `.env` 或 `--env-file` 里的 `DATAAGENT_HOME` 会被忽略，并在标准错误打出警告；npm 启动器预先载入仓库根 `.env` 属于进程环境阶段，见 [环境变量](../reference/cli-and-environment.md#环境变量)。

`--config` 由控制面读取（`dataagent serve --config`，TUI 拉起后端时的 `--config`，以及 `npm run start` / `./deploy.sh start` 的 `--config`）。传了它就用这份文件，不再读 Home `config.json`。启动目录里的 `config.json` 不会自动读取。连到已有控制面时，配置由那份进程决定。`./deploy.sh start` 在启动当时解析 `server`，把 API 主机名和端口写入部署状态。之后的 `./deploy.sh tui`、`status`、`doctor` 使用这份地址。改了监听地址后，重新启动才会记下新地址。

## 优先级

先区分三类内容：**产品配置**是 Home `config.json` 或显式 `--config` 二选一；**插件清单** `.plugin.json` 声明能力、依赖和可选 `defaults_file`；**插件业务参数**位于 `plugins.config.<id>`。插件清单本身不作为整份产品配置覆盖层。

实际准备过程：

1. 读取选中的产品文件，以字段默认值补齐，得到初步的 `plugins.enabled/paths`。`--config` 替代 Home `config.json`，两份文件不叠加，cwd 的配置也不自动加入。
2. 从内置、Home `plugins/<id>` 和显式 `plugins.paths` 查找插件，递归解析 `requires`。依赖先于依赖它的插件；独立项沿 `enabled` 顺序遍历，每个 ID 只处理一次。相同真实目录去重；同一启用 ID 对应不同目录时直接报错，没有“Home 覆盖内置”的规则。
3. 按上述顺序读取已选插件清单中的 `defaults_file`，再叠加第 1 步选中的产品文件。未选中插件的默认文件不参与。默认文件只允许贡献 `plugins.config`，不能设置模型、server、根 Agent 或反过来启用其他插件。
4. 对合并结果补齐仍缺少的字段默认值；最终启用列表包含依赖插件。业务参数再交给插件工厂解析，插件自身参数模型的默认值补齐未传字段。当前内置 nl2sql 没有声明 `defaults_file`，其默认业务值来自 nl2sql 参数模型。

因此，**同一业务字段的优先级**由低到高是：插件自身参数默认值 → 依赖/已选插件的 `defaults_file`（后者覆盖前者）→ 所选产品文件的 `plugins.config`。程序字段默认值只补缺省项，不覆盖显式值。

| 数据类型/字段 | 合并或覆盖规则 |
| --- | --- |
| 对象，包括 `plugins.config.<id>` | 递归按字段合并；省略字段继承，`{}` 不清空已有对象 |
| `models.<id>` | 按模型 ID、字段合并；插件默认文件不能贡献此段 |
| 标量、普通列表 | 后层替换；普通列表 `[]` 清空已有列表 |
| `dataagent.hooks` | 按来源顺序追加；`[]` 或空事件组不清除已有注册 |
| `plugins.config.<id>.run_hooks.before_run/after_run` | 同样按来源追加，重复注册不去重，`[]` 不清除 |
| `null` | 显式值，不代表省略；只在字段类型允许时使用 |

例如，某个已启用插件的 `defaults_file` 提供：

```json
{"plugins":{"config":{"demo":{"limit":100,"format":{"header":true,"delimiter":","},"tags":["default"]}}}}
```

所选产品文件里的对应参数为：

```json
{"plugins":{"config":{"demo":{"limit":20,"format":{"delimiter":";"},"tags":[]}}}}
```

最终传给该插件的业务参数是：

```json
{"limit":20,"format":{"header":true,"delimiter":";"},"tags":[]}
```

这些片段用于说明合并；运行时仍需完整的模型与插件声明。只写 `plugins.config.demo` 不会启用 demo。

相对路径按相应字段的声明规则解析：产品 `plugins.paths`、workspace 路径相对该产品文件；插件清单资源相对插件根，不能越界；`defaults_file` 中的运行 Hook 路径也相对插件根。业务参数里的普通字符串路径由插件解释，不能假定全被宿主转成绝对路径。`output_root` 规则见下文。未知产品字段、重复键、错误类型和非有限数值会使准备失败。

实现和验证依据：[启动准备](../../dataagent/bootstrap/startup.py)、[字段合并](../../dataagent/bootstrap/config_files.py)、[依赖解析](../../dataagent/plugin_config.py)、[配置合并测试](../../tests/test_plugin_configuration.py)。插件字段见 [插件](../extensions/plugins.md)。

环境文件同样只读一份，再由进程里的非空值覆盖：

1. 传了 `--env-file` 就只用这份文件。文件必须存在。
2. 没传时用 Home `.env`。`--config` 不改变这个选择，旁边的 `.env` 不读取。文件可以不存在。

空值不写入有效环境。输入目录里的 `.env` 不会被读取。生效环境只存在于这次准备过程，不会写回 `os.environ`。变量缺失时，错误信息指向这次选中的环境文件。

模型字段的 `$env{...}` 仅在默认模型中于启动时展开。其他模型在工具第一次调用 `get_model("别名")` 时，用启动时捕获的环境值解析。未使用的模型可以暂缺变量；真正用到且缺失时，那次调用失败。

改模型、服务地址、端口、Home、输入目录或 `dataagent.entry` 后，重新启动进程。插件、Skill、Hook 和 MCP 声明可以在下一轮请求前刷新，见 [扩展生命周期](../architecture/extension-lifecycle.md)。`refresh_extensions` 不改 `entry`。

## 最小可运行内容

保留模板里的 `schema_version: 4` 和 `models.primary`。把三个模型字段留成环境引用，再在 `.env` 填值。

```json
{
  "schema_version": 4,
  "models": {
    "primary": {
      "provider": "openai",
      "name": "$env{LLM_MODEL}",
      "base_url": "$env{LLM_BASE_URL}",
      "api_key": "$env{LLM_API_KEY}"
    }
  }
}
```

`provider` 取值为 `openai`。OpenAI 兼容服务使用同一取值。`base_url` 写到服务商要求的 API 根，通常包含 `/v1`，止于该根路径。需要时再写 `temperature`（0 到 2）、`max_tokens` 或 `extra_body`；省略前两项就用服务商默认值。

## 常见改动

**换默认模型。** 在 `models` 增加一个 ID，把 `dataagent.model` 改成该 ID，然后重启。没有自动回退。

**限制执行时长和循环次数。** 改 `dataagent.limits`。`timeout_seconds` 用于 REST 的 Agent 事件迭代、A2A 的 Agent 调用，以及模型、shell、MCP 和远端 A2A 的相关超时；入站 A2A 排队、会话准备和 Checkpoint 保存不计入 Agent 调用时限。它不是客户端请求总耗时上限，完整列表见 [配置字段](../reference/configuration.md)。SDK 调用方自己控制整轮时限。

**加入只读输入。** 在 `dataagent.workspaces` 写 `name`、已存在的 `path` 和 `"read_only": true`。命令行 `--workspace` 和环境变量 `DATAAGENT_WORKSPACE` 会盖过配置里的列表。

**指定产物根目录。** 在配置中增加：

```json
{
  "dataagent": {
    "output_root": "~/reports"
  }
}
```

`output_root` 是根目录，不是某个会话的目录。新会话写入 `<output_root>/<user>/<thread>/`，例如 alice 的 task-a 写入 `~/reports/alice/task-a/`。省略或设为 `null` 时，根目录为 `<home>/outputs`，产物写入 `<home>/outputs/<user>/<thread>/`。

- 只接受绝对路径或 `~/` 开头的路径，其他相对路径报错。
- 实际位置保存在 `sessions.sqlite` 的 `session_bindings` 表。之后修改配置只影响新会话，已有会话继续使用原目录。
- 派生出的目录里已有不明来源的文件时，创建会话会报错，不会接管。
- SDK 可以用 `output_dir` 为单个任务直接指定最终目录，优先于本配置，见 [SDK 用法](sdk.md#每个任务指定产物目录)。

完整规则（会话身份、独占、所有者标记、常见报错）见 [会话与产物](sessions-and-outputs.md)。

**启用插件。** 把插件目录放到 `<home>/plugins/<id>/`，或把插件自身目录写进 `plugins.paths`。再把 ID 写入 `plugins.enabled`。只加路径不会启用。示例见 [插件](../extensions/plugins.md)。

**接入语义服务。** 增加 `semantic` 对象。客户端用法、认证和 TLS 见 [语义服务](../extensions/semantic-service.md)。字段见配置参考。未配置 `semantic` 时，运行时不创建客户端。

**把编译型子代理当作整张图。** 在 `dataagent.entry` 写已启用插件里的编译型子代理名，然后重启。省略 `entry` 仍是 Deep Agents 根。可运行示例是 [nl2sql-entry.json](../../examples/nl2sql-entry.json)：`entry` 为 `nl2sql`，并带上模型、sqlite 库和语义服务地址。

高层配置里的 `entry` 替换低层。Hook、调用上限、工作区、Skill、工具、MCP 工具、插件系统提示和其他子代理留在配置里，不装进这张图。`limits.timeout_seconds` 仍约束宿主执行阶段。限制见 [入口模式](../extensions/subagents.md#入口模式)。

## 写在其他文件里的设置

- MCP 连接写在 Home 或已启用插件根目录的 `.mcp.json`
- 工具、Skill 和子代理写在插件清单，独立 Skill 放在 `<home>/skills/<name>/SKILL.md`
- TUI 的 `NO_COLOR` 等界面变量放在 TUI 进程环境，后端 `--env-file` 不读取它们；日志目录由进程的 `DATAAGENT_HOME` 统一决定，没有独立的日志目录或级别环境变量

密钥放在 `.env` 或进程环境。不要把真实密钥提交到仓库。
