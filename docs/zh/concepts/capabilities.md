# 能力全览

Web 和 TUI 共用 Server 的会话、配置和产物。下表说明两端可以做什么，以及相应的集成契约。

| 能力 | Web | TUI | Server |
| --- | --- | --- | --- |
| 对话 | 打开 `/data-tasks` 后提问 | 在终端输入问题 | `POST /api/v1/runs` |
| 会话与人工反馈 | 重新打开会话，或回答当前待处理的问题 | 用 `/resume` 打开会话，在对话中回复待处理的问题 | [会话详情](../reference/rest-api.md#session-detail)和 [AG-UI 人工反馈](../reference/events.md#hitl) |
| 产物 | 查看当前会话产物 | `/outputs` | [文件列表与预览](../reference/rest-api.md#outputs) |
| 取消一轮 | 停止当前轮次 | `/stop` 调用取消接口，随后关闭本地流 | `POST /api/v1/runs/{runId}/cancel` |
| 模型与数据输入 | 使用 Server 配置和会话已绑定的输入目录 | 同 Web | [配置](../guides/configuration.md)和[数据输入](../guides/inputs.md) |
| Skill、插件与 MCP | 使用 Server 已加载的扩展 | 同 Web | [扩展指南](../guides/integrate/extensions/overview.md) |
| 健康检查 | 直接访问 Server 地址检查 | 源码启动器会探测配置的 Server | `GET /healthz`、`GET /ready` |
| 身份 | 显示 Server 的本地用户 | 连接已有 Server 时使用同一本地用户 | `GET /api/v1/me` 返回由 `--user` 选择的本地用户 |

程序应查询[健康、身份与能力接口](../reference/rest-api.md#health-and-identity)确认字段和返回值。

Server 当前没有数据源注册、知识库、工作区配置、模型 Profile 创建或测试、会话改名、删除、分支或 Trace DAG 的 REST 接口。数据库问答需要配置并启用相应插件。详见[数据输入](../guides/inputs.md#supported-inputs)。
