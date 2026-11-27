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

完整字段见 [扩展声明](../../../reference/extension-schema.md)。可运行的完整插件见 [examples/plugins/common](../../../../../examples/plugins/common/README.md)。

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

候选目录来自包内 `dataagent/plugins`、Home `plugins/` 和 `plugins.paths`。同一个真实目录只计一次。同一个启用 ID 对应两个不同目录时，启动失败，不会静默覆盖。

清单 `requires` 声明加载依赖。宿主在已有候选目录中查找它们，按依赖优先顺序加载，每个 ID 只加载一次。
依赖缺失、重复来源或循环依赖会在启动时失败。不会联网安装，也不建立跨插件 Python import 路径。

```yaml
schema_version: 3
id: de-agent
requires:
- nl2sql
defaults_file: config.yaml
```


`defaults_file` 可给当前插件和已声明的依赖插件提供 `plugins.config` 默认参数。把部署覆盖值放在选中的产品配置中。字段规则见[配置合并参考](../../../reference/configuration.md#configuration-merge)，覆盖示例见[配置操作](../../configuration.md#优先级)。

工具和 Hook 通过 `runtime.context.get_plugin_config("de-agent")` 读取最终配置的独立副本，
通过 `runtime.context.session.outputs` 访问当前会话产物目录。模型和语义客户端仍使用
`runtime.context.get_model(alias)` 和 `runtime.context.semantic_client`。
这些接口由宿主提供。插件作者通过清单声明资源和处理器。

要添加工具、Skill、子代理、Hook 或人工交互策略，请使用[插件清单字段表](../../../reference/extension-schema.md#plugin-manifest)和对应的[扩展指南](overview.md)。

## 发现与刷新

修改扩展后，可以直接向 REST 服务或连接它的 TUI 发起下一轮请求。服务在下一轮创建或取得 Agent 时检查指纹，并在变更后尝试刷新。已经开始的轮次继续用原来的图；刷新失败时先修正文件，再发起新请求。

直接使用 Python SDK 时，重新执行 `prepare_runtime` 和 `create_agent`。自建长驻宿主可用 [`dataagent.host`](../../../reference/python-api.md#host-api) 的刷新接口。编译时机、缓存和需要重启的设置见[扩展生命周期](../../../../development/architecture/extension-lifecycle.md)。
