# 插件

插件是一个目录，根上有 `.plugin.json`。目录名、清单 `id` 和 `plugins.enabled` 里的 ID 必须相同。

最小清单：

```json
{
  "schema_version": 2,
  "id": "common",
  "tools": {
    "summarize_numbers": {
      "entrypoint": "tools/statistics.py:summarize_numbers"
    }
  }
}
```

完整字段见 [扩展声明](../reference/extension-schema.md)。可运行的完整插件见 [examples/plugins/common](../../examples/plugins/common/README.md)。

## 放入并启用

任选一种位置：

1. 复制到 `<home>/plugins/<id>/`。Home 会自动发现，不必写 `plugins.paths`。
2. 在 `plugins.paths` 写该插件自己的目录。写父目录不会把下面每个插件都算进来。

然后在 `<home>/config.json`：

```json
{
  "plugins": {
    "enabled": ["common"]
  }
}
```

`paths` 只增加候选。只有 `enabled` 里的 ID 会编译。ID 不能重复。保留 ID 是 `user` 和 `cli`。

候选目录来自包内 `builtin-plugins`、Home `plugins/` 和 `plugins.paths`。同一个真实目录只计一次。同一个启用 ID 对应两个不同目录时，启动失败，不会静默覆盖。

运行时不自动安装插件依赖。

## 清单里可以声明什么

| 字段 | 作用 |
| --- | --- |
| `system_prompt` | 相对插件根的 Markdown，拼进根 Agent 提示 |
| `tools` | 本地 ID 到 `file.py:函数` |
| `skills` | 相对插件根的 Skill 目录 |
| `subagents` | 相对插件根的子代理 JSON |
| `hooks` | 只作用于根 Agent 的 Hook |

路径必须相对插件根，不能是绝对路径，也不能跳出该目录。

## 发现与刷新

启动时只记录位置，不执行插件代码。编译发生在装配 Agent 时。

REST 和 TUI 在下一轮查询前比较扩展指纹。插件文件、启用列表、Home Skill、Hook 入口和 `.mcp.json` 变化后，会重新准备并编译。进行中的轮次保持旧图。编译失败时，这一轮查询被拒绝，已有图继续服务其他已开始的运行。

加载过程中文件又发生变化时，这次刷新失败，提示安装结束后再试。

SDK 不会自动刷新。调用 `refresh_extensions`，见 [SDK](../guides/sdk.md)。

模型、`server`、Home 和输入目录不在这轮刷新里。改它们要重启进程。
