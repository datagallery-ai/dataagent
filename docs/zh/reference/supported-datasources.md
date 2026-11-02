# 支持的输入

这篇只列现在能交给模型的输入种类。把目录接上的步骤在 [数据从哪里来](../guides/data-sources.md)。

控制面不提供 `GET /api/v1/datasource-types`，也没有按数据库类型渲染的注册表。

当前能交给模型的输入是：

| 输入 | 怎么配 |
| --- | --- |
| 本机目录 | `dataagent.workspaces`，或 `--workspace` / `DATAAGENT_WORKSPACE` |
| Skill | `<home>/skills/<name>/SKILL.md` |
| 插件 | `<home>/plugins/<id>/` 或 `plugins.paths`，再写入 `plugins.enabled` |
| MCP | Home 或已启用插件根目录的 `.mcp.json` |
| 语义服务 | `config.json` 的 `semantic`。未配置则不创建客户端 |

数据库、数仓和表格文件若要被查询，必须由已启用插件自己的配置声明。不要按旧的数据源类型表去调用 REST。
