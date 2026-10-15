# 发布

发布前在仓库根目录执行：

```bash
uv sync --project runtime/agentkit --locked
uv run --project runtime/agentkit ruff check runtime/agentkit
uv run --project runtime/agentkit pytest runtime/agentkit/tests -q -m 'not live'
npm test --workspace @datafoundry/tui
```

持有模型凭据时，可额外运行带 `-m live` 的测试，见 [测试](testing.md)。

构建使用 hatchling。wheel 包含 `dataagent` 和 `restapi`，并把 `builtin-plugins` 装到 `dataagent/builtin-plugins`。

## 配置兼容

| 对象 | 版本 | 读取规则 |
| --- | --- | --- |
| 产品 `config.json` | `schema_version` 3 | 其他版本会导致启动失败，程序不改写该文件 |
| 插件 `.plugin.json` | 2 | `hook_handlers` 会导致加载失败 |
| Hook | 六个事件和 Python 入口 | `command`、`middleware`、`callbacks` 会导致校验失败 |
| MCP | `.mcp.json` 的 `mcpServers` | 连接写在该文件中，不写在产品配置里 |

已有 Home 不会在启动时迁移。请把配置保存为上述 JSON。环境引用的写法是 `$env{NAME}`。

## 状态兼容

- Checkpoint 保存在 `<home>/runtime/checkpoints.sqlite`，使用 LangGraph 的 Sqlite saver。修改 `DeepAgentState` 或私有状态键之后，确认已有会话仍能 `aget_state`。
- `session.json` 在会话创建时写入，之后保持不变。调整字段时，要么继续读懂已有文件，要么明确拒绝并提示用户新建会话。
- 进程启动时，索引里仍为 `running` 的会话改为 `interrupted`。这些轮次不会自动重放。
- Trace 使用 OTLP JSON。更早的自定义轨迹文件保持原样。
- 长工具结果的路径写在消息里。修改 `artifacts/large_tool_results` 前缀后，旧消息中的路径会失效。
