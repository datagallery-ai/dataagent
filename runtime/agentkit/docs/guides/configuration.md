# 配置操作

这份说明帮助你改到一份可运行的配置。字段、类型、默认值和校验规则只维护在 [配置参考](../reference/configuration.md)。

示例文件：

- 最小模板：[config.example.json](../../config.example.json)
- 覆盖当前产品字段并带两个 Hook 的示例：[examples/config.json](../../examples/config.json)
- 示例字段怎么配合文件：[examples/README.md](../../examples/README.md)

配置是标准 JSON。不支持注释、尾逗号和重复键。

## 你要改的两个文件

| 文件 | 作用 |
| --- | --- |
| `<home>/config.json` | 个人默认配置。Home 默认是 `~/.dataagent` |
| `<home>/.env` | 模型名、地址、密钥，以及其他 `$env{NAME}` 引用 |

用进程环境变量 `DATAAGENT_HOME` 指定 Home。写在 `.env` 文件里的 `DATAAGENT_HOME` 会被忽略，并在标准错误打出警告。

显式增加一层配置：

```bash
npm run start:tui -- --v2 --config runtime/agentkit/examples/config.json
```

`--config` 是更高的一层，不是整文件替换。启动目录下的 `config.json` 不会被自动读取。输入目录也不提供配置。

## 优先级

配置由低到高：

1. 程序内默认值
2. Home `config.json`
3. `--config` 指定的文件

同一真实文件被显式选中时，只作为显式层应用一次。

合并规则：

- 对象按字段合并。省略的字段继承低层。
- `models.<id>` 按模型 ID 和字段合并。
- `dataagent.hooks` 按层追加。`"hooks": []` 或空事件组只表示这一层不追加。
- 其他列表由高层整表替换。`plugins.enabled: []` 会清空低层的启用列表。
- 相对路径以声明它的配置文件所在目录为基准，支持 `~/`。
- 未知字段、重复键、错误类型和非有限数值会使启动失败。

环境值由高到低：

1. 进程环境变量
2. `--env-file`
3. 显式配置旁边的 `.env`
4. Home `.env`

空值不覆盖低层已有的非空值。输入目录里的 `.env` 不会被读取。生效环境只存在于这次准备过程，不会写回 `os.environ`。

启动时只展开默认模型的 `$env{...}`。其他模型在工具第一次调用 `get_model("别名")` 时，用启动时捕获的环境值解析。未使用的模型可以暂缺变量；真正用到且缺失时，那次调用失败。

改模型、服务地址、端口、Home 或输入目录后，重新启动进程。插件、Skill、Hook 和 MCP 声明可以在下一轮请求前刷新，见 [扩展生命周期](../architecture/extension-lifecycle.md)。

## 最小可运行内容

保留模板里的 `schema_version: 3` 和 `models.primary`。把三个模型字段留成环境引用，再在 `.env` 填值。

```json
{
  "schema_version": 3,
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

`provider` 取值为 `openai`。OpenAI 兼容服务使用同一取值。`base_url` 写到服务商要求的 API 根，通常包含 `/v1`，止于该根路径。

## 常见改动

**换默认模型。** 在 `models` 增加一个 ID，把 `dataagent.model.default` 改成该 ID，然后重启。没有自动回退。

**限制一轮时长和循环次数。** 改 `dataagent.limits`。`timeout_seconds` 同时是 REST 整轮时限和模型请求超时。SDK 调用方自己控制整轮时限。

**加入只读输入。** 在 `dataagent.workspaces` 写 `name`、已存在的 `path` 和 `"read_only": true`。命令行 `--workspace` 和环境变量 `DATAAGENT_WORKSPACE` 会盖过配置里的列表。

**启用插件。** 把插件目录放到 `<home>/plugins/<id>/`，或把插件自身目录写进 `plugins.paths`。再把 ID 写入 `plugins.enabled`。只加路径不会启用。示例见 [插件](../extensions/plugins.md)。

**接入语义服务。** 增加 `semantic` 对象。客户端用法、认证和 TLS 见 [语义服务](../extensions/semantic-service.md)。字段见配置参考。未配置 `semantic` 时，运行时不创建客户端。

## 写在其他文件里的设置

- MCP 连接写在 Home 或已启用插件根目录的 `.mcp.json`
- 工具、Skill 和子代理写在插件清单，独立 Skill 放在 `<home>/skills/<name>/SKILL.md`
- TUI 主题和日志变量放在 TUI 进程环境，后端 `--env-file` 不读取它们

密钥放在 `.env` 或进程环境。不要把真实密钥提交到仓库。
