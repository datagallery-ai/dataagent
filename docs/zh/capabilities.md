# 能力全览

这篇对照两种展示和控制面，说明试用时各自能做什么。它不列插件作者要写的声明，也不列运行时内部步骤。

网页里可能还留着旧的资源面板。那些请求会得到 404，不能当成现有能力。

| 能力 | Web | TUI | 控制面 |
| --- | --- | --- | --- |
| 对话 | 打开 `/data-tasks` 后提问 | 直接输入 | `POST /api/v1/runs` |
| 会话列表 | 与 TUI 共用索引 | `/resume` | `GET /api/v1/sessions` |
| 产物 | 经会话接口查看 | `/outputs` | `GET /api/v1/sessions/{threadId}/outputs` |
| 取消一轮 | 调用取消接口 | 断开连接会使该轮中断 | `POST /api/v1/runs/{runId}/cancel` |
| 模型 | 使用进程已加载的配置 | 同左 | `config.yaml` 的 `models` 与 `.env` |
| 只读输入 | 使用进程已绑定的目录 | 同左 | `dataagent.input_workspaces` |
| Skill / 插件 / MCP | 使用 Home 里已启用的声明 | 同左 | `<home>/skills`、`plugins`、`.mcp.yaml` |
| 健康检查 | 同源转发 | 连接前可读 `/healthz` | `GET /healthz`、`GET /ready` |
| 身份 | `GET /api/v1/me` 返回 `--user` | 不登录 | 没有注册、Cookie 或 CSRF |

当前没有这些接口：`/api/v1/capabilities`、数据源注册、知识库、工作区配置 REST、模型 Profile 的创建和测试、会话改名、删除、分支、trace DAG。

数据库问答只在启用了相应插件、并且该插件自己的 `plugins.config` 配好之后才存在。它不是控制面的内置数据源服务。
