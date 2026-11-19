# 插件

插件是一个目录，根上有 `.plugin.yaml`。目录名、清单 `id` 和 `plugins.enabled` 里的 ID 必须相同。

最小清单：

```yaml
schema_version: 3
id: common
tools:
  summarize_numbers:
    entrypoint: tools/statistics.py:summarize_numbers
```

完整字段见 [扩展声明](../reference/extension-schema.md)。可运行的完整插件见 [examples/plugins/common](../../examples/plugins/common/README.md)。

## 放入并启用

任选一种位置：

1. 复制到 `<home>/plugins/<id>/`。Home 会自动发现，不必写 `plugins.paths`。
2. 在 `plugins.paths` 写该插件自己的目录。写父目录不会把下面每个插件都算进来。

然后在 `<home>/config.yaml`：

```yaml
plugins:
  enabled:
  - common
```

`paths` 只增加候选。`enabled` 里的 ID 及其传递依赖会编译。ID 不能重复。保留 ID 是 `user` 和 `cli`。

候选目录来自包内 `builtin-plugins`、Home `plugins/` 和 `plugins.paths`。同一个真实目录只计一次。同一个启用 ID 对应两个不同目录时，启动失败，不会静默覆盖。

清单 `requires` 声明加载依赖。宿主在已有候选目录中查找它们，按依赖优先顺序加载，每个 ID 只加载一次。
依赖缺失、重复来源或循环依赖会在启动时失败。不会联网安装，也不建立跨插件 Python import 路径。

```yaml
schema_version: 3
id: de-agent
requires:
- nl2sql
defaults_file: config.yaml
```

`defaults_file` 指向只含 `plugins.config` 的 YAML 默认配置片段，可以配置自身和已依赖插件。合并顺序：
依赖插件的 `plugins.config` → 依赖它的插件的 `plugins.config` → 选中的那一份产品文件。
未传 `--config` 时产品文件是 Home `config.yaml`，传了就只用那份文件，两份不同时参加。
对象按字段合并。普通数组整段替换，`[]` 清空。
`plugins.config.<id>.run_hooks.before_run` / `after_run` 按来源追加，空数组不清除已有 Hook。
同级插件按 `enabled` 声明顺序合并，后者覆盖前者；需要固定值时在 Agent 配置中显式指定。
片段不能修改模型、宿主连接、`enabled` 或 `paths`，因此不会引发第二轮依赖发现。逐字段覆盖示例见 [配置优先级](../guides/configuration.md#优先级)。

工具和 Hook 通过 `runtime.context.get_plugin_config("de-agent")` 读取最终配置的独立副本，
通过 `runtime.context.session.outputs` 访问当前会话产物目录。模型和语义客户端仍使用
`runtime.context.get_model(alias)` 和 `runtime.context.semantic_client`。
这些是宿主提供的公开接口，不需要插件 setup 或共享插件实例。

## 清单里可以声明什么

| 字段 | 作用 |
| --- | --- |
| `requires` | 依赖插件 ID 数组，默认空 |
| `defaults_file` | 只贡献 plugins.config 的 YAML 文件，相对插件根 |
| `system_prompt_file` | 相对插件根的 Markdown，拼进根 Agent 提示 |
| `tools` | 本地 ID 到 `file.py:函数` |
| `skills` | 相对插件根的 Skill 目录 |
| `subagents` | 相对插件根的子代理 YAML |
| `hooks` | 只作用于根 Agent 的 Hook |

路径必须相对插件根，不能是绝对路径，也不能跳出该目录。

## 发现与刷新

启动时只记录位置，不执行插件代码。编译发生在装配 Agent 时。

REST 和 TUI 在下一轮查询前比较扩展指纹。插件文件、启用列表、Home Skill、Hook 入口和 `.mcp.yaml` 变化后，会重新准备并编译。进行中的轮次保持旧图。编译失败时，这一轮查询被拒绝，已有图继续服务其他已开始的运行。

加载过程中文件又发生变化时，这次刷新失败，提示安装结束后再试。

SDK 不会自动刷新。改了扩展后重新 `prepare_runtime` 和 `create_agent`。长驻服务可以用 `dataagent.host.refresh_extensions` 只刷新扩展，见 [Python API](../reference/python-api.md#宿主接口dataagenthost)。

模型、Home、输入目录和 `server` 监听地址不在这轮刷新里。改它们要重启进程。
